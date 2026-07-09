#!/bin/sh
# Prove the native searcher accepts/rejects EXACTLY the seeds the Lua filter
# suite does, across every filter case, using the Lua implementation as oracle.
#   LUAJIT=/path/to/luajit tests/native_equivalence.sh   [NSEEDS=30000]
set -e
cd "$(dirname "$0")/.."
LUAJIT="${LUAJIT:-luajit}"
OUT="${TMPDIR:-/tmp}/brainstorm_native_fixtures"
rm -rf "$OUT"
mkdir -p "$OUT"
sh native/build.sh
"$LUAJIT" tests/dump_native_fixtures.lua Brainstorm_reroll.lua "$OUT" "${NSEEDS:-30000}"
fail=0
for cfg in "$OUT"/case*.cfg; do
	base="${cfg%.cfg}"
	./native/brainstorm_native_search verifychecks "$cfg" || { echo "FAIL verifychecks $(basename "$base")"; fail=1; continue; }
	./native/brainstorm_native_search fixture "$cfg" "$base.seeds" > "$base.got"
	if diff -q "$base.expected" "$base.got" >/dev/null 2>&1; then
		echo "PASS $(basename "$base")"
	else
		echo "FAIL $(basename "$base") -- first divergences:"
		diff "$base.expected" "$base.got" | head -8
		fail=1
	fi
done
if [ "$fail" = 0 ]; then echo "NATIVE EQUIVALENCE: ALL PASS"; else echo "NATIVE EQUIVALENCE: FAILURES"; fi
exit $fail
