#!/bin/sh
# Command-line tools the CKA questions use besides kubectl, pinned and checksum-verified, into
# iaac/.bin (never system-wide). Today: helm (CLU-015..018). Linux amd64 only; without it the Helm
# tests skip. (From CKS Megatrainer's tools.sh.)
#
#   iaac/tools.sh          fetch what is missing
#   iaac/tools.sh list     what is installed
cd "$(dirname "$0")" || exit 1
BIN=$PWD/.bin
mkdir -p "$BIN"

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}

# name version sha256 url member-in-archive ('-' = the download is the binary itself)
TOOLS='
helm v4.3.1 0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz linux-amd64/helm
'

if [ "${1:-}" = list ]; then
  ls -1 "$BIN"
  exit 0
fi
[ "$(uname -s)-$(uname -m)" = Linux-x86_64 ] || { echo "tools.sh: only Linux x86_64 is pinned; tests needing these tools will skip" >&2; exit 0; }

echo "$TOOLS" | while read -r name version sum url member; do
  [ -n "$name" ] || continue
  [ -x "$BIN/$name-$version" ] && { ln -sf "$name-$version" "$BIN/$name"; continue; }
  tmp=$(mktemp -d) || exit 1
  echo "fetching $name $version"
  curl -fsSL -o "$tmp/dl" "$url" || { echo "cannot download $url" >&2; rm -rf "$tmp"; exit 1; }
  [ "$(sha256 "$tmp/dl")" = "$sum" ] || { echo "$name: checksum mismatch" >&2; rm -rf "$tmp"; exit 1; }
  if [ "$member" = - ]; then
    mv "$tmp/dl" "$BIN/$name-$version"
  else
    tar -xzf "$tmp/dl" -C "$tmp" || { rm -rf "$tmp"; exit 1; }
    mv "$tmp/$member" "$BIN/$name-$version"
  fi
  chmod +x "$BIN/$name-$version" && ln -sf "$name-$version" "$BIN/$name"
  rm -rf "$tmp"
done
