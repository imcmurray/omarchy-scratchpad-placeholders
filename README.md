# Scratchpad placeholders

Remember Omarchy scratchpad apps across reboots and restore them as clickable
placeholders.

When you send an app to the scratchpad (`Super + Alt + S`) and later close it
or reboot, this overlay shows a tile for that app the next time you open the
scratchpad (`Super + S`). Left-click relaunches it on the scratchpad in the
same size and position. Right-click forgets it.

This is not a notes pad and not a bar occupancy indicator. It restores the
apps you actually kept on the Hyprland scratchpad.

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
