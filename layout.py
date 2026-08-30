#!/usr/bin/env python3
"""Remember the Omarchy scratchpad layout and restore apps as placeholders."""

from __future__ import annotations

import json
import os
import re
import contextlib
import fcntl
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

WORKSPACE = "special:scratchpad"
CONFIG_PATH = Path.home() / ".config/omarchy/scratchpad.json"
STATE_PATH = Path.home() / ".local/state/omarchy/scratchpad-tracker.json"
LOCK_PATH = Path.home() / ".local/state/omarchy/scratchpad-launch.lock"
PLUGIN_DIR = Path(__file__).resolve().parent

MIN_RESTORE_PX = 160
MIN_VISIBLE_PX = 48
MONITORS_TTL_SEC = 1.0
HYPRCTL_TIMEOUT_SEC = 3
HYPRCTL_MAX_BYTES = 1_048_576
DISPATCH_MAX_BYTES = 4096
CONFIG_MAX_BYTES = 262_144
STATE_MAX_BYTES = 65_536
STATUS_MAX_BYTES = 65_536
MAX_APPS = 32
MAX_TRACKED = 64
MAX_DESKTOP_FILES = 400
MAX_DESKTOP_FILE_BYTES = 32_768
MAX_DESKTOP_TOTAL_BYTES = 1_048_576
PROC_COMM_MAX_BYTES = 256
PROC_CHILDREN_MAX_BYTES = 4096
PROC_SCAN_MAX = 32
PROC_SCAN_DEPTH = 4
DESKTOP_SCAN_SEC = 1.5


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
  try:
    os.killpg(proc.pid, signal.SIGKILL)
  except (ProcessLookupError, PermissionError, OSError):
    try:
      proc.kill()
    except OSError:
      pass
  try:
    proc.wait(timeout=1)
  except (OSError, subprocess.TimeoutExpired):
    try:
      proc.kill()
      proc.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
      pass


def run_hyprctl(args: list[str], timeout: float = HYPRCTL_TIMEOUT_SEC, max_bytes: int = HYPRCTL_MAX_BYTES) -> bytes | None:
  try:
    proc = subprocess.Popen(
      ["hyprctl", *args],
      stdin=subprocess.DEVNULL,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      close_fds=True,
      start_new_session=True,
    )
  except OSError:
    return None
  if proc.stdout is None or proc.stderr is None:
    _kill_process_group(proc)
    return None

  stdout = bytearray()
  stderr = bytearray()
  deadline = time.monotonic() + timeout
  overflowed = False
  try:
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, stdout)
    selector.register(proc.stderr, selectors.EVENT_READ, stderr)
    while selector.get_map():
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        overflowed = True
        break
      for key, _ in selector.select(timeout=min(remaining, 0.25)):
        buf: bytearray = key.data
        try:
          chunk = os.read(key.fd, 4096)
        except OSError:
          chunk = b""
        if not chunk:
          selector.unregister(key.fileobj)
          key.fileobj.close()
          continue
        if len(buf) + len(chunk) > max_bytes or len(stdout) + len(stderr) + len(chunk) > max_bytes:
          overflowed = True
          break
        buf.extend(chunk)
      if overflowed:
        break
    if overflowed:
      _kill_process_group(proc)
      return None
    remaining = deadline - time.monotonic()
    try:
      code = proc.wait(timeout=max(remaining, 0.1))
    except subprocess.TimeoutExpired:
      _kill_process_group(proc)
      return None
    if code != 0:
      return None
    return bytes(stdout)
  except OSError:
    _kill_process_group(proc)
    return None
  finally:
    for stream in (proc.stdout, proc.stderr):
      try:
        stream.close()
      except OSError:
        pass


def hyprctl_json(args: list[str]) -> Any:
  raw = run_hyprctl(["-j", *args])
  if not raw or not raw.strip():
    return None
  try:
    return json.loads(raw.decode("utf-8"))
  except (UnicodeDecodeError, json.JSONDecodeError):
    return None


def read_regular_file(path: Path, max_bytes: int) -> str | None:
  flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
  if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
  try:
    fd = os.open(path, flags)
  except OSError:
    return None
  try:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
      return None
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
      try:
        chunk = os.read(fd, min(8192, max_bytes - total + 1))
      except BlockingIOError:
        break
      if not chunk:
        break
      total += len(chunk)
      if total > max_bytes:
        return None
      chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
  except OSError:
    return None
  finally:
    os.close(fd)


def load_json(path: Path, fallback: Any, max_bytes: int = CONFIG_MAX_BYTES) -> Any:
  raw = read_regular_file(path, max_bytes)
  if raw is None or not raw.strip():
    return fallback
  try:
    return json.loads(raw)
  except json.JSONDecodeError:
    return fallback


def ensure_parent(path: Path) -> bool:
  parent = path.parent
  try:
    if parent.exists():
      return (not parent.is_symlink()) and parent.is_dir()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return (not parent.is_symlink()) and parent.is_dir()
  except OSError:
    return False


def write_json(path: Path, payload: Any, max_bytes: int = CONFIG_MAX_BYTES) -> None:
  encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
  if len(encoded) > max_bytes or not ensure_parent(path):
    return
  fd = -1
  tmp: str | None = None
  try:
    fd, tmp = tempfile.mkstemp(prefix=".scratchpad-", suffix=".tmp", dir=str(path.parent))
    if Path(tmp).is_symlink():
      raise OSError("temporary path is a symlink")
    os.fchmod(fd, 0o600)
    view = memoryview(encoded)
    written = 0
    while written < len(encoded):
      n = os.write(fd, view[written:])
      if n <= 0:
        raise OSError("short write")
      written += n
    os.fsync(fd)
    os.close(fd)
    fd = -1
    os.replace(tmp, path)
    tmp = None
    try:
      os.chmod(path, 0o600, follow_symlinks=False)
    except (NotImplementedError, TypeError, OSError):
      os.chmod(path, 0o600)
  except OSError:
    if fd >= 0:
      try:
        os.close(fd)
      except OSError:
        pass
    if tmp:
      try:
        os.unlink(tmp)
      except OSError:
        pass


def dump_status(payload: Any) -> str:
  text = json.dumps(payload, separators=(",", ":"))
  if len(text.encode("utf-8")) <= STATUS_MAX_BYTES:
    return text
  if isinstance(payload, dict):
    trimmed = {
      "visible": bool(payload.get("visible")),
      "liveCount": int(payload.get("liveCount") or 0),
      "placeholders": list(payload.get("placeholders") or [])[:8],
      "apps": list(payload.get("apps") or [])[:8],
    }
    text = json.dumps(trimmed, separators=(",", ":"))
    if len(text.encode("utf-8")) <= STATUS_MAX_BYTES:
      return text
  return '{"visible":false,"liveCount":0,"placeholders":[],"apps":[]}'


def desktop_entries() -> list[dict[str, str]]:
  dirs = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
  ]
  entries: list[dict[str, str]] = []
  total_bytes = 0
  deadline = time.monotonic() + DESKTOP_SCAN_SEC
  for directory in dirs:
    if time.monotonic() > deadline or len(entries) >= MAX_DESKTOP_FILES:
      break
    try:
      if directory.is_symlink() or not directory.is_dir():
        continue
      with os.scandir(directory) as listing:
        for entry in listing:
          if time.monotonic() > deadline or len(entries) >= MAX_DESKTOP_FILES:
            break
          if not entry.name.endswith(".desktop"):
            continue
          if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            continue
          path = Path(entry.path)
          raw = read_regular_file(path, MAX_DESKTOP_FILE_BYTES)
          if raw is None:
            continue
          encoded_len = len(raw.encode("utf-8"))
          if total_bytes + encoded_len > MAX_DESKTOP_TOTAL_BYTES:
            return entries
          total_bytes += encoded_len
          name = ""
          icon = ""
          exec_line = ""
          wmclass = ""
          for line in raw.splitlines():
            if line.startswith("Name=") and not name:
              name = line.split("=", 1)[1].strip()
            elif line.startswith("Icon="):
              icon = line.split("=", 1)[1].strip()
            elif line.startswith("Exec="):
              exec_line = line.split("=", 1)[1].strip()
            elif line.startswith("StartupWMClass="):
              wmclass = line.split("=", 1)[1].strip()
          entries.append({
            "id": path.stem[:80],
            "name": name or path.stem,
            "icon": icon,
            "exec": exec_line,
            "wmclass": wmclass,
            "path": str(path),
          })
    except OSError:
      continue
  return entries


ICON_ROOTS = [
  Path.home() / ".local/share/icons",
  Path("/usr/share/icons"),
  Path("/usr/share/pixmaps"),
]


def under_icon_root(candidate: Path) -> str:
  """Absolute path of `candidate`, but only if it sits inside an icon tree.

  Icon names come from .desktop files and from the remembered config, so they
  are not ours. The overlay hands whatever comes back to an Image, and a name
  like ../../../../etc/hostname would otherwise walk straight out of the icon
  directories.
  """
  try:
    resolved = candidate.resolve(strict=True)
  except (OSError, RuntimeError):
    return ""
  if not resolved.is_file():
    return ""
  for root in ICON_ROOTS:
    try:
      resolved.relative_to(root.resolve(strict=False))
    except ValueError:
      continue
    return str(resolved)
  return ""


def resolve_icon(name: str) -> str:
  if not name:
    return ""
  if name.startswith("/"):
    return under_icon_root(Path(name))
  stems = [name, Path(name).stem]
  roots = [
    Path.home() / ".local/share/icons/hicolor",
    Path("/usr/share/icons/hicolor"),
    Path("/usr/share/pixmaps"),
  ]
  sizes = ["512x512", "256x256", "128x128", "64x64", "48x48", "scalable"]
  for stem in stems:
    for root in roots:
      if root.name == "pixmaps":
        for ext in (".png", ".svg", ".xpm"):
          found = under_icon_root(root / f"{stem}{ext}")
          if found:
            return found
        continue
      for size in sizes:
        for ext in (".png", ".svg"):
          found = under_icon_root(root / size / "apps" / f"{stem}{ext}")
          if found:
            return found
  return ""


HTTPS_URL = re.compile(
  r"^https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?(?:/[A-Za-z0-9._~/-]*)?$"
)
DESKTOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TUI_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
SAFE_EXACT_COMMANDS = {
  "omarchy-agent",
  "omarchy-launch-terminal",
  "uwsm-app -- obsidian",
}


TERMINAL_CLASSES = {"foot", "alacritty", "kitty", "com.mitchellh.ghostty"}
# Things a terminal runs on the way to the program you actually care about.
SHELL_COMMS = {
  "bash", "zsh", "fish", "sh", "dash", "ash", "ksh",
  "tmux", "screen", "login", "su", "sudo", "env",
}


def proc_field(pid: str, name: str, max_bytes: int) -> str:
  return (read_regular_file(Path("/proc") / pid / name, max_bytes) or "").strip()


def plantable(path: str) -> bool:
  """Whether this account could swap the binary at `path` for another one."""
  return os.access(path, os.W_OK) or os.access(os.path.dirname(path) or "/", os.W_OK)


def trusted_slug(slug: str) -> bool:
  """A name with nothing corroborating it, so only trust an unplantable target.

  A window class is chosen entirely by the window. Restoring resolves the name
  through PATH, and this PATH has several directories this account can write
  to, so a window claiming to be `org.omarchy.<name>` could otherwise point
  the tile at a binary the same attacker had just dropped there.
  """
  found = shutil.which(slug)
  return bool(found) and not plantable(found)


def names_its_own_binary(pid: str, comm: str) -> bool:
  """Whether `comm` is what this process actually is.

  A process can call itself anything, so the name alone is a claim. Where the
  binary can be read, require PATH to resolve to the very same one -- that
  catches a process calling itself `btop` while being something else.

  Where it cannot be read, fall back to requiring an unplantable target. A
  process can make itself unreadable at will (prctl), so an unreadable binary
  is not evidence of anything; but btop and friends carry capabilities and are
  legitimately unreadable, and refusing them outright would quietly downgrade
  a real TUI to a bare shell.

  Restoring a user-writable binary that is genuinely running is allowed on
  purpose: that is the feature, and it runs what was already running.
  """
  found = shutil.which(comm)
  if not found:
    return False
  try:
    running = os.readlink(f"/proc/{pid}/exe")
  except OSError:
    return not plantable(found)
  try:
    return os.path.samefile(running, found)
  except OSError:
    return False


def running_tui(pid: Any) -> str:
  """The program a terminal is actually running, if we can name it safely.

  Take the shallowest non-shell descendant, not the deepest: a terminal
  running `claude` reads bash -> claude -> bash -> python3, and the bottom of
  that is meaningless. The name has to pass TUI_SLUG and be the binary that
  process really is, so what gets remembered is the same
  `omarchy-launch-tui <slug>` form `is_safe_command` already trusts.
  """
  try:
    root = str(int(pid))
  except (TypeError, ValueError):
    return ""
  frontier = [root]
  seen = 0
  for _ in range(PROC_SCAN_DEPTH):
    nxt: list[str] = []
    for parent in frontier:
      kids = proc_field(parent, f"task/{parent}/children", PROC_CHILDREN_MAX_BYTES)
      for kid in kids.split():
        if not kid.isdigit():
          continue
        seen += 1
        if seen > PROC_SCAN_MAX:
          return ""
        comm = proc_field(kid, "comm", PROC_COMM_MAX_BYTES)
        if not comm:
          continue
        if comm in SHELL_COMMS:
          nxt.append(kid)
          continue
        if TUI_SLUG.fullmatch(comm) and names_its_own_binary(kid, comm):
          return comm
        return ""
    if not nxt:
      break
    frontier = nxt
  return ""


def restore_class(app: dict[str, Any]) -> str:
  """The class the window will have once we relaunch it.

  A program noticed inside a plain terminal is remembered as the Omarchy
  launcher for it, and that launcher gives the window its own app-id. So the
  window that comes back is not the class we originally saw it under.
  Derived rather than stored, so it cannot go stale when the command changes.
  """
  command = str(app.get("command") or "").strip()
  if command.startswith("omarchy-launch-tui "):
    slug = command.split(" ", 1)[1]
    if TUI_SLUG.fullmatch(slug):
      return "org.omarchy." + slug
  return str(app.get("class") or "")


def app_classes(app: dict[str, Any]) -> set[str]:
  return {str(app.get("class") or ""), restore_class(app)} - {""}


def sanitize_name(value: str) -> str:
  cleaned = "".join(ch for ch in str(value or "") if ch.isprintable() and ch not in "<>")
  cleaned = " ".join(cleaned.split())
  return cleaned[:80] if cleaned else "App"


def is_https_url(value: str) -> bool:
  return bool(HTTPS_URL.fullmatch(value or ""))


def is_safe_command(command: str) -> bool:
  command = str(command or "").strip()
  if command in SAFE_EXACT_COMMANDS:
    return True
  if command.startswith("omarchy-launch-tui "):
    slug = command.split(" ", 1)[1]
    return bool(TUI_SLUG.fullmatch(slug))
  if command.startswith("omarchy-launch-webapp "):
    return is_https_url(command.split(" ", 1)[1])
  if command.startswith("gtk-launch "):
    return bool(DESKTOP_ID.fullmatch(command.split(" ", 1)[1]))
  return False


def webapp_url_from_class(cls: str) -> str | None:
  if not cls.startswith("chrome-") or not cls.endswith("-Default"):
    return None
  raw = cls[len("chrome-"):-len("-Default")]
  path = raw.replace("_", "/")
  path = re.sub(r"/+", "/", path).strip("/")
  if not path:
    return None
  if "://" not in path:
    path = "https://" + path
  return path if is_https_url(path) else None


def gtk_launch(desktop_id: str) -> str:
  return "gtk-launch " + desktop_id if DESKTOP_ID.fullmatch(desktop_id) else ""


def command_for_client(client: dict[str, Any], desktops: list[dict[str, str]]) -> tuple[str, str, str]:
  cls = str(client.get("class") or client.get("initialClass") or "")
  title = str(client.get("title") or "")
  lowered = cls.lower()
  url = webapp_url_from_class(cls)
  if url:
    host = url.split("/")[2]
    for entry in desktops:
      if "omarchy-launch-webapp" in entry["exec"] and host in entry["exec"]:
        return sanitize_name(entry["name"] or host), "omarchy-launch-webapp " + url, entry["icon"]
    return sanitize_name(host), "omarchy-launch-webapp " + url, "chromium"

  if cls.startswith("org.omarchy."):
    slug = cls.split(".")[-1]
    if slug == "agent":
      return "Agent", "omarchy-agent", "utilities-terminal"
    if slug == "btop":
      return "Activity", "omarchy-launch-tui btop", "utilities-system-monitor"
    if TUI_SLUG.fullmatch(slug) and trusted_slug(slug):
      return sanitize_name(slug), "omarchy-launch-tui " + slug, "utilities-terminal"
    return sanitize_name(title or slug or cls), "", ""

  if "obsidian" in lowered:
    return "Obsidian", "uwsm-app -- obsidian", "obsidian"
  if lowered in TERMINAL_CLASSES:
    # Something running in the terminal is what the user wants back, so
    # remember it the Omarchy way rather than as a bare shell.
    slug = running_tui(client.get("pid"))
    if slug:
      return sanitize_name(slug), "omarchy-launch-tui " + slug, "utilities-terminal"
    return "Terminal", "omarchy-launch-terminal", "foot"

  cls_lower = lowered
  for entry in desktops:
    wm = entry["wmclass"].lower()
    stem = entry["id"].lower()
    command = gtk_launch(entry["id"])
    if not command:
      continue
    if wm and wm == cls_lower:
      return sanitize_name(entry["name"] or title), command, entry["icon"]
    if not wm and stem and stem == cls_lower:
      return sanitize_name(entry["name"] or title), command, entry["icon"]

  # Never treat a Hyprland window class as a shell command.
  return sanitize_name(title or cls), "", ""


def glyph_for(name: str, cls: str, command: str) -> str:
  blob = f"{name} {cls} {command}".lower()
  if "gitlab" in blob:
    return "\uf296"
  if "obsidian" in blob:
    return "\ueb71"
  if "btop" in blob or "activity" in blob:
    return "\uf201"
  if "agent" in blob or "grok" in blob:
    return "\uf135"
  if "terminal" in blob or "foot" in blob:
    return "\uf120"
  if "webapp" in command or cls.startswith("chrome-"):
    return "\uf0ac"
  return "\uf2d2"


IDENTITY_KEYS = ("id", "name", "class", "command", "icon", "glyph")
GEOMETRY_KEYS = ("x", "y", "w", "h", "floating", "monitor")


_monitors_cache: tuple[float, list[dict[str, Any]]] | None = None


def monitors() -> list[dict[str, Any]]:
  """Monitor list, briefly cached.

  Every client needs its monitor resolved, so an uncached read meant one
  hyprctl per window on every poll. The TTL keeps a long-lived restore from
  pinning a stale list if the pad moves screens mid-run.
  """
  global _monitors_cache
  now = time.monotonic()
  if _monitors_cache is not None and now - _monitors_cache[0] < MONITORS_TTL_SEC:
    return _monitors_cache[1]
  payload = hyprctl_json(["monitors"])
  found = payload if isinstance(payload, list) else []
  _monitors_cache = (now, found)
  return found


def monitor_for(client_or_name: Any) -> dict[str, Any] | None:
  found = monitors()
  if isinstance(client_or_name, dict):
    mid = client_or_name.get("monitor")
    for monitor in found:
      if monitor.get("id") == mid or str(monitor.get("id")) == str(mid):
        return monitor
    return None
  for monitor in found:
    if str(monitor.get("name") or "") == str(client_or_name) or str(monitor.get("id")) == str(client_or_name):
      return monitor
  return next((monitor for monitor in found if monitor.get("focused")), found[0] if found else None)


def geometry_from_client(client: dict[str, Any]) -> dict[str, Any]:
  at = client.get("at") if isinstance(client.get("at"), list) and len(client.get("at")) >= 2 else [0, 0]
  size = client.get("size") if isinstance(client.get("size"), list) and len(client.get("size")) >= 2 else [0, 0]
  monitor = monitor_for(client) or {}
  return {
    "x": int(at[0]),
    "y": int(at[1]),
    "w": int(size[0]),
    "h": int(size[1]),
    "floating": bool(client.get("floating")),
    "monitor": str(monitor.get("name") or client.get("monitor") or ""),
  }


def scratchpad_monitor() -> dict[str, Any] | None:
  """The monitor the scratchpad shows on -- a special workspace only has one."""
  found = monitors()
  for monitor in found:
    if "scratchpad" in str((monitor.get("specialWorkspace") or {}).get("name") or ""):
      return monitor
  return next((m for m in found if m.get("focused")), found[0] if found else None)


def monitor_bounds(monitor: dict[str, Any]) -> tuple[int, int, int, int] | None:
  """Logical x, y, width, height -- client rects are in logical pixels."""
  try:
    scale = float(monitor.get("scale") or 1) or 1.0
    width = int(int(monitor.get("width") or 0) / scale)
    height = int(int(monitor.get("height") or 0) / scale)
  except (TypeError, ValueError, ZeroDivisionError):
    return None
  if width <= 0 or height <= 0:
    return None
  return int(monitor.get("x") or 0), int(monitor.get("y") or 0), width, height


def geometry_of(app: dict[str, Any]) -> dict[str, Any] | None:
  try:
    w = int(app.get("w") or 0)
    h = int(app.get("h") or 0)
    if w <= 0 or h <= 0:
      return None
    return {
      "x": int(app.get("x") or 0),
      "y": int(app.get("y") or 0),
      "w": w,
      "h": h,
      "floating": bool(app.get("floating")),
      "monitor": str(app.get("monitor") or ""),
    }
  except (TypeError, ValueError):
    return None


def clamp_rect(geo: dict[str, Any], bounds: tuple[int, int, int, int]) -> dict[str, Any]:
  """Pull a rectangle onto the monitor and keep it big enough to grab."""
  mx, my, mw, mh = bounds
  out = dict(geo)
  out["w"] = max(MIN_RESTORE_PX, min(out["w"], mw))
  out["h"] = max(MIN_RESTORE_PX, min(out["h"], mh))
  out["x"] = min(max(out["x"], mx), mx + mw - out["w"])
  out["y"] = min(max(out["y"], my), my + mh - out["h"])
  return out


def out_of_view(client: dict[str, Any]) -> bool:
  """Whether too little of a window shows to notice it is there.

  A client cannot move itself under Wayland, but anything running as this
  user can ask Hyprland to, so a window on the pad can be parked off the edge
  of the desk or shrunk to a sliver and carry on running unseen. Treat both
  the same way: too little showing on either axis to see or grab.
  """
  monitor = scratchpad_monitor()
  bounds = monitor_bounds(monitor) if monitor else None
  if bounds is None:
    return False
  mx, my, mw, mh = bounds
  geo = geometry_from_client(client)
  shown_x = min(geo["x"] + geo["w"], mx + mw) - max(geo["x"], mx)
  shown_y = min(geo["y"] + geo["h"], my + mh) - max(geo["y"], my)
  return shown_x < MIN_VISIBLE_PX or shown_y < MIN_VISIBLE_PX


def target_geometry(app: dict[str, Any]) -> dict[str, Any] | None:
  """Remembered rect, moved onto the monitor the pad is on and kept inside it.

  Hyprland places a floating window exactly where it is told, past the edge of
  the desk included, so a rect saved on a wider screen -- or on a monitor that
  has since been unplugged -- would otherwise restore into a window nobody can
  see or reach. Undersized monitors shrink the window rather than lose it.
  """
  geo = geometry_of(app)
  if geo is None:
    return None
  monitor = scratchpad_monitor()
  bounds = monitor_bounds(monitor) if monitor else None
  if monitor is None or bounds is None:
    return geo
  mx, my, mw, mh = bounds
  saved = monitor_for(geo.get("monitor"))
  out = dict(geo)
  if saved is not None and str(saved.get("name") or "") != str(monitor.get("name") or ""):
    out["x"] = out["x"] - int(saved.get("x") or 0) + mx
    out["y"] = out["y"] - int(saved.get("y") or 0) + my
  out = clamp_rect(out, bounds)
  out["monitor"] = str(monitor.get("name") or out.get("monitor") or "")
  return out


def float_on_arrival(client: dict[str, Any]) -> dict[str, Any]:
  """Float a window the first time it turns up on the pad.

  Hyprland tiles an arriving window, and on a pad of floating windows that
  drops it in at full size underneath all of them -- there, but with no edge
  left to grab. Float it so it lands in front and can be put somewhere.

  Arrival means an address we did not see on the previous sync, not a slot we
  have never met: sending a window to a slot that already exists is still an
  arrival. Firing once per window leaves a pad deliberately re-tiled alone.
  """
  addr = str(client.get("address") or "")
  if client.get("floating") or not addr:
    return client
  dispatch(
    "hl.dsp.window.float({ action = 'enable', window = "
    + lua_str("address:" + addr)
    + " })"
  )
  time.sleep(0.2)
  return client_by_address(addr) or client


def app_key(app: dict[str, Any]) -> str:
  return str(app.get("id") or app.get("class") or "")


def next_id(cls: str, apps: list[dict[str, Any]]) -> str:
  """Pick an id for a new slot of `cls`.

  The first slot keeps the bare class as its id, so a scratchpad.json written
  before slots existed keeps working with no migration.
  """
  used = {app_key(app) for app in apps}
  if cls not in used:
    return cls
  n = 2
  while f"{cls}#{n}" in used:
    n += 1
  return f"{cls}#{n}"


def display_labels(apps: list[dict[str, Any]]) -> dict[str, str]:
  """Number only the tiles a user could not otherwise tell apart.

  Count by displayed name rather than by class: a terminal running something
  recognisable is already named for it, so two slots sharing the class `foot`
  can still read Terminal and cliamp with nothing to disambiguate.
  """
  counts: dict[str, int] = {}
  for app in apps:
    name = str(app.get("name") or "App")
    counts[name] = counts.get(name, 0) + 1
  seen: dict[str, int] = {}
  labels: dict[str, str] = {}
  for app in apps:
    name = str(app.get("name") or "App")
    seen[name] = seen.get(name, 0) + 1
    labels[app_key(app)] = name if counts.get(name, 0) < 2 else f"{name} {seen[name]}"
  return labels


def pair_apps(
  apps: list[dict[str, Any]],
  live: list[dict[str, Any]],
  bindings: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
  """Work out which remembered slot each live scratchpad window is.

  Two windows of one class are interchangeable and Wayland offers nothing
  stable to tell them apart across a reboot. So bind by remembered address
  while the session lasts, and otherwise give each window the nearest saved
  rectangle of its own class -- which is right precisely because identical
  windows are interchangeable. Each slot is claimed at most once.
  """
  taken: dict[str, dict[str, Any]] = {}
  by_id = {app_key(app): app for app in apps}

  rest: list[dict[str, Any]] = []
  for client in live:
    wanted = bindings.get(str(client.get("address") or ""))
    app = by_id.get(wanted) if wanted else None
    if app is not None and app_key(app) not in taken:
      taken[app_key(app)] = client
    else:
      rest.append(client)

  leftover: list[dict[str, Any]] = []
  for client in rest:
    cls = client_class(client)
    pool = [
      app for app in apps
      if cls in app_classes(app) and app_key(app) not in taken
    ]
    if not pool:
      leftover.append(client)
      continue
    current = geometry_from_client(client)
    taken[app_key(min(pool, key=lambda a: rect_distance(current, a)))] = client
  return taken, leftover


def rect_distance(current: dict[str, Any], app: dict[str, Any]) -> int:
  geo = geometry_of(app)
  if geo is None:
    return 1 << 30
  return (
    abs(current["x"] - geo["x"]) + abs(current["y"] - geo["y"])
    + abs(current["w"] - geo["w"]) + abs(current["h"] - geo["h"])
  )


def app_from_client(client: dict[str, Any], desktops: list[dict[str, str]]) -> dict[str, Any]:
  cls = str(client.get("class") or client.get("initialClass") or "")
  name, command, icon_name = command_for_client(client, desktops)
  app = {
    "id": cls or name.lower().replace(" ", "-"),
    "name": name,
    "class": cls,
    "command": command,
    "icon": resolve_icon(icon_name),
    "glyph": glyph_for(name, cls, command),
  }
  app.update(geometry_from_client(client))
  return app


def merge_app(existing: dict[str, Any], incoming: dict[str, Any], geometry: bool = True) -> None:
  for key in IDENTITY_KEYS:
    value = incoming.get(key)
    if value:
      existing[key] = value
  if not geometry and geometry_of(existing) is not None:
    return
  for key in GEOMETRY_KEYS:
    if key in incoming:
      existing[key] = incoming[key]


def clients() -> list[dict[str, Any]]:
  payload = hyprctl_json(["clients"])
  return payload if isinstance(payload, list) else []


def scratchpad_visible() -> bool:
  for monitor in monitors():
    special = monitor.get("specialWorkspace") or {}
    name = str(special.get("name") or "")
    if "scratchpad" in name:
      return True
  return False


def remembered() -> list[dict[str, Any]]:
  data = load_json(CONFIG_PATH, {}, CONFIG_MAX_BYTES)
  apps = data.get("apps") if isinstance(data, dict) else None
  out: list[dict[str, Any]] = []
  for app in apps or []:
    if not isinstance(app, dict) or not app.get("class"):
      continue
    out.append(app)
    if len(out) >= MAX_APPS:
      break
  return out


def save_remembered(apps: list[dict[str, Any]]) -> None:
  write_json(
    CONFIG_PATH,
    {"apps": apps[:MAX_APPS], "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")},
    CONFIG_MAX_BYTES,
  )


def tracker() -> dict[str, str]:
  data = load_json(STATE_PATH, {}, STATE_MAX_BYTES)
  last = data.get("addresses") if isinstance(data, dict) else None
  if not isinstance(last, dict):
    return {}
  out: dict[str, str] = {}
  for key, value in last.items():
    out[str(key)[:128]] = str(value)[:128]
    if len(out) >= MAX_TRACKED:
      break
  return out


def tracked_rects() -> dict[str, list[int]]:
  data = load_json(STATE_PATH, {}, STATE_MAX_BYTES)
  rects = data.get("rects") if isinstance(data, dict) else None
  out: dict[str, list[int]] = {}
  if not isinstance(rects, dict):
    return out
  for key, value in rects.items():
    if isinstance(value, list) and len(value) == 4:
      try:
        out[str(key)[:128]] = [int(v) for v in value]
      except (TypeError, ValueError):
        continue
    if len(out) >= MAX_TRACKED:
      break
  return out


def save_tracker(addresses: dict[str, str], rects: dict[str, list[int]] | None = None) -> None:
  trimmed = dict(list(addresses.items())[:MAX_TRACKED])
  payload: dict[str, Any] = {"addresses": trimmed}
  if rects is not None:
    payload["rects"] = dict(list(rects.items())[:MAX_TRACKED])
  write_json(STATE_PATH, payload, STATE_MAX_BYTES)


def sync() -> dict[str, Any]:
  desktops = desktop_entries()
  all_clients = clients()
  live = [c for c in all_clients if str((c.get("workspace") or {}).get("name") or "") == WORKSPACE]
  live_by_addr = {str(c.get("address") or ""): c for c in live if c.get("address")}
  all_by_addr = {str(c.get("address") or ""): c for c in all_clients if c.get("address")}

  apps = remembered()
  last = tracker()

  arrived = [
    client for client in live
    if str(client.get("address") or "") not in last and not client.get("floating")
  ]
  if arrived:
    for client in arrived:
      float_on_arrival(client)
    live = [
      c for c in clients()
      if str((c.get("workspace") or {}).get("name") or "") == WORKSPACE
    ]
    live_by_addr = {str(c.get("address") or ""): c for c in live if c.get("address")}

  taken, unmatched = pair_apps(apps, live, last)

  # A person moves one window at a time. Everything on the pad shifting between
  # one poll and the next is something else doing it -- locking the session
  # re-centres every floating window, and recording that would flatten the
  # layout into a stack, which is the very thing this plugin exists to avoid.
  was = tracked_rects()
  now: dict[str, list[int]] = {}
  moved: set[str] = set()
  fresh: set[str] = set()
  for ident, client in taken.items():
    geo = geometry_from_client(client)
    now[ident] = [geo["x"], geo["y"], geo["w"], geo["h"]]
    before = was.get(ident)
    if before is None:
      fresh.add(ident)
    elif before != now[ident]:
      moved.add(ident)
  shifted_together = len(taken) >= 2 and len(moved) == len(taken)

  # Tiled scratchpad windows reflow whenever a sibling leaves, and a restored
  # window is briefly wherever Hyprland first mapped it. Both look like a
  # layout change but neither is one, so only trust live geometry while every
  # remembered slot is actually filled. Otherwise a logout -- which closes the
  # apps one by one -- would overwrite the layout we exist to restore.
  layout_whole = len(taken) == len(apps)

  for app in apps:
    client = taken.get(app_key(app))
    if client is None:
      continue
    incoming = app_from_client(client, desktops)
    incoming["id"] = app_key(app)   # never let a second slot take the bare class
    # Only write a rectangle when this window is the one that just moved.
    # Anything else and a rejected change would simply be recorded on the next
    # poll instead, once it had stopped moving and become the status quo.
    # Recording where a window went to hide would make the hiding place the
    # thing we restore to, so keep the last rectangle that could be seen.
    ident = app_key(app)
    trust = (
      layout_whole
      and not shifted_together
      and not out_of_view(client)
      and (ident in moved or ident in fresh)
    )
    merge_app(app, incoming, geometry=trust)

  for client in unmatched:
    if len(apps) >= MAX_APPS:
      break
    incoming = app_from_client(client, desktops)
    incoming["id"] = next_id(str(incoming.get("class") or ""), apps)
    apps.append(incoming)
    taken[incoming["id"]] = client

  for addr, ident in last.items():
    if addr in live_by_addr:
      continue
    if all_by_addr.get(addr) is not None:
      apps = [app for app in apps if app_key(app) != ident]
      taken.pop(ident, None)

  save_remembered(apps)
  save_tracker(
    {
      str(client.get("address") or ""): ident
      for ident, client in taken.items() if client.get("address")
    },
    {ident: [geo["x"], geo["y"], geo["w"], geo["h"]]
     for ident, geo in ((i, geometry_from_client(c)) for i, c in taken.items())},
  )

  # A tile the plugin cannot start will never clear on its own, and while it
  # is remembered the pad is never whole, so geometry is never recorded again.
  # Say which tiles those are and let the user decide rather than quietly
  # dropping them or quietly staying frozen.
  labels = display_labels(apps)
  placeholders = [
    dict(
      app,
      label=labels.get(app_key(app), app.get("name") or "App"),
      restorable=is_safe_command(str(app.get("command") or "")),
    )
    for app in apps if app_key(app) not in taken
  ]

  hidden = []
  for app in apps:
    client = taken.get(app_key(app))
    if client is not None and out_of_view(client):
      hidden.append({
        "id": app_key(app),
        "label": labels.get(app_key(app), app.get("name") or "App"),
      })

  return {
    "visible": scratchpad_visible(),
    "liveCount": len(live),
    "layoutFrozen": not layout_whole,
    "shiftedTogether": shifted_together,
    "hidden": hidden[:MAX_APPS],
    "placeholders": placeholders[:MAX_APPS],
    "apps": apps[:MAX_APPS],
  }


def snapshot() -> dict[str, Any]:
  desktops = desktop_entries()
  live = [c for c in clients() if str((c.get("workspace") or {}).get("name") or "") == WORKSPACE]
  apps: list[dict[str, Any]] = []
  addresses: dict[str, str] = {}
  for client in live:
    if len(apps) >= MAX_APPS:
      break
    app = app_from_client(client, desktops)
    app["id"] = next_id(str(app.get("class") or ""), apps)
    apps.append(app)
    if client.get("address"):
      addresses[str(client.get("address"))] = app["id"]
  save_remembered(apps)
  save_tracker(addresses)
  return {"apps": apps, "count": len(apps)}


def lua_str(value: str) -> str:
  return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def dispatch(lua: str) -> bool:
  raw = run_hyprctl(["dispatch", lua], max_bytes=DISPATCH_MAX_BYTES)
  if raw is None:
    sys.stderr.write("dispatch failed\n")
    return False
  if raw.lstrip().lower().startswith(b"error"):
    sys.stderr.write("dispatch rejected: " + raw.decode("utf-8", "replace")[:200] + "\n")
    return False
  return True


def client_class(client: dict[str, Any]) -> str:
  return str(client.get("class") or client.get("initialClass") or "")


def find_app_window(cls: str, known_addrs: set[str] | None = None) -> dict[str, Any] | None:
  matches = [client for client in clients() if client_class(client) == cls]
  if known_addrs is not None:
    matches = [client for client in matches if str(client.get("address") or "") not in known_addrs]
  if not matches:
    return None
  on_scratchpad = [
    client for client in matches
    if str((client.get("workspace") or {}).get("name") or "") == WORKSPACE
  ]
  return (on_scratchpad or matches)[0]


def geometry_matches(client: dict[str, Any], geo: dict[str, Any], slop: int = 8) -> bool:
  current = geometry_from_client(client)
  return (
    abs(current["x"] - geo["x"]) <= slop
    and abs(current["y"] - geo["y"]) <= slop
    and abs(current["w"] - geo["w"]) <= slop
    and abs(current["h"] - geo["h"]) <= slop
  )


def apply_geometry(client: dict[str, Any], geo: dict[str, Any]) -> None:
  addr = str(client.get("address") or "")
  if not addr:
    return
  workspace = str((client.get("workspace") or {}).get("name") or "")
  if workspace != WORKSPACE:
    dispatch(
      "hl.dsp.window.move({ workspace = 'special:scratchpad', follow = false, window = "
      + lua_str("address:" + addr)
      + " })"
    )
  dispatch(
    "hl.dsp.window.float({ action = 'enable', window = "
    + lua_str("address:" + addr)
    + " })"
  )
  dispatch(
    "hl.dsp.window.resize({ x = "
    + str(geo["w"])
    + ", y = "
    + str(geo["h"])
    + ", relative = false, window = "
    + lua_str("address:" + addr)
    + " })"
  )
  dispatch(
    "hl.dsp.window.move({ x = "
    + str(geo["x"])
    + ", y = "
    + str(geo["y"])
    + ", relative = false, window = "
    + lua_str("address:" + addr)
    + " })"
  )


def client_by_address(addr: str) -> dict[str, Any] | None:
  return next((c for c in clients() if str(c.get("address") or "") == addr), None)


def restore_geometry(cls: str, geo: dict[str, Any], known_addrs: set[str]) -> None:
  """Wait for the window this launch produced, then hold it on its rectangle.

  Track it by address once found. Re-finding it by class would, with a second
  window of the same class already on the pad, hand this slot's rectangle to
  whichever one happened to match first.
  """
  deadline = time.time() + 15
  client = None
  while time.time() < deadline:
    client = find_app_window(cls, known_addrs)
    if client is not None:
      break
    time.sleep(0.15)
  if client is None:
    print(f"timed out waiting for {cls}", file=sys.stderr)
    return
  addr = str(client.get("address") or "")
  for delay in (0.0, 0.25, 0.7, 1.5):
    if delay:
      time.sleep(delay)
    current = client_by_address(addr) or client
    apply_geometry(current, geo)
    current = client_by_address(addr) or current
    if geometry_matches(current, geo):
      return


def reassert_layout(exclude_id: str) -> None:
  """Snap the rest of the pad back onto the remembered arrangement.

  Tiled siblings reflow the moment a window leaves, so the scratchpad we are
  restoring into is rarely the one that was saved. Re-applying the remembered
  geometry makes a restore land on the same arrangement every time instead of
  on whatever the tiler did while the app was gone. Floating a sibling reflows
  the ones still tiled, so make a second pass over anything left out of place.
  """
  apps, taken = live_pairs()
  targets = []
  for app in apps:
    if app_key(app) == exclude_id:
      continue
    client = taken.get(app_key(app))
    geo = target_geometry(app) if client is not None else None
    addr = str(client.get("address") or "") if client is not None else ""
    if geo and addr:
      targets.append((addr, geo))
  for _ in range(2):
    pending = []
    by_addr = {str(c.get("address") or ""): c for c in clients()}
    for addr, geo in targets:
      client = by_addr.get(addr)
      if client is None or str((client.get("workspace") or {}).get("name") or "") != WORKSPACE:
        continue
      if geometry_matches(client, geo):
        continue
      apply_geometry(client, geo)
      pending.append((addr, geo))
    if not pending:
      return
    targets = pending


def exec_rules(app: dict[str, Any]) -> str:
  parts = ["workspace = 'special:scratchpad'"]
  geo = target_geometry(app)
  if geo:
    # A monitor rule is ignored once a workspace rule is set, and move is
    # local to whichever monitor the special workspace opened on -- which is
    # the monitor target_geometry already placed this rect on.
    monitor = scratchpad_monitor() or {}
    local_x = geo["x"] - int(monitor.get("x") or 0)
    local_y = geo["y"] - int(monitor.get("y") or 0)
    parts.append("float = true")
    parts.append(f"size = {{{geo['w']}, {geo['h']}}}")
    parts.append(f"move = {{{local_x}, {local_y}}}")
  return "{ " + ", ".join(parts) + " }"


@contextlib.contextmanager
def launch_lock() -> Any:
  """Hold the right to start things, or yield False if someone else has it.

  A launch is slow -- up to fifteen seconds waiting for a window to appear --
  and until that window exists nothing can tell that the slot is being filled.
  Two launches racing therefore both believe the slot is empty and both start
  an app, leaving a duplicate window and a spare slot behind for good. Held
  across the whole launch, including the wait, so the second one is turned
  away rather than deciding for itself.
  """
  fd = -1
  try:
    if not ensure_parent(LOCK_PATH):
      yield False
      return
    try:
      fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
      if fd >= 0:
        os.close(fd)
        fd = -1
      yield False
      return
    yield True
  finally:
    if fd >= 0:
      try:
        fcntl.flock(fd, fcntl.LOCK_UN)
      except OSError:
        pass
      try:
        os.close(fd)
      except OSError:
        pass


def slot_is_filled(app: dict[str, Any]) -> bool:
  _, taken = live_pairs()
  return app_key(app) in taken


def restore_one(app: dict[str, Any]) -> bool:
  """Start one remembered app and wait for it to settle on its saved rect."""
  app_id = str(app.get("id") or app.get("class") or "?")
  command = str(app.get("command") or "").strip()
  if not command or not is_safe_command(command):
    print(f"no trusted command for {app_id}", file=sys.stderr)
    return False
  cls = restore_class(app)
  known_addrs = {str(client.get("address") or "") for client in clients() if client.get("address")}
  if not dispatch("hl.dsp.exec_cmd(" + lua_str(command) + ", " + exec_rules(app) + ")"):
    return False
  geo = target_geometry(app)
  if geo and cls:
    restore_geometry(cls, geo, known_addrs)
  return True


def launch(app_id: str) -> int:
  apps = remembered()
  app = next((item for item in apps if app_key(item) == app_id), None)
  if app is None:
    app = next((item for item in apps if item.get("class") == app_id), None)
  if not app:
    print(f"unknown scratchpad app: {app_id}", file=sys.stderr)
    return 1
  with launch_lock() as held:
    if not held:
      print(f"another restore is already running; ignoring {app_id}", file=sys.stderr)
      return 1
    # Starting a slot that already has a window would leave the extra one
    # unmatched, and the next sync would remember it as a slot of its own.
    if slot_is_filled(app):
      print(f"{app_id} is already on the scratchpad", file=sys.stderr)
      return 0
    if not restore_one(app):
      return 1
    reassert_layout(app_key(app))
  return 0


def live_pairs() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
  apps = remembered()
  live = [
    client for client in clients()
    if str((client.get("workspace") or {}).get("name") or "") == WORKSPACE
  ]
  taken, _ = pair_apps(apps, live, tracker())
  return apps, taken


def missing_apps() -> list[dict[str, Any]]:
  apps, taken = live_pairs()
  return [app for app in apps if app_key(app) not in taken]


def restore_all() -> int:
  """Bring back every remembered app that is not already on the pad.

  Started one at a time on purpose: each app is placed by its own exec rule
  as it maps, so launching them together would leave the rules racing over
  which window belongs to which rectangle.
  """
  with launch_lock() as held:
    if not held:
      print("another restore is already running", file=sys.stderr)
      return 1
    pending = missing_apps()
    started: list[str] = []
    skipped: list[str] = []
    for app in pending:
      name = str(app.get("name") or app.get("id") or "?")
      (started if restore_one(app) else skipped).append(name)
    reassert_layout("")
  print(dump_status({"restored": started, "skipped": skipped, "total": len(pending)}))
  return 0 if not skipped else 1


def reveal() -> int:
  """Bring scratchpad windows that are out of view back onto the monitor."""
  monitor = scratchpad_monitor()
  bounds = monitor_bounds(monitor) if monitor else None
  if bounds is None:
    print("no monitor to reveal onto", file=sys.stderr)
    return 1
  apps, taken = live_pairs()
  labels = display_labels(apps)
  revealed: list[str] = []
  for app in apps:
    client = taken.get(app_key(app))
    if client is None or not out_of_view(client):
      continue
    apply_geometry(client, clamp_rect(geometry_from_client(client), bounds))
    revealed.append(labels.get(app_key(app), str(app.get("name") or "App")))
  print(dump_status({"revealed": revealed}))
  return 0


def forget(app_id: str) -> int:
  apps = [app for app in remembered() if app_key(app) != app_id]
  save_remembered(apps)
  return 0


def main() -> int:
  action = sys.argv[1] if len(sys.argv) > 1 else "status"
  if action in {"status", "sync"}:
    print(dump_status(sync()))
    return 0
  if action == "snapshot":
    print(dump_status(snapshot()))
    return 0
  if action == "restore-all":
    return restore_all()
  if action == "reveal":
    return reveal()
  if action == "launch" and len(sys.argv) > 2:
    return launch(sys.argv[2])
  if action == "forget" and len(sys.argv) > 2:
    return forget(sys.argv[2])
  print(
    "usage: layout.py status|snapshot|restore-all|reveal|launch <id>|forget <id>",
    file=sys.stderr,
  )
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
