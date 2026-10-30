#!/bin/sh
# Text screenshot of the running app (needs tmux).
# usage: tools/screenshot.sh [COLS] [ROWS] [KEYS] [DELAY] [-- app args]
#   tools/screenshot.sh 100 38                          # title screen
#   tools/screenshot.sh 100 30 "Down Down Enter" 3      # first question
# KEYS use tmux send-keys names. Set CAPFLAGS=-e to keep the colour codes.
# On Fedora Silverblue run it in the toolbox: toolbox run -c cka-dev tools/screenshot.sh
W=${1:-100} H=${2:-38} KEYS=${3:-} DELAY=${4:-0.3}
[ $# -ge 4 ] && shift 4 || shift $#
[ "$1" = "--" ] && shift
cd "$(dirname "$0")/.." || exit 1
command -v tmux >/dev/null || { echo "tmux not found" >&2; exit 1; }
S=cka-shot-$$
tmux -L "$S" new-session -d -x "$W" -y "$H" "LANG=C.UTF-8 python3 cka_trainer.py --silent $*"
sleep 1.2
[ -n "$KEYS" ] && tmux -L "$S" send-keys $KEYS
sleep "$DELAY"
tmux -L "$S" capture-pane -p ${CAPFLAGS:-}
tmux -L "$S" kill-server
