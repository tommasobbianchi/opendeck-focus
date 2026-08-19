# OpenDeck Focus (GNOME Wayland)

Switches [OpenDeck](https://github.com/nekename/OpenDeck) profiles to follow the focused window
on GNOME under Wayland, where OpenDeck's own application watcher cannot see windows.

> [!WARNING]
> **This does not work against stock OpenDeck yet.** OpenDeck restricts the `switchProfile`
> event to two hardcoded plugin UUIDs, so profile switches from any third-party plugin are
> silently discarded. See [Status](#status).

## Why it exists

OpenDeck can already switch profiles per application, but its window watcher only covers X11
and KDE (via `kdotool`). Under GNOME Wayland nothing outside the compositor can learn what has
focus:

- `org.gnome.Shell.Introspect.GetWindows` answers `AccessDenied` to callers that are not on
  GNOME's internal allowlist.
- `xdotool` sees only XWayland clients, and reports stale focus when a native Wayland window is
  active — it will happily name a background app while you type in another.

A GNOME Shell extension is therefore the only reliable route. That is exactly why
StreamController ships one too.

## Parts

| Part | Role |
|---|---|
| `gnome-shell-extension/opendeck-focus@nativedev` | Publishes the focused window's WM class and title on the session bus |
| the Rust plugin | Subscribes to that signal and asks OpenDeck to switch profile |

The extension exposes `org.gnome.Shell.Extensions.OpenDeckFocus` at
`/org/gnome/Shell/Extensions/OpenDeckFocus`, with a `FocusedWindowChanged` signal and a
`GetFocusedWindow` method, both carrying JSON so fields can be added without breaking the
interface.

## Install

```bash
cp -r gnome-shell-extension/opendeck-focus@nativedev ~/.local/share/gnome-shell/extensions/
gnome-extensions enable opendeck-focus@nativedev
```

Under Wayland the shell cannot be restarted in place, and it only scans for new extensions at
session start, so a newly dropped extension needs one logout. To avoid forcing that, append the
uuid to the enabled list and it loads itself at the next natural login:

```bash
gsettings get org.gnome.shell enabled-extensions   # then append 'opendeck-focus@nativedev'
```

Then build and install the plugin:

```bash
cargo build --release
DEST=~/.config/opendeck/plugins/dev.native.plugins.opendeck-focus.sdPlugin
mkdir -p "$DEST"
cp -r assets manifest.json "$DEST/"
cp target/release/opendeck-focus "$DEST/opendeck-focus-linux"
```

## Rules

`~/.config/opendeck-focus/rules.json`:

```json
{
  "device": "n1-81D0DA783809",
  "default_profile": "Default",
  "rules": [
    { "wm_class": "freecad", "profile": "FreeCAD" },
    { "wm_class": "firefox", "profile": "Web" }
  ]
}
```

The device id is in the device plugin's log. Matching is a case-insensitive **substring**,
because WM classes vary by packaging — Firefox reports `firefox` on X11 and
`org.mozilla.firefox` on some Wayland builds. First matching rule wins, so put specific rules
first. Profiles do not need to exist beforehand: OpenDeck creates a profile store on demand.

A missing rules file is not fatal; the plugin logs and stays idle rather than wedging OpenDeck.

## Status

The GNOME half works. The OpenDeck half is blocked upstream:

```rust
// src-tauri/src/events/inbound/mod.rs
} else if matches!(decoded, InboundEventType::SwitchProfile(_) | InboundEventType::DeviceBrightness(_))
    && uuid != "com.amansprojects.starterpack.sdPlugin"
    && uuid != "opendeck_alternative_elgato_implementation"
{
    return;
}
```

Profile switches from this plugin hit that `return` — no log, no error, so the plugin looks
healthy while doing nothing. Resolving it needs one of:

1. OpenDeck accepting a GNOME-Wayland window backend, so its built-in per-app switching works
   natively. Best outcome: fixes this for every GNOME user, and makes this repo unnecessary.
2. OpenDeck relaxing the allowlist for `switchProfile`.

Option 1 is the one worth pursuing.

## License

GPL-3.0.
