#!/bin/sh
# Zylo installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/Darkvus/zylo/main/install.sh | sh
#
# Downloads the standalone `zylo` binary of a release, checks its SHA-256 and installs it.
# Environment: ZYLO_VERSION (default: latest, e.g. v0.5.0), ZYLO_INSTALL_DIR (default: ~/.local/bin),
#              ZYLO_BASE_URL and ZYLO_TARGET (advanced / testing).
# Uninstall:   sh install.sh --uninstall
set -eu

REPO="Darkvus/zylo"
BASE_URL="${ZYLO_BASE_URL:-https://github.com/$REPO/releases}"
VERSION="${ZYLO_VERSION:-latest}"
INSTALL_DIR="${ZYLO_INSTALL_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$INSTALL_DIR/zylo"
  say "removed $INSTALL_DIR/zylo (your projects and .zylo-env folders are untouched)"
  exit 0
fi

target="${ZYLO_TARGET:-}"
if [ -z "$target" ]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64 | Linux-amd64) target="linux-x86_64" ;;
    Darwin-arm64) target="macos-arm64" ;;
    *) fail "no prebuilt binary for $(uname -s) $(uname -m). Install from source instead:
       pip install \"zylo @ git+https://github.com/$REPO.git\"" ;;
  esac
fi

if [ "$VERSION" = "latest" ]; then
  url="$BASE_URL/latest/download"
else
  url="$BASE_URL/download/$VERSION"
fi

if command -v curl >/dev/null 2>&1; then
  download() { curl -fsSL "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
  download() { wget -q "$1" -O "$2"; }
else
  fail "curl or wget is required"
fi

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d ' ' -f 1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
else
  fail "sha256sum or shasum is required to verify the download"
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT INT TERM

say "Downloading zylo-$target ($VERSION)..."
download "$url/zylo-$target" "$tmp/zylo" || fail "could not download $url/zylo-$target (is the release public, and does it exist?)"
download "$url/SHA256SUMS" "$tmp/SHA256SUMS" || fail "could not download $url/SHA256SUMS"

expected="$(grep " zylo-$target\$" "$tmp/SHA256SUMS" | head -n 1 | cut -d ' ' -f 1)"
[ -n "$expected" ] || fail "zylo-$target is not listed in SHA256SUMS"
actual="$(sha256 "$tmp/zylo")"
[ "$expected" = "$actual" ] || fail "checksum mismatch for zylo-$target (expected $expected, got $actual); nothing was installed"

chmod +x "$tmp/zylo"
mkdir -p "$INSTALL_DIR"
mv "$tmp/zylo" "$INSTALL_DIR/zylo"
if ! installed_version="$("$INSTALL_DIR/zylo" --version 2>&1)"; then
  rm -f "$INSTALL_DIR/zylo"
  fail "the downloaded binary does not run on this system ($installed_version); it was removed"
fi
say "Installed $installed_version to $INSTALL_DIR/zylo"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) say ""; say "Add it to your PATH:  export PATH=\"$INSTALL_DIR:\$PATH\"  (put that line in ~/.profile or ~/.zshrc)" ;;
esac

if ! command -v rustc >/dev/null 2>&1; then
  say ""
  say "Zylo compiles through Rust, and 'rustc' was not found. Install it with:"
  say "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
fi
