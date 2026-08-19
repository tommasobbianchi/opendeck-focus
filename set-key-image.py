#!/usr/bin/env python3
"""Sets the image on a deck key, from a file or from a site's favicon.

OpenDeck can do this from its editor -- click a key, then click the image preview -- and that
is the right way to set one or two. This exists for the cases the editor is tedious for:
setting a whole page of Open URL keys at once, or scripting a profile from a list of sites.

    --auto  reads the URL off every Open URL key and gives it that site's own icon.

Finding "the right icon" means asking the page rather than guessing at /favicon.ico: the
<link rel=icon> tags, the apple-touch-icon, and the web app manifest are all consulted, largest
first, because that is where the sharp 180px and 512px versions live. /favicon.ico and Google's
favicon cache are the fallbacks, for sites that declare nothing or block us.

Images are written into the profile as data URIs, which is one of the forms OpenDeck's renderer
accepts natively, so nothing has to resolve a path later.

OpenDeck must not be running: it holds profiles in memory and rewrites them on exit.
"""

import argparse
import base64
import io
import json
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image

CONFIG = Path.home() / ".config" / "opendeck"
KEY_SIZE = (96, 96)


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


OPEN_URL_ACTION = "com.amansprojects.starterpack.openurl"


def key_url(key):
    """The URL an Open URL key opens. Key down is what the user actually presses; a key that
    only acts on release or on a dial turn still has one, so fall back through them."""
    settings = key.get("settings") or {}
    for field in ("down", "up", "clockwise", "anticlockwise"):
        url = (settings.get(field) or "").strip()
        if url:
            return url
    return None


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
    """Gives every Open URL key the icon of the site it opens.

    This replaces the image on every such key, including one you picked by hand: OpenDeck
    rasterises every key to `0.png` on restart, so an icon chosen in the editor and the
    plugin's default globe are indistinguishable by then. The profile is backed up first."""
    total = 0
    for path in profile_paths(profiles_root, device, arguments.profile):
        profile = json.loads(path.read_text())
        changed = False

        for position, key in enumerate(profile.get("keys") or []):
            if not key or (key.get("action") or {}).get("uuid") != OPEN_URL_ACTION:
                continue
            url = key_url(key)
            if not url:
                print(f"  {path.stem} key {position}: no URL set, skipped")
                continue
            print(f"  {path.stem} key {position}: {url}")
            try:
                image = favicon(url)
            except (LookupError, OSError) as error:
                print(f"    no icon: {error}")
                continue
            apply_image(key, image, arguments.background, arguments.state)
            changed = True
            total += 1

        if changed:
            save(path, profile)
            print(f"  wrote {path.name}")

    print(f"\n{total} key(s) given their site's icon.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="device id (default: the only one)")
    parser.add_argument("--profile", help="profile name (default: all of them, with --auto)")
    parser.add_argument("--key", type=int, help="grid position of a single key to set")
    parser.add_argument("--state", type=int, default=None, help="state to set (default: all)")
    parser.add_argument("--auto", action="store_true", help="give every Open URL key the icon of the site it opens")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--image", help="path to an image file")
    source.add_argument("--favicon", help="URL or domain whose icon to use")
    parser.add_argument("--background", default="#000000", help="fill behind a transparent icon")
    arguments = parser.parse_args()

    if not arguments.auto and (arguments.key is None or not (arguments.image or arguments.favicon)):
        parser.error("give --auto, or --key with one of --image / --favicon")

    if subprocess.run(["pgrep", "-x", "opendeck"], capture_output=True).returncode == 0:
        sys.exit("OpenDeck is running; stop it first or it will overwrite this on exit.")

    profiles_root = CONFIG / "profiles"
    devices = sorted(path.name for path in profiles_root.iterdir() if path.is_dir())
    device = arguments.device or (devices[0] if len(devices) == 1 else None)
    if device is None:
        sys.exit(f"Pick one with --device: {', '.join(devices) or 'no devices found'}")

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
