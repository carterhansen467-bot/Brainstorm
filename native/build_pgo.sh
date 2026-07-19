#!/bin/sh
# Build both native helpers with separate exact-workload LLVM profiles.
#
# Usage:
#   ./build_pgo.sh train [new-profile-directory]
#   ./build_pgo.sh use /path/to/profile-directory
#
# `train` creates fresh profiles with this compiler and source, then consumes
# them immediately. If a new directory is named, the two profiles are also
# published there atomically with their derived training snapshot for a pinned
# release build. `use` requires that directory contract; stale or incomplete
# profiles are rejected instead of silently degrading. Search and pool profiles
# must never be merged: the two translation units intentionally compile
# different sets of shared functions.
set -eu

CALLER_DIR=$(pwd)
SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
cd "$SCRIPT_DIR"

CC=${CC:-clang}
MODE=${1:-train}
SEARCH_PROFILE_NAME=brainstorm_native_search.profdata
POOL_PROFILE_NAME=brainstorm_seed_pool.profdata
IDENTITY_NAME=brainstorm_pgo.identity
TRAINING_CONFIG_NAME=brainstorm_pgo_training.cfg
SEARCH_TRAINING_FILTER=$SCRIPT_DIR/pgo_search_training.awk
PGO_DRIVER=$SCRIPT_DIR/build_pgo.sh

absolute_from_caller() {
	case "$1" in
		/*) printf '%s\n' "$1" ;;
		*) printf '%s/%s\n' "$CALLER_DIR" "$1" ;;
	esac
}

case "$CC" in
	*/*) CC=$(absolute_from_caller "$CC") ;;
esac

if [ -n "${BRAINSTORM_NATIVE_SNAPSHOT:-}" ]; then
	SNAPSHOT=$(absolute_from_caller "$BRAINSTORM_NATIVE_SNAPSHOT")
else
	SNAPSHOT=$SCRIPT_DIR/../native_search.cfg
fi
if [ -n "${BRAINSTORM_PGO_TRAINING:-}" ]; then
	POOL_TRAINING=$(absolute_from_caller "$BRAINSTORM_PGO_TRAINING")
else
	POOL_TRAINING=$SCRIPT_DIR/pgo_seed_pool.cfg
fi

find_profdata() {
	if [ -n "${LLVM_PROFDATA:-}" ]; then
		case "$LLVM_PROFDATA" in
			*/*) absolute_from_caller "$LLVM_PROFDATA" ;;
			*) command -v "$LLVM_PROFDATA" 2>/dev/null ;;
		esac
		return
	fi
	# Prefer the profiler shipped with the selected compiler.  A PATH lookup
	# can accidentally pair (for example) Homebrew clang with Apple profdata.
	compiler_profdata=$("$CC" -print-prog-name=llvm-profdata 2>/dev/null || true)
	if [ -n "$compiler_profdata" ]; then
		case "$compiler_profdata" in
			*/*) if [ -x "$compiler_profdata" ]; then printf '%s\n' "$compiler_profdata"; return; fi ;;
			*) compiler_profdata_path=$(command -v "$compiler_profdata" 2>/dev/null || true)
				if [ -n "$compiler_profdata_path" ]; then printf '%s\n' "$compiler_profdata_path"; return; fi ;;
		esac
	fi
	if command -v xcrun >/dev/null 2>&1; then
		xcrun_profdata=$(xcrun --find llvm-profdata 2>/dev/null || true)
		if [ -n "$xcrun_profdata" ] && [ -x "$xcrun_profdata" ]; then
			printf '%s\n' "$xcrun_profdata"
			return
		fi
	fi
	if command -v llvm-profdata >/dev/null 2>&1; then
		command -v llvm-profdata
	fi
}

hash_file() {
	file=$1
	if command -v sha256sum >/dev/null 2>&1; then
		hash_line=$(sha256sum "$file") || return 1
		printf '%s\n' "$hash_line" | awk '{ print $1 }'
	elif command -v shasum >/dev/null 2>&1; then
		hash_line=$(shasum -a 256 "$file") || return 1
		printf '%s\n' "$hash_line" | awk '{ print $1 }'
	elif command -v openssl >/dev/null 2>&1; then
		hash_line=$(openssl dgst -sha256 "$file") || return 1
		printf '%s\n' "$hash_line" | awk '{ print $NF }'
	else
		echo "error: SHA-256 tool not found (need sha256sum, shasum, or openssl)" >&2
		return 1
	fi
}

write_identity() {
	output=$1
	training_config=$2
	compiler_version=$PGO_TMP/compiler-version.txt
	"$CC" --version > "$compiler_version"
	compiler_target=$("$CC" -dumpmachine)
	search_source_hash=$(hash_file brainstorm_native_search.c)
	pool_source_hash=$(hash_file brainstorm_seed_pool.c)
	platform_header_hash=$(hash_file platform.h)
	search_training_filter_hash=$(hash_file "$SEARCH_TRAINING_FILTER")
	pool_training_config_hash=$(hash_file "$POOL_TRAINING")
	pgo_driver_hash=$(hash_file "$PGO_DRIVER")
	derived_training_config_hash=$(hash_file "$training_config")
	compiler_version_hash=$(hash_file "$compiler_version")
	{
		printf '%s\n' 'BRAINSTORM_PGO_IDENTITY 1'
		printf 'search_source_sha256 %s\n' "$search_source_hash"
		printf 'pool_source_sha256 %s\n' "$pool_source_hash"
		printf 'platform_header_sha256 %s\n' "$platform_header_hash"
		printf 'search_training_filter_sha256 %s\n' "$search_training_filter_hash"
		printf 'pool_training_config_sha256 %s\n' "$pool_training_config_hash"
		printf 'pgo_driver_sha256 %s\n' "$pgo_driver_hash"
		printf 'derived_training_config_sha256 %s\n' "$derived_training_config_hash"
		printf 'compiler_version_sha256 %s\n' "$compiler_version_hash"
		printf 'compiler_target %s\n' "$compiler_target"
		printf '%s\n' 'search_instrument_flags -O3|-fprofile-instr-generate|-Wall|-ffp-contract=off|-pthread|-lm'
		printf '%s\n' 'pool_instrument_flags -O3|-fprofile-instr-generate|-Wall|-Wno-unused-function|-ffp-contract=off|-pthread|-lm'
		printf '%s\n' 'search_use_flags -O3|-fprofile-instr-use=<profile>|-Werror=profile-instr-out-of-date|-Werror=profile-instr-unprofiled|-Wall|-ffp-contract=off|-pthread|-lm'
		printf '%s\n' 'pool_use_flags -O3|-fprofile-instr-use=<profile>|-Werror=profile-instr-out-of-date|-Werror=profile-instr-unprofiled|-Wall|-Wno-unused-function|-ffp-contract=off|-pthread|-lm'
		printf '%s\n' 'search_profile_merge sparse|full-space=32|restricted-pool=1'
		printf '%s\n' 'pool_profile_merge sparse|exact-count=1'
		printf '%s\n' 'search_training_schema full-space-bench2+restricted-bspool-exhaustion-v2'
		printf '%s\n' 'pool_training_schema exact-count-only-v1'
		printf '%s\n' 'end'
	} > "$output"
}

derive_search_training_config() {
	output=$1
	# native_search.cfg also carries the user's active filters, unlock state, and
	# optional .bspool. Keep its parity/catalog metadata, force the required
	# standard training availability, and canonicalize the active overlay to the
	# measured first-Legendary/Omen workload. bench adds an impossible independent
	# tag after config finalization, so it exercises
	# every configured Legendary/Charm/Omen rejection path without stopping on
	# a hit; threads=1 makes both benchmark passes single-threaded.
	awk -f "$SEARCH_TRAINING_FILTER" "$SNAPSHOT" > "$output"
}

derive_pool_binary_training_config() {
	output=$1
	# The exact candidate workload is identical to pgo_seed_pool.cfg, but this
	# discarded instrumentation pass writes a temporary .bspool so search's
	# distinct restricted-pool worker can train without a pre-existing helper.
	awk '
		$1 == "threads" { print "threads 1"; next }
		$1 == "resume"  { print "resume 0"; next }
		$1 == "format"  { print "format binary"; next }
		{ print }
	' "$POOL_TRAINING" > "$output"
}

derive_restricted_search_config() {
	input=$1
	pool=$2
	output=$3
	# Neutralize the ordinary filters and reject on an impossible voucher. Pool
	# overlay rules run first, so every decoded record replays its embedded exact
	# Legendary/Charm/Omen route before the late voucher rejection exhausts it.
	awk -v pool="$pool" '
		$1 == "legendary"   { print "legendary -"; next }
		$1 == "leganywhere" { print "leganywhere 0"; next }
		$1 == "tag"         { print "tag -"; next }
		$1 == "voucher"     { print "voucher v_bench_impossible"; next }
		$1 == "end" { print "poolfile " pool; print; next }
		{ print }
	' "$input" > "$output"
}

build_search_from_profile() {
	profile=$1
	"$CC" -O3 -fprofile-instr-use="$profile" \
		-Werror=profile-instr-out-of-date -Werror=profile-instr-unprofiled \
		-Wall -ffp-contract=off -pthread \
		-o "$PGO_TMP/brainstorm_native_search" \
		brainstorm_native_search.c -lm
}

build_pool_from_profile() {
	profile=$1
	"$CC" -O3 -fprofile-instr-use="$profile" \
		-Werror=profile-instr-out-of-date -Werror=profile-instr-unprofiled \
		-Wall -Wno-unused-function -ffp-contract=off -pthread \
		-o "$PGO_TMP/brainstorm_seed_pool" brainstorm_seed_pool.c -lm
}

case "$MODE" in
	train)
		if [ "$#" -gt 2 ]; then
			echo "usage: $0 train [new-profile-directory]" >&2
			exit 2
		fi
		PROFDATA=$(find_profdata || true)
		if [ -z "$PROFDATA" ] || [ ! -x "$PROFDATA" ]; then
			echo "error: llvm-profdata was not found; set LLVM_PROFDATA or use native/build.sh" >&2
			exit 1
		fi
		if [ ! -r "$SNAPSHOT" ]; then
			echo "error: native snapshot not found: $SNAPSHOT" >&2
			echo "set BRAINSTORM_NATIVE_SNAPSHOT or generate native_search.cfg in Balatro" >&2
			exit 1
		fi
		if [ ! -r "$POOL_TRAINING" ]; then
			echo "error: PGO training criteria not found: $POOL_TRAINING" >&2
			exit 1
		fi
		if [ ! -r "$SEARCH_TRAINING_FILTER" ]; then
			echo "error: PGO search-training filter not found: $SEARCH_TRAINING_FILTER" >&2
			exit 1
		fi
		PROFILE_OUTPUT=
		if [ "$#" -eq 2 ]; then
			case "$2" in
				*/) echo "error: new profile directory must not end in a slash: $2" >&2; exit 1 ;;
			esac
			PROFILE_OUTPUT=$(absolute_from_caller "$2")
			if [ -e "$PROFILE_OUTPUT" ] || [ -L "$PROFILE_OUTPUT" ]; then
				echo "error: profile output already exists: $PROFILE_OUTPUT" >&2
				exit 1
			fi
		fi
		;;
	use)
		if [ "$#" -ne 2 ]; then
			echo "usage: $0 use /path/to/profile-directory" >&2
			exit 2
		fi
		PROFILE_INPUT=$(absolute_from_caller "$2")
		SEARCH_PROFILE=$PROFILE_INPUT/$SEARCH_PROFILE_NAME
		POOL_PROFILE=$PROFILE_INPUT/$POOL_PROFILE_NAME
		PROFILE_IDENTITY=$PROFILE_INPUT/$IDENTITY_NAME
		PROFILE_TRAINING_CONFIG=$PROFILE_INPUT/$TRAINING_CONFIG_NAME
		if [ ! -d "$PROFILE_INPUT" ] || [ ! -r "$SEARCH_PROFILE" ] \
				|| [ ! -r "$POOL_PROFILE" ] || [ ! -r "$PROFILE_IDENTITY" ] \
				|| [ ! -r "$PROFILE_TRAINING_CONFIG" ]; then
			echo "error: profile directory must contain readable $SEARCH_PROFILE_NAME, $POOL_PROFILE_NAME, $IDENTITY_NAME, and $TRAINING_CONFIG_NAME: $PROFILE_INPUT" >&2
			exit 1
		fi
		if [ ! -r "$POOL_TRAINING" ] || [ ! -r "$SEARCH_TRAINING_FILTER" ]; then
			echo "error: local PGO training definition is incomplete; expected $POOL_TRAINING and $SEARCH_TRAINING_FILTER" >&2
			exit 1
		fi
		;;
	*)
		echo "usage: $0 train [new-profile-directory] | use /path/to/profile-directory" >&2
		exit 2
		;;
esac

# Stage both helpers beside their final destinations. A failed compile or a
# rejected profile leaves both existing helpers untouched. Each final rename
# is atomic, but the two independent executables are not moved as one atomic
# pair: interruption between the renames is repaired by rerunning this script.
PGO_TMP=$(mktemp -d "$SCRIPT_DIR/.brainstorm-pgo.XXXXXX")
PROFILE_STAGE=
cleanup() {
	rm -rf "$PGO_TMP"
	if [ -n "$PROFILE_STAGE" ]; then rm -rf "$PROFILE_STAGE"; fi
}
trap cleanup EXIT HUP INT TERM

if [ "$MODE" = "train" ]; then
	derive_search_training_config "$PGO_TMP/$TRAINING_CONFIG_NAME"
	derive_pool_binary_training_config "$PGO_TMP/pool-binary-training.cfg"
	IDENTITY_TRAINING_CONFIG=$PGO_TMP/$TRAINING_CONFIG_NAME
else
	IDENTITY_TRAINING_CONFIG=$PROFILE_TRAINING_CONFIG
fi
write_identity "$PGO_TMP/$IDENTITY_NAME" "$IDENTITY_TRAINING_CONFIG"
if [ "$MODE" = "use" ] && ! cmp -s "$PROFILE_IDENTITY" "$PGO_TMP/$IDENTITY_NAME"; then
	echo "error: PGO profile identity does not match this source, compiler target/version, or fixed training/build definition: $PROFILE_INPUT" >&2
	exit 1
fi

if [ "$MODE" = "train" ]; then
	"$CC" -O3 -fprofile-instr-generate \
		-Wall -ffp-contract=off -pthread \
		-o "$PGO_TMP/brainstorm_native_search.instrumented" \
		brainstorm_native_search.c -lm
	"$CC" -O3 -fprofile-instr-generate \
		-Wall -Wno-unused-function -ffp-contract=off -pthread \
		-o "$PGO_TMP/brainstorm_seed_pool.instrumented" \
		brainstorm_seed_pool.c -lm
	echo "training native-search profile: first Perkeo, physical packs, Charm/Omen recovery"
	LLVM_PROFILE_FILE="$PGO_TMP/native_search.profraw" \
		"$PGO_TMP/brainstorm_native_search.instrumented" bench \
		"$PGO_TMP/$TRAINING_CONFIG_NAME" 2
	echo "training seed-pool profile: exact Perkeo, first Soul, antes 1-6"
	LLVM_PROFILE_FILE="$PGO_TMP/seed_pool.profraw" \
		"$PGO_TMP/brainstorm_seed_pool.instrumented" scan \
		"$PGO_TMP/$TRAINING_CONFIG_NAME" "$POOL_TRAINING" \
		"$PGO_TMP/training.count"
	echo "creating representative restricted pool (profile discarded)"
	LLVM_PROFILE_FILE="$PGO_TMP/seed_pool-binary-discard.profraw" \
		"$PGO_TMP/brainstorm_seed_pool.instrumented" scan \
		"$PGO_TMP/$TRAINING_CONFIG_NAME" "$PGO_TMP/pool-binary-training.cfg" \
		"$PGO_TMP/search-training.bspool"
	derive_restricted_search_config "$PGO_TMP/$TRAINING_CONFIG_NAME" \
		"$PGO_TMP/search-training.bspool" "$PGO_TMP/restricted-search-training.cfg"
	echo "training native-search profile: restricted .bspool decode and exact evaluation"
	if LLVM_PROFILE_FILE="$PGO_TMP/native_search-pool.profraw" \
			"$PGO_TMP/brainstorm_native_search.instrumented" search \
			"$PGO_TMP/restricted-search-training.cfg" "$PGO_TMP/restricted.status" \
			"$PGO_TMP/restricted.stop" "$PGO_TMP/restricted.heartbeat"; then
		echo "error: restricted PGO workload unexpectedly found a match" >&2
		exit 1
	else
		restricted_rc=$?
		if [ "$restricted_rc" -ne 3 ]; then
			echo "error: restricted PGO workload failed with status $restricted_rc (expected complete no-match status 3)" >&2
			exit 1
		fi
	fi
	# Restricted replay is much more control-flow-heavy per candidate. Weight
	# the primary full-space sample so it keeps the dominant layout while the
	# independently measured pool_worker path remains profiled rather than cold.
	"$PROFDATA" merge -sparse \
		--weighted-input="32,$PGO_TMP/native_search.profraw" \
		--weighted-input="1,$PGO_TMP/native_search-pool.profraw" \
		-o "$PGO_TMP/$SEARCH_PROFILE_NAME"
	"$PROFDATA" merge -sparse "$PGO_TMP/seed_pool.profraw" \
		-o "$PGO_TMP/$POOL_PROFILE_NAME"
	SEARCH_PROFILE=$PGO_TMP/$SEARCH_PROFILE_NAME
	POOL_PROFILE=$PGO_TMP/$POOL_PROFILE_NAME
fi
build_search_from_profile "$SEARCH_PROFILE"
build_pool_from_profile "$POOL_PROFILE"

if [ "$MODE" = "train" ] && [ -n "$PROFILE_OUTPUT" ]; then
	PROFILE_PARENT=$(dirname "$PROFILE_OUTPUT")
	PROFILE_BASE=$(basename "$PROFILE_OUTPUT")
	PROFILE_STAGE=$(mktemp -d "$PROFILE_PARENT/.$PROFILE_BASE.XXXXXX")
	cp "$SEARCH_PROFILE" "$PROFILE_STAGE/$SEARCH_PROFILE_NAME"
	cp "$POOL_PROFILE" "$PROFILE_STAGE/$POOL_PROFILE_NAME"
	cp "$PGO_TMP/$IDENTITY_NAME" "$PROFILE_STAGE/$IDENTITY_NAME"
	cp "$PGO_TMP/$TRAINING_CONFIG_NAME" "$PROFILE_STAGE/$TRAINING_CONFIG_NAME"
	mv "$PROFILE_STAGE" "$PROFILE_OUTPUT"
	PROFILE_STAGE=
	echo "published matching PGO profiles: $PROFILE_OUTPUT"
fi

mv "$PGO_TMP/brainstorm_native_search" brainstorm_native_search
mv "$PGO_TMP/brainstorm_seed_pool" brainstorm_seed_pool
echo "built (Legendary/Omen-trained PGO): $(pwd)/brainstorm_native_search"
echo "built (exact-trained PGO): $(pwd)/brainstorm_seed_pool"
