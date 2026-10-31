#!/bin/sh
# Idempotent setup, checked automatically before launching the retro game window.
# Fonts (SIL Open Font License) go to ~/.local/share/fonts/cka-megatrainer (user only).
# On Fedora Silverblue the terminal (foot) is installed in a 'cka-dev' toolbox,
# leaving the host system untouched.
set -e
BASE="https://github.com/google/fonts/raw/main/ofl"
linux_install() {
  SUDO=""
  if [ "$(id -u)" != 0 ]; then
    if ! command -v sudo >/dev/null; then
      echo "Installing $* requires root or sudo. Install them and rerun make start-exam." >&2
      exit 1
    fi
    SUDO=sudo
  fi
  if command -v apt-get >/dev/null; then
    MANAGER=apt-get
    $SUDO apt-get update
    $SUDO apt-get install -y "$@"
  elif command -v dnf >/dev/null; then
    MANAGER=dnf
    $SUDO dnf install -y "$@"
  elif command -v pacman >/dev/null; then
    MANAGER=pacman
    $SUDO pacman -S --needed --noconfirm "$@"
  elif command -v zypper >/dev/null; then
    MANAGER=zypper
    $SUDO zypper --non-interactive install "$@"
  elif command -v apk >/dev/null; then
    MANAGER=apk
    $SUDO apk add "$@"
  else
    echo "No supported package manager found. Install $* and rerun make start-exam." >&2
    exit 1
  fi
  for package in "$@"; do
    touch "$STATE_DIR/package-$MANAGER-$package"
  done
}

if [ "$(uname -s)" = Darwin ]; then
  FONT_DIR="$HOME/Library/Fonts"  # macOS picks fonts up there, no cache to rebuild
  LICENSE_DIR="$HOME/Library/Application Support/cka-megatrainer"
  STATE_DIR="$LICENSE_DIR/install-state"
else
  FONT_DIR="$HOME/.local/share/fonts/cka-megatrainer"
  LICENSE_DIR="$FONT_DIR"
  STATE_DIR="$HOME/.local/share/cka-megatrainer/install-state"
fi
mkdir -p "$STATE_DIR"
if [ "$(uname -s)" != Darwin ]; then
  command -v curl >/dev/null || linux_install curl
  command -v fc-cache >/dev/null && command -v fc-list >/dev/null || linux_install fontconfig
fi

FONTS_CHANGED=0
mkdir -p "$FONT_DIR" "$LICENSE_DIR"
for f in vt323/VT323-Regular.ttf vt323/OFL.txt pressstart2p/PressStart2P-Regular.ttf pressstart2p/OFL.txt; do
  case "$f" in
    */OFL.txt) dest="$LICENSE_DIR/OFL-${f%%/*}.txt" ;;  # one licence file per font
    *)         dest="$FONT_DIR/$(basename "$f")" ;;
  esac
  if [ ! -s "$dest" ]; then
    echo "Downloading $f"
    curl -fsSL -o "$dest.tmp" "$BASE/$f"
    touch "$STATE_DIR/file-$(basename "$dest")"
    mv "$dest.tmp" "$dest"
    FONTS_CHANGED=1
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
  if command -v brew >/dev/null; then
    echo "Installing kitty for the retro game window..."
    brew install --cask kitty
    touch "$STATE_DIR/kitty"
    echo "Terminal: kitty"
    exit 0
  fi
  echo "Homebrew was not found; you can still play in your current terminal."
  echo "For the retro game window, install kitty or alacritty:"
  echo "  Install Homebrew from https://brew.sh first, then run:"
  echo "  brew install --cask kitty"
  echo "Or pick 'VT323' in your Terminal / iTerm2 profile and play with: make start-exam-here"
  exit 0
fi

[ "$FONTS_CHANGED" = 0 ] || fc-cache -f "$FONT_DIR"
echo "Fonts: $(fc-list : family | grep -E '^(VT323|Press Start 2P)$' | sort | tr '\n' ' ')"

for t in foot kitty alacritty xterm; do
  if command -v "$t" >/dev/null; then echo "Terminal: $t (host)"; exit 0; fi
done

if [ -f /run/ostree-booted ] && command -v toolbox >/dev/null; then
  if ! toolbox list -c | grep -qw cka-dev; then
    toolbox create -y cka-dev
    touch "$STATE_DIR/toolbox-created"
  fi
  for pair in foot:foot pw-play:pipewire-utils python3:python3; do
    if ! toolbox run -c cka-dev sh -c "command -v ${pair%%:*}" >/dev/null; then
      toolbox run -c cka-dev sudo dnf install -y "${pair#*:}"
      touch "$STATE_DIR/toolbox-package-${pair#*:}"
    fi
  done
  echo "Terminal: foot (toolbox cka-dev)"
  exit 0
fi

# foot is Wayland-only; xterm also works in X11 sessions.
if [ -n "${WAYLAND_DISPLAY:-}" ]; then TERMINAL=foot; else TERMINAL=xterm; fi
echo "Installing $TERMINAL for the retro game window..."
linux_install "$TERMINAL"
echo "Terminal: $TERMINAL (host)"
