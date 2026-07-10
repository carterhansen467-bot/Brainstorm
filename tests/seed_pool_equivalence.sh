#!/bin/sh
# Verify the generalized external pool engine against the established native
# Model-3 implementation over their shared A1-A8 semantics.
#
# Usage:
#   tests/seed_pool_equivalence.sh [native-snapshot.cfg] [seed-count]
#
# native_search.cfg is produced by Brainstorm whenever its native search runs.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-1000000}"
OUT="${TMPDIR:-/tmp}/brainstorm_seed_pool_equivalence"
mkdir -p "$OUT"

sh native/build.sh

# The snapshot on disk may predate the current config protocol; the catalog
# data (pools/checks) is version-independent, so rewrite only the handshake.
sed 's/^modelver [0-9][0-9]*$/modelver 4/' "$SNAPSHOT" > "$OUT/snapshot.cfg"
SNAPSHOT="$OUT/snapshot.cfg"
clang -O3 -Wall -Wno-unused-function -ffp-contract=off -pthread \
	-o "$OUT/seed_pool_compat" tests/seed_pool_compat.c -lm

{
	echo "poolver 1"
	echo "threads 1"
	echo "start 0"
	echo "count $COUNT"
	echo "checkpoint $COUNT"
	echo "chunk 16384"
	echo "resume 0"
	echo "format count"
	echo "tag_route collect"
	echo "tag tag_rare 1 8 1"
	echo "legendary j_perkeo 1 8"
	echo "end"
} > "$OUT/criteria.cfg"

"$OUT/seed_pool_compat" "$SNAPSHOT" "$OUT/criteria.cfg" "$COUNT"
