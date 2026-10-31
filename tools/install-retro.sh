#!/bin/sh
# One-time setup for the retro game window: 80s fonts + a terminal that can use them.
# Fonts (SIL Open Font License) go to ~/.local/share/fonts/cka-megatrainer (user only).
# On Fedora Silverblue the terminal (foot) is installed in a 'cka-dev' toolbox,
# leaving the host system untouched.
set -e
BASE="https://github.com/google/fonts/raw/main/ofl"
if [ "$(uname -s)" = Darwin ]; then
  FONT_DIR="$HOME/Library/Fonts"  # macOS picks fonts up there, no cache to rebuild
  LICENSE_DIR="$HOME/Library/Application Support/cka-megatrainer"
else
  FONT_DIR="$HOME/.local/share/fonts/cka-megatrainer"
  LICENSE_DIR="$FONT_DIR"
fi

mkdir -p "$FONT_DIR" "$LICENSE_DIR"
for f in vt323/VT323-Regular.ttf vt323/OFL.txt pressstart2p/PressStart2P-Regular.ttf pressstart2p/OFL.txt; do
  case "$f" in
    */OFL.txt) dest="$LICENSE_DIR/OFL-${f%%/*}.txt" ;;  # one licence file per font
    *)         dest="$FONT_DIR/$(basename "$f")" ;;
  esac
  if [ ! -s "$dest" ]; then
    echo "Downloading $f"
    curl -fsSL -o "$dest" "$BASE/$f"
  fi
done

if [ "$(uname -s)" = Darwin ]; then
  echo "Fonts: VT323 and Press Start 2P installed in $FONT_DIR"
  for app in kitty Alacritty; do  # the only macOS terminals that take a font per window
    bin=$(echo "$app" | tr '[:upper:]' '[:lower:]')
    if command -v "$bin" >/dev/null || [ -x "/Applications/$app.app/Contents/MacOS/$bin" ]; then
      echo "Terminal: $bin"
      exit 0
    fi
  done
  echo "For the retro game window, install kitty or alacritty:"
  echo "  brew install --cask kitty"
  echo "Or pick 'VT323' in your Terminal / iTerm2 profile and play with: make start-exam-here"
  exit 0
fi

fc-cache -f "$FONT_DIR"
echo "Fonts: $(fc-list : family | grep -E '^(VT323|Press Start 2P)$' | sort | tr '\n' ' ')"

for t in foot kitty alacritty xterm; do
  if command -v "$t" >/dev/null; then echo "Terminal: $t (host)"; exit 0; fi
done

if [ -f /run/ostree-booted ] && command -v toolbox >/dev/null; then
  toolbox list -c | grep -qw cka-dev || toolbox create -y cka-dev
  toolbox run -c cka-dev sh -c 'command -v foot >/dev/null && command -v pw-play >/dev/null' \
    || toolbox run -c cka-dev sudo dnf install -y foot pipewire-utils python3
  echo "Terminal: foot (toolbox cka-dev)"
  exit 0
fi

echo "No supported terminal found. Install one of them, e.g.:"
echo "  sudo dnf install foot        # Fedora"
echo "  sudo apt install foot        # Debian/Ubuntu"
exit 1
