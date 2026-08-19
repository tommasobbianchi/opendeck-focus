#!/usr/bin/env python3
"""Sets the image on a deck key, from a file or from a site's favicon.

OpenDeck can do this from its editor -- click a key, then click the image preview -- and that
is the right way to set one or two. This exists for the cases the editor is tedious for:
setting a whole page of Open URL keys at once, or scripting a profile from a list of sites.

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
from pathlib import Path

from PIL import Image

CONFIG = Path.home() / ".config" / "opendeck"
KEY_SIZE = (96, 96)


# Both favicon sources refuse the default urllib agent, and the site fallback below is a
# plain browser request, so send a browser's.
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def favicon(url):
    """Fetches a site's icon, preferring Google's favicon service because it already handles
    the <link rel=icon> / apple-touch-icon / -favicon.ico guessing game, and falling back to
    the site's own /favicon.ico when the service has nothing indexed."""
    domain = urllib.parse.urlparse(url if "//" in url else f"https://{url}").netloc or url
    sources = [
        f"https://www.google.com/s2/favicons?domain={urllib.parse.quote(domain)}&sz=128",
        f"https://{domain}/favicon.ico",
        f"https://{domain.removeprefix('cad.').removeprefix('www.')}/favicon.ico",
    ]
    errors = []
    for source in sources:
        try:
            raw = fetch(source)
        except Exception as error:  # noqa: BLE001 -- any failure just means try the next source
            errors.append(f"  {source}: {error}")
            continue
        # A 200 carrying an HTML error page is the usual failure here, not an HTTP error.
        if raw[:1] not in (b"<",):
            return raw
        errors.append(f"  {source}: served HTML, not an image")
    raise SystemExit("Could not find an icon for " + domain + ":\n" + "\n".join(errors))


def to_data_uri(raw, size, background):
    image = Image.open(io.BytesIO(raw))
    # .ico files hold several resolutions; pick the largest before scaling down.
    if getattr(image, "n_frames", 1) > 1 and image.format == "ICO":
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    image = image.convert("RGBA")
    image.thumbnail(size, Image.LANCZOS)

    # Centre it on a key-sized canvas so small favicons are not stretched into mush.
    canvas = Image.new("RGBA", size, background)
    canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2), image)

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", help="device id (default: the only one)")
    parser.add_argument("--profile", default="Default")
    parser.add_argument("--key", type=int, required=True, help="grid position")
    parser.add_argument("--state", type=int, default=None, help="state to set (default: all)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="path to an image file")
    source.add_argument("--favicon", help="URL or domain whose icon to use")
    parser.add_argument("--background", default="#000000", help="fill behind a transparent icon")
    arguments = parser.parse_args()

    if subprocess.run(["pgrep", "-x", "opendeck"], capture_output=True).returncode == 0:
        sys.exit("OpenDeck is running; stop it first or it will overwrite this on exit.")

    profiles_root = CONFIG / "profiles"
    devices = sorted(path.name for path in profiles_root.iterdir() if path.is_dir())
    device = arguments.device or (devices[0] if len(devices) == 1 else None)
    if device is None:
        sys.exit(f"Pick one with --device: {', '.join(devices) or 'no devices found'}")

    path = profiles_root / device / f"{arguments.profile}.json"
    if not path.is_file():
        sys.exit(f"No such profile: {path}")

    raw = Path(arguments.image).read_bytes() if arguments.image else favicon(arguments.favicon)
    data_uri = to_data_uri(raw, KEY_SIZE, arguments.background)

    profile = json.loads(path.read_text())
    key = profile.get("keys", [])[arguments.key] if arguments.key < len(profile.get("keys", [])) else None
    if key is None:
        sys.exit(f"Key {arguments.key} of {arguments.profile} is empty; put an action on it first.")

    states = range(len(key["states"])) if arguments.state is None else [arguments.state]
    for index in states:
        key["states"][index]["image"] = data_uri
        # A key with an image does not also need the action's name burnt into it.
        key["states"][index]["text"] = key["states"][index].get("text", "")

    shutil.copyfile(path, str(path) + ".bak")
    path.write_text(json.dumps(profile, indent=1))
    print(f"{arguments.profile} key {arguments.key}: image set ({len(data_uri) // 1024} KiB), state(s) {list(states)}")


if __name__ == "__main__":
    main()
