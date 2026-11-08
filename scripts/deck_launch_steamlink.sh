#!/bin/sh
# Steam Deck: Steam Link with the gamepad bridge (scripts/deck_pad_bridge.py), as one shortcut, so the bridge sees the gamepad Steam makes for it.
#
# In the Deck's desktop mode: copy this script and deck_pad_bridge.py to one folder (say ~/nxnative), make this one executable (chmod +x), then in Steam
# "Add a Non-Steam Game" > Browse > this script. In the shortcut's properties set the launch options to your Mac's name, e.g. macmini.local (or set NX_MAC_HOST below),
# and under Controller choose the "Gamepad" template, so Steam makes the gamepad for the shortcut. In Game Mode start the shortcut and stream the Mac as usual.
# On the Mac the game runs with DM_PAD_UDP=9777.
#
# Steam passes the shortcut's launch options as this script's arguments: the first is the Mac, anything after is given to the bridge (--print, --nintendo-layout, --grab, ...).
HERE=$(cd "$(dirname "$0")" && pwd)
MAC=${1:-${NX_MAC_HOST:-}}
if [ -z "$MAC" ]; then
    echo "deck_launch_steamlink.sh: the Mac's name or address is missing (launch options, or NX_MAC_HOST)" >&2
    exit 2
fi
[ $# -gt 0 ] && shift
LOG="${HOME}/deck_pad_bridge.log"   # Game Mode shows no terminal: what the bridge says (the gamepad it found, errors) is read here from desktop mode
python3 "$HERE/deck_pad_bridge.py" "$MAC" "$@" >"$LOG" 2>&1 &
BRIDGE=$!
trap 'kill "$BRIDGE" 2>/dev/null' EXIT INT TERM
flatpak run com.valvesoftware.SteamLink
