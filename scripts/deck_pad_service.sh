#!/bin/sh
# Steam Deck: the gamepad bridge (deck_pad_bridge.py) as a service of the deck user, so the Deck's own controller plays the Mac's game whenever the Deck streams it, by
# Steam's Remote Play from the library or by the Steam Link app, with no shortcut to start. It reads the controller beside Steam (hidraw) and sends only while a stream
# runs (--when streaming), so the Mac's own players keep their slots the rest of the time. On the Mac the game runs with DM_PAD_UDP=9777 (NX Play and the Mac app's launcher:
# "Network controller").
#
# In the Deck's desktop mode, from the folder that holds this script and deck_pad_bridge.py:
#   sh deck_pad_service.sh install <the Mac's name or address> [bridge options...]   e.g. macmini.local, or 192.168.1.20; options such as --nintendo-layout
#   sh deck_pad_service.sh status                                                     running or not, and what the bridge said last
#   sh deck_pad_service.sh uninstall
# The service starts with the deck user's session, in Game Mode as on the desktop, and survives reboots. Installing again replaces the Mac and the options.
set -eu

NAME=nxnative-deck-pad
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/$NAME"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$NAME.service"

die() { echo "deck_pad_service.sh: $*" >&2; exit 2; }

quote() {   # one argument as systemd's ExecStart takes it: in double quotes, with \ and " escaped
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

case "${1:-}" in
install)
    [ $# -ge 2 ] || die "usage: sh deck_pad_service.sh install <the Mac's name or address> [bridge options...]"
    shift
    [ -f "$HERE/deck_pad_bridge.py" ] || die "deck_pad_bridge.py is not beside this script ($HERE)"
    command -v python3 >/dev/null || die "python3 is missing"
    command -v systemctl >/dev/null || die "systemctl is missing: this needs systemd (SteamOS has it)"
    mkdir -p "$DEST" "$UNIT_DIR"
    cp "$HERE/deck_pad_bridge.py" "$DEST/deck_pad_bridge.py"
    args=""
    for a in "$@"; do args="$args $(quote "$a")"; done
    cat >"$UNIT" <<EOF
[Unit]
Description=NXNative: the Steam Deck's controller to the Mac's game while the Deck streams it
After=network-online.target

[Service]
ExecStart=$(command -v python3) -u $(quote "$DEST/deck_pad_bridge.py") --source deck --when streaming$args
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$NAME.service" >/dev/null
    systemctl --user restart "$NAME.service"
    echo "installed: $UNIT"
    if ! python3 "$DEST/deck_pad_bridge.py" --list | grep -q 'Steam Deck controller'; then
        echo "warning: the Deck's controller cannot be read now (no hidraw device of Valve 28DE:1205 readable by $(id -un)); the service waits for it" >&2
    fi
    sleep 1
    journalctl --user -u "$NAME.service" -n 5 --no-pager -o cat 2>/dev/null || true
    ;;
status)
    systemctl --user --no-pager status "$NAME.service" || true
    ;;
uninstall)
    systemctl --user disable --now "$NAME.service" 2>/dev/null || true
    rm -f "$UNIT"
    rm -rf "$DEST"
    systemctl --user daemon-reload
    echo "removed"
    ;;
*)
    die "usage: sh deck_pad_service.sh install <mac> [bridge options...] | status | uninstall"
    ;;
esac
