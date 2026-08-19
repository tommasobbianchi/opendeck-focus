# OpenDeck Focus (GNOME Wayland)

Makes an [OpenDeck](https://github.com/nekename/OpenDeck) deck **contextual** on GNOME under
Wayland — the keys follow the focused application — and gives it a launcher page on a dedicated
key.

## What this is, and what it deliberately is not

Almost all of this already exists and is not reimplemented here:

| Job | Who does it |
|---|---|
| Switch profile when the focused app changes | OpenDeck itself (`applications.json` + its application watcher) |
| Launch an application from a key | the **Linux App Launcher** plugin (`me.amankhanna.oadesktopentry`) |
| Run a command from a key | the **OpenDeck Starter Pack** plugin |
| Know which window has focus, on GNOME Wayland | **nothing** — this repo |

So the whole of this project is that last row, plus the wiring to hold it together. There is no
OpenDeck plugin here and no profile-switching logic: the application-to-profile mapping lives in
OpenDeck's own UI, where a user would look for it.

## Why GNOME needs anything at all

OpenDeck's watcher calls `active_win_pos_rs::get_active_window()`. On Wayland that knows KWin
and Hyprland; on GNOME it falls through to the XCB path and reads `_NET_ACTIVE_WINDOW` from the
X root window. While a native Wayland client has focus, mutter points that at an
XWayland-internal window with no `WM_CLASS`, so the watcher reads an empty application name and
per-app profiles never fire. That is upstream issue
[#149](https://github.com/nekename/OpenDeck/issues/149), closed as "X11 only".

Asking GNOME directly does not work either: `org.gnome.Shell.Introspect.GetWindows` answers
`AccessDenied` to callers that are not on GNOME's internal allowlist, and `xdotool` sees only
XWayland clients and reports stale focus for the rest. A shell extension is the only reliable
route — which is why StreamController ships one too.

## How it works

```
GNOME Shell extension          opendeck-focus daemon              OpenDeck
notify::focus-window  ──D-Bus──▶  shim X11 window  ──_NET_ACTIVE_WINDOW──▶  application watcher
                                        ▲                                          │
deck key ──Run Command──▶ mode socket ──┘                                          ▼
                                                                          switches profile
```

The daemon keeps one unmapped X11 window whose `WM_CLASS` mirrors the really-focused window,
and points `_NET_ACTIVE_WINDOW` at it. It answers the question OpenDeck is already asking
instead of patching OpenDeck, so stock packages keep working across upgrades — and any other
X11-only tool on the machine gets correct focus for free.

Mutter rewrites `_NET_ACTIVE_WINDOW` only when XWayland focus changes, so for native Wayland
clients ours stands. When an XWayland client is focused both are correct anyway, though the
class strings can differ slightly (GNOME's `wm_class` vs the X property), which can show the
same app twice in OpenDeck's application list.

## The two screenless buttons

The N1's two buttons without screens are the mode keys, bound to `Run Command`:

| Button | Command | Effect |
|---|---|---|
| left | `opendeck-focus mode launcher` | pins the synthetic application `OpenDeckLauncher`, which is mapped to the Launcher profile |
| right | `opendeck-focus mode contextual` | resumes following the focused window |

The launcher un-pins itself as soon as focus actually moves to another application — which is
exactly what launching something does, so you land on the new app's keys rather than staring at
the launcher you just used. Pressing deck keys does not move focus, so it stays up while you
read it.

Pinning a synthetic application rather than sending a profile switch is deliberate: the mode
keys and the focus watcher then drive OpenDeck through the same single mechanism, so they cannot
fight each other. (It also sidesteps OpenDeck restricting `switchProfile` to two hardcoded
plugin UUIDs.)

## Install

```bash
./install.sh          # daemon + shell extension + user service
pkill -x opendeck
./setup-n1.py         # Launcher profile from your GNOME favourites, mode keys in every profile
systemd-run --user --unit=opendeck --collect /usr/bin/opendeck
```

The shell only scans for new extensions at session start, so `install.sh` writes the uuid into
`org.gnome.shell enabled-extensions` and it loads itself at your **next login** — no forced
logout. Until then the daemon runs and the mode keys work; only the focus-following part waits.

`setup-n1.py` backs up every file it touches next to the original (`.bak`).

## Configuring it

Assign applications to profiles in OpenDeck: **Settings → Applications**. Names appear in that
list as you focus each app, which now works because of the shim.

The mode keys have to be present in *every* profile, otherwise you could switch away and have no
way back. `setup-n1.py` stamps them into every profile that exists when it runs; re-run it after
creating profiles in the UI.

## Checking it

```bash
journalctl --user -u opendeck-focus -f      # "Publishing <WM_CLASS>" on each focus change
gnome-extensions info opendeck-focus@nativedev
```

## Layout assumption

`setup-n1.py` assumes the VSD Stream Dock N1 (15 LCD keys, two screenless buttons at positions
15 and 16, one encoder) — see [opendeck-vsd-n1](https://github.com/tommasobbianchi/opendeck-vsd-n1).
For another deck, change `MODE_KEYS` at the top of the script; nothing else is device-specific,
and the daemon is not device-specific at all.

## License

GPL-3.0.
