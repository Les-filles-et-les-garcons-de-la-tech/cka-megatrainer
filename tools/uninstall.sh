#!/bin/sh
# Reset setup-owned components so the next start exercises installation again.
set -e
SCRIPT_DIR=$(dirname "$0")
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
if [ "$(uname -s)" = Darwin ]; then
  FONT_DIR="$HOME/Library/Fonts"
  LICENSE_DIR="$HOME/Library/Application Support/cka-megatrainer"
  STATE_DIR="$LICENSE_DIR/install-state"
else
  FONT_DIR="$HOME/.local/share/fonts/cka-megatrainer"
  LICENSE_DIR="$FONT_DIR"
  STATE_DIR="$HOME/.local/share/cka-megatrainer/install-state"
fi

as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}

if [ -f "$STATE_DIR/kitty" ]; then
  command -v brew >/dev/null || { echo "Homebrew is needed to uninstall setup-installed Kitty." >&2; exit 1; }
  if brew list --cask kitty >/dev/null 2>&1; then
    download=$(brew --cache --cask kitty)
    brew uninstall --cask kitty
    rm -f "$download"
  fi
  rm -rf "$HOME/Library/Caches/kitty"
  rm -f "$STATE_DIR/kitty"
fi

if [ -f "$STATE_DIR/toolbox-created" ]; then
  if toolbox list -c | grep -qw cka-dev; then
    toolbox rm --force cka-dev
  fi
  rm -f "$STATE_DIR/toolbox-created" "$STATE_DIR"/toolbox-package-*
else
  for marker in "$STATE_DIR"/toolbox-package-*; do
    [ -f "$marker" ] || continue
    package=${marker##*/toolbox-package-}
    case "$package" in foot|pipewire-utils|python3) ;; *) exit 1 ;; esac
    toolbox run -c cka-dev sudo dnf remove -y "$package"
    rm -f "$marker"
  done
fi

for marker in "$STATE_DIR"/package-*; do
  [ -f "$marker" ] || continue
  entry=${marker##*/package-}
  case "$entry" in
    apt-get-*) manager=apt-get; package=${entry#apt-get-} ;;
    dnf-*) manager=dnf; package=${entry#dnf-} ;;
    pacman-*) manager=pacman; package=${entry#pacman-} ;;
    zypper-*) manager=zypper; package=${entry#zypper-} ;;
    apk-*) manager=apk; package=${entry#apk-} ;;
    *) echo "Unknown setup record: $entry" >&2; exit 1 ;;
  esac
  case "$package" in curl|fontconfig|foot|xterm) ;; *) exit 1 ;; esac
  case "$manager" in
    apt-get) as_root apt-get remove -y "$package" ;;
    dnf) as_root dnf remove -y "$package" ;;
    pacman) as_root pacman -R --noconfirm "$package" ;;
    zypper) as_root zypper --non-interactive remove "$package" ;;
    apk) as_root apk del "$package" ;;
  esac
  rm -f "$marker"
done

FONTS_CHANGED=0
for file in VT323-Regular.ttf PressStart2P-Regular.ttf OFL-vt323.txt OFL-pressstart2p.txt; do
  [ -f "$STATE_DIR/file-$file" ] || continue
  case "$file" in *.ttf) directory=$FONT_DIR ;; *) directory=$LICENSE_DIR ;; esac
  rm -f "$directory/$file" "$directory/$file.tmp" "$STATE_DIR/file-$file"
  FONTS_CHANGED=1
done
if [ "$FONTS_CHANGED" = 1 ] && [ "$(uname -s)" != Darwin ] && command -v fc-cache >/dev/null; then
  fc-cache -f
fi

# Only remove empty setup directories; unrelated files are left in place.
for directory in "$STATE_DIR" "$LICENSE_DIR"; do
  if [ -d "$directory" ]; then rmdir "$directory" 2>/dev/null || :; fi
done
rm -rf "$HOME/.cache/cka-megatrainer" "$ROOT/dist" "$ROOT/__pycache__"
echo "Uninstalled setup-owned components and removed game caches/builds."
echo "Run make start-exam to test installation again."
