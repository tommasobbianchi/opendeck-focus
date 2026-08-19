#!/usr/bin/env bash
# Installs the daemon, the shell extension and the user service. Run it on the machine with
# the deck; nothing here needs root.
set -euo pipefail
cd "$(dirname "$0")"

cargo build --release
install -Dm755 target/release/opendeck-focus ~/.local/bin/opendeck-focus
install -Dm644 opendeck-focus.service ~/.config/systemd/user/opendeck-focus.service

EXTENSIONS=~/.local/share/gnome-shell/extensions
mkdir -p "$EXTENSIONS"
cp -r gnome-shell-extension/opendeck-focus@nativedev "$EXTENSIONS/"

# The shell only scans for new extensions at session start, so enabling it through
# gnome-extensions fails until then; writing the uuid into the list makes it load itself at the
# next login, no forced logout.
CURRENT=$(gsettings get org.gnome.shell enabled-extensions)
if [[ "$CURRENT" != *"opendeck-focus@nativedev"* ]]; then
    gsettings set org.gnome.shell enabled-extensions "${CURRENT%]*}, 'opendeck-focus@nativedev']"
    echo "Extension enabled; it loads at your next login."
fi

systemctl --user daemon-reload
systemctl --user enable --now opendeck-focus.service
systemctl --user --no-pager status opendeck-focus.service | head -5

echo
echo "Next: stop OpenDeck, run ./setup-n1.py, start OpenDeck."
