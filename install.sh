#!/bin/sh
# baton installer.
#
#   curl -fsSL https://ps1ke.github.io/Baton/install.sh | sh
#
# Installs baton for this user only: the code in ~/.local/lib/baton, a private Python environment there, and a
# `baton` command in ~/.local/bin. Needs Python 3.11+ and git. Run it again to update.
# Settings: BATON_REF (branch or tag, default main), BATON_PREFIX (default ~/.local).
set -eu

REPO="ps1ke/Baton"
REF="${BATON_REF:-main}"
PREFIX="${BATON_PREFIX:-$HOME/.local}"
LIB="$PREFIX/lib/baton"
BIN="$PREFIX/bin"

say() { printf '%s\n' "$*"; }
die() { printf '\nbaton install: %s\n' "$*" >&2; exit 1; }

say "Installing baton ($REF) for $(id -un 2>/dev/null || echo you)…"

command -v python3 >/dev/null 2>&1 || die "Python 3.11 or newer is needed and python3 was not found. Install it, then run this again."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python 3.11 or newer is needed; this machine has $(python3 -V 2>&1). Install a newer Python, then run this again."
command -v git >/dev/null 2>&1 || die "git is needed (projects start from a repository). Install git, then run this again."

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q "$1" -O "$2"; }
else
  die "curl or wget is needed to download baton."
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM
# The exact commit, so baton can tell later whether GitHub has something newer (it updates itself from there).
COMMIT=""
if command -v curl >/dev/null 2>&1; then
  COMMIT="$(curl -fsSL -H 'Accept: application/vnd.github.sha' "https://api.github.com/repos/$REPO/commits/$REF" 2>/dev/null || true)"
fi
case "$COMMIT" in *[!0-9a-f]*|"") COMMIT="" ;; esac
fetch "https://codeload.github.com/$REPO/tar.gz/${COMMIT:-$REF}" "$TMP/baton.tar.gz" \
  || die "could not download $REPO at $REF. Check your connection, or the BATON_REF you set."
tar -xzf "$TMP/baton.tar.gz" -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
[ -f "$SRC/baton/cli.py" ] || die "the download does not look like baton."

mkdir -p "$LIB" "$BIN"
rm -rf "$LIB/app.new"
cp -R "$SRC" "$LIB/app.new"
[ -n "$COMMIT" ] && printf '%s\n' "$COMMIT" > "$LIB/app.new/.baton-commit"
rm -rf "$LIB/app"
mv "$LIB/app.new" "$LIB/app"

# A private environment, so the faster signature library can be added without touching the system's Python.
PY="python3"
if [ -x "$LIB/venv/bin/python" ] || python3 -m venv "$LIB/venv" >/dev/null 2>&1; then
  PY="$LIB/venv/bin/python"
  if "$PY" -m pip install -q --disable-pip-version-check --upgrade cryptography >/dev/null 2>&1; then
    FAST="on"
  else
    FAST="off (baton uses its built-in signatures; everything still works)"
  fi
else
  FAST="off (python3 -m venv is unavailable; baton uses its built-in signatures)"
fi

cat > "$BIN/baton" <<EOF
#!/bin/sh
# baton launcher, written by the installer. Run the installer again to update.
PYTHONPATH="$LIB/app\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PY" -m baton "\$@"
EOF
chmod +x "$BIN/baton"

VERSION="$("$BIN/baton" --version 2>/dev/null || echo "?")"
say ""
say "  baton $VERSION installed"
say "  command:          $BIN/baton"
say "  fast signatures:  $FAST"
say ""
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) say "  $BIN is not on your PATH yet. Add this line to your shell profile (~/.bashrc, ~/.zshrc):"
     say "    export PATH=\"$BIN:\$PATH\""
     say "" ;;
esac
say "Next: run  baton  and it opens in your browser, where you set up this machine and start or join a project."
say "baton keeps itself up to date from github.com/$REPO (turn it off on the This machine page, or BATON_NO_UPDATE=1)."
