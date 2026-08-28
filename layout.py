#!/usr/bin/env python3
"""Remember the Omarchy scratchpad layout and restore apps as placeholders."""

from __future__ import annotations

import json
import os
import re
import selectors
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
PLUGIN_DIR = Path(__file__).resolve().parent

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


def resolve_icon(name: str) -> str:
  if not name:
    return ""
  if name.startswith("/") and Path(name).is_file():
    return name
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
          candidate = root / f"{stem}{ext}"
          if candidate.is_file():
            return str(candidate)
        continue
      for size in sizes:
        for ext in (".png", ".svg"):
          candidate = root / size / "apps" / f"{stem}{ext}"
          if candidate.is_file():
            return str(candidate)
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
    if TUI_SLUG.fullmatch(slug):
      return sanitize_name(slug), "omarchy-launch-tui " + slug, "utilities-terminal"
    return sanitize_name(title or slug or cls), "", ""

  if "obsidian" in lowered:
    return "Obsidian", "uwsm-app -- obsidian", "obsidian"
  if lowered in {"foot", "alacritty", "kitty", "com.mitchellh.ghostty"}:
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


def monitors() -> list[dict[str, Any]]:
  payload = hyprctl_json(["monitors"])
  return payload if isinstance(payload, list) else []


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


def merge_app(existing: dict[str, Any], incoming: dict[str, Any]) -> None:
  for key in IDENTITY_KEYS:
    value = incoming.get(key)
    if value:
      existing[key] = value
  for key in GEOMETRY_KEYS:
    if key in incoming:
      existing[key] = incoming[key]


def clients() -> list[dict[str, Any]]:
  payload = hyprctl_json(["clients"])
  return payload if isinstance(payload, list) else []


def scratchpad_visible() -> bool:
  monitors = hyprctl_json(["monitors"])
  if not isinstance(monitors, list):
    return False
  for monitor in monitors:
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


def save_tracker(addresses: dict[str, str]) -> None:
  trimmed = dict(list(addresses.items())[:MAX_TRACKED])
  write_json(STATE_PATH, {"addresses": trimmed}, STATE_MAX_BYTES)


def sync() -> dict[str, Any]:
  desktops = desktop_entries()
  all_clients = clients()
  live = [c for c in all_clients if str((c.get("workspace") or {}).get("name") or "") == WORKSPACE]
  live_by_addr = {str(c.get("address") or ""): c for c in live if c.get("address")}
  all_by_addr = {str(c.get("address") or ""): c for c in all_clients if c.get("address")}

  apps = remembered()
  last = tracker()

  for client in live_by_addr.values():
    incoming = app_from_client(client, desktops)
    existing = next((app for app in apps if app.get("class") == incoming["class"]), None)
    if existing:
      merge_app(existing, incoming)
    elif len(apps) < MAX_APPS:
      apps.append(incoming)

  for addr, cls in last.items():
    if addr in live_by_addr:
      continue
    leftover = all_by_addr.get(addr)
    if leftover is not None:
      apps = [app for app in apps if app.get("class") != cls]

  save_remembered(apps)
  save_tracker({addr: str(c.get("class") or "") for addr, c in live_by_addr.items()})

  live_classes = [str(c.get("class") or "") for c in live]
  placeholders: list[dict[str, Any]] = []
  remaining = list(live_classes)
  for app in apps:
    cls = str(app.get("class") or "")
    if cls in remaining:
      remaining.remove(cls)
    elif len(placeholders) < MAX_APPS:
      placeholders.append(app)

  return {
    "visible": scratchpad_visible(),
    "liveCount": len(live),
    "placeholders": placeholders[:MAX_APPS],
    "apps": apps[:MAX_APPS],
  }


def snapshot() -> dict[str, Any]:
  desktops = desktop_entries()
  live = [c for c in clients() if str((c.get("workspace") or {}).get("name") or "") == WORKSPACE]
  apps = []
  seen = set()
  for client in live:
    app = app_from_client(client, desktops)
    if app["class"] in seen or len(apps) >= MAX_APPS:
      continue
    seen.add(app["class"])
    apps.append(app)
  save_remembered(apps)
  save_tracker({str(c.get("address") or ""): str(c.get("class") or "") for c in live if c.get("address")})
  return {"apps": apps, "count": len(apps)}


def lua_str(value: str) -> str:
  return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def dispatch(lua: str) -> bool:
  raw = run_hyprctl(["dispatch", lua], max_bytes=DISPATCH_MAX_BYTES)
  if raw is None:
    sys.stderr.write("dispatch failed\n")
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


def restore_geometry(cls: str, geo: dict[str, Any], known_addrs: set[str]) -> None:
  deadline = time.time() + 15
  client = None
  while time.time() < deadline:
    client = find_app_window(cls, known_addrs)
    if client is not None:
      break
    time.sleep(0.15)
  if client is None:
    client = find_app_window(cls)
  if client is None:
    print(f"timed out waiting for {cls}", file=sys.stderr)
    return
  for delay in (0.0, 0.25, 0.7, 1.5):
    if delay:
      time.sleep(delay)
    current = find_app_window(cls) or client
    apply_geometry(current, geo)
    current = find_app_window(cls) or current
    if geometry_matches(current, geo):
      return


def exec_rules(app: dict[str, Any]) -> str:
  parts = ["workspace = 'special:scratchpad'"]
  geo = geometry_of(app)
  if geo:
    monitor = monitor_for(geo.get("monitor")) or {}
    local_x = geo["x"] - int(monitor.get("x") or 0)
    local_y = geo["y"] - int(monitor.get("y") or 0)
    parts.append("float = true")
    parts.append(f"size = {{{geo['w']}, {geo['h']}}}")
    parts.append(f"move = {{{local_x}, {local_y}}}")
  return "{ " + ", ".join(parts) + " }"


def launch(app_id: str) -> int:
  apps = remembered()
  app = next((item for item in apps if item.get("id") == app_id or item.get("class") == app_id), None)
  if not app:
    print(f"unknown scratchpad app: {app_id}", file=sys.stderr)
    return 1
  command = str(app.get("command") or "").strip()
  if not command or not is_safe_command(command):
    print(f"no trusted command for {app_id}", file=sys.stderr)
    return 1
  cls = str(app.get("class") or "")
  known_addrs = {str(client.get("address") or "") for client in clients() if client.get("address")}
  if not dispatch("hl.dsp.exec_cmd(" + lua_str(command) + ", " + exec_rules(app) + ")"):
    return 1
  geo = geometry_of(app)
  if geo and cls:
    restore_geometry(cls, geo, known_addrs)
  return 0


def forget(app_id: str) -> int:
  apps = [app for app in remembered() if app.get("id") != app_id and app.get("class") != app_id]
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
  if action == "launch" and len(sys.argv) > 2:
    return launch(sys.argv[2])
  if action == "forget" and len(sys.argv) > 2:
    return forget(sys.argv[2])
  print("usage: layout.py status|snapshot|launch <id>|forget <id>", file=sys.stderr)
  return 2


if __name__ == "__main__":
  raise SystemExit(main())
