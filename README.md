# Scratchpad placeholders

Remember Omarchy scratchpad apps across reboots and restore them as clickable
placeholders.

Anything you put on the scratchpad is remembered automatically. Move or
resize those windows and the saved layout updates with them. Close an app or
reboot, then open the scratchpad (`Super + S`) — a tile stands in for each
missing app. Left-click relaunches it on the scratchpad in the last size and
position. Right-click forgets it.

This is not a notes pad and not a bar occupancy indicator. It restores the
apps you actually kept on the Hyprland scratchpad.

![Scratchpad placeholder tiles for GitLab, Terminal, Obsidian, and Activity](preview.png)

## How it remembers

No snapshot command. The plugin watches `special:scratchpad`:

- **New window** — sending an app to the scratchpad (`Super + Alt + S`) adds
  it to the remembered set: name, launch command, icon, size, and position.
- **Layout changes** — while the app is still on the scratchpad, moving,
  resizing, or floating it updates the saved geometry. The next restore uses
  whatever you left it as, not the first size you happened to use.
- **Closed or after reboot** — if the app is gone, opening the scratchpad
  shows a placeholder tile instead of an empty drop-down.
- **Moved off the scratchpad** — sending the live window to a normal
  workspace forgets it. The scratchpad is only for what you keep there.
- **Restore** — left-click a tile to launch that app back onto the
  scratchpad at the saved size and position. Right-click removes the tile.

Requires **Omarchy 4** (Quattro) and Python 3. The helper uses only the
standard library plus `hyprctl`.

## Install

```sh
omarchy plugin add https://github.com/imcmurray/omarchy-scratchpad-placeholders.git --enable
```

From a local checkout:

```sh
omarchy plugin add /home/ianm/Development/omarchy-scratchpad-placeholders --enable
```

Then rescan if the overlay does not appear:

```sh
omarchy-shell shell rescanPlugins
omarchy plugin list | grep ianm.scratchpad
```

## Usage

| Action | How |
| --- | --- |
| Toggle the scratchpad | `Super + S` |
| Send the focused window to the scratchpad | `Super + Alt + S` |
| Relaunch a remembered app | Left-click its tile |
| Forget a remembered app | Right-click its tile |

Remembered apps are stored in `~/.config/omarchy/scratchpad.json`. Tracker
state lives in `~/.local/state/omarchy/scratchpad-tracker.json`. The plugin
does not rewrite Hyprland config or other user files.

## Remove

```sh
omarchy plugin remove ianm.scratchpad
```

Optional cleanup of remembered apps:

```sh
rm -f ~/.config/omarchy/scratchpad.json
rm -f ~/.local/state/omarchy/scratchpad-tracker.json
```

## Develop

```sh
omarchy plugin validate .
```

The overlay entry point is `Main.qml`. Layout tracking and launch live in
`layout.py`.

## License

MIT
