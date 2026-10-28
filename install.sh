#!/usr/bin/env bash
# T3 Lighthouse installer.
#
#   curl -fsSL https://raw.githubusercontent.com/dpreil-ydlp/t3-lighthouse/main/install.sh | bash
#
# Downloads Lighthouse to ~/t3-lighthouse (or $LIGHTHOUSE_HOME), then hands over to the
# `bin/lighthouse` script in that folder, which writes the config, starts the containers, and
# prints the dashboard address and your sign-in token. Run it again at any time: an
# existing install is kept as it is.
#
# Options: run with --help, or see the README.
set -euo pipefail

REPO="${LIGHTHOUSE_REPO:-dpreil-ydlp/t3-lighthouse}"
REF="${LIGHTHOUSE_REF:-main}"
DEST="${LIGHTHOUSE_HOME:-$HOME/t3-lighthouse}"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: curl -fsSL https://raw.githubusercontent.com/dpreil-ydlp/t3-lighthouse/main/install.sh | bash -s -- [options]

  --demo             load the fictional demo projects
  --with-collector   macOS: install the Mac collector without asking
  --no-collector     macOS: skip the Mac collector
  --replace-collector  macOS: replace a collector that another install folder set up
  --collector-only   macOS: install only the collector, for a server on another machine
                     (with --api-url URL and --agent-token-file FILE)
  --build            build the images from source instead of pulling them
  --update           update an existing install
  --uninstall        remove an existing install (add --yes to skip the question)

Environment: LIGHTHOUSE_HOME (install folder, default ~/t3-lighthouse),
LIGHTHOUSE_REF (branch or tag, default main), LIGHTHOUSE_REPO (GitHub owner/repo to
download from, default dpreil-ydlp/t3-lighthouse).
USAGE
}

fetch() {
  local tmp
  if command -v git >/dev/null 2>&1; then
    say "Downloading Lighthouse ($REF) with git into $DEST"
    git clone --quiet --depth 1 --branch "$REF" "https://github.com/$REPO.git" "$DEST" \
      || fail "git clone of https://github.com/$REPO ($REF) failed"
  elif command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
    say "Downloading Lighthouse ($REF) as a tarball into $DEST"
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/lighthouse.XXXXXX")
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" -o "$tmp/src.tar.gz" \
      || { rm -rf "$tmp"; fail "download of $REPO ($REF) failed"; }
    mkdir -p "$DEST"
    tar -xzf "$tmp/src.tar.gz" -C "$DEST" --strip-components 1
    rm -rf "$tmp"
  else
    fail "install git, or curl and tar, and run this again"
  fi
}

main() {
  local action=install arg
  for arg in "$@"; do
    case "$arg" in
      --update) action=update ;;
      --uninstall) action=uninstall ;;
      -h|--help) usage; exit 0 ;;
    esac
  done

  if [ -x "$DEST/bin/lighthouse" ]; then
    case "$action" in
      update) exec "$DEST/bin/lighthouse" update ;;
      uninstall) exec "$DEST/bin/lighthouse" uninstall "$@" ;;
    esac
  elif [ "$action" != install ]; then
    fail "no Lighthouse install found in $DEST"
  elif [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
    fail "$DEST already exists and is not a Lighthouse install. Move it, or set LIGHTHOUSE_HOME to another folder."
  else
    fetch
  fi
  [ -x "$DEST/bin/lighthouse" ] || fail "the download in $DEST is missing bin/lighthouse"
  LIGHTHOUSE_REF="$REF" LIGHTHOUSE_REPO="$REPO" exec "$DEST/bin/lighthouse" install "$@"
}

# Everything runs from here, so a download cut short by the network does nothing.
main "$@"
