#!/bin/zsh
set -eu
EDITOR_DIR="${0:A:h}"
exec "$EDITOR_DIR/sovereign" ui --project "$EDITOR_DIR/projects/cherrygrove"
