#!/bin/sh
# Launch CKA Megatrainer in its own window with an 80s font.
# Falls back to the current terminal when no graphical session or no supported
# terminal is available (or when CKA_INLINE=1).
#
#   RETRO_FONT  "VT323" (default, 80s DEC terminal) or "Press Start 2P" (NES arcade)
#   RETRO_SIZE  font size in points (default: 15 for VT323, 10 for Press Start 2P)
#   RETRO_TERM  force one of: foot kitty alacritty xterm
# All other arguments are passed to cka_trainer.py.
cd "$(dirname "$0")/.." || exit 1

FONT=${RETRO_FONT:-VT323}
case "$FONT" in
  "Press Start 2P") SIZE=${RETRO_SIZE:-10} ;;
  *)                SIZE=${RETRO_SIZE:-15} ;;
esac
COLS=100 ROWS=38
TITLE="CKA MEGATRAINER"
GAME="python3 cka_trainer.py"

inline() { exec $GAME "$@"; }

[ "${CKA_INLINE:-0}" = 1 ] && inline "$@"
[ -z "$WAYLAND_DISPLAY$DISPLAY" ] && inline "$@"

if ! fc-list : family | grep -qx "$FONT"; then
  echo "Font '$FONT' is not installed - run 'make install-retro'. Starting in this terminal." >&2
  sleep 2
  inline "$@"
fi

# A terminal on the host, else the one in the 'cka-dev' toolbox (Fedora Silverblue).
RUN=""
pick() {
  for t in ${RETRO_TERM:-foot kitty alacritty xterm}; do
    if $RUN sh -c "command -v $t" >/dev/null 2>&1; then echo "$t"; return; fi
  done
}
TERMINAL=$(pick)
if [ -z "$TERMINAL" ] && command -v toolbox >/dev/null && toolbox list -c 2>/dev/null | grep -qw cka-dev; then
  RUN="toolbox run -c cka-dev"
  TERMINAL=$(pick)
fi
[ -z "$TERMINAL" ] && { echo "No retro-capable terminal found - run 'make install-retro'." >&2; inline "$@"; }

case "$TERMINAL" in
  foot)      exec $RUN foot --title "$TITLE" --app-id cka-megatrainer \
               -o "font=$FONT:size=$SIZE" -o pad=12x12 -W "${COLS}x${ROWS}" $GAME "$@" ;;
  kitty)     exec $RUN kitty --title "$TITLE" -o "font_family=$FONT" -o "font_size=$SIZE" \
               -o initial_window_width=${COLS}c -o initial_window_height=${ROWS}c $GAME "$@" ;;
  alacritty) exec $RUN alacritty --title "$TITLE" -o "font.normal.family=\"$FONT\"" \
               -o "font.size=$SIZE" -o "window.dimensions.columns=$COLS" \
               -o "window.dimensions.lines=$ROWS" -e $GAME "$@" ;;
  xterm)     exec $RUN xterm -T "$TITLE" -fa "$FONT" -fs "$SIZE" -geometry "${COLS}x${ROWS}" \
               -bg black -fg white -e $GAME "$@" ;;
esac
