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
# Windows CI runs this same harness under Git Bash against the .exe builds.
case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh ;;
	*) EXE=; BUILD=native/build.sh ;;
esac
sh "$BUILD"
"$LUAJIT" tests/dump_native_fixtures.lua Brainstorm_reroll.lua "$OUT" "${NSEEDS:-30000}"
fail=0
for cfg in "$OUT"/case*.cfg; do
	base="${cfg%.cfg}"
	"./native/brainstorm_native_search$EXE" verifychecks "$cfg" || { echo "FAIL verifychecks $(basename "$base")"; fail=1; continue; }
	"./native/brainstorm_native_search$EXE" fixture "$cfg" "$base.seeds" > "$base.got"
	if diff -q "$base.expected" "$base.got" >/dev/null 2>&1; then
		echo "PASS $(basename "$base")"
	else
		echo "FAIL $(basename "$base") -- first divergences:"
		diff "$base.expected" "$base.got" | head -8
		fail=1
	fi
done

# Exact equality alone could let both implementations accidentally omit tag
# rewards. Require the generated corpus to contain accepted first-Soul hits
# sourced from each Model-6 reward type.
if grep -Eq 'LegA[0-9]+Charm(Sm|Big)' "$OUT"/case*.expected; then
	echo "PASS collected Charm reward produced a legendary hit"
else
	echo "FAIL no collected Charm reward legendary hit in fixtures"; fail=1
fi
if grep -Eq 'LegA[0-9]+Ethereal(Sm|Big)' "$OUT"/case*.expected; then
	echo "PASS collected Ethereal reward produced a legendary hit"
else
	echo "FAIL no collected Ethereal reward legendary hit in fixtures"; fail=1
fi

# Malformed/truncated snapshots are external files and must fail cleanly,
# never dereference a missing token or degrade to an accept-all search.
awk 'BEGIN { changed=0 }
	/^boostdef / && !changed { sub(/ [01]$/, ""); changed=1 }
	{ print }' "$OUT/case1.cfg" > "$OUT/malformed.cfg"
if "./native/brainstorm_native_search$EXE" verifychecks "$OUT/malformed.cfg" >/dev/null 2>&1; then
	echo "FAIL malformed config was accepted"; fail=1
else
	echo "PASS malformed config rejected cleanly"
fi
awk '{ if ($1 == "taganywhere") $2 = "not-a-number"; print }' \
	"$OUT/case1.cfg" > "$OUT/malformed-number.cfg"
if "./native/brainstorm_native_search$EXE" verifychecks "$OUT/malformed-number.cfg" >/dev/null 2>&1; then
	echo "FAIL malformed numeric value was accepted"; fail=1
else
	echo "PASS malformed numeric value rejected cleanly"
fi
sed 's/^modelver 6$/modelver 5/' "$OUT/case1.cfg" > "$OUT/stale-model.cfg"
if "./native/brainstorm_native_search$EXE" verifychecks "$OUT/stale-model.cfg" >/dev/null 2>&1; then
	echo "FAIL stale Model-5 config was accepted"; fail=1
else
	echo "PASS stale Model-5 config rejected cleanly"
fi
if [ "$fail" = 0 ]; then echo "NATIVE EQUIVALENCE: ALL PASS"; else echo "NATIVE EQUIVALENCE: FAILURES"; fi
exit $fail
