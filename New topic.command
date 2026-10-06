#!/bin/sh
# macOS double-click launcher: runs ./new-topic from this folder, then waits for a key so the window stays open.
ONTO_SETUP_CWD=${ONTO_SETUP_CWD:-$PWD}
export ONTO_SETUP_CWD
# opened through a symlink (say one on the Desktop), the kit is next to the file the link points at
self=$0
while [ -L "$self" ]; do
  link=$(readlink -- "$self")
  case $link in
    /*) self=$link ;;
    *) self=$(dirname -- "$self")/$link ;;
  esac
done
cd "$(dirname -- "$self")" || exit 1
code=0
./new-topic "$@" || code=$?
printf '\nPress Return to close this window. '
read -r _ || true
exit "$code"  # not "status": zsh reserves that name
