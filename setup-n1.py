#!/usr/bin/env python3
"""Wires an OpenDeck device up for contextual switching plus a launcher page.

Image paths here are absolute on purpose: OpenDeck's webserver serves files by absolute path
(refusing anything outside the config directory), so a relative "plugins/..." path renders as a
blank key.

Everything here is ordinary OpenDeck configuration -- profiles, and the application-to-profile
map its own watcher consults. Nothing is patched; the script exists because clicking it
together in the UI is tedious, not because the UI cannot do it.

Creates a Launcher profile from your GNOME favourites, points the synthetic OpenDeckLauncher
application at it, and puts the two mode keys on the deck's screenless buttons in every profile
(they have to be in every profile, otherwise you could switch away and not be able to get back).

OpenDeck must not be running: it holds these files in memory and rewrites them on exit.
"""

import argparse
import base64
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

CONFIG = Path.home() / ".config" / "opendeck"
LAUNCHER_PROFILE = "Launcher"
LAUNCHER_APPLICATION = "OpenDeckLauncher"  # must match LAUNCHER_CLASS in src/main.rs

# The strip of small screens sits physically above the 15 main keys, so it is grid row 0: the
# two with buttons behind them take slots 0 and 1, and the knob's screen takes slot 2.
MODE_KEYS = {0: ("launcher", "Launcher"), 1: ("contextual", "Auto")}
FIRST_MAIN_KEY = 3

# The mode keys have no application behind them to borrow an icon from, so they ship with
# their own. Both leave their centre dark, because OpenDeck draws the key's text over the
# image and the label needs somewhere to sit.
HERE = Path(__file__).resolve().parent
MODE_ICONS = {"launcher": HERE / "assets/mode-launcher.png", "contextual": HERE / "assets/mode-contextual.png"}

STARTERPACK = "com.amansprojects.starterpack.sdPlugin"
LAUNCHAPP = "me.amankhanna.oadesktopentry.sdPlugin"

APPLICATION_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
]


def state(image, text=""):
    return {
        "alignment": "middle",
        "background_colour": "#000000",
        "colour": "#FFFFFF",
        "family": "Liberation Sans",
        "image": image,
        "image_scale": 100,
        "name": "",
        "show": True,
        "size": 16,
        "stroke_colour": "#000000",
        "stroke_size": 3,
        "style": "Regular",
        "text": text,
        "underline": False,
    }


def key(position, plugin, uuid, name, tooltip, icon, inspector, settings, text=""):
    return {
        "action": {
            "controllers": ["Keypad", "Encoder"],
            "disable_automatic_states": False,
            "encoder": None,
            "icon": icon,
            "name": name,
            "plugin": plugin,
            "property_inspector": inspector,
            "states": [state(icon)],
            "supported_in_multi_actions": True,
            "tooltip": tooltip,
            "uuid": uuid,
            "visible_in_action_list": True,
        },
        "children": None,
        "context": f"Keypad.{position}.0",
        "current_state": 0,
        "settings": settings,
        "states": [state(icon, text)],
    }


def mode_icon(mode):
    """The key's own icon as a data URI, or the plugin's default if the file is missing."""
    path = MODE_ICONS.get(mode)
    if not (path and path.is_file()):
        return str(CONFIG / "plugins" / STARTERPACK / "icons/runCommand.png")

    image = Image.open(path).convert("RGB").resize((96, 96), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def mode_key(position, mode, label, binary):
    return key(
        position,
        STARTERPACK,
        "com.amansprojects.starterpack.runcommand",
        "Run Command",
        "Run a command",
        mode_icon(mode),
        f"plugins/{STARTERPACK}/propertyInspector/runCommand.html",
        {"down": f"{binary} mode {mode}", "up": "", "rotate": "", "file": "", "show": False},
        label,
    )


def launch_key(position, desktop_file):
    return key(
        position,
        LAUNCHAPP,
        "me.amankhanna.oadesktopentry.launchapp",
        "Launch App",
        "Launch an application",
        str(CONFIG / "plugins" / LAUNCHAPP / "icon.png"),
        f"plugins/{LAUNCHAPP}/pi/launchapp.html",
        {"app": str(desktop_file)},
    )


def find_desktop_file(name):
    for directory in APPLICATION_DIRS:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def gnome_favourites():
    try:
        raw = subprocess.run(
            ["gsettings", "get", "org.gnome.shell", "favorite-apps"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    # GVariant array of strings, close enough to a Python list literal to read directly.
    return [entry.strip().strip("'") for entry in raw.strip("[]").split(",") if entry.strip()]


def load(path, default):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def save(path, value):
    if path.is_file():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(value, indent=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="device id, e.g. n1-81D0DA783809 (default: the only one)")
    parser.add_argument("--binary", default=str(Path.home() / ".local/bin/opendeck-focus"),
                        help="path to the opendeck-focus binary the mode keys will run")
    arguments = parser.parse_args()

    if subprocess.run(["pgrep", "-x", "opendeck"], capture_output=True).returncode == 0:
        sys.exit("OpenDeck is running; stop it first or it will overwrite these files on exit.")

    profiles_root = CONFIG / "profiles"
    devices = sorted(path.name for path in profiles_root.iterdir() if path.is_dir())
    if arguments.device:
        device = arguments.device
    elif len(devices) == 1:
        device = devices[0]
    else:
        sys.exit(f"Pick one with --device: {', '.join(devices) or 'no devices found'}")

    device_dir = profiles_root / device
    if not device_dir.is_dir():
        sys.exit(f"No such device: {device}")

    # --- the launcher page ---------------------------------------------------------------
    launcher_keys = [None] * 18
    position = FIRST_MAIN_KEY
    for entry in gnome_favourites():
        if position >= 18:  # the main block ends here
            break
        desktop_file = find_desktop_file(entry)
        if desktop_file is None:
            print(f"  skipping {entry}: no .desktop file found")
            continue
        launcher_keys[position] = launch_key(position, desktop_file)
        position += 1

    launcher_path = device_dir / f"{LAUNCHER_PROFILE}.json"
    launcher = load(launcher_path, {"infobars": [], "keys": launcher_keys, "sliders": [None]})
    launcher["keys"] = launcher_keys
    save(launcher_path, launcher)
    print(f"Launcher profile: {position - FIRST_MAIN_KEY} app(s) -> {launcher_path}")

    # --- mode keys in every profile ------------------------------------------------------
    for profile_path in sorted(device_dir.glob("*.json")):
        profile = load(profile_path, None)
        if profile is None:
            continue
        keys = profile.setdefault("keys", [None] * 18)
        while len(keys) < 18:
            keys.append(None)
        for slot, (mode, label) in MODE_KEYS.items():
            keys[slot] = mode_key(slot, mode, label, arguments.binary)
        save(profile_path, profile)
        print(f"Mode keys in {profile_path.name}")

    # --- point the synthetic application at the launcher profile -------------------------
    applications_path = CONFIG / "applications.json"
    applications = load(applications_path, {})
    applications.setdefault(LAUNCHER_APPLICATION, {})[device] = LAUNCHER_PROFILE
    # OpenDeck's own fallback, used for any application without a profile of its own -- which
    # is also what leaving the launcher lands on when nothing else matches.
    applications.setdefault("opendeck_default", {}).setdefault(device, "Default")
    save(applications_path, applications)
    print(f"{LAUNCHER_APPLICATION} -> {LAUNCHER_PROFILE} for {device}")

    print("\nStart OpenDeck. Assign your other applications to profiles in Settings -> "
          "Applications; they appear there as you focus them.")


if __name__ == "__main__":
    main()
