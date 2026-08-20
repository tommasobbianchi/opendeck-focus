#!/usr/bin/env python3
"""Gives deck keys the image that belongs on them.

    --auto  works out every key's image from what the key does, and applies it.

Where the image comes from depends on the action:

  Open URL     the site's own icon -- the page is read for its <link rel=icon> tags, its
               apple-touch-icon and its web app manifest, largest first, because that is where
               the sharp 180px and 512px versions live. /favicon.ico and Google's favicon cache
               are only the fallbacks, for sites that declare nothing or block the request.
  Launch App   the application's icon, from `Icon=` in its .desktop file, resolved through the
               icon themes on this system (SVGs are rasterised) or taken as an absolute path,
               which is how snap and flatpak entries usually give it.
  Run Command  the icon of the application the command runs, if a .desktop file launches the
               same executable.
  anything     a tile carrying the key's own words. This is a proposal rather than an answer:
  else         it is offered because a page of identical plugin logos tells you nothing about
               what the keys do. `--no-labels` declines it.

Single keys can also be set by hand, with --key and --image or --favicon.

OpenDeck must not be running: it holds profiles in memory and rewrites them on exit.
"""

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CONFIG = Path.home() / ".config" / "opendeck"
KEY_SIZE = (96, 96)

APPLICATION_DIRS = [
    Path.home() / ".local/share/applications",
    Path("/usr/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
]


# Sites refuse the default urllib agent often enough that it is not worth finding out which.
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

ICON_RELS = {"icon", "shortcut icon", "apple-touch-icon", "apple-touch-icon-precomposed", "fluid-icon"}


def fetch(url, limit=4 * 1024 * 1024):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(limit), response.url


class IconLinkParser(HTMLParser):
    """Collects <link rel=icon> and friends, plus the web app manifest, from a page head."""

    def __init__(self):
        super().__init__()
        self.icons = []      # (declared_size, href)
        self.manifest = None

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        attributes = {key.lower(): (value or "") for key, value in attrs}
        rels = {rel.strip().lower() for rel in attributes.get("rel", "").split()}
        href = attributes.get("href")
        if not href:
            return
        if "manifest" in rels:
            self.manifest = href
        elif rels & ICON_RELS or " ".join(sorted(rels)) in ICON_RELS:
            # "192x192", or several sizes; take the largest declared.
            declared = 0
            for token in attributes.get("sizes", "").lower().split():
                if "x" in token:
                    try:
                        declared = max(declared, int(token.split("x")[0]))
                    except ValueError:
                        pass
            # An apple-touch-icon is 180px and rarely says so; a bare <link rel=icon> with no
            # size is usually the 16px one, so rank it below anything that declares itself.
            if not declared:
                declared = 180 if "apple-touch-icon" in " ".join(rels) else 1
            self.icons.append((declared, href))


def decode(raw):
    """Returns the largest usable frame of an image, or None if it is not one."""
    try:
        image = Image.open(io.BytesIO(raw))
        # .ico files carry several resolutions and PIL opens the first, not the best.
        if image.format == "ICO":
            largest = max(image.ico.sizes())
            image = image.ico.getimage(largest)
        image.load()
        return image
    except Exception:  # noqa: BLE001 -- an HTML error page reaches here as often as a bad image
        return None


def icon_candidates(url):
    """Yields candidate icon URLs for a page, best guesses first."""
    page_url = url if "//" in url else f"https://{url}"
    parsed = urllib.parse.urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        body, final_url = fetch(page_url)
    except Exception as error:  # noqa: BLE001
        print(f"    could not read {page_url}: {error}")
        body, final_url = b"", page_url

    if body[:1] == b"<" or b"<html" in body[:2048].lower():
        parser = IconLinkParser()
        try:
            parser.feed(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 -- malformed markup is not worth failing over
            pass

        # A web app manifest usually lists the largest icons a site has.
        if parser.manifest:
            try:
                manifest_raw, manifest_url = fetch(urllib.parse.urljoin(final_url, parser.manifest))
                for entry in json.loads(manifest_raw).get("icons", []):
                    size = max((int(t.split("x")[0]) for t in entry.get("sizes", "").lower().split() if "x" in t), default=1)
                    if entry.get("src"):
                        parser.icons.append((size, urllib.parse.urljoin(manifest_url, entry["src"])))
            except Exception:  # noqa: BLE001
                pass

        for _, href in sorted(parser.icons, key=lambda item: -item[0]):
            yield urllib.parse.urljoin(final_url, href)

    yield f"{origin}/favicon.ico"
    yield f"{origin}/apple-touch-icon.png"
    # Last resort: the site has nothing discoverable, or blocked us. Google has it cached.
    domain = parsed.netloc
    yield f"https://www.google.com/s2/favicons?domain={urllib.parse.quote(domain)}&sz=128"


def favicon(url, verbose=True):
    """Reads a page and fetches the best icon it declares, largest first.

    Sites disagree about where their icon lives -- <link rel=icon>, an apple-touch-icon, a web
    app manifest, or nothing at all but /favicon.ico -- so all of them are tried in the order
    that gives the sharpest key, and the first that decodes to at least 32px wins."""
    best = None
    for candidate in icon_candidates(url):
        try:
            raw, _ = fetch(candidate)
        except Exception:  # noqa: BLE001
            continue
        image = decode(raw)
        if image is None:
            continue
        if verbose:
            print(f"    {image.width}x{image.height} from {candidate}")
        if best is None or image.width > best[0].width:
            best = (image, candidate)
        # Anything at key resolution or better is as good as this is going to get.
        if best[0].width >= 96:
            break
    if best is None:
        raise LookupError(f"no usable icon found for {url}")
    return best[0]


def to_data_uri(image, size, background):
    image = image.convert("RGBA")
    image.thumbnail(size, Image.LANCZOS)

    # Centre it on a key-sized canvas so small favicons are not stretched into mush.
    canvas = Image.new("RGBA", size, background)
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2), image)

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


# --- where an icon comes from, per action -------------------------------------------------

OPEN_URL_ACTION = "com.amansprojects.starterpack.openurl"
LAUNCH_APP_ACTION = "me.amankhanna.oadesktopentry.launchapp"
RUN_COMMAND_ACTION = "com.amansprojects.starterpack.runcommand"
SWITCH_PROFILE_ACTION = "com.amansprojects.starterpack.switchprofile"

ICON_ROOTS = [
    Path.home() / ".local/share/icons",
    Path.home() / ".icons",
    Path("/usr/local/share/icons"),
    Path("/usr/share/icons"),
    Path("/usr/share/pixmaps"),
    Path("/var/lib/flatpak/exports/share/icons"),
]

_icon_index = None


def icon_index():
    """Maps icon name -> files, from every theme directory on the system.

    Built by walking once rather than globbing per name: a dozen keys against a full icon theme
    is thousands of stat calls otherwise. Not a full freedesktop theme lookup -- it ignores
    theme inheritance and picks by resolution instead, which is what actually matters here,
    since a key wants the sharpest version of the icon and not the themed one."""
    global _icon_index
    if _icon_index is not None:
        return _icon_index

    _icon_index = {}
    for root in ICON_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() in (".png", ".svg", ".xpm") and path.is_file():
                _icon_index.setdefault(path.stem, []).append(path)
    return _icon_index


def icon_file_size(path):
    """Best guess at an icon file's resolution, from the theme directory it sits in."""
    if path.suffix.lower() == ".svg":
        return 10_000  # scalable: better than any raster, if we can rasterise it
    for part in path.parts:
        head = part.split("x")[0]
        if head.isdigit():
            return int(head)
    return 0


def rasterise_svg(path, size):
    """SVGs need an external renderer; ImageMagick is the one most likely to be present."""
    for command in (
        ["rsvg-convert", "-w", str(size), "-h", str(size), str(path)],
        ["magick", "-background", "none", "-density", "384", str(path), "-resize", f"{size}x{size}", "png:-"],
        ["convert", "-background", "none", "-density", "384", str(path), "-resize", f"{size}x{size}", "png:-"],
    ):
        try:
            result = subprocess.run(command, capture_output=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout:
            return decode(result.stdout)
    return None


def icon_by_name(name, size=256):
    """Resolves a .desktop `Icon=` value: an absolute path, or a name in an icon theme."""
    if not name:
        return None

    path = Path(name)
    if path.is_absolute():
        candidates = [path] if path.is_file() else []
    else:
        candidates = icon_index().get(name, [])
        if not candidates:
            # Some entries name the file rather than the icon, e.g. Icon=foo.png
            candidates = icon_index().get(path.stem, [])

    best = None
    for candidate in sorted(candidates, key=icon_file_size, reverse=True):
        image = rasterise_svg(candidate, size) if candidate.suffix.lower() == ".svg" else decode(candidate.read_bytes())
        if image is None:
            continue
        if best is None or image.width > best.width:
            best = image
        if best.width >= 96:
            break
    return best


def executables_in(command):
    """The binaries a .desktop Exec line ends up running.

    Applications installed outside the package manager -- AppImages, self-contained builds --
    are usually launched through a wrapper script, and their icon lives in their own install
    tree rather than in any icon theme. So the wrapper is read for the absolute paths it
    mentions, and those are searched too."""
    if not command:
        return []

    first = Path(command.split()[0])
    found = [first] if first.is_file() else []

    if first.is_file() and first.stat().st_size < 256 * 1024:
        try:
            text = first.read_text(errors="replace")
        except OSError:
            return found
        if text.startswith("#!"):
            for token in re.findall(r"/[\w.@+/-]+", text.replace("$HOME", str(Path.home()))):
                path = Path(token)
                if path.is_file() and path not in found:
                    found.append(path)
    return found


def icon_near_executable(command, name):
    """Looks for an icon inside an application's own install tree."""
    if not name:
        return None

    for executable in executables_in(command):
        # bin/foo -> the prefix that holds share/, resources/, lib/ alongside it
        prefix = executable.parent.parent if executable.parent.name == "bin" else executable.parent
        for subdirectory in ("resources/images", "resources", "share/icons", "share/pixmaps", "share"):
            root = prefix / subdirectory
            if not root.is_dir():
                continue
            for suffix in (".svg", ".png"):
                for candidate in sorted(root.rglob(f"{name}{suffix}"))[:4]:
                    image = rasterise_svg(candidate, 256) if suffix == ".svg" else decode(candidate.read_bytes())
                    if image is not None:
                        return image, candidate
    return None


def desktop_entry(path):
    """Reads a .desktop file into a dict, from its [Desktop Entry] group only."""
    entry, in_group = {}, False
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return entry
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_group = line == "[Desktop Entry]"
        elif in_group and "=" in line and not line.startswith("#"):
            field, _, value = line.partition("=")
            entry.setdefault(field.strip(), value.strip())
    return entry


_command_index = None


def command_index():
    """Maps an executable's basename to a .desktop file that launches it, so a Run Command key
    can borrow the icon of the application it runs."""
    global _command_index
    if _command_index is not None:
        return _command_index

    _command_index = {}
    for directory in APPLICATION_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.desktop")):
            executable = desktop_entry(path).get("Exec", "").split()
            if executable:
                _command_index.setdefault(Path(executable[0]).name, path)
    return _command_index


def wrap_to_width(draw, text, font, width):
    """Greedy wrap that also breaks a single word too long to fit, which a plain word wrap
    leaves hanging off both edges of the key."""
    lines, line = [], ""
    for word in text.split():
        while draw.textlength(word, font=font) > width and len(word) > 1:
            cut = len(word)
            while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                cut -= 1
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def load_font(size):
    for name in ("LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf"):
        for candidate in Path("/usr/share/fonts").rglob(name):
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


def label_tile(text, size, background):
    """The proposal of last resort: the key's own words, set cleanly, instead of a plugin logo
    repeated across a page of keys that do different things.

    An empty label is not a failure -- it means OpenDeck already has text for this key and will
    draw it itself, so the tile is just a clean background to draw it on."""
    image = Image.new("RGB", size, background)
    if not text:
        return image

    draw = ImageDraw.Draw(image)
    margin = 8
    usable = size[0] - margin * 2

    # Shrink until it fits the key in both directions, rather than clipping. A word broken
    # across two lines reads as a rendering fault rather than a label, so a size that would
    # need one is rejected while there is still room to go smaller.
    for font_size in range(26, 8, -2):
        font = load_font(font_size)
        spacing = int(font_size * 1.25)
        lines = wrap_to_width(draw, text, font, usable)
        words_fit = all(draw.textlength(word, font=font) <= usable for word in text.split())
        if words_fit and len(lines) * spacing <= size[1] - margin:
            break

    top = (size[1] - spacing * len(lines)) // 2
    for index, line in enumerate(lines):
        width = draw.textlength(line, font=font)
        draw.text(((size[0] - width) / 2, top + index * spacing), line, font=font, fill="#FFFFFF")
    return image


def resolve_icon(key, background):
    """Finds the image that belongs on a key. Returns (image, description, is_proposal)."""
    action = key.get("action") or {}
    uuid = action.get("uuid", "")
    settings = key.get("settings") or {}

    if uuid == OPEN_URL_ACTION:
        url = first_setting(settings, "down", "up", "clockwise", "anticlockwise")
        if url:
            try:
                return favicon(url), url, False
            except (LookupError, OSError) as error:
                print(f"    no icon for {url}: {error}")

    elif uuid == LAUNCH_APP_ACTION:
        path = settings.get("app")
        if path:
            entry = desktop_entry(path)
            image = icon_by_name(entry.get("Icon"))
            if image is not None:
                return image, f"{entry.get('Name', Path(path).stem)} ({entry.get('Icon')})", False

            print(f"    no icon named {entry.get('Icon')!r} in any theme; looking in the app's own files")
            found = icon_near_executable(entry.get("Exec"), entry.get("Icon"))
            if found is not None:
                image, source = found
                return image, f"{entry.get('Name', Path(path).stem)} ({source})", False

    elif uuid == RUN_COMMAND_ACTION:
        command = first_setting(settings, "down", "up", "rotate")
        if command:
            executable = Path(command.split()[0]).name
            path = command_index().get(executable)
            if path:
                image = icon_by_name(desktop_entry(path).get("Icon"))
                if image is not None:
                    return image, f"{executable} (via {path.name})", False

    # Nothing authoritative to show. Offer the key's own words on a clean background -- but if
    # OpenDeck already has text for this key it draws that itself, so the tile stays empty
    # rather than printing the same words twice. That also makes a second --auto run a no-op
    # instead of a game of telephone with its own output.
    existing = (key.get("states") or [{}])[0].get("text", "").strip()
    label = "" if existing else proposed_label(key)
    return label_tile(label, KEY_SIZE, background), f"label {label!r}" if label else f"clean background under {existing!r}", True


def page_name(url):
    """A name for a page that has no icon -- its <title>, or failing that its host.

    Self-hosted services on a LAN address are the usual case here: no favicon, nothing indexed
    anywhere, but a perfectly good title sitting in the markup."""
    try:
        body, _ = fetch(url if "//" in url else f"https://{url}", limit=64 * 1024)
        match = re.search(r"<title[^>]*>(.*?)</title>", body.decode("utf-8", "replace"), re.I | re.S)
        if match:
            title = " ".join(match.group(1).split())
            # Titles are often "Name - section - site"; the first part is the name.
            for separator in ("—", "–", " - ", " | ", ": "):
                if separator in title:
                    title = title.split(separator)[0].strip()
                    break
            if title:
                return title[:24]
    except Exception:  # noqa: BLE001 -- an unreachable host just means fall back to the URL
        pass

    host = urllib.parse.urlparse(url if "//" in url else f"https://{url}").netloc
    return (host or url).removeprefix("www.").split(":")[0]


def first_setting(settings, *fields):
    for field in fields:
        value = (settings.get(field) or "").strip()
        if value:
            return value
    return None


def proposed_label(key):
    """What to write on a key we could not find a real icon for."""
    action = key.get("action") or {}
    settings = key.get("settings") or {}

    if action.get("uuid") == SWITCH_PROFILE_ACTION and settings.get("profile"):
        return settings["profile"]
    if action.get("uuid") == OPEN_URL_ACTION:
        url = first_setting(settings, "down", "up", "clockwise", "anticlockwise")
        if url:
            return page_name(url)

    if action.get("uuid") == RUN_COMMAND_ACTION:
        tokens = (first_setting(settings, "down", "up", "rotate") or "").split()
        if tokens:
            # `opendeck-focus mode launcher` is a launcher key, not an opendeck-focus key: the
            # subcommand says what it does, the binary only says who does it.
            return tokens[-1] if len(tokens) > 1 else Path(tokens[0]).name
    return action.get("name", "?")



def apply_image(key, image, background, state=None):
    data_uri = to_data_uri(image, KEY_SIZE, background)
    states = range(len(key["states"])) if state is None else [state]
    for index in states:
        key["states"][index]["image"] = data_uri
    return len(data_uri)


def save(path, profile):
    shutil.copyfile(path, str(path) + ".bak")
    path.write_text(json.dumps(profile, indent=1))


def profile_paths(profiles_root, device, name):
    directory = profiles_root / device
    if name:
        path = directory / f"{name}.json"
        if not path.is_file():
            sys.exit(f"No such profile: {path}")
        return [path]
    return sorted(directory.glob("*.json"))


def run_auto(profiles_root, device, arguments):
    """Gives every key the image that belongs on it.

    This replaces the image on every key it can resolve, including one you picked by hand:
    OpenDeck rasterises every key to `0.png` on restart, so by then a hand-picked icon and a
    plugin's default are indistinguishable. Profiles are backed up first."""
    applied = proposed = 0

    for path in profile_paths(profiles_root, device, arguments.profile):
        profile = json.loads(path.read_text())
        changed = False

        slots = [("key", index, slot) for index, slot in enumerate(profile.get("keys") or [])]
        slots += [("dial", index, slot) for index, slot in enumerate(profile.get("sliders") or [])]

        for kind, position, slot in slots:
            if not slot or not slot.get("states"):
                continue
            print(f"  {path.stem} {kind} {position}: {(slot.get('action') or {}).get('name', '?')}")

            image, description, is_proposal = resolve_icon(slot, arguments.background)
            if is_proposal and arguments.no_labels:
                print("    no icon found, leaving it alone")
                continue

            apply_image(slot, image, arguments.background, arguments.state)
            if is_proposal:
                proposed += 1
            else:
                applied += 1
            changed = True
            print(f"    {'proposed' if is_proposal else 'applied'}: {description}")

        if changed:
            save(path, profile)
            print(f"  wrote {path.name}\n")

    print(f"{applied} icon(s) applied, {proposed} label(s) proposed.")


# --- live mode ----------------------------------------------------------------------------
#
# OpenDeck holds profiles in memory, so editing the files under a running instance achieves
# nothing. It does however accept a whole inbound event on the command line:
#
#     opendeck --process-message '{"event":"setImage", ...}'
#
# which the running instance handles through its single-instance IPC. That path is explicitly
# unauthenticated (`process_incoming_message(..., "", true)` in main.rs), so unlike a plugin
# WebSocket connection -- where setImage is refused for keys the plugin does not own -- it can
# set the image of any key at all. Nothing here is patched or private to this fork.


def opendeck_binary():
    """The binary of the running instance, so a locally built OpenDeck is not bypassed in
    favour of a packaged one that is not the instance holding the profiles."""
    result = subprocess.run(["pgrep", "-x", "opendeck"], capture_output=True, text=True)
    for pid in result.stdout.split():
        try:
            return str(Path(f"/proc/{pid}/exe").resolve())
        except OSError:
            continue
    return None


def push_image(device, profile, position, controller, data_uri):
    binary = opendeck_binary()
    if binary is None:
        return False
    # OpenDeck wants the context as the flat string it uses everywhere else --
    # device.profile.controller.position.index. Handing it the object those fields came from
    # is refused with "invalid type: map, expected a string", and because the refusal is only
    # a warning in OpenDeck's log the watcher looks like it is working while pushing nothing.
    message = json.dumps({
        "event": "setImage",
        "context": f"{device}.{profile}.{controller}.{position}.0",
        "payload": {"image": data_uri},
    })
    result = subprocess.run([binary, "--process-message", message], capture_output=True, timeout=30)
    return result.returncode == 0


def signature(slot):
    """What a key *is*, ignoring how it looks. A key keeps its image while this is unchanged,
    so an icon set by hand survives, and re-pushing our own image does not loop."""
    action = (slot.get("action") or {}).get("uuid", "")
    return json.dumps([action, slot.get("settings") or {}], sort_keys=True)


def scan(profiles_root, device):
    """Yields (profile, controller, position, slot) for every occupied slot of a device."""
    for path in sorted((profiles_root / device).glob("*.json")):
        try:
            profile = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for controller, field in (("Keypad", "keys"), ("Encoder", "sliders")):
            for position, slot in enumerate(profile.get(field) or []):
                if slot and slot.get("states"):
                    yield path.stem, controller, position, slot


CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "opendeck-icons.json"


def load_known():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def save_known(known):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(known))
    except OSError as error:
        print(f"    could not write {CACHE}: {error}")


def run_watch(profiles_root, device, arguments):
    """Gives a key its icon as soon as it gets a purpose, without stopping OpenDeck.

    What has already been seen is remembered on disk. Without that, every restart of this
    service -- a logout, a crash, an edit to it -- would silently adopt whatever you had added
    since as 'already known' and those keys would never get an icon. That is exactly how two
    keys ended up bare.

    Only the very first run ever is silent, because at that point every key looks new and
    claiming them all would wipe images set by hand. Run --auto once for the initial fill."""
    known = load_known()
    first_pass = known is None
    known = known or {}
    print(f"Watching {profiles_root / device}"
          + (" (first run: adopting what is already there)" if first_pass else f" ({len(known)} key(s) known)"))

    while True:
        dirty = False
        for profile, controller, position, slot in scan(profiles_root, device):
            key = f"{profile}.{controller}.{position}"
            current = signature(slot)
            if known.get(key) == current:
                continue
            known[key] = current
            dirty = True

            if first_pass:
                continue

            name = (slot.get("action") or {}).get("name", "?")
            print(f"{profile} {controller.lower()} {position}: {name}")
            image, description, is_proposal = resolve_icon(slot, arguments.background)
            if is_proposal and arguments.no_labels:
                print("    no icon found, leaving it alone")
                continue

            data_uri = to_data_uri(image, KEY_SIZE, arguments.background)
            verb = "proposed" if is_proposal else "applied"
            if push_image(device, profile, position, controller, data_uri):
                print(f"    {verb}: {description}")
            else:
                print(f"    could not reach OpenDeck to apply {description}")
                del known[key]  # try again next time round

        if dirty:
            save_known(known)
        first_pass = False
        time.sleep(arguments.interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="device id (default: the only one)")
    parser.add_argument("--profile", help="profile name (default: all of them, with --auto)")
    parser.add_argument("--key", type=int, help="grid position of a single key to set")
    parser.add_argument("--state", type=int, default=None, help="state to set (default: all)")
    parser.add_argument("--auto", action="store_true", help="give every key the image that belongs on it")
    parser.add_argument("--no-labels", action="store_true", help="leave a key alone rather than proposing a text tile")
    parser.add_argument("--watch", action="store_true", help="run in the background, giving each new key its image as it is created")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between checks in --watch")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image", help="path to an image file")
    source.add_argument("--favicon", help="URL or domain whose icon to use")
    parser.add_argument("--background", default="#000000", help="fill behind a transparent icon")
    arguments = parser.parse_args()

    if not (arguments.auto or arguments.watch) and (arguments.key is None or not (arguments.image or arguments.favicon)):
        parser.error("give --watch, --auto, or --key with one of --image / --favicon")

    # --watch talks to the running instance instead of its files, so it wants the opposite.
    if not arguments.watch and subprocess.run(["pgrep", "-x", "opendeck"], capture_output=True).returncode == 0:
        sys.exit("OpenDeck is running; stop it first or it will overwrite this on exit.")

    profiles_root = CONFIG / "profiles"
    devices = sorted(path.name for path in profiles_root.iterdir() if path.is_dir())
    device = arguments.device or (devices[0] if len(devices) == 1 else None)
    if device is None:
        sys.exit(f"Pick one with --device: {', '.join(devices) or 'no devices found'}")

    if arguments.watch:
        return run_watch(profiles_root, device, arguments)

    if arguments.auto:
        return run_auto(profiles_root, device, arguments)

    path = profile_paths(profiles_root, device, arguments.profile or "Default")[0]
    profile = json.loads(path.read_text())
    keys = profile.get("keys") or []
    key = keys[arguments.key] if arguments.key < len(keys) else None
    if key is None:
        sys.exit(f"Key {arguments.key} of {path.stem} is empty; put an action on it first.")

    if arguments.image:
        image = decode(Path(arguments.image).read_bytes())
        if image is None:
            sys.exit(f"Not an image: {arguments.image}")
    else:
        image = favicon(arguments.favicon)

    size = apply_image(key, image, arguments.background, arguments.state)
    save(path, profile)
    print(f"{path.stem} key {arguments.key}: image set ({size // 1024} KiB)")


if __name__ == "__main__":
    main()
