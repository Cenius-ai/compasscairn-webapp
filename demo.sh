#!/bin/sh
# CompassCairn end-to-end demo: no prompts, no network, no leftover state.
# Runs the whole logging loop against a throwaway store in a temp directory.
set -eu

cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
DEMO_DIR="$(mktemp -d)"
STORE="$DEMO_DIR/compasscairn.json"
COMPASSCAIRN_PASSPHRASE="cairn-demo"
export COMPASSCAIRN_PASSPHRASE
export NO_COLOR=1

cleanup() {
    rm -rf "$DEMO_DIR"
}
trap cleanup EXIT

run() {
    printf '$ compasscairn %s\n' "$*"
    "$PYTHON" compasscairn.py --store "$STORE" "$@"
    printf '\n'
}

printf 'CompassCairn demo — store: %s\n\n' "$STORE"

run init
run declination set Reykjavik -13.5
run declination set "Sable Point" 20.0
run declination set "Cape Fury" -2.5
run declination list
run declination get reykjavik
run waypoint add "Whale Bay" --location Reykjavik --mag 170 --true -170
run waypoint add "Kelp Reef" --location "Sable Point" --mag 170 --true -170
run waypoint add "North Spur" --location "Gull Rock" --mag 10 --true 10
run waypoint add "Harpoon Head" --location "Cape Fury" --mag 96.5 --true 94.0
run waypoint list
run drift list
run drift list --threshold 50
run config set threshold 5
run drift list

printf 'Demo complete — the store was written atomically and removed on exit.\n'
