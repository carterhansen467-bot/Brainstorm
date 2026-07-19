/* ===========================================================================
 * Brainstorm native seed searcher (macOS / any POSIX, plain C11)
 * ---------------------------------------------------------------------------
 * Bit-exact C port of the per-seed filter suite in Brainstorm_reroll.lua
 * (Brainstorm.passesAllFilters and helpers), run across all CPU cores.
 * The mod snapshots pools + settings to a config file, spawns this binary,
 * and polls a small status file; every hit is still re-verified on the main
 * thread by the mod's trusted Lua path before being applied (safety rail).
 *
 * Bit-exactness strategy:
 *  - All Lua-level arithmetic (pseudohash, the pseudoseed advance, round13)
 *    is separate IEEE ops -- LuaJIT's VM/JIT never fuses a*b+c -- so this
 *    file MUST be compiled with -ffp-contract=off (see build.sh).
 *  - math.randomseed/math.random are ported verbatim from LuaJIT's
 *    lib_math.c / lj_prng.h (Tausworthe TW223). The seeding step d = d*pi+e
 *    is C code inside LuaJIT, so ITS rounding depends on how the game's
 *    LuaJIT binary was compiled (fused or not). We implement both and pick
 *    the one that reproduces parity checks the mod computes at runtime with
 *    the game's own functions (check_* lines in the config). If neither
 *    matches, we refuse to search and the mod falls back to its Lua search.
 *
 * Modes:
 *   search  <cfg> <status> <stop> <heartbeat>   normal operation (detached)
 *   fixture <cfg> <seedfile>                    print "<seed> <0|1> <label>"
 *   verifychecks <cfg>                          run check_* lines, report
 *   bench   <cfg> <seconds>                     measure seeds/sec (1 thread
 *                                               and all threads)
 * =========================================================================== */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <ctype.h>
#include <errno.h>
#include <limits.h>
#include <math.h>
#include <stdatomic.h>
#include <time.h>
#include "platform.h"

/* ---------------------------------------------------------------- limits */
#define MAX_KEY 48
#define MAX_TAGS 64
#define MAX_VOUCH 96
#define MAX_JOKERS 256
#define MAX_BOOST 64
#define MAX_PACKF 16
#define MAX_CHECKS 80
#define MAX_SLOTS 64      /* shop slots per ante */
#define MAX_RESAMPLE 64   /* persistent resample streams per base key */
#define MAX_SEQ 80        /* per-ante joker sequence entries */
#ifdef BRAINSTORM_NATIVE_CORE_ONLY
#define BOOST_SOUL_RUN_CAP (MAX_BOOST * 2 + 1)
#define BOOST_SOUL_LUT_BITS 10
#define BOOST_SOUL_LUT_SIZE (1 << BOOST_SOUL_LUT_BITS)
#define BOOST_SOUL_LUT_MIXED UINT16_MAX
#endif

static const double LUA_PI = 3.14159265358979323846; /* == math.pi bits */
typedef union { uint64_t u64; double d; } U64double;

/* Lua 5.1 `x % 1` == x - floor(x/1)*1; exact for our non-negative x. */
static inline double lua_mod1(double x) { return x - floor(x); }

/* ------------------------------------------------------------- round13 --
 * Port of the mod's round13 (Brainstorm_reroll.lua): bit-identical stand-in
 * for math.abs(tonumber(string.format("%.13f", x))), x in [0,1).
 *
 * A floating x*1e13 cannot reliably decide values within about 0.001 of a
 * decimal half-way point.  Keep that very cheap arithmetic test for the
 * common 99.7%, but replace the former snprintf+strtod slow path with an
 * exact binary64 calculation.  For x = mantissa / 2^shift, the 97-bit
 * product mantissa*10^13 fits in unsigned __int128, so its remainder decides
 * decimal rounding without floating error.  Only a genuine exact tie retains
 * the string fallback (and therefore the runtime's tie rule).  The final
 * integer and 10^13 are exact doubles; IEEE division returns the same nearest
 * binary64 value as parsing the emitted 13-place decimal. */
static double round13_exact(double x) {
	uint64_t bits;
	memcpy(&bits, &x, sizeof bits);
	uint64_t fraction = bits & UINT64_C(0x000fffffffffffff);
	unsigned exponent = (unsigned)((bits >> 52) & 0x7ffu);
	/* The search hot paths pass only finite non-negative values below one.
	 * Preserve the old routine's behavior defensively for any other caller. */
	if ((bits >> 63) || exponent >= 1023u) goto decimal_fallback;
	if (exponent == 0) return 0.0; /* zero/subnormal: far below 0.5e-13 */
	unsigned shift = 1075u - exponent;
	unsigned __int128 scaled = (unsigned __int128)
			(fraction | UINT64_C(0x0010000000000000))
			* UINT64_C(10000000000000);
	/* scaled < 2^97, so a denominator of 2^98 or larger rounds to zero. */
	if (shift >= 98u) return 0.0;
	unsigned __int128 rounded = scaled >> shift;
	unsigned __int128 remainder = scaled
			& (((unsigned __int128)1u << shift) - 1u);
	unsigned __int128 halfway = (unsigned __int128)1u << (shift - 1u);
	if (remainder > halfway) rounded++;
	else if (remainder == halfway) goto decimal_fallback;
	return (double)(uint64_t)rounded / 1e13;

decimal_fallback: {
		char buf[48];
		snprintf(buf, sizeof buf, "%.13f", x);
		return fabs(strtod(buf, NULL));
	}
}

static inline double round13(double x) {
	double q = x * 1e13;
	double n = floor(q);
	double f = q - n;
	if (f > 0.5015) n += 1.0;
	else if (f > 0.4985) return round13_exact(x);
	return n / 1e13;
}

/* ---------------------------------------------------- LuaJIT TW223 PRNG --
 * Verbatim from LuaJIT 2.1 lj_prng.h/lib_math.c (the interpreter LOVE
 * embeds; the game never overrides math.random). */
typedef struct { uint64_t u[4]; } PRNG;

#define TW223_GEN(rs, z, r, i, k, q, s) \
	z = (rs)->u[i]; \
	z = (((z << q) ^ z) >> (k - s)) ^ ((z & ((uint64_t)(int64_t)-1 << (64 - k))) << s); \
	r ^= z; (rs)->u[i] = z;

static inline uint64_t prng_step(PRNG *rs) {
	uint64_t z, r = 0;
	TW223_GEN(rs, z, r, 0, 63, 31, 18)
	TW223_GEN(rs, z, r, 1, 58, 19, 28)
	TW223_GEN(rs, z, r, 2, 55, 24, 7)
	TW223_GEN(rs, z, r, 3, 47, 21, 8)
	return r;
}

/* g_seed_fma: how the game's LuaJIT binary rounded d*pi+e in random_seed.
 * 1 = fused (single rounding), 0 = separate mul+add. Decided at startup by
 * parity checks that must unambiguously select exactly one mode. */
static int g_seed_fma = 0;

static inline double seed_step(double d) {
	if (g_seed_fma)
		return fma(d, 3.14159265358979323846, 2.7182818284590452354);
	return d * 3.14159265358979323846 + 2.7182818284590452354;
}

static void lj_random_seed(PRNG *rs, double d) {
	uint32_t r = 0x11090601; /* 64-k[i] as four 8 bit constants. */
	for (int i = 0; i < 4; i++) {
		U64double u;
		uint32_t m = 1u << (r & 255);
		r >>= 8;
		d = seed_step(d);
		u.d = d;
		if (u.u64 < m) u.u64 += m; /* Ensure k[i] MSB of u[i] are non-zero. */
		rs->u[i] = u.u64;
	}
	for (int i = 0; i < 10; i++) (void)prng_step(rs);
}

static inline double lj_random(PRNG *rs) {
	U64double u;
	u.u64 = (prng_step(rs) & 0x000fffffffffffffULL) | 0x3ff0000000000000ULL;
	return u.d - 1.0; /* [0,1) */
}

/* math.random(n): d = floor(d*n) + 1.0, an int in [1, n]. */
static inline int lj_random_n(PRNG *rs, int n) {
	double d = floor(lj_random(rs) * (double)n) + 1.0;
	if (!(d >= 1.0 && d <= (double)n)) return -1; /* NaN guard (Lua would error too) */
	return (int)d;
}

/* Every Balatro pseudorandom draw reseeds TW223 and consumes exactly its first
 * output.  The component recurrences are linear over GF(2); composing their
 * eleven steps (ten discarded warm-ups plus the returned draw) yields these
 * exact shift/mask forms.  Only the low 52 output bits are materialized.
 * Keeping the canonical stateful functions above makes parity calibration and
 * differential tests independent of this one-shot hot path. */
static inline uint64_t prng_first_0(uint64_t x) {
	return ((x & UINT64_C(0xfe00000000000000)) >> 57)
			^ ((x & UINT64_C(0xff00000000000000)) >> 56)
			^ ((x & UINT64_C(0xffc0000000000000)) >> 54)
			^ ((x & UINT64_C(0x00000001fc000000)) >> 26)
			^ ((x & UINT64_C(0xfffffffe00000000)) >> 25)
			^ ((x & UINT64_C(0xfffffffe00000000)) >> 23)
			^ ((x & UINT64_C(0x00000001fffffffe)) << 6)
			^ ((x & UINT64_C(0x00001ffffffffffe)) << 7)
			^ ((x & UINT64_C(0x00000ffe00000000)) << 8)
			^ ((x & UINT64_C(0x000007fffffffffe)) << 9)
			^ ((x & UINT64_C(0x0000000000003ffe)) << 38)
			^ ((x & UINT64_C(0x0000000000000ffe)) << 40);
}

static inline uint64_t prng_first_1(uint64_t x) {
	return ((x & UINT64_C(0xc000000000000000)) >> 62)
			^ ((x & UINT64_C(0xe000000000000000)) >> 61)
			^ ((x & UINT64_C(0xf000000000000000)) >> 60)
			^ ((x & UINT64_C(0x0000180000000000)) >> 43)
			^ ((x & UINT64_C(0xffffe00000000000)) >> 42)
			^ ((x & UINT64_C(0x00001e0000000000)) >> 41)
			^ ((x & UINT64_C(0xffffff0000000000)) >> 40)
			^ ((x & UINT64_C(0xffffe00000000000)) >> 23)
			^ ((x & UINT64_C(0xffffffffffc00000)) >> 22)
			^ ((x & UINT64_C(0xffffe00000000000)) >> 21)
			^ ((x & UINT64_C(0x00001fffffffffc0)) >> 4)
			^ ((x & UINT64_C(0x00001fffffffffc0)) >> 3)
			^ ((x & UINT64_C(0x00001fffffffffc0)) >> 2)
			^ ((x & UINT64_C(0x0000000fffffffc0)) << 16)
			^ ((x & UINT64_C(0x00000003ffffffc0)) << 18)
			^ ((x & UINT64_C(0x000000000001ffc0)) << 35)
			^ ((x & UINT64_C(0x000000000000ffc0)) << 36)
			^ ((x & UINT64_C(0x0000000000007fc0)) << 37);
}

static inline uint64_t prng_first_2(uint64_t x) {
	return ((x & UINT64_C(0xffffff0000000000)) >> 40)
			^ ((x & UINT64_C(0xfffffffe00000000)) >> 33)
			^ ((x & UINT64_C(0x000000ffffff0000)) >> 16)
			^ ((x & UINT64_C(0x1fffff0000000000)) >> 9)
			^ ((x & UINT64_C(0x0000001ffffffe00)) << 15)
			^ ((x & UINT64_C(0x000000003ffffe00)) << 22);
}

static inline uint64_t prng_first_3(uint64_t x) {
	return ((x & UINT64_C(0x8000000000000000)) >> 63)
			^ ((x & UINT64_C(0xfc00000000000000)) >> 58)
			^ ((x & UINT64_C(0xffe0000000000000)) >> 53)
			^ ((x & UINT64_C(0x0000040000000000)) >> 42)
			^ ((x & UINT64_C(0xfffff80000000000)) >> 37)
			^ ((x & UINT64_C(0x000007ff00000000)) >> 32)
			^ ((x & UINT64_C(0x000007fffffe0000)) >> 16)
			^ ((x & UINT64_C(0x7ffffffffffe0000)) >> 11)
			^ ((x & UINT64_C(0x03fffffffffe0000)) >> 6)
			^ ((x & UINT64_C(0x000003fffffe0000)) << 10);
}

/* Bits 44..51 alone decide 255/256 comparisons against both hot thresholds
 * (0.997 for Soul/Negative/Black Hole and 0.8 for Omen conversion). */
static inline uint64_t prng_first_hi_0(uint64_t x) {
	return ((x & UINT64_C(0x00001fe000000000)) << 7)
			^ ((x & UINT64_C(0x00000ff000000000)) << 8)
			^ ((x & UINT64_C(0x000007f800000000)) << 9)
			^ ((x & UINT64_C(0x0000000000003fc0)) << 38)
			^ ((x & UINT64_C(0x0000000000000ff0)) << 40);
}

static inline uint64_t prng_first_hi_1(uint64_t x) {
	return ((x & UINT64_C(0x0000000ff0000000)) << 16)
			^ ((x & UINT64_C(0x00000003fc000000)) << 18)
			^ ((x & UINT64_C(0x000000000001fe00)) << 35)
			^ ((x & UINT64_C(0x000000000000ff00)) << 36)
			^ ((x & UINT64_C(0x0000000000007f80)) << 37);
}

static inline uint64_t prng_first_hi_2(uint64_t x) {
	return ((x & UINT64_C(0x1fe0000000000000)) >> 9)
			^ ((x & UINT64_C(0x0000001fe0000000)) << 15)
			^ ((x & UINT64_C(0x000000003fc00000)) << 22);
}

static inline uint64_t prng_first_hi_3(uint64_t x) {
	return ((x & UINT64_C(0x7f80000000000000)) >> 11)
			^ ((x & UINT64_C(0x03fc000000000000)) >> 6)
			^ ((x & UINT64_C(0x000003fc00000000)) << 10);
}

static inline double lj_random_seed_one(double d) {
	uint32_t masks = UINT32_C(0x11090601);
	uint64_t result = 0;
	for (int i = 0; i < 4; i++) {
		U64double u;
		uint32_t minimum = 1u << (masks & 255u);
		masks >>= 8;
		d = seed_step(d);
		u.d = d;
		if (u.u64 < minimum) u.u64 += minimum;
		if (i == 0) result ^= prng_first_0(u.u64);
		else if (i == 1) result ^= prng_first_1(u.u64);
		else if (i == 2) result ^= prng_first_2(u.u64);
		else result ^= prng_first_3(u.u64);
	}
	U64double out;
	out.u64 = (result & UINT64_C(0x000fffffffffffff))
			| UINT64_C(0x3ff0000000000000);
	return out.d - 1.0;
}

/* The stock voucher catalog has exactly 32 entries.  math.random(32) only
 * observes output bits 47..51 of the 52-bit fraction, all of which are already
 * produced by the much smaller threshold transform above. */
static inline int lj_random_seed_one_n32(double d) {
	uint32_t masks = UINT32_C(0x11090601);
	uint64_t high = 0;
	for (int i = 0; i < 4; i++) {
		U64double u;
		uint32_t minimum = 1u << (masks & 255u);
		masks >>= 8;
		d = seed_step(d);
		u.d = d;
		if (u.u64 < minimum) u.u64 += minimum;
		if (i == 0) high ^= prng_first_hi_0(u.u64);
		else if (i == 1) high ^= prng_first_hi_1(u.u64);
		else if (i == 2) high ^= prng_first_hi_2(u.u64);
		else high ^= prng_first_hi_3(u.u64);
	}
	return (int)((high >> 47) & UINT64_C(31)) + 1;
}

static inline int lj_random_seed_one_n(double seed, int n) {
	if (n == 32) return lj_random_seed_one_n32(seed);
	double d = floor(lj_random_seed_one(seed) * (double)n) + 1.0;
	if (!(d >= 1.0 && d <= (double)n)) return -1;
	return (int)d;
}

static inline int lj_random_seed_gt(double d, uint8_t thresholdHi,
		uint64_t thresholdFloor) {
	uint32_t masks = UINT32_C(0x11090601);
	uint64_t word[4], high = 0;
	for (int i = 0; i < 4; i++) {
		U64double u;
		uint32_t minimum = 1u << (masks & 255u);
		masks >>= 8;
		d = seed_step(d);
		u.d = d;
		if (u.u64 < minimum) u.u64 += minimum;
		word[i] = u.u64;
		if (i == 0) high ^= prng_first_hi_0(u.u64);
		else if (i == 1) high ^= prng_first_hi_1(u.u64);
		else if (i == 2) high ^= prng_first_hi_2(u.u64);
		else high ^= prng_first_hi_3(u.u64);
	}
	uint8_t outputHi = (uint8_t)(high >> 44);
	if (outputHi != thresholdHi) return outputHi > thresholdHi;
	uint64_t result = prng_first_0(word[0]) ^ prng_first_1(word[1])
			^ prng_first_2(word[2]) ^ prng_first_3(word[3]);
	return (result & UINT64_C(0x000fffffffffffff)) > thresholdFloor;
}

static inline int lj_random_seed_gt_997(double seed) {
	return lj_random_seed_gt(seed, UINT8_C(0xff),
			UINT64_C(0x000ff3b645a1cac0));
}

static inline int lj_random_seed_gt_08(double seed) {
	return lj_random_seed_gt(seed, UINT8_C(0xcc),
			UINT64_C(0x000ccccccccccccd));
}

#define PRNG_BATCH_MAX 8
static void lj_random_seed_components_batch(const double *seed, int count,
		uint64_t word[4][PRNG_BATCH_MAX]) {
	double d[PRNG_BATCH_MAX];
	if (count < 1 || count > PRNG_BATCH_MAX) return;
	for (int lane = 0; lane < count; lane++) d[lane] = seed[lane];
	uint32_t masks = UINT32_C(0x11090601);
	for (int component = 0; component < 4; component++) {
		if (g_seed_fma) {
#pragma clang loop vectorize(enable)
			for (int lane = 0; lane < count; lane++)
				d[lane] = fma(d[lane], 3.14159265358979323846,
						2.7182818284590452354);
		} else {
#pragma clang loop vectorize(enable)
			for (int lane = 0; lane < count; lane++)
				d[lane] = d[lane] * 3.14159265358979323846
						+ 2.7182818284590452354;
		}
		uint32_t minimum = 1u << (masks & 255u);
		masks >>= 8;
		for (int lane = 0; lane < count; lane++) {
			U64double u = { .d = d[lane] };
			if (u.u64 < minimum) u.u64 += minimum;
			word[component][lane] = u.u64;
		}
	}
}

#ifdef BRAINSTORM_NATIVE_CORE_ONLY
static void lj_random_seed_word_batch(const double *seed, int count,
		uint64_t *result) {
	uint64_t word[4][PRNG_BATCH_MAX];
	lj_random_seed_components_batch(seed, count, word);
	for (int lane = 0; lane < count; lane++) {
		result[lane] = prng_first_0(word[0][lane])
				^ prng_first_1(word[1][lane])
				^ prng_first_2(word[2][lane])
				^ prng_first_3(word[3][lane]);
	}
}

static void lj_random_seed_one_batch(const double *seed, int count,
		double *output) {
	uint64_t result[PRNG_BATCH_MAX];
	lj_random_seed_word_batch(seed, count, result);
	for (int lane = 0; lane < count; lane++) {
		U64double u;
		u.u64 = (result[lane] & UINT64_C(0x000fffffffffffff))
				| UINT64_C(0x3ff0000000000000);
		output[lane] = u.d - 1.0;
	}
}
#endif

static void lj_random_seed_one_n_batch(const double *seed, int count,
		int n, int *output) {
	uint64_t word[4][PRNG_BATCH_MAX];
	lj_random_seed_components_batch(seed, count, word);
	if (n == 32) {
		for (int lane = 0; lane < count; lane++) {
			uint64_t high = prng_first_hi_0(word[0][lane])
					^ prng_first_hi_1(word[1][lane])
					^ prng_first_hi_2(word[2][lane])
					^ prng_first_hi_3(word[3][lane]);
			output[lane] = (int)((high >> 47) & UINT64_C(31)) + 1;
		}
		return;
	}
	for (int lane = 0; lane < count; lane++) {
		uint64_t result = prng_first_0(word[0][lane])
				^ prng_first_1(word[1][lane])
				^ prng_first_2(word[2][lane])
				^ prng_first_3(word[3][lane]);
		U64double u;
		u.u64 = (result & UINT64_C(0x000fffffffffffff))
				| UINT64_C(0x3ff0000000000000);
		double value = floor((u.d - 1.0) * (double)n) + 1.0;
		output[lane] = value >= 1.0 && value <= (double)n ? (int)value : -1;
	}
}

/* ------------------------------------------------------------ pseudohash --
 * Balatro's pseudohash over key..seed (Lua-level math: no contraction). */
static double pseudohash_ks(const char *key, const char *seed) {
	char buf[96];
	size_t kl = strlen(key);
	size_t sl = strlen(seed); /* 8 natural; 1..8 in either expanded space */
	memcpy(buf, key, kl);
	memcpy(buf + kl, seed, sl);
	int len = (int)(kl + sl);
	double num = 1.0;
	for (int i = len; i >= 1; i--) {
		num = lua_mod1((1.1239285023 / num) * (double)(unsigned char)buf[i - 1] * LUA_PI + LUA_PI * (double)i);
	}
	return num;
}

static double pseudohash_str(const char *s) { /* whole string (checks, seed) */
	int len = (int)strlen(s);
	double num = 1.0;
	for (int i = len; i >= 1; i--) {
		num = lua_mod1((1.1239285023 / num) * (double)(unsigned char)s[i - 1] * LUA_PI + LUA_PI * (double)i);
	}
	return num;
}

/* ----------------------------------------------------------------- config */
#define MODELVER 6 /* RNG/shop model + config protocol version; must match the
                    * config's modelver. 6 interleaves collected Charm/Ethereal
                    * reward packs with the reachable shop-pack route. An older
                    * binary would silently ignore those Soul events, so reject it. */
#define MAX_POOL_ROUTE_RULES 16
/* Canonical cumulative Soul rules: at most one per exact depth (1, 2) plus up
 * to two distinct-key either-depth rules -- more than that cannot be satisfied
 * by the run's first two Souls. soulDepth 0 encodes "either Soul #1 or #2". */
#define MAX_POOL_LEGEND_RULES 4
#define MAX_POOL_VOUCHER_RULES 8
#define MAX_POOL_VOUCHER_EXCLUSIONS 16
#define SOUL_DEPTH_ANY 0
#define MAX_SEARCH_ANTE 39

enum {
	BSPOOL_LEGENDARY_ROUTES_FULL = 0,
	BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM = 1
};

typedef struct {
	int threads;
	double entropy;
	long session;
	int sawEnd; /* config must terminate with "end" (guards truncation) */
	int modelver; /* mod<->helper model handshake (stale binary => hard error) */
	/* filters */
	int soulCount;
	char legendary[MAX_KEY]; int negLegendary;
	char tag[MAX_KEY]; int tagAnywhere;
	char voucher[MAX_KEY]; int voucherAnte; /* 1-8 exact, 0 = any 1-4, -1 = any 1-8 */
	int legAnywhere;
	int packSlots; /* physical pack slots scanned per ante for joker matching (2 or 6) */
	int matchAny;
	/* wild: -1 = specific key, else wildcard rarity (0 = any, 1-3) */
	struct { char key[MAX_KEY]; int neg; int used; int rarity; int wild; } jslot[3];
	int maSlots[9], maPacks[9];
	int npack; char packKeys[MAX_PACKF][MAX_KEY];
	/* pools (avail flags resolved by the mod, same source as the Lua filters) */
	int ntags; char tagKey[MAX_TAGS][MAX_KEY];
	uint8_t tagReqOk[MAX_TAGS]; int tagMinAnte[MAX_TAGS]; /* 0 = none */
	int nvouch; char vouchKey[MAX_VOUCH][MAX_KEY]; uint8_t vouchAvail[MAX_VOUCH];
	/* Fresh-run voucher routing keeps unlocked/banned eligibility separate
	 * from dynamic prerequisite ownership. Older snapshots omit vouchroute;
	 * those retain the legacy base-voucher-only availability above. */
	uint8_t vouchRouteAvail[MAX_VOUCH], vouchRouteDefined[MAX_VOUCH];
	uint8_t vouchInitiallyOwned[MAX_VOUCH];
	int vouchPrereq[MAX_VOUCH];
	char vouchPrereqKey[MAX_VOUCH][MAX_KEY];
	int njoker[5]; char jokerKey[5][MAX_JOKERS][MAX_KEY]; uint8_t jokerAvail[5][MAX_JOKERS];
	int nboost; char boostKey[MAX_BOOST][MAX_KEY]; double boostW[MAX_BOOST];
	uint8_t boostBuf[MAX_BOOST], boostAvail[MAX_BOOST]; int boostCards[MAX_BOOST];
	uint8_t boostSoul[MAX_BOOST]; /* 0 none, 1 Arcana(Tarot), 2 Spectral */
	int tagRewardCards[3]; /* indexed by boostSoul kind: Charm=1, Ethereal=2 */
	/* Available boosters in source order with the exact running sums used by
	 * Lua's weighted poll.  The compact table removes an availability branch
	 * and permits a lower-bound search in every simulated shop-pack slot. */
	int nboostAvailable, boostAvailableIndex[MAX_BOOST];
	double boostAvailableLower[MAX_BOOST], boostAvailableCume[MAX_BOOST], boostCume;
#ifdef BRAINSTORM_NATIVE_CORE_ONLY
	/* Exact projection of weighted booster picks onto the only fields observed
	 * by seed-pool Soul walks: (content kind, card count). Runs are expressed
	 * in the PRNG's 52-bit fraction domain; the high-bit LUT resolves every
	 * bucket except the few containing a weighted-catalog boundary. */
	int nboostSoulRuns;
	uint64_t boostSoulRunEnd[BOOST_SOUL_RUN_CAP];
	uint16_t boostSoulRunClass[BOOST_SOUL_RUN_CAP];
	uint16_t boostSoulLut[BOOST_SOUL_LUT_SIZE];
#endif
	int soulAllowed, blackHoleAllowed, forceBuffoon, sawSpecialDef;
	/* joker-search derived */
	int ntargets; int wanted[4]; int needNeg; int anyAnteActive;
	/* parity checks */
	int nchecks;
	struct { int kind; char str[MAX_KEY]; double x; int n; double expect; } checks[MAX_CHECKS];
	/* first RNG stream the active filter chain touches (batched-hash preload) */
	int fsId; int fsAnte; const char *fsKey;
	/* optional .bspool restricting search to a prebuilt seed set (may contain
	 * spaces: the directive consumes the rest of its config line) */
	char poolFile[1024];
	int npoolRouteRules;
	struct { int poolIndex, minAnte, minPhase, maxAnte, maxPhase, minCount, collect; }
		poolRouteRules[MAX_POOL_ROUTE_RULES];
	int npoolLegendRules;
	int poolLegendaryRoutes;
	int poolLegendNeedsEdition; /* derived: any cumulative pool rule requires Negative */
	struct {
		int poolIndex, minAnte, minPhase, maxAnte, maxPhase;
		int neg, source, humanLocation, soulDepth;
	}
		poolLegendRules[MAX_POOL_LEGEND_RULES];
	int npoolVoucherRules;
	struct { int poolIndex, minAnte, maxAnte; }
		poolVoucherRules[MAX_POOL_VOUCHER_RULES];
	int npoolVoucherExclusions;
	int poolVoucherExclusions[MAX_POOL_VOUCHER_EXCLUSIONS];
	int poolVoucherMaxAnte;
	/* Immutable catalog classifications resolved once at config load so the
	 * per-candidate hot paths never rescan the catalog or strcmp identity
	 * keys (config_finalize_catalog). */
	int omenVoucherIdx;   /* v_omen_globe, -1 if absent */
	int charmTagIdx;      /* tag_charm, -1 if absent */
	int activeLegendaryIdx; /* active legendary key in joker rarity-4 pool */
	int activeTagReward;  /* tag_reward_kind of the active tag filter */
	uint8_t vouchIsReducer[MAX_VOUCH]; /* v_hieroglyph / v_petroglyph */
	uint8_t tagRewardKindOf[MAX_TAGS]; /* 0 none, 1 Charm, 2 Ethereal */
	/* Voucher-route availability bitmasks (defined only when nvouch <= 64,
	 * which the route DFS already requires): bit i of vouchUnlocksMask[p] is
	 * set when voucher i's prerequisite is voucher p. */
	uint64_t vouchRouteEligibleMask, vouchNoPrereqMask;
	uint64_t vouchInitiallyOwnedMask, vouchInitialAvailMask, vouchReducerMask;
	uint64_t omenPrereqMask;
	uint64_t vouchUnlocksMask[MAX_VOUCH];
} Config;

enum { CK_PH = 1, CK_R13, CK_PR, CK_PRN };
enum { FS_NONE = 0, FS_SOUL, FS_TAG, FS_PACK, FS_VOUCH, FS_JCDT, FS_JPACK,
	FS_LEGEND };

/* ------------------------------------------------------------ candidates --
 * Charset matches the game's random_string output (no 0/O). Sequential
 * counter from an entropy-derived start: no duplicate candidates, and
 * pseudohash decorrelates neighbours. */
static const char CHARSET[] = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ";
#define CHARSET_N 34
static const uint64_t SEEDSPACE = 1785793904896ULL; /* 34^8 */

static void make_seed(uint64_t k, char out[9]) {
	for (int i = 0; i < 8; i++) {
		out[i] = CHARSET[k % CHARSET_N];
		k /= CHARSET_N;
	}
	out[8] = 0;
}

/* The natural space above is what the game GENERATES. "settable" is every
 * seed vanilla's seed box can preserve: 1-9 A-Z (O included, 0 excluded),
 * lengths 1..8. "total" adds 0, which vanilla remaps to O but Brainstorm can
 * pass directly to start_run when Illegal Seed Input is enabled. Ranks order
 * variable-length seeds shortest-first; within a length the digit order is
 * little-endian, same convention as make_seed. Only .bspool files carry a
 * space choice -- live full-space searches stay natural, since they hunt
 * seeds the game can actually deal. */
static const char CHARSET_SETTABLE[] = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
#define CHARSET_SETTABLE_N 35
static const uint64_t SEEDSPACE_SETTABLE = 2318107019760ULL; /* 35^1 + ... + 35^8 */

static const char CHARSET_TOTAL[] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
#define CHARSET_TOTAL_N 36
static const uint64_t SEEDSPACE_TOTAL = 2901713047668ULL; /* 36^1 + ... + 36^8 */

/* Keep SPACE_TOTAL=1 for checkpoint/header compatibility with existing pools. */
enum { SPACE_NATURAL = 0, SPACE_TOTAL = 1, SPACE_SETTABLE = 2 };

/* the last two are used by the pool-builder translation unit only */
#if defined(__GNUC__) || defined(__clang__)
#define BS_MAYBE_UNUSED __attribute__((unused))
#else
#define BS_MAYBE_UNUSED
#endif
static uint64_t space_size(int space) {
	if (space == SPACE_TOTAL) return SEEDSPACE_TOTAL;
	if (space == SPACE_SETTABLE) return SEEDSPACE_SETTABLE;
	return SEEDSPACE;
}
BS_MAYBE_UNUSED static const char *space_charset(int space) {
	if (space == SPACE_TOTAL) return CHARSET_TOTAL;
	if (space == SPACE_SETTABLE) return CHARSET_SETTABLE;
	return CHARSET;
}
BS_MAYBE_UNUSED static const char *space_name(int space) {
	if (space == SPACE_TOTAL) return "total";
	if (space == SPACE_SETTABLE) return "settable";
	return "natural";
}

/* rank -> seed in a given space; returns the seed's length. The length clamp
 * only matters for corrupt out-of-space ranks (callers validate first). */
static int make_seed_in(int space, uint64_t k, char out[9]) {
	if (space == SPACE_NATURAL) {
		make_seed(k, out);
		return 8;
	}
	const char *charset = space == SPACE_SETTABLE ? CHARSET_SETTABLE : CHARSET_TOTAL;
	uint64_t base = space == SPACE_SETTABLE ? CHARSET_SETTABLE_N : CHARSET_TOTAL_N;
	uint64_t block = base;
	int len = 1;
	while (len < 8 && k >= block) {
		k -= block;
		block *= base;
		len++;
	}
	for (int i = 0; i < len; i++) {
		out[i] = charset[k % base];
		k /= base;
	}
	out[len] = 0;
	return len;
}

static uint64_t splitmix64(uint64_t x) {
	x += 0x9E3779B97f4A7C15ULL;
	x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
	x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
	return x ^ (x >> 31);
}

/* ------------------------------------------------------- per-thread state */
typedef struct { double state; uint64_t gen; } Stream;

#define CTX_SOUL_EVENTS_PER_ANTE 8
#define CTX_SOUL_CARDS_PER_EVENT 64
#define CTX_SOUL_CARDS_PER_ANTE \
	(CTX_SOUL_EVENTS_PER_ANTE * CTX_SOUL_CARDS_PER_EVENT)
#define CTX_TAROT_TAPE_WORDS ((CTX_SOUL_CARDS_PER_ANTE + 63) / 64)
#define CTX_SPECTRAL_TAPE_WORDS ((CTX_SOUL_CARDS_PER_ANTE * 2 + 63) / 64)
#define CTX_OMEN_TAPE_BITS ((MAX_SEARCH_ANTE + 1) * CTX_SOUL_CARDS_PER_ANTE)
#define CTX_OMEN_TAPE_WORDS ((CTX_OMEN_TAPE_BITS + 63) / 64)

typedef struct CtxOmenTrace CtxOmenTrace;

/* Persistent resample streams: same "_resample<it>" key can be advanced more
 * than once per candidate (two UNAVAILABLE picks in the same ante+rarity), so
 * they need per-key state exactly like Brainstorm.random_state. Bases:
 * 0 Joker4 | 1..8 Voucher<a> | 9..32 Joker<r>sho<a> | 33..56 Joker<r>buf<a>
 * | 57..64 Tag<a> */
#define RB_JOKER4 0
#define RB_VOUCHER(a) (a)                         /* 1..8 */
#define RB_JSHO(r, a) (8 + ((r) - 1) * 8 + (a))   /* 9..32 */
#define RB_JBUF(r, a) (32 + ((r) - 1) * 8 + (a))  /* 33..56 */
#define RB_TAG(a) (56 + (a))                      /* 57..64 */
#define RBASES 65

#define PACK_FORCED (-2) /* ante-1 slot 1: the run's forced normal Buffoon */

typedef struct {
	const Config *g;
	char seed[9];
	double hashed_seed;
	uint64_t gen;
	PRNG prng;
	Stream joker4;
	Stream tagS[MAX_SEARCH_ANTE + 1], soulT[MAX_SEARCH_ANTE + 1];
	Stream soulS[MAX_SEARCH_ANTE + 1], edisouA[MAX_SEARCH_ANTE + 1];
	Stream omen;
	Stream voucher[MAX_SEARCH_ANTE + 1], shop_pack[MAX_SEARCH_ANTE + 1];
	Stream cdt[9], rarity_sho[9], rarity_buf[9], edisho[9], edibuf[9];
	Stream joker_sho[4][9], joker_buf[4][9];
	Stream resample[RBASES][MAX_RESAMPLE];
	Stream tagResample[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE];
	Stream poolVoucherResample[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE];
	uint8_t poolVoucherVisits[MAX_SEARCH_ANTE + 1];
	/* Reducer-free Omen routes read each VoucherN stream once. Cache its raw
	 * catalog draws so DFS/frontier siblings and Charm variants only redo the
	 * cheap availability-mask selection. */
	uint64_t voucherRawGen[MAX_SEARCH_ANTE + 1];
	uint8_t voucherRawN[MAX_SEARCH_ANTE + 1];
	uint8_t voucherRaw[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE + 1];
	uint64_t poolVoucherPurchased;
	uint8_t poolVoucherPurchaseAnte[MAX_VOUCH];
	uint8_t poolVoucherPurchaseVisit[MAX_VOUCH];
	int tagRoll[MAX_SEARCH_ANTE + 1][2];
	uint8_t tagRollDone[MAX_SEARCH_ANTE + 1][2];
	uint64_t packs_gen[MAX_SEARCH_ANTE + 1];
	int packs_n[MAX_SEARCH_ANTE + 1], pack_idx[MAX_SEARCH_ANTE + 1][6];
	/* Raw shop-pack draws are route-independent; a skipped blind merely uses a
	 * shorter prefix. Alternate Charm/Omen routes therefore share this cache. */
	uint64_t shopPackRawGen[MAX_SEARCH_ANTE + 1];
	uint8_t shopPackRawN[MAX_SEARCH_ANTE + 1];
	int16_t shopPackRaw[MAX_SEARCH_ANTE + 1][6];
	/* per-candidate blind-skip assumption (mirrors skipsFromFilters /
	 * setPackSkipAssumption): a skipped blind's shop never opens, so its two
	 * get_pack picks never roll. forcedAnte = first ante that still opens a
	 * shop; that shop's slot 1 is the run's forced normal Buffoon. */
	uint8_t skipSm[MAX_SEARCH_ANTE + 1], skipBig[MAX_SEARCH_ANTE + 1]; int forcedAnte;
	/* Soul-capable pack opened immediately in place of a collected blind's shop:
	 * 1 = Charm/Mega Arcana, 2 = Ethereal/Spectral. A plain skipped blind is 0. */
	uint8_t rewardSm[MAX_SEARCH_ANTE + 1], rewardBig[MAX_SEARCH_ANTE + 1];
	/* Threshold-only random tape shared by every Soul replay for this seed. */
	uint64_t soulTapeGen;
	Stream tapeTarot[MAX_SEARCH_ANTE + 1], tapeSpectral[MAX_SEARCH_ANTE + 1];
	Stream tapeEdition[MAX_SEARCH_ANTE + 1], tapeOmen;
	uint16_t tarotFilled[MAX_SEARCH_ANTE + 1], spectralFilled[MAX_SEARCH_ANTE + 1];
	uint8_t editionFilled[MAX_SEARCH_ANTE + 1];
	uint32_t omenFilled;
	uint64_t tarotHit[MAX_SEARCH_ANTE + 1][CTX_TAROT_TAPE_WORDS];
	uint64_t spectralHit[MAX_SEARCH_ANTE + 1][CTX_SPECTRAL_TAPE_WORDS];
	uint64_t editionNegative[MAX_SEARCH_ANTE + 1];
	uint64_t omenConvert[CTX_OMEN_TAPE_WORDS];
	/* Reachability result from the no-extra-Charm Omen probe. */
	int omenRoutePossible;
	CtxOmenTrace *omenTrace;
	char label[64];
	char legloc[24];
} Ctx;

static const char *K_CDT[9] = { 0, "cdt1", "cdt2", "cdt3", "cdt4", "cdt5", "cdt6", "cdt7", "cdt8" };
static const char *K_RSHO[9] = { 0, "rarity1sho", "rarity2sho", "rarity3sho", "rarity4sho",
	"rarity5sho", "rarity6sho", "rarity7sho", "rarity8sho" };
static const char *K_RBUF[9] = { 0, "rarity1buf", "rarity2buf", "rarity3buf", "rarity4buf",
	"rarity5buf", "rarity6buf", "rarity7buf", "rarity8buf" };
static const char *K_VOUCHER[9] = { 0, "Voucher1", "Voucher2", "Voucher3", "Voucher4",
	"Voucher5", "Voucher6", "Voucher7", "Voucher8" };
static const char *K_SHOPPACK[9] = { 0, "shop_pack1", "shop_pack2", "shop_pack3", "shop_pack4",
	"shop_pack5", "shop_pack6", "shop_pack7", "shop_pack8" };
static const char *K_EDISHO[9] = { 0, "edisho1", "edisho2", "edisho3", "edisho4",
	"edisho5", "edisho6", "edisho7", "edisho8" };
static const char *K_EDIBUF[9] = { 0, "edibuf1", "edibuf2", "edibuf3", "edibuf4",
	"edibuf5", "edibuf6", "edibuf7", "edibuf8" };
static const char *K_JSHO[4][9] = {
	{ 0 },
	{ 0, "Joker1sho1", "Joker1sho2", "Joker1sho3", "Joker1sho4", "Joker1sho5", "Joker1sho6", "Joker1sho7", "Joker1sho8" },
	{ 0, "Joker2sho1", "Joker2sho2", "Joker2sho3", "Joker2sho4", "Joker2sho5", "Joker2sho6", "Joker2sho7", "Joker2sho8" },
	{ 0, "Joker3sho1", "Joker3sho2", "Joker3sho3", "Joker3sho4", "Joker3sho5", "Joker3sho6", "Joker3sho7", "Joker3sho8" },
};
static const char *K_JBUF[4][9] = {
	{ 0 },
	{ 0, "Joker1buf1", "Joker1buf2", "Joker1buf3", "Joker1buf4", "Joker1buf5", "Joker1buf6", "Joker1buf7", "Joker1buf8" },
	{ 0, "Joker2buf1", "Joker2buf2", "Joker2buf3", "Joker2buf4", "Joker2buf5", "Joker2buf6", "Joker2buf7", "Joker2buf8" },
	{ 0, "Joker3buf1", "Joker3buf2", "Joker3buf3", "Joker3buf4", "Joker3buf5", "Joker3buf6", "Joker3buf7", "Joker3buf8" },
};
static const char *RBASE_KEY[RBASES] = {
	"Joker4",
	"Voucher1", "Voucher2", "Voucher3", "Voucher4", "Voucher5", "Voucher6", "Voucher7", "Voucher8",
	"Joker1sho1", "Joker1sho2", "Joker1sho3", "Joker1sho4", "Joker1sho5", "Joker1sho6", "Joker1sho7", "Joker1sho8",
	"Joker2sho1", "Joker2sho2", "Joker2sho3", "Joker2sho4", "Joker2sho5", "Joker2sho6", "Joker2sho7", "Joker2sho8",
	"Joker3sho1", "Joker3sho2", "Joker3sho3", "Joker3sho4", "Joker3sho5", "Joker3sho6", "Joker3sho7", "Joker3sho8",
	"Joker1buf1", "Joker1buf2", "Joker1buf3", "Joker1buf4", "Joker1buf5", "Joker1buf6", "Joker1buf7", "Joker1buf8",
	"Joker2buf1", "Joker2buf2", "Joker2buf3", "Joker2buf4", "Joker2buf5", "Joker2buf6", "Joker2buf7", "Joker2buf8",
	"Joker3buf1", "Joker3buf2", "Joker3buf3", "Joker3buf4", "Joker3buf5", "Joker3buf6", "Joker3buf7", "Joker3buf8",
	"Tag1", "Tag2", "Tag3", "Tag4", "Tag5", "Tag6", "Tag7", "Tag8",
};
static const char *K_TAG[9] = { 0, "Tag1", "Tag2", "Tag3", "Tag4", "Tag5", "Tag6", "Tag7", "Tag8" };
static const char *K_SOULT[9] = { 0, "soul_Tarot1", "soul_Tarot2", "soul_Tarot3", "soul_Tarot4",
	"soul_Tarot5", "soul_Tarot6", "soul_Tarot7", "soul_Tarot8" };
static const char *K_EDISOUA[9] = { 0, "edisou1", "edisou2", "edisou3", "edisou4",
	"edisou5", "edisou6", "edisou7", "edisou8" };

/* Precomputed RNG key strings for every ante/resample the engines can touch.
 * The resample and deep-ante loops run per candidate on every worker thread;
 * building these with snprintf there serializes threads on libc internals.
 * Filled once by init_key_tables() before any worker starts, then read-only. */
static char KT_TAG[MAX_SEARCH_ANTE + 1][12];
static char KT_VOUCHER[MAX_SEARCH_ANTE + 1][12];
static char KT_SHOPPACK[MAX_SEARCH_ANTE + 1][16];
static char KT_SOULT[MAX_SEARCH_ANTE + 1][16];
static char KT_SOULS[MAX_SEARCH_ANTE + 1][20];
static char KT_EDISOU[MAX_SEARCH_ANTE + 1][12];
static char RS_RBASE[RBASES][MAX_RESAMPLE][24];
static char RS_TAG[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE][24];
static char RS_VOUCHER[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE][24];
static uint8_t KL_TAG[MAX_SEARCH_ANTE + 1];
static uint8_t KL_VOUCHER[MAX_SEARCH_ANTE + 1];
static uint8_t KL_SHOPPACK[MAX_SEARCH_ANTE + 1];
static uint8_t KL_SOULT[MAX_SEARCH_ANTE + 1];
static uint8_t KL_SOULS[MAX_SEARCH_ANTE + 1];
static uint8_t KL_EDISOU[MAX_SEARCH_ANTE + 1];
static uint8_t KL_RS_RBASE[RBASES][MAX_RESAMPLE];
static uint8_t KL_RS_TAG[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE];
static uint8_t KL_RS_VOUCHER[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE];

static void init_key_tables(void) {
	for (int a = 1; a <= MAX_SEARCH_ANTE; a++) {
		KL_TAG[a] = (uint8_t)snprintf(KT_TAG[a], sizeof KT_TAG[a], "Tag%d", a);
		KL_VOUCHER[a] = (uint8_t)snprintf(KT_VOUCHER[a], sizeof KT_VOUCHER[a], "Voucher%d", a);
		KL_SHOPPACK[a] = (uint8_t)snprintf(KT_SHOPPACK[a], sizeof KT_SHOPPACK[a], "shop_pack%d", a);
		KL_SOULT[a] = (uint8_t)snprintf(KT_SOULT[a], sizeof KT_SOULT[a], "soul_Tarot%d", a);
		KL_SOULS[a] = (uint8_t)snprintf(KT_SOULS[a], sizeof KT_SOULS[a], "soul_Spectral%d", a);
		KL_EDISOU[a] = (uint8_t)snprintf(KT_EDISOU[a], sizeof KT_EDISOU[a], "edisou%d", a);
		for (int it = 2; it < MAX_RESAMPLE + 2; it++) {
			KL_RS_TAG[a][it - 2] = (uint8_t)snprintf(
					RS_TAG[a][it - 2], sizeof RS_TAG[a][it - 2],
					"Tag%d_resample%d", a, it);
			KL_RS_VOUCHER[a][it - 2] = (uint8_t)snprintf(
					RS_VOUCHER[a][it - 2], sizeof RS_VOUCHER[a][it - 2],
					"Voucher%d_resample%d", a, it);
		}
	}
	for (int b = 0; b < RBASES; b++) {
		for (int it = 2; it < MAX_RESAMPLE + 2; it++) {
			KL_RS_RBASE[b][it - 2] = (uint8_t)snprintf(
					RS_RBASE[b][it - 2], sizeof RS_RBASE[b][it - 2],
					"%s_resample%d", RBASE_KEY[b], it);
		}
	}
}

/* Brainstorm.pseudoseed(key..seed): lazy pseudohash init, round13 advance. */
static inline double stream_next(Ctx *c, Stream *s, const char *key) {
	if (s->gen != c->gen) {
		s->state = pseudohash_ks(key, c->seed);
		s->gen = c->gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashed_seed) / 2.0;
}

static inline double resample_next(Ctx *c, int base, int it /* >=2 */) {
	Stream *s = &c->resample[base][it - 2 < MAX_RESAMPLE ? it - 2 : MAX_RESAMPLE - 1];
	if (it - 2 >= MAX_RESAMPLE) { /* astronomically unlikely; reject via NaN */
		return NAN;
	}
	if (s->gen != c->gen) {
		s->state = pseudohash_ks(RS_RBASE[base][it - 2], c->seed);
		s->gen = c->gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashed_seed) / 2.0;
}

/* pseudorandom(seed) / pseudorandom_element index (worker fast path). */
static inline double psr(Ctx *c, double seedval) {
	(void)c;
	return lj_random_seed_one(seedval);
}
static inline int psr_n(Ctx *c, double seedval, int n) {
	(void)c;
	return lj_random_seed_one_n(seedval, n);
}
static inline int psr_gt_997(Ctx *c, double seedval) {
	(void)c;
	return lj_random_seed_gt_997(seedval);
}
static inline int psr_gt_08(Ctx *c, double seedval) {
	(void)c;
	return lj_random_seed_gt_08(seedval);
}

static inline void ctx_tape_store(uint64_t *bits, uint32_t at, int value) {
	uint64_t mask = UINT64_C(1) << (at & 63u);
	if (value) bits[at >> 6] |= mask;
	else bits[at >> 6] &= ~mask;
}

static inline int ctx_tape_load(const uint64_t *bits, uint32_t at) {
	return (int)((bits[at >> 6] >> (at & 63u)) & 1u);
}

static void ctx_soul_tape_prepare(Ctx *c) {
	if (c->soulTapeGen == c->gen) return;
	c->soulTapeGen = c->gen;
	memset(c->tarotFilled, 0, sizeof c->tarotFilled);
	memset(c->spectralFilled, 0, sizeof c->spectralFilled);
	memset(c->editionFilled, 0, sizeof c->editionFilled);
	c->omenFilled = 0;
}

#ifdef BRAINSTORM_VERIFY_INTERACTIVE_SOUL_TAPE
static int ctx_soul_tape_reference(Ctx *c, const char *key,
		uint32_t at, double threshold) {
	Stream state = { 0 };
	double value = 0.0;
	for (uint32_t i = 0; i <= at; i++)
		value = psr(c, stream_next(c, &state, key));
	return value > threshold;
}
#define CTX_VERIFY_TAPE(c_, key_, at_, threshold_, got_) do { \
	int reference_ = ctx_soul_tape_reference((c_), (key_), (at_), (threshold_)); \
	if ((got_) != reference_) { \
		fprintf(stderr, "interactive Soul tape mismatch seed=%s key=%s at=%u cached=%d reference=%d\n", \
				(c_)->seed, (key_), (unsigned)(at_), (got_), reference_); \
		abort(); \
	} \
} while (0)
#else
#define CTX_VERIFY_TAPE(c_, key_, at_, threshold_, got_) ((void)0)
#endif

static int ctx_soul_tape_tarot(Ctx *c, int ante, uint16_t at) {
	ctx_soul_tape_prepare(c);
	if (ante < 1 || ante > MAX_SEARCH_ANTE || at >= CTX_SOUL_CARDS_PER_ANTE)
		return 0;
	while (c->tarotFilled[ante] <= at) {
		uint16_t pos = c->tarotFilled[ante]++;
		int hit = psr_gt_997(c, stream_next(c,
				&c->tapeTarot[ante], KT_SOULT[ante]));
		ctx_tape_store(c->tarotHit[ante], pos, hit);
		CTX_VERIFY_TAPE(c, KT_SOULT[ante], pos, 0.997, hit);
	}
	return ctx_tape_load(c->tarotHit[ante], at);
}

static int ctx_soul_tape_spectral(Ctx *c, int ante, uint16_t at) {
	ctx_soul_tape_prepare(c);
	if (ante < 1 || ante > MAX_SEARCH_ANTE
			|| at >= CTX_SOUL_CARDS_PER_ANTE * 2) return 0;
	while (c->spectralFilled[ante] <= at) {
		uint16_t pos = c->spectralFilled[ante]++;
		int hit = psr_gt_997(c, stream_next(c,
				&c->tapeSpectral[ante], KT_SOULS[ante]));
		ctx_tape_store(c->spectralHit[ante], pos, hit);
		CTX_VERIFY_TAPE(c, KT_SOULS[ante], pos, 0.997, hit);
	}
	return ctx_tape_load(c->spectralHit[ante], at);
}

static int ctx_soul_tape_edition(Ctx *c, int ante, uint8_t at) {
	ctx_soul_tape_prepare(c);
	if (ante < 1 || ante > MAX_SEARCH_ANTE || at >= 64) return 0;
	while (c->editionFilled[ante] <= at) {
		uint8_t pos = c->editionFilled[ante]++;
		int negative = psr_gt_997(c, stream_next(c,
				&c->tapeEdition[ante], KT_EDISOU[ante]));
		ctx_tape_store(&c->editionNegative[ante], pos, negative);
		CTX_VERIFY_TAPE(c, KT_EDISOU[ante], pos, 0.997, negative);
	}
	return ctx_tape_load(&c->editionNegative[ante], at);
}

static int ctx_soul_tape_omen(Ctx *c, uint32_t at) {
	ctx_soul_tape_prepare(c);
	if (at >= CTX_OMEN_TAPE_BITS) return 0;
	while (c->omenFilled <= at) {
		uint32_t pos = c->omenFilled++;
		int convert = psr_gt_08(c,
				stream_next(c, &c->tapeOmen, "omen_globe"));
		ctx_tape_store(c->omenConvert, pos, convert);
		CTX_VERIFY_TAPE(c, "omen_globe", pos, 0.8, convert);
	}
	return ctx_tape_load(c->omenConvert, at);
}

/* Pick from an index-preserving culled pool with the resample loop. Returns
 * pool index (0-based) of an available entry, or -1 on guard failure. */
static int pick_culled(Ctx *c, Stream *first, const char *firstKey, int rbase,
		const uint8_t *avail, int n, int forbid /* -1 or 0-based index */) {
	int idx = psr_n(c, stream_next(c, first, firstKey), n);
	int it = 1;
	while (idx > 0 && (!avail[idx - 1] || (idx - 1) == forbid)) {
		it++;
		double sv = resample_next(c, rbase, it);
		if (isnan(sv)) return -1;
		idx = psr_n(c, sv, n);
	}
	return idx > 0 ? idx - 1 : -1;
}

/* getSimulatedPacks: an ante's PHYSICAL pack slots from the shared shop_pack
 * stream, extended on demand. SOURCE-VERIFIED SHOP MODEL: ease_ante fires on
 * boss death BEFORE its shop, so the post-boss shop draws from the NEXT
 * ante's streams -- ante 1 opens Small+Big (up to 4 slots), antes 2+ lead
 * with that post-boss "entry" shop (up to 6), and an assumed-skipped blind
 * removes its shop's 2 slots. The run's first opened shop leads with the
 * forced normal Buffoon (get_pack first_shop_buffoon), which consumes NO
 * stream advance. `count` is a cap; consumers must read c->packs_n[a] (and
 * never past their own cap). */
static int pack_max_slots(const Ctx *c, int a) {
	if (a < 1 || a > MAX_SEARCH_ANTE) return 0;
	int shops = (a >= 2 ? 3 : 2) - (c->skipSm[a] ? 1 : 0) - (c->skipBig[a] ? 1 : 0);
	return 2 * shops;
}

/* Exact get_pack weighted choice over the compact available catalog. The
 * lower test looks redundant in real arithmetic, but Lua's source loop
 * recomputes `running_sum - weight` after a rounded addition. Retain it so
 * even a rounding gap/overlap at a weight boundary selects the same entry. */
static inline int boost_pick_index(const Config *g, double poll) {
	int lo = 0, hi = g->nboostAvailable;
	while (lo < hi) {
		int mid = lo + (hi - lo) / 2;
		if (g->boostAvailableCume[mid] >= poll) hi = mid;
		else lo = mid + 1;
	}
	while (lo < g->nboostAvailable && g->boostAvailableLower[lo] > poll) lo++;
	return lo < g->nboostAvailable ? g->boostAvailableIndex[lo] : -1;
}

#ifdef BRAINSTORM_NATIVE_CORE_ONLY
static inline double boost_poll_from_fraction(const Config *g,
		uint64_t fraction) {
	U64double u = { .u64 = (fraction & UINT64_C(0x000fffffffffffff))
			| UINT64_C(0x3ff0000000000000) };
	return (u.d - 1.0) * g->boostCume;
}

/* First 52-bit PRNG fraction whose rounded weighted poll is >=/> boundary. */
static uint64_t boost_fraction_lower_bound(const Config *g, double boundary,
		int strict) {
	const uint64_t limit = UINT64_C(1) << 52;
	uint64_t lo = 0, hi = limit;
	while (lo < hi) {
		uint64_t mid = lo + ((hi - lo) >> 1);
		double poll = boost_poll_from_fraction(g, mid);
		if (strict ? poll > boundary : poll >= boundary) hi = mid;
		else lo = mid + 1;
	}
	return lo;
}

static void boost_add_soul_run(Config *g, uint64_t end, uint16_t cls) {
	int n = g->nboostSoulRuns;
	if (n && g->boostSoulRunClass[n - 1] == cls) {
		g->boostSoulRunEnd[n - 1] = end;
		return;
	}
	if (n >= BOOST_SOUL_RUN_CAP) return;
	g->boostSoulRunEnd[n] = end;
	g->boostSoulRunClass[n] = cls;
	g->nboostSoulRuns = n + 1;
}

static int boost_soul_run_index(const Config *g, uint64_t fraction) {
	int lo = 0, hi = g->nboostSoulRuns;
	while (lo < hi) {
		int mid = lo + ((hi - lo) >> 1);
		if (g->boostSoulRunEnd[mid] >= fraction) hi = mid;
		else lo = mid + 1;
	}
	return lo;
}

static inline uint16_t boost_pick_soul_fraction(const Config *g,
		uint64_t fraction) {
	fraction &= UINT64_C(0x000fffffffffffff);
	uint16_t cls = g->boostSoulLut[fraction >> (52 - BOOST_SOUL_LUT_BITS)];
	if (cls == BOOST_SOUL_LUT_MIXED) {
		int run = boost_soul_run_index(g, fraction);
		cls = run < g->nboostSoulRuns ? g->boostSoulRunClass[run] : 0;
	}
#ifdef BRAINSTORM_VERIFY_BOOST_SOUL_FRACTION
	int reference = boost_pick_index(g, boost_poll_from_fraction(g, fraction));
	uint16_t referenceClass = reference >= 0 && g->boostSoul[reference]
			? (uint16_t)((g->boostSoul[reference] << 8)
					| g->boostCards[reference]) : 0;
	if (cls != referenceClass) {
		fprintf(stderr,
				"booster Soul fraction mismatch fraction=%llu got=%u reference=%u\n",
				(unsigned long long)fraction, cls, referenceClass);
		abort();
	}
#endif
	return cls;
}

static int boost_fraction_boundary_compare(const void *a, const void *b) {
	uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
	return x < y ? -1 : x > y;
}

static void boost_build_soul_fraction_index(Config *g) {
	const uint64_t limit = UINT64_C(1) << 52;
	uint64_t boundary[MAX_BOOST * 2 + 2];
	int nboundary = 0;
	boundary[nboundary++] = 0;
	boundary[nboundary++] = limit;
	/* Every comparison in boost_pick_index is invariant between these exact
	 * integer-domain change points.  Sweeping them handles even pathological
	 * rounded gaps, overlaps, or non-monotone lower bounds from tiny weights. */
	for (int at = 0; at < g->nboostAvailable; at++) {
		boundary[nboundary++] = boost_fraction_lower_bound(g,
				g->boostAvailableCume[at], 1);
		boundary[nboundary++] = boost_fraction_lower_bound(g,
				g->boostAvailableLower[at], 0);
	}
	qsort(boundary, (size_t)nboundary, sizeof boundary[0],
			boost_fraction_boundary_compare);
	g->nboostSoulRuns = 0;
	for (int at = 0; at + 1 < nboundary;) {
		uint64_t start = boundary[at];
		int nextAt = at + 1;
		while (nextAt < nboundary && boundary[nextAt] == start) nextAt++;
		if (start >= limit || nextAt >= nboundary) break;
		uint64_t next = boundary[nextAt];
		int pick = boost_pick_index(g, boost_poll_from_fraction(g, start));
		uint16_t cls = pick >= 0 && g->boostSoul[pick]
				? (uint16_t)((g->boostSoul[pick] << 8) | g->boostCards[pick]) : 0;
		boost_add_soul_run(g, next - 1, cls);
		at = nextAt;
	}
	for (unsigned bucket = 0; bucket < BOOST_SOUL_LUT_SIZE; bucket++) {
		uint64_t low = (uint64_t)bucket << (52 - BOOST_SOUL_LUT_BITS);
		uint64_t high = (((uint64_t)bucket + 1u)
				<< (52 - BOOST_SOUL_LUT_BITS)) - 1u;
		int first = boost_soul_run_index(g, low);
		int last = boost_soul_run_index(g, high);
		g->boostSoulLut[bucket] = first == last
				? g->boostSoulRunClass[first] : BOOST_SOUL_LUT_MIXED;
	}
#ifdef BRAINSTORM_VERIFY_BOOST_SOUL_FRACTION
	for (int run = 0; run < g->nboostSoulRuns; run++) {
		uint64_t edge = g->boostSoulRunEnd[run];
		uint64_t lo = edge > 3 ? edge - 3 : 0;
		uint64_t hi = edge + 3 < limit ? edge + 3 : limit - 1;
		for (uint64_t fraction = lo; fraction <= hi; fraction++)
			(void)boost_pick_soul_fraction(g, fraction);
	}
	for (unsigned bucket = 0; bucket < BOOST_SOUL_LUT_SIZE; bucket++) {
		uint64_t lo = (uint64_t)bucket << (52 - BOOST_SOUL_LUT_BITS);
		uint64_t hi = (((uint64_t)bucket + 1u)
				<< (52 - BOOST_SOUL_LUT_BITS)) - 1u;
		(void)boost_pick_soul_fraction(g, lo);
		(void)boost_pick_soul_fraction(g, lo + ((hi - lo) >> 1));
		(void)boost_pick_soul_fraction(g, hi);
	}
	uint64_t sample = UINT64_C(0x243f6a8885a308d3);
	for (int i = 0; i < 1000000; i++) {
		sample = splitmix64(sample);
		(void)boost_pick_soul_fraction(g,
				sample & UINT64_C(0x000fffffffffffff));
	}
#endif
}
#endif

static void resolve_forced_pack_ante(Ctx *c) {
	c->forcedAnte = 1;
	for (int a = 1; a <= MAX_SEARCH_ANTE; a++) {
		if (pack_max_slots(c, a) > 0) { c->forcedAnte = a; break; }
	}
}

static void sim_packs(Ctx *c, int a, int count) {
	const Config *g = c->g;
	if (a < 1 || a > MAX_SEARCH_ANTE) return;
	if (c->shopPackRawGen[a] != c->gen) {
		c->shopPackRawGen[a] = c->gen;
		c->shopPackRawN[a] = 0;
	}
	int forced = g->forceBuffoon && a == c->forcedAnte;
	if (c->packs_gen[a] != c->gen) {
		c->packs_gen[a] = c->gen;
		c->packs_n[a] = 0;
		if (forced) {
			c->pack_idx[a][0] = PACK_FORCED;
			c->packs_n[a] = 1;
		}
	}
	int max = pack_max_slots(c, a);
	if (count > max) count = max;
	const char *packKey = KT_SHOPPACK[a];
	int rawNeeded = count - forced;
	while (c->shopPackRawN[a] < rawNeeded) {
		double poll = psr(c, stream_next(c, &c->shop_pack[a], packKey)) * g->boostCume;
		int pick = boost_pick_index(g, poll);
		c->shopPackRaw[a][c->shopPackRawN[a]++] = (int16_t)pick;
	}
	while (c->packs_n[a] < count) {
		int rawAt = c->packs_n[a] - forced;
		c->pack_idx[a][c->packs_n[a]++] = c->shopPackRaw[a][rawAt];
	}
}

/* Charm/Ethereal tags do more than remove a shop: skip_blind immediately
 * opens their reward pack at that blind choice, before the next reachable
 * shop. Those packs consume the same per-Ante Soul streams as shop packs. */
static int tag_reward_kind(const Config *g, int tagIndex) {
	if (tagIndex < 0 || tagIndex >= g->ntags) return 0;
	return g->tagRewardKindOf[tagIndex];
}

static int tag_reward_kind_key(const char *key) {
	if (key && !strcmp(key, "tag_charm")) return 1;
	if (key && !strcmp(key, "tag_ethereal")) return 2;
	return 0;
}

typedef struct {
	uint8_t soulKind, cards;
	uint8_t humanAnte; /* displayed route position, distinct from the RNG Ante key */
	uint8_t phase;     /* 0 Boss, 1 Small, 2 Big */
	uint8_t source;    /* 1 shop, 2 Charm reward, 3 Ethereal reward */
} SoulPackEvent;

/* Omen timing variants share one physical pack timeline for the current
 * skip/reward route. Random Soul/Omen/edition decisions live in Ctx's tape. */
struct CtxOmenTrace {
	uint64_t routeGen;
	int routeMaxAnte, forcedAnte;
	uint8_t routeSkipSm[MAX_SEARCH_ANTE + 1];
	uint8_t routeSkipBig[MAX_SEARCH_ANTE + 1];
	uint8_t routeRewardSm[MAX_SEARCH_ANTE + 1];
	uint8_t routeRewardBig[MAX_SEARCH_ANTE + 1];
	uint8_t eventCount[MAX_SEARCH_ANTE + 1];
	SoulPackEvent event[MAX_SEARCH_ANTE + 1][CTX_SOUL_EVENTS_PER_ANTE];
};

enum { SOUL_PHASE_BOSS = 0, SOUL_PHASE_SMALL = 1, SOUL_PHASE_BIG = 2 };
enum { SOUL_SOURCE_SHOP = 1, SOUL_SOURCE_CHARM = 2, SOUL_SOURCE_ETHEREAL = 3 };

static int bspool_route_position(int ante, int phase) {
	/* Displayed route chronology is Small -> Big -> Boss. Boss is generated
	 * from the following RNG Ante, despite its enum value sorting first. */
	int order = phase == SOUL_PHASE_SMALL ? 0
			: phase == SOUL_PHASE_BIG ? 1 : 2;
	return ante * 3 + order;
}

static bool bspool_location_in_range(int ante, int phase,
		int minAnte, int minPhase, int maxAnte, int maxPhase) {
	int position = bspool_route_position(ante, phase);
	return position >= bspool_route_position(minAnte, minPhase)
		&& position <= bspool_route_position(maxAnte, maxPhase);
}

static void append_shop_pair(const Ctx *c, int ante, int humanAnte, int phase, int *cursor,
		SoulPackEvent events[8], int *n) {
	const Config *g = c->g;
	for (int i = 0; i < 2 && *cursor < c->packs_n[ante]; i++) {
		int slot = (*cursor)++;
		int pi = c->pack_idx[ante][slot];
		int soulKind = pi >= 0 ? g->boostSoul[pi] : 0;
		if (!soulKind) continue;
		SoulPackEvent *e = &events[(*n)++];
		e->soulKind = (uint8_t)soulKind;
		e->cards = (uint8_t)g->boostCards[pi];
		e->humanAnte = (uint8_t)humanAnte;
		e->phase = (uint8_t)phase;
		e->source = SOUL_SOURCE_SHOP;
	}
}

static void append_tag_reward(const Ctx *c, int ante, int kind, int blind,
		SoulPackEvent events[8], int *n) {
	if (!kind) return;
	SoulPackEvent *e = &events[(*n)++];
	e->soulKind = (uint8_t)kind;
	e->cards = (uint8_t)c->g->tagRewardCards[kind];
	e->humanAnte = (uint8_t)ante;
	e->phase = (uint8_t)(blind == 0 ? SOUL_PHASE_SMALL : SOUL_PHASE_BIG);
	e->source = (uint8_t)(kind == 1 ? SOUL_SOURCE_CHARM : SOUL_SOURCE_ETHEREAL);
}

/* Build one Ante's opened-pack timeline. Antes 2+ begin with the previous
 * boss's entry shop; each played Small/Big then contributes its two shop pack
 * offers, while a skipped Charm/Ethereal blind contributes its immediate tag
 * reward instead. Shop-pack RNG advances remain compressed across skips. */
static int soul_pack_events(Ctx *c, int ante, SoulPackEvent events[8]) {
	sim_packs(c, ante, 6);
	int cursor = 0, n = 0;
	if (ante >= 2) append_shop_pair(c, ante, ante - 1, SOUL_PHASE_BOSS, &cursor, events, &n);
	if (c->skipSm[ante]) append_tag_reward(c, ante, c->rewardSm[ante], 0, events, &n);
	else append_shop_pair(c, ante, ante, SOUL_PHASE_SMALL, &cursor, events, &n);
	if (c->skipBig[ante]) append_tag_reward(c, ante, c->rewardBig[ante], 1, events, &n);
	else append_shop_pair(c, ante, ante, SOUL_PHASE_BIG, &cursor, events, &n);
	return n;
}

static bool ctx_voucher_owned_for_soul_event(const Ctx *c, int voucherIndex,
		const SoulPackEvent *event) {
	if (voucherIndex < 0 || voucherIndex >= c->g->nvouch) return false;
	if (c->g->vouchInitiallyOwned[voucherIndex]) return true;
	if (!(c->poolVoucherPurchased & (UINT64_C(1) << voucherIndex))) return false;
	int ante = c->poolVoucherPurchaseAnte[voucherIndex];
	int visit = c->poolVoucherPurchaseVisit[voucherIndex];
	int purchaseAnte, purchasePhase;
	if (ante == 1 && visit == 1) {
		purchaseAnte = 1;
		purchasePhase = c->skipSm[1] ? SOUL_PHASE_BIG : SOUL_PHASE_SMALL;
	} else if (visit == 1 && ante >= 2) {
		purchaseAnte = ante - 1;
		purchasePhase = SOUL_PHASE_BOSS;
	} else return false;
	return bspool_route_position(event->humanAnte, event->phase)
			>= bspool_route_position(purchaseAnte, purchasePhase);
}

static void soul_event_location(char *out, size_t outsz, const SoulPackEvent *event) {
	const char *phase = event->phase == SOUL_PHASE_BOSS ? "Boss"
			: event->phase == SOUL_PHASE_SMALL ? "Sm" : "Big";
	const char *source = event->source == SOUL_SOURCE_SHOP ? "Shop"
			: event->source == SOUL_SOURCE_CHARM ? "Charm" : "Ethereal";
	snprintf(out, outsz, "LegA%d%s%s", event->humanAnte, source, phase);
}

/* One ante's joker sequence: shop slots then (separately) buffoon-pack cards.
 * Mirrors simulateShopJokers / simulatePackJokers including the rarity-skip
 * and the needNeg edition rolls. seqKey[i] == NULL means "pick skipped". */
typedef struct { const char *key; bool neg; int rarity; } SeqEnt;

static int sim_shop(Ctx *c, int a, int nslots, SeqEnt *seq) {
	const Config *g = c->g;
	int m = 0;
	for (int slot = 0; slot < nslots; slot++) {
		double ctr = psr(c, stream_next(c, &c->cdt[a], K_CDT[a])) * 28.0;
		if (ctr < 20.0) {
			double rr = psr(c, stream_next(c, &c->rarity_sho[a], K_RSHO[a]));
			int rarity = rr > 0.95 ? 3 : (rr > 0.7 ? 2 : 1);
			const char *chosen = NULL;
			if (g->wanted[rarity]) {
				int idx = pick_culled(c, &c->joker_sho[rarity][a], K_JSHO[rarity][a],
						RB_JSHO(rarity, a), g->jokerAvail[rarity], g->njoker[rarity], -1);
				if (idx < 0) return -1;
				chosen = g->jokerKey[rarity][idx];
			}
			bool neg = false;
			if (g->needNeg) neg = psr_gt_997(c,
					stream_next(c, &c->edisho[a], K_EDISHO[a]));
			seq[m].key = chosen; seq[m].neg = neg; seq[m].rarity = rarity; m++;
		}
	}
	return m;
}

static int sim_pack_jokers(Ctx *c, int a, SeqEnt *seq) {
	const Config *g = c->g;
	int nslots = g->packSlots > 0 ? g->packSlots : 2;
	sim_packs(c, a, nslots);
	/* the memo may hold MORE slots than this consumer's window (filled by the
	 * legendary-anywhere scan) and the ante may physically have FEWER */
	if (nslots > c->packs_n[a]) nslots = c->packs_n[a];
	int m = 0;
	for (int slot = 0; slot < nslots; slot++) {
		int pi = c->pack_idx[a][slot];
		int ncards;
		if (pi == PACK_FORCED) ncards = 2; /* forced normal Buffoon */
		else if (pi >= 0 && g->boostBuf[pi]) ncards = g->boostCards[pi];
		else continue;
		for (int card = 0; card < ncards; card++) {
			double rr = psr(c, stream_next(c, &c->rarity_buf[a], K_RBUF[a]));
			int rarity = rr > 0.95 ? 3 : (rr > 0.7 ? 2 : 1);
			const char *chosen = NULL;
			if (g->wanted[rarity]) {
				int idx = pick_culled(c, &c->joker_buf[rarity][a], K_JBUF[rarity][a],
						RB_JBUF(rarity, a), g->jokerAvail[rarity], g->njoker[rarity], -1);
				if (idx < 0) return -1;
				chosen = g->jokerKey[rarity][idx];
			}
			bool neg = false;
			if (g->needNeg) neg = psr_gt_997(c,
					stream_next(c, &c->edibuf[a], K_EDIBUF[a]));
			seq[m].key = chosen; seq[m].neg = neg; seq[m].rarity = rarity; m++;
		}
	}
	return m;
}

/* rollVoucherSequence + checkVoucherSearch. mode: 1-8 exact ante, 0 = any of
 * antes 1-4, -1 = any of antes 1-8. */
static bool check_voucher(Ctx *c) {
	const Config *g = c->g;
	int mode = g->voucherAnte;
	int any_to = (mode == 0) ? 4 : (mode == -1 ? 8 : 0);
	int max_ante = any_to ? any_to : mode;
	bool any = false;
	for (int a = 1; a <= max_ante; a++) {
		int idx = pick_culled(c, &c->voucher[a], K_VOUCHER[a], RB_VOUCHER(a),
				g->vouchAvail, g->nvouch, -1);
		if (idx < 0) return false;
		if (any_to) {
			if (!strcmp(g->vouchKey[idx], g->voucher)) any = true;
		} else if (a == mode) {
			return !strcmp(g->vouchKey[idx], g->voucher);
		}
	}
	return any_to ? any : false;
}

/* Source-verified tag roll (get_next_tag_key): index pick over the culled
 * pool (requires-undiscovered / min_ante > ante => UNAVAILABLE) with
 * 'Tag'..ante..'_resample'..it resamples. One call = one blind's tag. */
static int roll_tag_at(Ctx *c, int a, int blind) {
	if (a < 1 || a > MAX_SEARCH_ANTE || blind < 0 || blind > 1) return -1;
	if (c->tagRollDone[a][blind]) return c->tagRoll[a][blind];
	/* Big is the second advance of Tag<a>; materialize Small first if this
	 * caller asks for Big before another predicate has read Small. */
	if (blind == 1 && !c->tagRollDone[a][0] && roll_tag_at(c, a, 0) < 0) return -1;
	const Config *g = c->g;
	int idx = psr_n(c, stream_next(c, &c->tagS[a], KT_TAG[a]), g->ntags);
	int it = 1;
	while (idx > 0 && !(g->tagReqOk[idx - 1] && (g->tagMinAnte[idx - 1] == 0 || g->tagMinAnte[idx - 1] <= a))) {
		it++;
		if (it - 2 >= MAX_RESAMPLE) return -1;
		double sv = stream_next(c, &c->tagResample[a][it - 2], RS_TAG[a][it - 2]);
		idx = psr_n(c, sv, g->ntags);
	}
	idx = idx > 0 ? idx - 1 : -1;
	c->tagRoll[a][blind] = idx;
	c->tagRollDone[a][blind] = 1;
	return idx;
}

/* Merge every collected tag route embedded in the selected pool into this
 * candidate's physical shop layout. Pool membership already guarantees the
 * counts; replaying the deterministic tag rolls identifies WHICH blinds are
 * skipped. Observe-only rules intentionally contribute no skips. */
static bool apply_pool_route(Ctx *c) {
	const Config *g = c->g;
	int counts[MAX_POOL_ROUTE_RULES] = { 0 };
	int maxAnte = 0;
	for (int r = 0; r < g->npoolRouteRules; r++)
		if (g->poolRouteRules[r].maxAnte > maxAnte) maxAnte = g->poolRouteRules[r].maxAnte;
	for (int a = 1; a <= maxAnte; a++) {
		for (int blind = 0; blind < 2; blind++) {
			int phase = blind == 0 ? SOUL_PHASE_SMALL : SOUL_PHASE_BIG;
			int needRoll = 0;
			for (int r = 0; r < g->npoolRouteRules; r++) {
				if (g->poolRouteRules[r].collect && counts[r] < g->poolRouteRules[r].minCount
						&& bspool_location_in_range(a, phase,
								g->poolRouteRules[r].minAnte, g->poolRouteRules[r].minPhase,
								g->poolRouteRules[r].maxAnte, g->poolRouteRules[r].maxPhase)) {
					needRoll = 1; break;
				}
			}
			if (!needRoll) continue;
			int idx = roll_tag_at(c, a, blind);
			if (idx < 0) return false;
			for (int r = 0; r < g->npoolRouteRules; r++) {
				if (!g->poolRouteRules[r].collect
						|| counts[r] >= g->poolRouteRules[r].minCount
						|| !bspool_location_in_range(a, phase,
								g->poolRouteRules[r].minAnte, g->poolRouteRules[r].minPhase,
								g->poolRouteRules[r].maxAnte, g->poolRouteRules[r].maxPhase)
						|| idx != g->poolRouteRules[r].poolIndex) continue;
				counts[r]++;
				int reward = tag_reward_kind(g, idx);
				if (blind == 0) {
					c->skipSm[a] = 1;
					if (reward) c->rewardSm[a] = (uint8_t)reward;
				} else {
					c->skipBig[a] = 1;
					if (reward) c->rewardBig[a] = (uint8_t)reward;
				}
			}
		}
	}
	return true;
}

/* Revalidate cumulative voucher criteria embedded in a selected .bspool.
 * This mirrors the standalone builder's fresh-run route search: an unbought
 * offer remains eligible, purchases dynamically unlock upgrades, exclusions
 * remove only the buy edge, and Hieroglyph/Petroglyph create another visit
 * to the same displayed Ante and VoucherN stream. */
typedef struct {
	Stream main;
	Stream resample[MAX_RESAMPLE];
	int nresample;
	uint8_t visits;
} CtxVoucherUndo;

typedef struct {
	int found, purchases;
	int maxAnte, requireOmen, requirePoolLegends, requireActiveLegend, omenIndex;
	int forbidAnteReducers;
	uint64_t purchased;
	uint8_t purchaseAnte[MAX_VOUCH], purchaseVisit[MAX_VOUCH];
} CtxVoucherBest;

static void reset_pool_legend_streams(Ctx *c);
static bool check_pool_legend_rules(Ctx *c);
static bool check_legendary_anywhere(Ctx *c, const char **locOut);
static uint16_t active_omen_activation_mask(Ctx *c, int omenIndex,
		int routeMaxAnte, uint16_t candidates);

static bool pool_voucher_excluded(const Config *g, int index) {
	for (int i = 0; i < g->npoolVoucherExclusions; i++)
		if (g->poolVoucherExclusions[i] == index) return true;
	return false;
}

/* Return the first draw from VoucherN or one independent resample key. This
 * is valid for reducer-free routes: each displayed Ante is visited once, so
 * purchased state only selects which immutable raw catalog value survives. */
static int route_raw_voucher(Ctx *c, int ante, int position) {
	if (ante < 1 || ante > 8 || position < 0 || position > MAX_RESAMPLE)
		return -1;
	if (c->voucherRawGen[ante] != c->gen) {
		c->voucherRawGen[ante] = c->gen;
		c->voucherRawN[ante] = 0;
	}
	while (c->voucherRawN[ante] <= position) {
		int at = c->voucherRawN[ante];
		const char *key = at == 0 ? KT_VOUCHER[ante]
				: RS_VOUCHER[ante][at - 1];
		double state = pseudohash_ks(key, c->seed);
		state = round13(lua_mod1(2.134453429141 + state * 1.72431234));
		int index = psr_n(c, (state + c->hashed_seed) / 2.0, c->g->nvouch);
		if (index < 1 || index > c->g->nvouch) return -1;
		c->voucherRaw[ante][at] = (uint8_t)index;
		c->voucherRawN[ante]++;
#ifdef BRAINSTORM_VERIFY_INTERACTIVE_VOUCHER_RAW
		Stream referenceState = { 0 };
		int reference = psr_n(c, stream_next(c, &referenceState, key), c->g->nvouch);
		if (index != reference) {
			fprintf(stderr,
					"interactive voucher raw mismatch seed=%s ante=%d position=%d cached=%d reference=%d\n",
					c->seed, ante, at, index, reference);
			abort();
		}
#endif
	}
	return c->voucherRaw[ante][position];
}

static bool prefill_omen_voucher_raw(Ctx *c, int maxAnte) {
	double seedval[PRNG_BATCH_MAX];
	int result[PRNG_BATCH_MAX], anteOf[PRNG_BATCH_MAX];
	int count = 0;
	for (int ante = 1; ante <= maxAnte; ante++) {
		if (c->voucherRawGen[ante] != c->gen) {
			c->voucherRawGen[ante] = c->gen;
			c->voucherRawN[ante] = 0;
		}
		if (!c->voucherRawN[ante]) anteOf[count++] = ante;
	}
	if (!count) return true;
	for (int lane = 0; lane < count; lane++) {
		int ante = anteOf[lane];
		double state = pseudohash_ks(KT_VOUCHER[ante], c->seed);
		state = round13(lua_mod1(2.134453429141 + state * 1.72431234));
		seedval[lane] = (state + c->hashed_seed) / 2.0;
	}
	lj_random_seed_one_n_batch(seedval, count, c->g->nvouch, result);
	for (int lane = 0; lane < count; lane++) {
		if (result[lane] < 1 || result[lane] > c->g->nvouch) return false;
		int ante = anteOf[lane];
		c->voucherRaw[ante][0] = (uint8_t)result[lane];
		c->voucherRawN[ante] = 1;
#ifdef BRAINSTORM_VERIFY_INTERACTIVE_VOUCHER_RAW
		int reference = psr_n(c, seedval[lane], c->g->nvouch);
		if (result[lane] != reference) {
			fprintf(stderr,
					"interactive voucher raw batch mismatch seed=%s ante=%d cached=%d reference=%d\n",
					c->seed, ante, result[lane], reference);
			abort();
		}
#endif
	}
	return true;
}

static int route_raw_offer(Ctx *c, int ante, uint64_t availMask) {
	if (!availMask) return -1;
	for (int position = 0; position <= MAX_RESAMPLE; position++) {
		int raw = route_raw_voucher(c, ante, position);
		if (raw < 1 || raw > c->g->nvouch) return -1;
		int index = raw - 1;
		if (availMask & (UINT64_C(1) << index)) return index;
	}
	return -1;
}

typedef struct {
	uint64_t purchased, availMask;
	uint16_t buyMask, order;
	uint8_t purchases, omenAnte;
} CtxOmenRouteState;

typedef struct {
	uint16_t feasibleMask;
	uint8_t found[9];
	CtxOmenRouteState best[9];
} CtxOmenRoutes;

static int omen_state_better(const CtxOmenRouteState *a,
		const CtxOmenRouteState *b) {
	return a->purchases < b->purchases
			|| (a->purchases == b->purchases && a->order < b->order);
}

static uint64_t pool_voucher_exclusion_mask(const Config *g) {
	uint64_t mask = 0;
	for (int i = 0; i < g->npoolVoucherExclusions; i++) {
		int index = g->poolVoucherExclusions[i];
		if (index >= 0 && index < 64) mask |= UINT64_C(1) << index;
	}
	return mask;
}

/* Exact skip/buy frontier for the common no-predicate Omen fallback. It
 * retains the generic DFS's minimum-purchase, skip-first winner separately
 * for every possible Omen purchase Ante. */
static bool collect_omen_routes(Ctx *c, int maxAnte, CtxOmenRoutes *routes) {
	const Config *g = c->g;
	memset(routes, 0, sizeof *routes);
	if (maxAnte < 1 || maxAnte > 8 || g->omenVoucherIdx < 0
			|| g->omenVoucherIdx >= g->nvouch || g->nvouch > 64) return false;
	uint64_t omenBit = UINT64_C(1) << g->omenVoucherIdx;
	if (g->vouchInitiallyOwnedMask & omenBit) return false;
	uint64_t forbidden = pool_voucher_exclusion_mask(g) | g->vouchReducerMask;
	if (g->omenPrereqMask & (forbidden | ~g->vouchRouteEligibleMask)) return false;
	if (!prefill_omen_voucher_raw(c, maxAnte)) return false;

	enum { FRONTIER_CAP = 1 << 8 };
	uint64_t purchasedA[FRONTIER_CAP], purchasedB[FRONTIER_CAP];
	uint64_t availA[FRONTIER_CAP], availB[FRONTIER_CAP];
	uint16_t buyA[FRONTIER_CAP], buyB[FRONTIER_CAP];
	uint16_t orderA[FRONTIER_CAP], orderB[FRONTIER_CAP];
	uint64_t *curPurchased = purchasedA, *nextPurchased = purchasedB;
	uint64_t *curAvail = availA, *nextAvail = availB;
	uint16_t *curBuy = buyA, *nextBuy = buyB;
	uint16_t *curOrder = orderA, *nextOrder = orderB;
	int ncur = 1;
	curPurchased[0] = g->vouchInitiallyOwnedMask;
	curAvail[0] = g->vouchInitialAvailMask;
	curBuy[0] = 0;
	curOrder[0] = 0;
	for (int ante = 1; ante <= maxAnte && ncur; ante++) {
		int nnext = 0;
		int visible = ante != 1 || !c->skipSm[1] || !c->skipBig[1];
		for (int si = 0; si < ncur; si++) {
			uint64_t purchased = curPurchased[si];
			uint64_t availMask = curAvail[si];
			uint16_t buyMask = curBuy[si];
			uint16_t order = curOrder[si];
			int missing = __builtin_popcountll(g->omenPrereqMask & ~purchased);
			if (missing > maxAnte - ante + 1) continue;
			int index = route_raw_offer(c, ante, availMask);
			if (index < 0) continue;
			nextPurchased[nnext] = purchased;
			nextAvail[nnext] = availMask;
			nextBuy[nnext] = buyMask;
			nextOrder[nnext] = (uint16_t)(order << 1);
			nnext++;
			uint64_t bit = UINT64_C(1) << index;
			if (!visible || (forbidden & bit)) continue;
			uint64_t bought = purchased | bit;
			uint64_t boughtAvail = (availMask & ~bit)
					| (g->vouchUnlocksMask[index]
							& g->vouchRouteEligibleMask & ~bought);
			uint16_t boughtMask = buyMask | (UINT16_C(1) << (ante - 1));
			uint16_t boughtOrder = (uint16_t)((order << 1) | 1u);
			if (index == g->omenVoucherIdx) {
				int valid = 1;
				for (int later = ante + 1; later <= maxAnte; later++) {
					if (route_raw_offer(c, later, boughtAvail) < 0) {
						valid = 0;
						break;
					}
				}
				if (!valid) continue;
				CtxOmenRouteState state = {
					.purchased = bought, .availMask = boughtAvail,
					.buyMask = boughtMask,
					.order = (uint16_t)(boughtOrder << (maxAnte - ante)),
					.purchases = (uint8_t)__builtin_popcount((unsigned)boughtMask),
					.omenAnte = (uint8_t)ante,
				};
				if (!routes->found[ante]
						|| omen_state_better(&state, &routes->best[ante])) {
					routes->found[ante] = 1;
					routes->best[ante] = state;
					routes->feasibleMask |= UINT16_C(1) << (ante - 1);
				}
				continue;
			}
			nextPurchased[nnext] = bought;
			nextAvail[nnext] = boughtAvail;
			nextBuy[nnext] = boughtMask;
			nextOrder[nnext] = boughtOrder;
			nnext++;
		}
		uint64_t *tmp64 = curPurchased;
		curPurchased = nextPurchased; nextPurchased = tmp64;
		tmp64 = curAvail; curAvail = nextAvail; nextAvail = tmp64;
		uint16_t *tmp16 = curBuy;
		curBuy = nextBuy; nextBuy = tmp16;
		tmp16 = curOrder; curOrder = nextOrder; nextOrder = tmp16;
		ncur = nnext;
	}
	return routes->feasibleMask != 0;
}

static const CtxOmenRouteState *best_omen_route(
		const CtxOmenRoutes *routes, uint16_t timings) {
	const CtxOmenRouteState *best = NULL;
	for (int ante = 1; ante <= 8; ante++) {
		if (!(timings & (UINT16_C(1) << (ante - 1))) || !routes->found[ante])
			continue;
		const CtxOmenRouteState *candidate = &routes->best[ante];
		if (!best || omen_state_better(candidate, best)) best = candidate;
	}
	return best;
}

static bool materialize_omen_route(Ctx *c, const CtxOmenRouteState *state,
		int maxAnte, CtxVoucherBest *best) {
	const Config *g = c->g;
	uint64_t purchased = g->vouchInitiallyOwnedMask;
	uint64_t availMask = g->vouchInitialAvailMask;
	memset(best->purchaseAnte, 0, sizeof best->purchaseAnte);
	memset(best->purchaseVisit, 0, sizeof best->purchaseVisit);
	for (int ante = 1; ante <= maxAnte; ante++) {
		int index = route_raw_offer(c, ante, availMask);
		if (index < 0) return false;
		if (!(state->buyMask & (UINT16_C(1) << (ante - 1)))) continue;
		best->purchaseAnte[index] = (uint8_t)ante;
		best->purchaseVisit[index] = 1;
		uint64_t bit = UINT64_C(1) << index;
		purchased |= bit;
		availMask = (availMask & ~bit)
				| (g->vouchUnlocksMask[index]
						& g->vouchRouteEligibleMask & ~purchased);
	}
	if (purchased != state->purchased) return false;
	best->found = 1;
	best->purchases = state->purchases;
	best->purchased = purchased;
	return true;
}

static int roll_pool_voucher(Ctx *c, int ante, uint64_t purchased,
		CtxVoucherUndo *undo, int firstVisitOnly) {
	const Config *g = c->g;
	if (ante < 1 || ante > MAX_SEARCH_ANTE || g->nvouch < 1 || g->nvouch > 64) {
		*undo = (CtxVoucherUndo){0};
		return -1;
	}
	const char *key = KT_VOUCHER[ante];
	undo->main = c->voucher[ante];
	undo->nresample = 0;
	undo->visits = c->poolVoucherVisits[ante];
	c->poolVoucherVisits[ante]++;

	/* Bit i is available iff route-eligible, not yet purchased, and its
	 * prerequisite (if any) is owned -- identical to the old per-entry
	 * catalog walk, assembled from masks precomputed at config load. */
	uint64_t unlocked = 0;
	for (uint64_t owned = purchased; owned; owned &= owned - 1)
		unlocked |= g->vouchUnlocksMask[__builtin_ctzll(owned)];
	uint64_t availMask = g->vouchRouteEligibleMask & ~purchased
			& (g->vouchNoPrereqMask | unlocked);
	if (!availMask) return -1;

	int idx = firstVisitOnly ? route_raw_voucher(c, ante, 0)
			: psr_n(c, stream_next(c, &c->voucher[ante], key), g->nvouch);
	int it = 1;
	while (idx > 0 && !(availMask >> (idx - 1) & 1)) {
		it++;
		if (it - 2 >= MAX_RESAMPLE) return -1;
		if (firstVisitOnly) {
			idx = route_raw_voucher(c, ante, it - 1);
		} else {
			undo->resample[undo->nresample++] = c->poolVoucherResample[ante][it - 2];
			idx = psr_n(c, stream_next(c, &c->poolVoucherResample[ante][it - 2],
					RS_VOUCHER[ante][it - 2]), g->nvouch);
		}
	}
	return idx > 0 ? idx - 1 : -1;
}

static void undo_pool_voucher(Ctx *c, int ante, const CtxVoucherUndo *undo) {
	c->voucher[ante] = undo->main;
	c->poolVoucherVisits[ante] = undo->visits;
	for (int i = 0; i < undo->nresample; i++)
		c->poolVoucherResample[ante][i] = undo->resample[i];
}

static void search_pool_voucher_route(Ctx *c, int ante, uint64_t purchased,
		uint32_t matched, int purchases, CtxVoucherBest *best) {
	const Config *g = c->g;
	if (best->requireOmen && best->forbidAnteReducers
			&& best->omenIndex >= 0
			&& !(purchased & (UINT64_C(1) << best->omenIndex))) {
		int missing = __builtin_popcountll(g->omenPrereqMask & ~purchased);
		if (missing > best->maxAnte - ante + 1) return;
	}
	uint32_t all = (UINT32_C(1) << g->npoolVoucherRules) - 1u;
	if (best->found && purchases > best->purchases) return;
	for (int i = 0; i < g->npoolVoucherRules; i++)
		if (!(matched & (UINT32_C(1) << i))
				&& ante > g->poolVoucherRules[i].maxAnte) return;
	if (ante > best->maxAnte) {
		if (matched == all && (!best->found || purchases < best->purchases)) {
			if (best->requireOmen && (best->omenIndex < 0
					|| !(purchased & (UINT64_C(1) << best->omenIndex)))) return;
			if (best->requirePoolLegends || best->requireActiveLegend) {
				c->poolVoucherPurchased = purchased;
				bool valid = true;
				if (best->requirePoolLegends) {
					reset_pool_legend_streams(c);
					valid = check_pool_legend_rules(c);
				}
				if (valid && best->requireActiveLegend) {
					const char *unusedLocation = NULL;
					reset_pool_legend_streams(c);
					valid = check_legendary_anywhere(c, &unusedLocation);
				}
				reset_pool_legend_streams(c);
				if (!valid) return;
			}
			best->found = 1;
			best->purchases = purchases;
			best->purchased = purchased;
			memcpy(best->purchaseAnte, c->poolVoucherPurchaseAnte,
					sizeof best->purchaseAnte);
			memcpy(best->purchaseVisit, c->poolVoucherPurchaseVisit,
					sizeof best->purchaseVisit);
		}
		return;
	}

	CtxVoucherUndo undo;
	int index = roll_pool_voucher(c, ante, purchased, &undo,
			best->forbidAnteReducers);
	if (index < 0) {
		undo_pool_voucher(c, ante, &undo);
		return;
	}
	uint8_t visit = c->poolVoucherVisits[ante];
	int visible = ante != 1 || visit != 1 || !c->skipSm[1] || !c->skipBig[1];
	uint32_t offered = 0;
	for (int i = 0; visible && i < g->npoolVoucherRules; i++)
		if (index == g->poolVoucherRules[i].poolIndex
				&& ante >= g->poolVoucherRules[i].minAnte
				&& ante <= g->poolVoucherRules[i].maxAnte)
			offered |= UINT32_C(1) << i;
	uint32_t nextMatched = matched | offered;

	search_pool_voucher_route(c, ante + 1, purchased, nextMatched,
			purchases, best);
	int needMore = nextMatched != all
			|| (best->requireOmen && (best->omenIndex < 0
				|| !(purchased & (UINT64_C(1) << best->omenIndex))));
	if (visible && needMore
			&& (!best->found || purchases + 1 <= best->purchases)
			&& !pool_voucher_excluded(g, index)) {
		if (best->forbidAnteReducers && g->vouchIsReducer[index])
			goto skip_pool_voucher_buy;
		uint8_t oldAnte = c->poolVoucherPurchaseAnte[index];
		uint8_t oldVisit = c->poolVoucherPurchaseVisit[index];
		c->poolVoucherPurchaseAnte[index] = (uint8_t)ante;
		c->poolVoucherPurchaseVisit[index] = visit;
		int repeatsAnte = g->vouchIsReducer[index];
		search_pool_voucher_route(c, repeatsAnte ? ante : ante + 1,
				purchased | (UINT64_C(1) << index), nextMatched,
				purchases + 1, best);
		c->poolVoucherPurchaseAnte[index] = oldAnte;
		c->poolVoucherPurchaseVisit[index] = oldVisit;
	}
skip_pool_voucher_buy:
	undo_pool_voucher(c, ante, &undo);
}

/* Cheap Omen-timing gate. Omen affects the Soul streams only through its
 * visit-1 activation Ante (reducers are never bought in legend-recovery
 * modes, and any other purchased voucher is invisible to the Soul oracle),
 * so if no hypothetical activation Ante can produce the required result the
 * expensive purchase-tree DFS cannot succeed and must not run at all. */
static bool omen_timing_possible(Ctx *c, int omenIndex, int maxAnte,
		int requirePoolLegends, int requireActiveLegend) {
	if (omenIndex < 0) return false;
	uint64_t oldPurchased = c->poolVoucherPurchased;
	uint8_t oldAnte = c->poolVoucherPurchaseAnte[omenIndex];
	uint8_t oldVisit = c->poolVoucherPurchaseVisit[omenIndex];
	bool possible = false;
	for (int ante = 1; ante <= maxAnte && !possible; ante++) {
		c->poolVoucherPurchased = oldPurchased | (UINT64_C(1) << omenIndex);
		c->poolVoucherPurchaseAnte[omenIndex] = (uint8_t)ante;
		c->poolVoucherPurchaseVisit[omenIndex] = 1;
		bool valid = true;
		if (requirePoolLegends) {
			reset_pool_legend_streams(c);
			valid = check_pool_legend_rules(c);
		}
		if (valid && requireActiveLegend) {
			const char *unusedLocation = NULL;
			reset_pool_legend_streams(c);
			valid = check_legendary_anywhere(c, &unusedLocation);
		}
		possible = valid;
	}
	c->poolVoucherPurchased = oldPurchased;
	c->poolVoucherPurchaseAnte[omenIndex] = oldAnte;
	c->poolVoucherPurchaseVisit[omenIndex] = oldVisit;
	reset_pool_legend_streams(c);
	return possible;
}

static bool check_pool_voucher_route_mode(Ctx *c, int requireOmen,
		int requirePoolLegends, int requireActiveLegend, int routeMaxAnte) {
	const Config *g = c->g;
	if (!g->npoolVoucherRules && !requireOmen
			&& !requirePoolLegends && !requireActiveLegend) return true;
	memset(c->voucher, 0, sizeof c->voucher);
	memset(c->poolVoucherResample, 0, sizeof c->poolVoucherResample);
	memset(c->poolVoucherVisits, 0, sizeof c->poolVoucherVisits);
	memset(c->poolVoucherPurchaseAnte, 0, sizeof c->poolVoucherPurchaseAnte);
	memset(c->poolVoucherPurchaseVisit, 0, sizeof c->poolVoucherPurchaseVisit);
	c->poolVoucherPurchased = 0;
	CtxVoucherBest best = {
		.purchases = INT_MAX,
		.maxAnte = routeMaxAnte,
		.requireOmen = requireOmen,
		.requirePoolLegends = requirePoolLegends,
		.requireActiveLegend = requireActiveLegend,
		.omenIndex = g->omenVoucherIdx,
		.forbidAnteReducers = requirePoolLegends || requireActiveLegend
				|| g->npoolRouteRules,
	};
	if (best.maxAnte < g->poolVoucherMaxAnte) best.maxAnte = g->poolVoucherMaxAnte;
	if (best.maxAnte < 1 || best.maxAnte > 8) return false;
	if (requireOmen && best.omenIndex < 0) return false;
	if (requireOmen && (requirePoolLegends || requireActiveLegend))
		c->omenRoutePossible = -1;
	uint64_t initialPurchased = g->vouchInitiallyOwnedMask;
	int haveBest = 0;
	/* Active legAnywhere without embedded voucher predicates is the common
	 * in-game fallback. Establish exact route reachability first, then test
	 * only those Omen timings against the shared Soul trace. */
	if (requireOmen && requireActiveLegend && !requirePoolLegends
			&& !g->npoolVoucherRules && best.forbidAnteReducers) {
		CtxOmenRoutes routes;
		if (!collect_omen_routes(c, best.maxAnte, &routes)) {
			/* Adding a Charm skip can only hide the initial A1 voucher; it cannot
			 * reveal an offer or buy edge. */
			c->omenRoutePossible = 0;
			return false;
		}
		c->omenRoutePossible = 1;
		uint16_t validTimings = active_omen_activation_mask(c,
				best.omenIndex, best.maxAnte, routes.feasibleMask);
		if (!validTimings) return false;
		const CtxOmenRouteState *chosen = best_omen_route(&routes, validTimings);
		if (!chosen || !materialize_omen_route(c, chosen, best.maxAnte, &best))
			return false;
#ifdef BRAINSTORM_VERIFY_INTERACTIVE_OMEN_FRONTIER
		/* Differentially prove both reachability and the minimum-purchase,
		 * skip-first winner against the original recursive route engine. */
		CtxVoucherBest fastBest = best;
		memset(c->voucher, 0, sizeof c->voucher);
		memset(c->poolVoucherResample, 0, sizeof c->poolVoucherResample);
		memset(c->poolVoucherVisits, 0, sizeof c->poolVoucherVisits);
		memset(c->poolVoucherPurchaseAnte, 0,
				sizeof c->poolVoucherPurchaseAnte);
		memset(c->poolVoucherPurchaseVisit, 0,
				sizeof c->poolVoucherPurchaseVisit);
		c->poolVoucherPurchased = 0;
		CtxVoucherBest referenceBest = {
			.purchases = INT_MAX,
			.maxAnte = best.maxAnte,
			.requireOmen = requireOmen,
			.requirePoolLegends = requirePoolLegends,
			.requireActiveLegend = requireActiveLegend,
			.omenIndex = best.omenIndex,
			.forbidAnteReducers = best.forbidAnteReducers,
		};
		search_pool_voucher_route(c, 1, initialPurchased, 0, 0,
				&referenceBest);
		if (!referenceBest.found
				|| referenceBest.purchases != fastBest.purchases
				|| referenceBest.purchased != fastBest.purchased
				|| memcmp(referenceBest.purchaseAnte, fastBest.purchaseAnte,
					sizeof fastBest.purchaseAnte)
				|| memcmp(referenceBest.purchaseVisit, fastBest.purchaseVisit,
					sizeof fastBest.purchaseVisit)) {
			fprintf(stderr,
					"interactive Omen frontier mismatch seed=%s fast_purchases=%d reference_found=%d reference_purchases=%d\n",
					c->seed, fastBest.purchases, referenceBest.found,
					referenceBest.purchases);
			abort();
		}
		best = fastBest;
#endif
		haveBest = 1;
	}
	if (!haveBest) {
		if (requireOmen && (requirePoolLegends || requireActiveLegend)
				&& !omen_timing_possible(c, best.omenIndex, best.maxAnte,
						requirePoolLegends, requireActiveLegend)) return false;
		search_pool_voucher_route(c, 1, initialPurchased, 0, 0, &best);
	}
	memset(c->voucher, 0, sizeof c->voucher);
	memset(c->poolVoucherResample, 0, sizeof c->poolVoucherResample);
	memset(c->poolVoucherVisits, 0, sizeof c->poolVoucherVisits);
	if (!best.found) return false;
	c->poolVoucherPurchased = best.purchased;
	memcpy(c->poolVoucherPurchaseAnte, best.purchaseAnte,
			sizeof c->poolVoucherPurchaseAnte);
	memcpy(c->poolVoucherPurchaseVisit, best.purchaseVisit,
			sizeof c->poolVoucherPurchaseVisit);
	return true;
}

static bool check_pool_voucher_route(Ctx *c) {
	return check_pool_voucher_route_mode(c, 0, 0, 0,
			c->g->poolVoucherMaxAnte);
}

/* Pool criteria and active overlay filters must each read their RNG streams
 * from the beginning, while sharing the final merged blind-skip route. Reset
 * only the streams touched by the pool's cumulative Soul oracle; tag-roll
 * caches and skip arrays intentionally survive. */
static void reset_pool_legend_streams(Ctx *c) {
	memset(&c->joker4, 0, sizeof c->joker4);
	memset(c->resample[RB_JOKER4], 0, sizeof c->resample[RB_JOKER4]);
	memset(c->soulT, 0, sizeof c->soulT);
	memset(c->soulS, 0, sizeof c->soulS);
	memset(c->edisouA, 0, sizeof c->edisouA);
	memset(&c->omen, 0, sizeof c->omen);
	memset(c->packs_gen, 0, sizeof c->packs_gen);
	memset(c->packs_n, 0, sizeof c->packs_n);
}

/* Re-verify every exact Soul rule embedded in the selected pool on the final
 * route (pool collection + active overlay skips). Membership alone proves the
 * source route; an added collected tag can remove a shop, so route-sensitive
 * Soul criteria must be evaluated again before a native hit is returned. */
static bool check_pool_legend_rules(Ctx *c) {
	const Config *g = c->g;
	if (!g->npoolLegendRules) return true;
	int first = pick_culled(c, &c->joker4, "Joker4", RB_JOKER4,
			g->jokerAvail[4], g->njoker[4], -1);
	if (first < 0) return false;
	/* Either-depth rules resolve deterministically: the exclusive Soul #2 pick
	 * can never repeat Soul #1's legendary, so the target is at depth 1 iff it
	 * IS the first pick, otherwise it must be the second. */
	int resolved[MAX_POOL_LEGEND_RULES];
	int needDepth = 1, maxAnte = 0;
	for (int i = 0; i < g->npoolLegendRules; i++) {
		int d = g->poolLegendRules[i].soulDepth;
		if (d == SOUL_DEPTH_ANY)
			d = g->poolLegendRules[i].poolIndex == first ? 1 : 2;
		resolved[i] = d;
		if (d > needDepth) needDepth = d;
		int rngMax = g->poolLegendRules[i].maxAnte
				+ (g->poolLegendRules[i].humanLocation
					&& g->poolLegendRules[i].maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
		if (rngMax > maxAnte) maxAnte = rngMax;
	}
	int second = -1;
	if (needDepth == 2) {
		second = pick_culled(c, &c->joker4, "Joker4", RB_JOKER4,
				g->jokerAvail[4], g->njoker[4], first);
		if (second < 0) return false;
	}
	for (int i = 0; i < g->npoolLegendRules; i++) {
		int chosen = resolved[i] == 1 ? first : second;
		if (chosen != g->poolLegendRules[i].poolIndex) return false;
	}

	int found = 0, eventAnte[3] = { 0 };
	SoulPackEvent eventPack[3] = { 0 };
	double eventEdition[3] = { 0.0 };
	ctx_soul_tape_prepare(c);
	uint16_t tarotAt[MAX_SEARCH_ANTE + 1] = { 0 };
	uint16_t spectralAt[MAX_SEARCH_ANTE + 1] = { 0 };
	uint8_t editionAt[MAX_SEARCH_ANTE + 1] = { 0 };
	uint32_t omenAt = 0;
	int omenIndex = g->omenVoucherIdx;
	for (int ante = 1; ante <= maxAnte && found < needDepth; ante++) {
		SoulPackEvent packs[8];
		int npacks = soul_pack_events(c, ante, packs);
		for (int slot = 0; slot < npacks && found < needDepth; slot++) {
			int kind = packs[slot].soulKind, cards = packs[slot].cards;
			if (!kind) continue;
			bool omenOwned = ctx_voucher_owned_for_soul_event(c, omenIndex, &packs[slot]);
			bool soulInPack = false, blackHoleInPack = false;
			for (int card = 0; card < cards && found < needDepth; card++) {
				int contentKind = kind;
				if (kind == 1 && omenOwned
						&& ctx_soul_tape_omen(c, omenAt++))
					contentKind = 2;
				bool soul = false;
				if (!soulInPack) {
					if (contentKind == 1)
						soul = ctx_soul_tape_tarot(c, ante, tarotAt[ante]++);
					else
						soul = ctx_soul_tape_spectral(c, ante, spectralAt[ante]++);
				}
				if (contentKind == 2 && !blackHoleInPack) {
					bool blackHole = ctx_soul_tape_spectral(c, ante,
							spectralAt[ante]++);
					if (blackHole) {
						if (g->blackHoleAllowed) blackHoleInPack = true;
						soul = false;
					}
				}
				if (!soul) continue;
				soulInPack = true;
				found++;
				eventAnte[found] = ante;
				eventPack[found] = packs[slot];
				if (g->poolLegendNeedsEdition)
					eventEdition[found] = ctx_soul_tape_edition(c, ante,
							editionAt[ante]++);
			}
		}
	}
	if (found < needDepth) return false;
	for (int i = 0; i < g->npoolLegendRules; i++) {
		int depth = resolved[i];
		if (g->poolLegendRules[i].humanLocation) {
			if (!bspool_location_in_range(eventPack[depth].humanAnte,
					eventPack[depth].phase,
					g->poolLegendRules[i].minAnte, g->poolLegendRules[i].minPhase,
					g->poolLegendRules[i].maxAnte, g->poolLegendRules[i].maxPhase)
					|| (g->poolLegendRules[i].source
						&& eventPack[depth].source != g->poolLegendRules[i].source)) return false;
		} else if (eventAnte[depth] < g->poolLegendRules[i].minAnte
				|| eventAnte[depth] > g->poolLegendRules[i].maxAnte) return false;
		if (g->poolLegendRules[i].neg && !eventEdition[depth]) return false;
	}
	return true;
}

static bool try_pool_targeted_charm(Ctx *c, int soulMaxAnte, int tagMaxAnte,
		int requireOmen) {
	const Config *g = c->g;
	/* Buying Charm cannot create an Omen route: the voucher frontier is
	 * independent of every tag reward and physical pack draw. */
	if (requireOmen && c->omenRoutePossible == 0) return false;
	int charmIndex = g->charmTagIdx;
	if (charmIndex < 0) return false;
	if (tagMaxAnte > MAX_SEARCH_ANTE) tagMaxAnte = MAX_SEARCH_ANTE;
	int routeMaxAnte = soulMaxAnte > 8 ? 8 : soulMaxAnte;
	if (routeMaxAnte < g->poolVoucherMaxAnte) routeMaxAnte = g->poolVoucherMaxAnte;
	if (routeMaxAnte < 1) routeMaxAnte = 1;
	for (int ante = 1; ante <= tagMaxAnte; ante++) {
		for (int blind = 0; blind < 2; blind++) {
			int idx = roll_tag_at(c, ante, blind);
			if (idx < 0) return false;
			uint8_t *skip = blind == 0 ? &c->skipSm[ante] : &c->skipBig[ante];
			uint8_t *reward = blind == 0 ? &c->rewardSm[ante] : &c->rewardBig[ante];
			if (idx != charmIndex || *skip) continue;
			uint8_t oldSkip = *skip, oldReward = *reward;
			int oldForced = c->forcedAnte;
			*skip = 1;
			*reward = 1;
			resolve_forced_pack_ante(c);
			if (check_pool_voucher_route_mode(c, requireOmen, 1, 0,
					routeMaxAnte)) {
				reset_pool_legend_streams(c);
				if (check_pool_legend_rules(c)) return true;
			}
			*skip = oldSkip;
			*reward = oldReward;
			c->forcedAnte = oldForced;
		}
	}
	return false;
}

/* checkLegendaryAnywhere: the run's FIRST Soul across chronological collected
 * Charm/Ethereal rewards and reachable shop packs in antes 1-8. */
static bool check_legendary_anywhere(Ctx *c, const char **locOut) {
	const Config *g = c->g;
	if (!g->soulAllowed) return false;
	ctx_soul_tape_prepare(c);
	uint16_t tarotAt[9] = { 0 }, spectralAt[9] = { 0 };
	uint8_t editionAt[9] = { 0 };
	uint32_t omenAt = 0;
	int omenIndex = g->omenVoucherIdx;
	for (int a = 1; a <= 8; a++) {
		SoulPackEvent packs[8];
		int npacks = soul_pack_events(c, a, packs);
		for (int slot = 0; slot < npacks; slot++) {
			int sk = packs[slot].soulKind;
			if (!sk) continue;
			int ncards = packs[slot].cards;
			bool omenOwned = ctx_voucher_owned_for_soul_event(c, omenIndex, &packs[slot]);
			bool soul_in_pack = false, bh_in_pack = false;
			for (int card = 0; card < ncards; card++) {
				int contentKind = sk;
				if (sk == 1 && omenOwned
						&& ctx_soul_tape_omen(c, omenAt++))
					contentKind = 2;
				bool soul = false;
				if (!soul_in_pack) {
					if (contentKind == 1)
						soul = ctx_soul_tape_tarot(c, a, tarotAt[a]++);
					else
						soul = ctx_soul_tape_spectral(c, a, spectralAt[a]++);
				}
				if (contentKind == 2 && !bh_in_pack) {
					bool bh = ctx_soul_tape_spectral(c, a, spectralAt[a]++);
					if (bh) {
						/* A banned Black Hole still consumes its roll and
						 * overwrites a same-card Soul, but it never enters the
						 * pack and therefore cannot suppress later BH rolls. */
						if (g->blackHoleAllowed) bh_in_pack = true;
						soul = false; /* black hole overwrites the soul */
					}
				}
				if (soul) {
					if (g->activeLegendaryIdx < 0) return false;
					if (g->negLegendary
							&& !ctx_soul_tape_edition(c, a, editionAt[a]++)) {
						return false;
					}
					soul_event_location(c->legloc, sizeof c->legloc, &packs[slot]);
					*locOut = c->legloc;
					return true;
				}
			}
		}
	}
	return false;
}

static bool omen_trace_same_route(const Ctx *c, const CtxOmenTrace *t,
		int maxAnte) {
	size_t bytes = (size_t)(maxAnte + 1) * sizeof(uint8_t);
	return t->routeGen == c->gen && t->routeMaxAnte == maxAnte
			&& t->forcedAnte == c->forcedAnte
			&& !memcmp(t->routeSkipSm, c->skipSm, bytes)
			&& !memcmp(t->routeSkipBig, c->skipBig, bytes)
			&& !memcmp(t->routeRewardSm, c->rewardSm, bytes)
			&& !memcmp(t->routeRewardBig, c->rewardBig, bytes);
}

static bool omen_trace_prepare(Ctx *c, int maxAnte) {
	if (maxAnte < 1 || maxAnte > MAX_SEARCH_ANTE) return false;
	if (!c->omenTrace) c->omenTrace = calloc(1, sizeof *c->omenTrace);
	CtxOmenTrace *t = c->omenTrace;
	if (!t) return false;
	ctx_soul_tape_prepare(c);
	if (omen_trace_same_route(c, t, maxAnte)) return true;
	memset(c->packs_gen, 0, sizeof c->packs_gen);
	memset(c->packs_n, 0, sizeof c->packs_n);
	for (int ante = 1; ante <= maxAnte; ante++) {
		int n = soul_pack_events(c, ante, t->event[ante]);
		if (n < 0 || n > CTX_SOUL_EVENTS_PER_ANTE) return false;
		t->eventCount[ante] = (uint8_t)n;
		for (int i = 0; i < n; i++)
			if (t->event[ante][i].cards > CTX_SOUL_CARDS_PER_EVENT) return false;
	}
	t->routeMaxAnte = maxAnte;
	t->forcedAnte = c->forcedAnte;
	size_t bytes = (size_t)(maxAnte + 1) * sizeof(uint8_t);
	memcpy(t->routeSkipSm, c->skipSm, bytes);
	memcpy(t->routeSkipBig, c->skipBig, bytes);
	memcpy(t->routeRewardSm, c->rewardSm, bytes);
	memcpy(t->routeRewardBig, c->rewardBig, bytes);
	t->routeGen = c->gen;
	memset(c->packs_gen, 0, sizeof c->packs_gen);
	memset(c->packs_n, 0, sizeof c->packs_n);
	return true;
}

static bool omen_trace_owned_at(const Ctx *c, int purchaseAnte,
		const SoulPackEvent *event) {
	int omenIndex = c->g->omenVoucherIdx;
	if (omenIndex >= 0 && omenIndex < c->g->nvouch
			&& c->g->vouchInitiallyOwned[omenIndex]) return true;
	int humanAnte, phase;
	if (purchaseAnte == 1) {
		humanAnte = 1;
		phase = c->skipSm[1] ? SOUL_PHASE_BIG : SOUL_PHASE_SMALL;
	} else {
		humanAnte = purchaseAnte - 1;
		phase = SOUL_PHASE_BOSS;
	}
	return bspool_route_position(event->humanAnte, event->phase)
			>= bspool_route_position(humanAnte, phase);
}

static bool omen_trace_active_matches(Ctx *c, const CtxOmenTrace *t,
		int purchaseAnte, const char **locOut) {
	const Config *g = c->g;
	uint16_t tarotAt[9] = { 0 }, spectralAt[9] = { 0 };
	uint8_t editionAt[9] = { 0 };
	uint32_t omenAt = 0;
	for (int ante = 1; ante <= 8; ante++) {
		for (int slot = 0; slot < t->eventCount[ante]; slot++) {
			const SoulPackEvent *event = &t->event[ante][slot];
			int soulKind = event->soulKind;
			if (!soulKind) continue;
			bool omenOwned = omen_trace_owned_at(c, purchaseAnte, event);
			bool soulInPack = false, blackHoleInPack = false;
			for (int card = 0; card < event->cards; card++) {
				int contentKind = soulKind;
				if (soulKind == 1 && omenOwned
						&& ctx_soul_tape_omen(c, omenAt++)) contentKind = 2;
				bool soul = false;
				if (!soulInPack) {
					if (contentKind == 1)
						soul = ctx_soul_tape_tarot(c, ante, tarotAt[ante]++);
					else
						soul = ctx_soul_tape_spectral(c, ante, spectralAt[ante]++);
				}
				if (contentKind == 2 && !blackHoleInPack) {
					bool blackHole = ctx_soul_tape_spectral(c, ante,
							spectralAt[ante]++);
					if (blackHole) {
						if (g->blackHoleAllowed) blackHoleInPack = true;
						soul = false;
					}
				}
				if (!soul) continue;
				soulInPack = true;
				if (g->negLegendary
						&& !ctx_soul_tape_edition(c, ante, editionAt[ante]++))
					return false;
				if (locOut) {
					soul_event_location(c->legloc, sizeof c->legloc, event);
					*locOut = c->legloc;
				}
				return g->activeLegendaryIdx >= 0;
			}
		}
	}
	return false;
}

static uint16_t active_omen_activation_mask_reference(Ctx *c, int omenIndex,
		int routeMaxAnte, uint16_t candidates) {
	uint64_t oldPurchased = c->poolVoucherPurchased;
	uint8_t oldAnte = c->poolVoucherPurchaseAnte[omenIndex];
	uint8_t oldVisit = c->poolVoucherPurchaseVisit[omenIndex];
	uint16_t mask = 0;
	for (int ante = 1; ante <= routeMaxAnte; ante++) {
		uint16_t bit = UINT16_C(1) << (ante - 1);
		if (!(candidates & bit)) continue;
		c->poolVoucherPurchased = oldPurchased | (UINT64_C(1) << omenIndex);
		c->poolVoucherPurchaseAnte[omenIndex] = (uint8_t)ante;
		c->poolVoucherPurchaseVisit[omenIndex] = 1;
		const char *unusedLocation = NULL;
		reset_pool_legend_streams(c);
		if (check_legendary_anywhere(c, &unusedLocation)) mask |= bit;
	}
	c->poolVoucherPurchased = oldPurchased;
	c->poolVoucherPurchaseAnte[omenIndex] = oldAnte;
	c->poolVoucherPurchaseVisit[omenIndex] = oldVisit;
	reset_pool_legend_streams(c);
	return mask;
}

static uint16_t active_omen_activation_mask(Ctx *c, int omenIndex,
		int routeMaxAnte, uint16_t candidates) {
	if (omenIndex < 0 || omenIndex >= c->g->nvouch) return 0;
	if (__builtin_popcount((unsigned)candidates) <= 1)
		return active_omen_activation_mask_reference(c, omenIndex,
				routeMaxAnte, candidates);
	if (!omen_trace_prepare(c, 8))
		return active_omen_activation_mask_reference(c, omenIndex,
				routeMaxAnte, candidates);
	uint16_t mask = 0;
	for (int ante = 1; ante <= routeMaxAnte; ante++) {
		uint16_t bit = UINT16_C(1) << (ante - 1);
		if ((candidates & bit)
				&& omen_trace_active_matches(c, c->omenTrace, ante, NULL))
			mask |= bit;
	}
#ifdef BRAINSTORM_VERIFY_INTERACTIVE_OMEN_TRACE
	uint16_t reference = active_omen_activation_mask_reference(c, omenIndex,
			routeMaxAnte, candidates);
	if (mask != reference) {
		fprintf(stderr,
				"interactive Omen trace mismatch seed=%s candidates=%04x trace=%04x reference=%04x\n",
				c->seed, candidates, mask, reference);
		abort();
	}
#endif
	return mask;
}

static bool try_active_targeted_charm(Ctx *c, const char **locOut,
		int requireOmen) {
	const Config *g = c->g;
	/* The no-Charm route search already proved this candidate has no legal
	 * Omen route. Charm only inserts a forced pack, so repeating the full
	 * voucher search for each of 16 tag positions cannot change that proof. */
	if (requireOmen && c->omenRoutePossible == 0) return false;
	int charmIndex = g->charmTagIdx;
	if (charmIndex < 0) return false;
	/* The zero-purchase branch must evaluate with no route purchases owned,
	 * exactly as it did when it always ran after a failed (state-resetting)
	 * Omen route search. */
	if (!requireOmen) {
		memset(c->poolVoucherPurchaseAnte, 0, sizeof c->poolVoucherPurchaseAnte);
		memset(c->poolVoucherPurchaseVisit, 0, sizeof c->poolVoucherPurchaseVisit);
		c->poolVoucherPurchased = 0;
	}
	for (int ante = 1; ante <= 8; ante++) {
		for (int blind = 0; blind < 2; blind++) {
			int idx = roll_tag_at(c, ante, blind);
			if (idx < 0) return false;
			uint8_t *skip = blind == 0 ? &c->skipSm[ante] : &c->skipBig[ante];
			uint8_t *reward = blind == 0 ? &c->rewardSm[ante] : &c->rewardBig[ante];
			if (idx != charmIndex || *skip) continue;
			uint8_t oldSkip = *skip, oldReward = *reward;
			int oldForced = c->forcedAnte;
			*skip = 1;
			*reward = 1;
			resolve_forced_pack_ante(c);
			if (!requireOmen) {
				reset_pool_legend_streams(c);
				if (check_legendary_anywhere(c, locOut)) return true;
			} else if (check_pool_voucher_route_mode(c, 1,
					g->npoolLegendRules > 0, 1, 8)) {
				reset_pool_legend_streams(c);
				if (check_legendary_anywhere(c, locOut)) return true;
			}
			*skip = oldSkip;
			*reward = oldReward;
			c->forcedAnte = oldForced;
		}
	}
	return false;
}

/* checkMultiAnteJokerSearch. Specific keys keep the FIRST-occurrence rule
 * (the joker you'd actually see is a fixed card); wildcard targets match if
 * ANY entry of the right rarity satisfies the negative requirement -- a
 * failed candidate doesn't consume the wildcard. Mirrors the Lua matchSeq. */
static bool check_jokers(Ctx *c) {
	const Config *g = c->g;
	c->label[0] = 0;
	if (g->ntargets == 0 || !g->anyAnteActive) return true;

	const char *foundAt[3] = { 0, 0, 0 };
	int remaining = g->ntargets;
	SeqEnt seq[MAX_SEQ];
	static const char *SHOP_L[9] = { 0, "A1Shop", "A2Shop", "A3Shop", "A4Shop", "A5Shop", "A6Shop", "A7Shop", "A8Shop" };
	static const char *PACK_L[9] = { 0, "A1Pack", "A2Pack", "A3Pack", "A4Pack", "A5Pack", "A6Pack", "A7Pack", "A8Pack" };

	for (int a = 1; a <= 8 && remaining > 0; a++) {
		for (int src = 0; src < 2; src++) {
			int m = -2;
			const char *lab = NULL;
			if (src == 0 && g->maSlots[a] > 0) {
				m = sim_shop(c, a, g->maSlots[a], seq);
				lab = SHOP_L[a];
			} else if (src == 1 && g->maPacks[a]) {
				m = sim_pack_jokers(c, a, seq);
				lab = PACK_L[a];
			}
			if (m == -1) return false;
			if (m >= 0) {
				for (int t = 0; t < 3; t++) {
					if (!g->jslot[t].used || foundAt[t]) continue;
					if (g->jslot[t].wild >= 0) {
						int w = g->jslot[t].wild;
						for (int i = 0; i < m; i++) {
							if ((w == 0 || seq[i].rarity == w) && (!g->jslot[t].neg || seq[i].neg)) {
								foundAt[t] = lab;
								remaining--;
								break;
							}
						}
					} else {
						for (int i = 0; i < m; i++) {
							if (seq[i].key && !strcmp(seq[i].key, g->jslot[t].key)) {
								if (!g->jslot[t].neg || seq[i].neg) {
									foundAt[t] = lab;
									remaining--;
								}
								break;
							}
						}
					}
				}
				if (remaining == 0 || (g->matchAny && remaining < g->ntargets)) goto done;
			}
		}
	}
done:;
	bool ok = g->matchAny ? (remaining < g->ntargets) : (remaining == 0);
	if (!ok) return false;
	char *p = c->label;
	for (int t = 0; t < 3; t++) {
		if (foundAt[t]) {
			p += snprintf(p, sizeof(c->label) - (size_t)(p - c->label), "%sJ%d%s",
					p == c->label ? "" : " ", t + 1, foundAt[t]);
		}
	}
	return true;
}

/* passesAllFilters, same order and early exits as the Lua original. Assumes
 * the caller advanced c->gen, set c->hashed_seed, and (optionally) preloaded
 * the first filter stream's init hash. */
static bool passes_prepared(Ctx *c) {
	const Config *g = c->g;
	c->label[0] = 0;
	c->omenRoutePossible = -1;
	memset(c->tagRollDone, 0, sizeof c->tagRollDone);
	/* Voucher routes are candidate-local. A purchased-Omen fallback found for
	 * one seed must never remain owned while the next fixture/search candidate
	 * is evaluated (initially owned vouchers are tracked separately in Config). */
	c->poolVoucherPurchased = 0;
	memset(c->poolVoucherPurchaseAnte, 0, sizeof c->poolVoucherPurchaseAnte);
	memset(c->poolVoucherPurchaseVisit, 0, sizeof c->poolVoucherPurchaseVisit);
	bool legAnywhere = g->legAnywhere && g->legendary[0];
	const char *tagLoc = NULL, *legLoc = NULL;
	char tagLocBuf[12];
	/* Legendary identity is independent of every pack, Charm, and Omen route.
	 * Reject the other rarity-4 picks before paying for a physical Soul walk. */
	if (legAnywhere) {
		if (!g->soulAllowed) return false;
		int first = pick_culled(c, &c->joker4, "Joker4", RB_JOKER4,
				g->jokerAvail[4], g->njoker[4], -1);
		if (first < 0 || first != g->activeLegendaryIdx) return false;
	}

	/* 1) soul / legendary, ante-1 charm-tag convention (replaced by the
	 * anywhere pack-scan below when the toggle is on) */
	if (!legAnywhere && (g->soulCount > 0 || g->legendary[0])) {
		if (!g->soulAllowed) return false;
		int needed = g->soulCount > 0 ? g->soulCount : 1;
		bool last = false;
		for (int i = 0; i < needed; i++) {
			bool found = false;
			for (int j = 0; j < g->tagRewardCards[1]; j++) {
				if (psr_gt_997(c,
						stream_next(c, &c->soulT[1], K_SOULT[1]))) found = true;
			}
			last = found;
			if (!found) return false;
		}
		if (g->legendary[0]) {
			if (!last) return false;
			int idx = pick_culled(c, &c->joker4, "Joker4", RB_JOKER4,
					g->jokerAvail[4], g->njoker[4], -1);
			if (idx < 0) return false;
			if (strcmp(g->jokerKey[4][idx], g->legendary)) return false;
			if (g->negLegendary && !psr_gt_997(c,
					stream_next(c, &c->edisouA[1], K_EDISOUA[1]))) return false;
		}
	}
	/* 2) tag: source-verified culled roll; anywhere = both blinds, antes 1-8
	 * (Small rolls before Big within an ante) */
	if (g->tag[0]) {
		if (g->tagAnywhere) {
			for (int a = 1; a <= 8 && !tagLoc; a++) {
				int idx = roll_tag_at(c, a, 0);
				if (idx < 0) return false;
				if (!strcmp(g->tagKey[idx], g->tag)) {
					snprintf(tagLocBuf, sizeof tagLocBuf, "TagA%dSm", a);
					tagLoc = tagLocBuf;
					break;
				}
				idx = roll_tag_at(c, a, 1);
				if (idx < 0) return false;
				if (!strcmp(g->tagKey[idx], g->tag)) {
					snprintf(tagLocBuf, sizeof tagLocBuf, "TagA%dBig", a);
					tagLoc = tagLocBuf;
					break;
				}
			}
			if (!tagLoc) return false;
		} else {
			int idx = roll_tag_at(c, 1, 0);
			if (idx < 0) return false;
			if (strcmp(g->tagKey[idx], g->tag)) return false;
		}
	}
	/* 2.4) this seed's blind-skip assumption, BEFORE any pack consumer
	 * (mirrors Brainstorm.skipsFromFilters + setPackSkipAssumption): taking a
	 * filtered tag/soul means skipping that blind, and a skipped blind's shop
	 * never rolls its two get_pack picks. Classic soul/legendary implies the
	 * ante-1 Small skip (charm-tag convention); the tag filter implies
	 * skipping the matched blind. */
	memset(c->skipSm, 0, sizeof c->skipSm);
	memset(c->skipBig, 0, sizeof c->skipBig);
	memset(c->rewardSm, 0, sizeof c->rewardSm);
	memset(c->rewardBig, 0, sizeof c->rewardBig);
	if (!apply_pool_route(c)) return false;
	if (!legAnywhere && (g->soulCount > 0 || g->legendary[0])) {
		c->skipSm[1] = 1;
		c->rewardSm[1] = 1; /* classic filter's Ante-1 Charm-tag convention */
	}
	if (g->tag[0]) {
		int reward = g->activeTagReward;
		if (g->tagAnywhere) {
			int a = atoi(tagLoc + 4); /* "TagA<n>Sm|Big" */
			if (a >= 1 && a <= 8) {
				if (tagLoc[strlen(tagLoc) - 1] == 'm') {
					c->skipSm[a] = 1;
					if (reward) c->rewardSm[a] = (uint8_t)reward;
				} else {
					c->skipBig[a] = 1;
					if (reward) c->rewardBig[a] = (uint8_t)reward;
				}
			}
		} else {
			c->skipSm[1] = 1;
			if (reward) c->rewardSm[1] = (uint8_t)reward;
		}
	}
	if (g->npoolVoucherRules && !check_pool_voucher_route(c)) return false;
	resolve_forced_pack_ante(c);
	if (g->npoolLegendRules) {
		reset_pool_legend_streams(c);
		if (!check_pool_legend_rules(c)) {
			/* Alternate routes, cheapest and least-purchased first: an actual
			 * Charm Tag with no Omen purchase, then an Omen voucher route,
			 * then a Charm branch combined with Omen. */
			int soulMaxAnte = 1;
			for (int i = 0; i < g->npoolLegendRules; i++) {
				int rngMax = g->poolLegendRules[i].maxAnte
						+ (g->poolLegendRules[i].humanLocation
							&& g->poolLegendRules[i].maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
				if (rngMax > soulMaxAnte) soulMaxAnte = rngMax;
			}
			int voucherMaxAnte = soulMaxAnte > 8 ? 8 : soulMaxAnte;
			int tagMaxAnte = 1;
			for (int i = 0; i < g->npoolLegendRules; i++)
				if (g->poolLegendRules[i].maxAnte > tagMaxAnte)
					tagMaxAnte = g->poolLegendRules[i].maxAnte;
			bool recovered = try_pool_targeted_charm(c, soulMaxAnte,
					tagMaxAnte, 0);
			if (!recovered && g->poolLegendaryRoutes
					== BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM)
				return false;
			if (!recovered) {
				recovered = check_pool_voucher_route_mode(c, 1, 1, 0,
						voucherMaxAnte);
				if (recovered) {
					reset_pool_legend_streams(c);
					recovered = check_pool_legend_rules(c);
				}
			}
			if (!recovered
					&& !try_pool_targeted_charm(c, soulMaxAnte, tagMaxAnte, 1))
				return false;
		}
		reset_pool_legend_streams(c);
	}
	/* 2.5) legendary ANYWHERE: first Soul across antes 1-8 */
	if (legAnywhere) {
		bool recovered = check_legendary_anywhere(c, &legLoc);
		if (!recovered) recovered = try_active_targeted_charm(c, &legLoc, 0);
		if (!recovered) {
			recovered = check_pool_voucher_route_mode(c, 1,
					g->npoolLegendRules > 0, 1, 8);
			if (recovered) {
				reset_pool_legend_streams(c);
				recovered = check_legendary_anywhere(c, &legLoc);
			}
		}
		if (!recovered && !try_active_targeted_charm(c, &legLoc, 1)) return false;
	}
	/* 3) pack: ALL of ante 1's physical slots (Small+Big shops only -- the
	 * post-boss shop is ante 2's -- minus assumed skips; the forced buffoon
	 * matches any normal-buffoon target) */
	if (g->npack > 0) {
		sim_packs(c, 1, 6);
		bool found = false;
		for (int slot = 0; slot < c->packs_n[1] && !found; slot++) {
			int pi = c->pack_idx[1][slot];
			if (pi == PACK_FORCED) {
				for (int i = 0; i < g->npack; i++) {
					if (!strcmp(g->packKeys[i], "p_buffoon_normal_1")
							|| !strcmp(g->packKeys[i], "p_buffoon_normal_2")) { found = true; break; }
				}
			} else if (pi >= 0) {
				for (int i = 0; i < g->npack; i++) {
					if (!strcmp(g->boostKey[pi], g->packKeys[i])) { found = true; break; }
				}
			}
		}
		if (!found) return false;
	}
	/* 4) voucher */
	if (g->voucher[0]) {
		if (!check_voucher(c)) return false;
	}
	/* 5) multi-ante jokers */
	if (!check_jokers(c)) return false;
	/* label: joker parts, then legendary location, then tag location --
	 * byte-identical to the Lua composition */
	if (tagLoc || legLoc) {
		char *p = c->label + strlen(c->label);
		if (legLoc) p += snprintf(p, (size_t)(c->label + sizeof c->label - p), "%s%s",
				p == c->label ? "" : " ", legLoc);
		if (tagLoc) snprintf(p, (size_t)(c->label + sizeof c->label - p), "%s%s",
				p == c->label ? "" : " ", tagLoc);
	}
	return true;
}

/* Serial single-candidate evaluation (fixture remainders, re-checks). */
static bool passes(Ctx *c) {
	c->gen++;
	c->hashed_seed = pseudohash_ks("", c->seed);
	return passes_prepared(c);
}

/* ------------------------------------------------- interleaved hashing --
 * pseudohash is a serial chain of fdivs, so one candidate is latency-bound.
 * Hash ILV independent candidates in lockstep and the divides pipeline
 * (~2x). Each candidate's own chain performs the IDENTICAL operations in the
 * identical order as pseudohash_ks -- only temporally interleaved with other
 * candidates' chains -- so results are bit-exact by construction (enforced
 * by batch_selftest below and end-to-end by tests/native_equivalence.sh,
 * whose fixture mode runs this exact path). */
#define ILV 8

/* slen: shared length of every seed in the batch (8 for natural seeds;
 * expanded-space callers group same-length candidates before batching). */
static void batch_hash_seed_n(const char seeds[ILV][9], int slen, double *out) {
	double num[ILV];
	for (int i = 0; i < ILV; i++) num[i] = 1.0;
	for (int pos = slen; pos >= 1; pos--) {
		double pi_pos = LUA_PI * (double)pos; /* same product as the serial code */
		for (int i = 0; i < ILV; i++) {
			double x = (1.1239285023 / num[i]) * (double)(unsigned char)seeds[i][pos - 1] * LUA_PI + pi_pos;
			num[i] = x - floor(x);
		}
	}
	for (int i = 0; i < ILV; i++) out[i] = num[i];
}

static void batch_hash_seed(const char seeds[ILV][9], double *out) {
	batch_hash_seed_n(seeds, 8, out);
}

static void batch_hash_key_n(const char *key, const char seeds[ILV][9], int slen, double *out) {
	int kl = (int)strlen(key);
	int len = kl + slen;
	double num[ILV];
	for (int i = 0; i < ILV; i++) num[i] = 1.0;
	for (int pos = len; pos >= 1; pos--) {
		double pi_pos = LUA_PI * (double)pos;
		if (pos > kl) {
			int si = pos - kl - 1;
			for (int i = 0; i < ILV; i++) {
				double x = (1.1239285023 / num[i]) * (double)(unsigned char)seeds[i][si] * LUA_PI + pi_pos;
				num[i] = x - floor(x);
			}
		} else {
			double b = (double)(unsigned char)key[pos - 1];
			for (int i = 0; i < ILV; i++) {
				double x = (1.1239285023 / num[i]) * b * LUA_PI + pi_pos;
				num[i] = x - floor(x);
			}
		}
	}
	for (int i = 0; i < ILV; i++) out[i] = num[i];
}

static void batch_hash_key(const char *key, const char seeds[ILV][9], double *out) {
	batch_hash_key_n(key, seeds, 8, out);
}

/* ------------------------------------------------ shared-suffix hashing --
 * make_seed emits the fastest-changing digit at index 0 and pseudohash walks
 * the string from its END to its START, so ILV candidates from consecutive
 * ranks usually share seed[1..slen-1] -- and with it the whole chain state
 * up to the final seed character. Compute that shared state once, then run
 * only the last seed step (plus the key characters, which the walk visits
 * after the seed) per candidate. Every candidate still performs the
 * IDENTICAL operations in the IDENTICAL order as the serial pseudohash_ks --
 * the shared prefix has the same value for each of them -- so results are
 * bit-exact by construction (enforced by the selftests and end-to-end by
 * the equivalence fixtures). */
static bool batch_seeds_share_suffix(const char seeds[ILV][9], int slen) {
	for (int i = 1; i < ILV; i++) {
		if (memcmp(seeds[0] + 1, seeds[i] + 1, (size_t)(slen - 1))) return false;
	}
	return true;
}

/* Chain state after the shared characters seed[1..slen-1], which sit at
 * string positions offset+2..offset+slen (offset = key length; 0 for the
 * seed-only hash). */
static double hash_shared_suffix(const char *seed, int slen, int offset) {
	double num = 1.0;
	for (int pos = slen; pos >= 2; pos--) {
		num = lua_mod1((1.1239285023 / num) * (double)(unsigned char)seed[pos - 1] * LUA_PI
				+ LUA_PI * (double)(pos + offset));
	}
	return num;
}

static void batch_hash_seed_shared(const char seeds[ILV][9], int slen, double *out) {
	double suffix = hash_shared_suffix(seeds[0], slen, 0);
	double pi_pos = LUA_PI * 1.0;
	for (int i = 0; i < ILV; i++) {
		double x = (1.1239285023 / suffix) * (double)(unsigned char)seeds[i][0] * LUA_PI + pi_pos;
		out[i] = x - floor(x);
	}
}

static void batch_hash_key_shared(const char *key, const char seeds[ILV][9],
		int slen, double *out) {
	int kl = (int)strlen(key);
	double suffix = hash_shared_suffix(seeds[0], slen, kl);
	double num[ILV];
	double pi_first = LUA_PI * (double)(kl + 1);
	for (int i = 0; i < ILV; i++) {
		double x = (1.1239285023 / suffix) * (double)(unsigned char)seeds[i][0] * LUA_PI + pi_first;
		num[i] = x - floor(x);
	}
	for (int pos = kl; pos >= 1; pos--) {
		double pi_pos = LUA_PI * (double)pos;
		double b = (double)(unsigned char)key[pos - 1];
		for (int i = 0; i < ILV; i++) {
			double x = (1.1239285023 / num[i]) * b * LUA_PI + pi_pos;
			num[i] = x - floor(x);
		}
	}
	for (int i = 0; i < ILV; i++) out[i] = num[i];
}

/* Per-lane-suffix variants: contiguous scans keep ONE suffix state alive
 * across a whole base-N run and recompute it only when the odometer below
 * carries past the seed's first digit, instead of rehashing it once per ILV
 * group. Lanes carry their own suffix so a group may straddle a carry or a
 * length change without breaking the eight-wide interleave. The last seed
 * character always sits at string position 1 (seed hash) / kl+1 (key hash)
 * regardless of seed length, so these are length-agnostic; all length
 * dependence lives in the suffix values themselves. */
static void batch_hash_seed_pre(const double sufSeed[ILV],
		const char seeds[ILV][9], double *out) {
	double pi_pos = LUA_PI * 1.0;
	for (int i = 0; i < ILV; i++) {
		double x = (1.1239285023 / sufSeed[i]) * (double)(unsigned char)seeds[i][0] * LUA_PI + pi_pos;
		out[i] = x - floor(x);
	}
}

static void batch_hash_key_pre(const char *key, int kl, const double sufKey[ILV],
		const char seeds[ILV][9], double *out) {
	double num[ILV];
	double pi_first = LUA_PI * (double)(kl + 1);
	for (int i = 0; i < ILV; i++) {
		double x = (1.1239285023 / sufKey[i]) * (double)(unsigned char)seeds[i][0] * LUA_PI + pi_first;
		num[i] = x - floor(x);
	}
	for (int pos = kl; pos >= 1; pos--) {
		double pi_pos = LUA_PI * (double)pos;
		double b = (double)(unsigned char)key[pos - 1];
		for (int i = 0; i < ILV; i++) {
			double x = (1.1239285023 / num[i]) * b * LUA_PI + pi_pos;
			num[i] = x - floor(x);
		}
	}
	for (int i = 0; i < ILV; i++) out[i] = num[i];
}

/* ------------------------------------------------------- seed odometer --
 * Contiguous rank scans increment the little-endian digit string directly
 * instead of re-deriving every seed with eight divisions. odometer_next
 * advances exactly one rank (wrapping the whole space back to rank 0, and
 * growing the length in the expanded spaces at block boundaries, exactly
 * like make_seed_in) and reports whether any digit past index 0 changed --
 * the signal that cached suffix chain states are stale. */
typedef struct {
	int space, len;
	uint8_t dig[8];
	char seed[9];
} SeedOdometer;

static void odometer_init(SeedOdometer *o, int space, uint64_t rank) {
	o->space = space;
	o->len = make_seed_in(space, rank, o->seed);
	const char *cs = space_charset(space);
	for (int j = 0; j < o->len; j++) {
		o->dig[j] = (uint8_t)(strchr(cs, o->seed[j]) - cs);
	}
}

static bool odometer_next(SeedOdometer *o) {
	const char *cs = space_charset(o->space);
	uint8_t base = o->space == SPACE_NATURAL ? CHARSET_N
			: o->space == SPACE_SETTABLE ? CHARSET_SETTABLE_N : CHARSET_TOTAL_N;
	int j = 0;
	for (;;) {
		if (++o->dig[j] < base) {
			o->seed[j] = cs[o->dig[j]];
			return j > 0;
		}
		o->dig[j] = 0;
		o->seed[j] = cs[0];
		j++;
		if (j == o->len) {
			if (o->space != SPACE_NATURAL && o->len < 8) {
				/* end of this length's block: the next rank is the shortest
				 * seed of the next length, all digits zero */
				o->dig[o->len] = 0;
				o->seed[o->len] = cs[0];
				o->len++;
				o->seed[o->len] = 0;
			} else if (o->space != SPACE_NATURAL) {
				/* wrap of the whole expanded space back to rank 0 */
				o->len = 1;
				o->seed[1] = 0;
			}
			/* natural space: all eight digits just wrapped to rank 0 */
			return true;
		}
	}
}

static Stream *first_stream(Ctx *c) {
	const Config *g = c->g;
	switch (g->fsId) {
	case FS_SOUL: return &c->soulT[1];
	case FS_TAG: return &c->tagS[1];
	case FS_PACK: return &c->shop_pack[1];
	case FS_VOUCH: return &c->voucher[1];
	case FS_JCDT: return &c->cdt[g->fsAnte];
	case FS_JPACK: return &c->shop_pack[g->fsAnte];
	case FS_LEGEND: return &c->joker4;
	}
	return NULL;
}

/* Evaluate one candidate whose hashes were precomputed by the batch. */
static bool passes_pre(Ctx *c, const char seed[9], double hseed, double hfirst) {
	memcpy(c->seed, seed, 9);
	c->gen++;
	c->hashed_seed = hseed;
	Stream *fs = first_stream(c);
	if (fs) {
		fs->state = hfirst;
		fs->gen = c->gen;
	}
	return passes_prepared(c);
}

/* Always-on guard: batched hashing must agree with the serial reference. */
static bool batch_selftest(const Config *g) {
	char seeds[ILV][9];
	double hs[ILV], hf[ILV];
	uint64_t k = 987654321ULL;
	for (int r = 0; r < 4; r++) {
		for (int i = 0; i < ILV; i++) {
			make_seed(k % SEEDSPACE, seeds[i]);
			k += 104729ULL;
		}
		batch_hash_seed(seeds, hs);
		if (g->fsKey) batch_hash_key(g->fsKey, seeds, hf);
		for (int i = 0; i < ILV; i++) {
			if (hs[i] != pseudohash_ks("", seeds[i])) return false;
			if (g->fsKey && hf[i] != pseudohash_ks(g->fsKey, seeds[i])) return false;
		}
	}
	/* Shared-suffix fast path: consecutive ranks in every space/length the
	 * scan loops can produce, verified against the serial reference. */
	const int spaces[] = { SPACE_NATURAL, SPACE_SETTABLE, SPACE_TOTAL };
	for (size_t si = 0; si < sizeof spaces / sizeof spaces[0]; si++) {
		uint64_t base = 246813579ULL;
		for (int r = 0; r < 4; r++) {
			int l0 = make_seed_in(spaces[si], base % space_size(spaces[si]), seeds[0]);
			int uniform = 1;
			for (int i = 1; i < ILV; i++) {
				int sl = make_seed_in(spaces[si], (base + (uint64_t)i) % space_size(spaces[si]), seeds[i]);
				if (sl != l0) uniform = 0;
			}
			if (uniform && batch_seeds_share_suffix(seeds, l0)) {
				batch_hash_seed_shared(seeds, l0, hs);
				if (g->fsKey) batch_hash_key_shared(g->fsKey, seeds, l0, hf);
				for (int i = 0; i < ILV; i++) {
					if (hs[i] != pseudohash_ks("", seeds[i])) return false;
					if (g->fsKey && hf[i] != pseudohash_ks(g->fsKey, seeds[i])) return false;
				}
			}
			base = base * 31ULL + 17ULL;
		}
	}
	return true;
}

/* ----------------------------------------------------------- config load */
static char *next_tok(char **sp) {
	if (!sp || !*sp) return NULL;
	char *s = *sp;
	while (*s == ' ' || *s == '\t') s++;
	if (!*s) { *sp = NULL; return NULL; }
	char *out = s;
	while (*s && *s != ' ' && *s != '\t') s++;
	if (*s) *s++ = 0;
	*sp = s;
	return out;
}
static bool config_tok_long(char **sp, long *out) {
	char *t = next_tok(sp), *end = NULL;
	if (!t || !*t) return false;
	errno = 0;
	long v = strtol(t, &end, 10);
	if (errno || !end || *end) return false;
	*out = v;
	return true;
}

static bool config_tok_int(char **sp, int *out) {
	long v;
	if (!config_tok_long(sp, &v) || v < INT_MIN || v > INT_MAX) return false;
	*out = (int)v;
	return true;
}

static bool config_tok_bool(char **sp, int *out) {
	int v;
	if (!config_tok_int(sp, &v) || (v != 0 && v != 1)) return false;
	*out = v;
	return true;
}

static bool config_tok_double(char **sp, double *out) {
	char *t = next_tok(sp), *end = NULL;
	if (!t || !*t) return false;
	errno = 0;
	double v = strtod(t, &end);
	if (errno || !end || *end || !isfinite(v)) return false;
	*out = v;
	return true;
}

static bool config_key_ok(const char *s) {
	return s && *s && strlen(s) < MAX_KEY;
}

static int config_arity(const char *d) {
	if (!strcmp(d, "end")) return 0;
	if (!strcmp(d, "poolfile")) return -2; /* nonempty rest-of-line */
	if (!strcmp(d, "jslot") || !strcmp(d, "jokerdef") || !strcmp(d, "check_prn")) return 3;
	if (!strcmp(d, "maslots") || !strcmp(d, "mapacks")) return 8;
	if (!strcmp(d, "tagdef")) return 3;
	if (!strcmp(d, "vouchdef") || !strcmp(d, "specialdef")
			|| !strcmp(d, "check_ph") || !strcmp(d, "check_r13") || !strcmp(d, "check_pr")) return 2;
	if (!strcmp(d, "vouchroute")) return 3;
	if (!strcmp(d, "vouchowned")) return 1;
	if (!strcmp(d, "boostdef")) return 6;
	if (!strcmp(d, "threads") || !strcmp(d, "modelver") || !strcmp(d, "entropy")
			|| !strcmp(d, "session") || !strcmp(d, "soul") || !strcmp(d, "legendary")
			|| !strcmp(d, "neglegendary") || !strcmp(d, "tag") || !strcmp(d, "voucher")
			|| !strcmp(d, "voucherante") || !strcmp(d, "taganywhere")
			|| !strcmp(d, "leganywhere") || !strcmp(d, "packslots")
			|| !strcmp(d, "matchany") || !strcmp(d, "pack")) return 1;
	return -1;
}

static int config_count_tokens(const char *s) {
	int n = 0, in = 0;
	for (; s && *s; s++) {
		if (*s == ' ' || *s == '\t') in = 0;
		else if (!in) { in = 1; n++; }
	}
	return n;
}

enum {
	CS_THREADS = UINT64_C(1) << 0, CS_MODEL = UINT64_C(1) << 1,
	CS_ENTROPY = UINT64_C(1) << 2, CS_SESSION = UINT64_C(1) << 3,
	CS_SOUL = UINT64_C(1) << 4, CS_LEGENDARY = UINT64_C(1) << 5,
	CS_NEGLEG = UINT64_C(1) << 6, CS_TAG = UINT64_C(1) << 7,
	CS_VOUCHER = UINT64_C(1) << 8, CS_VOUCHER_ANTE = UINT64_C(1) << 9,
	CS_TAG_ANY = UINT64_C(1) << 10, CS_LEG_ANY = UINT64_C(1) << 11,
	CS_PACK_SLOTS = UINT64_C(1) << 12, CS_MATCH_ANY = UINT64_C(1) << 13,
	CS_MA_SLOTS = UINT64_C(1) << 14, CS_MA_PACKS = UINT64_C(1) << 15,
	CS_SPECIAL = UINT64_C(1) << 16, CS_POOLFILE = UINT64_C(1) << 17
};

static uint64_t config_single_bit(const char *d) {
	if (!strcmp(d, "threads")) return CS_THREADS;
	if (!strcmp(d, "modelver")) return CS_MODEL;
	if (!strcmp(d, "entropy")) return CS_ENTROPY;
	if (!strcmp(d, "session")) return CS_SESSION;
	if (!strcmp(d, "soul")) return CS_SOUL;
	if (!strcmp(d, "legendary")) return CS_LEGENDARY;
	if (!strcmp(d, "neglegendary")) return CS_NEGLEG;
	if (!strcmp(d, "tag")) return CS_TAG;
	if (!strcmp(d, "voucher")) return CS_VOUCHER;
	if (!strcmp(d, "voucherante")) return CS_VOUCHER_ANTE;
	if (!strcmp(d, "taganywhere")) return CS_TAG_ANY;
	if (!strcmp(d, "leganywhere")) return CS_LEG_ANY;
	if (!strcmp(d, "packslots")) return CS_PACK_SLOTS;
	if (!strcmp(d, "matchany")) return CS_MATCH_ANY;
	if (!strcmp(d, "maslots")) return CS_MA_SLOTS;
	if (!strcmp(d, "mapacks")) return CS_MA_PACKS;
	if (!strcmp(d, "specialdef")) return CS_SPECIAL;
	if (!strcmp(d, "poolfile")) return CS_POOLFILE;
	return 0;
}

/* Resolve every immutable catalog classification the per-candidate paths
 * would otherwise rescan: fixed key indices, reducer identity, tag reward
 * kinds, and the voucher-route availability masks. Pure precomputation over
 * already-parsed catalog data -- no eligibility or route semantics change. */
static void config_finalize_catalog(Config *g) {
	g->omenVoucherIdx = -1;
	g->charmTagIdx = -1;
	g->activeLegendaryIdx = -1;
	memset(g->vouchIsReducer, 0, sizeof g->vouchIsReducer);
	memset(g->tagRewardKindOf, 0, sizeof g->tagRewardKindOf);
	g->vouchRouteEligibleMask = 0;
	g->vouchNoPrereqMask = 0;
	g->vouchInitiallyOwnedMask = 0;
	g->vouchInitialAvailMask = 0;
	g->vouchReducerMask = 0;
	g->omenPrereqMask = 0;
	g->poolLegendNeedsEdition = 0;
	memset(g->vouchUnlocksMask, 0, sizeof g->vouchUnlocksMask);
	for (int i = 0; i < g->nvouch; i++) {
		if (!strcmp(g->vouchKey[i], "v_omen_globe")) g->omenVoucherIdx = i;
		if (!strcmp(g->vouchKey[i], "v_hieroglyph")
				|| !strcmp(g->vouchKey[i], "v_petroglyph"))
			g->vouchIsReducer[i] = 1;
	}
	if (g->nvouch <= 64) {
		for (int i = 0; i < g->nvouch; i++) {
			if (g->vouchRouteAvail[i]) g->vouchRouteEligibleMask |= UINT64_C(1) << i;
			if (g->vouchInitiallyOwned[i]) g->vouchInitiallyOwnedMask |= UINT64_C(1) << i;
			if (g->vouchIsReducer[i]) g->vouchReducerMask |= UINT64_C(1) << i;
			int prerequisite = g->vouchPrereq[i];
			if (prerequisite < 0) g->vouchNoPrereqMask |= UINT64_C(1) << i;
			else if (prerequisite < g->nvouch)
				g->vouchUnlocksMask[prerequisite] |= UINT64_C(1) << i;
		}
		uint64_t unlocked = 0;
		for (uint64_t owned = g->vouchInitiallyOwnedMask; owned; owned &= owned - 1)
			unlocked |= g->vouchUnlocksMask[__builtin_ctzll(owned)];
		g->vouchInitialAvailMask = g->vouchRouteEligibleMask
				& ~g->vouchInitiallyOwnedMask & (g->vouchNoPrereqMask | unlocked);
		int index = g->omenVoucherIdx;
		while (index >= 0 && index < g->nvouch
				&& !(g->omenPrereqMask & (UINT64_C(1) << index))) {
			g->omenPrereqMask |= UINT64_C(1) << index;
			index = g->vouchPrereq[index];
		}
	}
	for (int i = 0; i < g->ntags; i++) {
		if (!strcmp(g->tagKey[i], "tag_charm")) {
			g->charmTagIdx = i;
			g->tagRewardKindOf[i] = 1;
		} else if (!strcmp(g->tagKey[i], "tag_ethereal")) {
			g->tagRewardKindOf[i] = 2;
		}
	}
	if (g->legendary[0]) {
		for (int i = 0; i < g->njoker[4]; i++) {
			if (!strcmp(g->jokerKey[4][i], g->legendary)) {
				g->activeLegendaryIdx = i;
				break;
			}
		}
	}
	for (int i = 0; i < g->npoolLegendRules; i++)
		if (g->poolLegendRules[i].neg) g->poolLegendNeedsEdition = 1;
	g->activeTagReward = tag_reward_kind_key(g->tag[0] ? g->tag : NULL);
}

static bool load_config(const char *path, Config *g, char *err, size_t errsz) {
	/* Idempotent; anchored here (not only in the mains) so embedders like the
	 * compat test harness can never evaluate with unfilled key tables. */
	init_key_tables();
	memset(g, 0, sizeof *g);
	g->threads = 1;
	FILE *f = fopen(path, "r");
	if (!f) { snprintf(err, errsz, "cannot open config %s", path); return false; }
	char line[512];
	int lineno = 0;
	uint64_t seenSingles = 0;
	int sawJslot[3] = { 0 };
	while (fgets(line, sizeof line, f)) {
		lineno++;
		size_t L = strlen(line);
		if (L == sizeof line - 1 && line[L - 1] != '\n') {
			snprintf(err, errsz, "config line %d is too long", lineno); goto fail;
		}
		while (L && (line[L - 1] == '\n' || line[L - 1] == '\r')) line[--L] = 0;
		if (!L || line[0] == '#') continue;
		char *sp = line;
		char *d = next_tok(&sp);
		if (!d) continue;
		int arity = config_arity(d);
		if (arity == -1) {
			snprintf(err, errsz, "unknown config directive '%s' on line %d", d, lineno); goto fail;
		}
		if ((arity >= 0 && config_count_tokens(sp) != arity)
				|| (arity == -2 && (!sp || !*sp))) {
			snprintf(err, errsz, "malformed config directive '%s' on line %d", d, lineno); goto fail;
		}
		uint64_t single = config_single_bit(d);
		if (single && (seenSingles & single)) {
			snprintf(err, errsz, "duplicate config directive '%s' on line %d", d, lineno); goto fail;
		}
		seenSingles |= single;
		if (!strcmp(d, "threads")) { if (!config_tok_int(&sp, &g->threads)) goto bad_value; }
		else if (!strcmp(d, "modelver")) { if (!config_tok_int(&sp, &g->modelver)) goto bad_value; }
		else if (!strcmp(d, "entropy")) { if (!config_tok_double(&sp, &g->entropy)) goto bad_value; }
		else if (!strcmp(d, "session")) { if (!config_tok_long(&sp, &g->session)) goto bad_value; }
		else if (!strcmp(d, "soul")) { if (!config_tok_int(&sp, &g->soulCount)) goto bad_value; }
		else if (!strcmp(d, "legendary")) {
			char *v = next_tok(&sp); if (!config_key_ok(v)) goto bad_value;
			if (strcmp(v, "-")) snprintf(g->legendary, MAX_KEY, "%s", v);
		}
		else if (!strcmp(d, "neglegendary")) { if (!config_tok_bool(&sp, &g->negLegendary)) goto bad_value; }
		else if (!strcmp(d, "tag")) {
			char *v = next_tok(&sp); if (!config_key_ok(v)) goto bad_value;
			if (strcmp(v, "-")) snprintf(g->tag, MAX_KEY, "%s", v);
		}
		else if (!strcmp(d, "voucher")) {
			char *v = next_tok(&sp); if (!config_key_ok(v)) goto bad_value;
			if (strcmp(v, "-")) snprintf(g->voucher, MAX_KEY, "%s", v);
		}
		else if (!strcmp(d, "voucherante")) { if (!config_tok_int(&sp, &g->voucherAnte)) goto bad_value; }
		else if (!strcmp(d, "taganywhere")) { if (!config_tok_bool(&sp, &g->tagAnywhere)) goto bad_value; }
		else if (!strcmp(d, "leganywhere")) { if (!config_tok_bool(&sp, &g->legAnywhere)) goto bad_value; }
		else if (!strcmp(d, "packslots")) { if (!config_tok_int(&sp, &g->packSlots)) goto bad_value; }
		else if (!strcmp(d, "matchany")) { if (!config_tok_bool(&sp, &g->matchAny)) goto bad_value; }
		else if (!strcmp(d, "poolfile")) {
			/* rest of the line verbatim: mod paths contain spaces */
			char *v = sp;
			while (v && (*v == ' ' || *v == '\t')) v++;
			if (!v || !*v || strlen(v) >= sizeof g->poolFile) {
				snprintf(err, errsz, "invalid poolfile path on line %d", lineno); goto fail;
			}
			snprintf(g->poolFile, sizeof g->poolFile, "%s", v);
			sp = NULL;
		}
		else if (!strcmp(d, "jslot")) {
			int i, neg;
			if (!config_tok_int(&sp, &i)) goto bad_value;
			i--;
			char *k = next_tok(&sp);
			if (!config_key_ok(k) || !config_tok_bool(&sp, &neg)) goto bad_value;
			if (i < 0 || i >= 3) { snprintf(err, errsz, "invalid jslot index"); goto fail; }
			if (sawJslot[i]) { snprintf(err, errsz, "duplicate jslot index"); goto fail; }
			sawJslot[i] = 1;
			if (i >= 0 && i < 3 && strcmp(k, "-")) {
				snprintf(g->jslot[i].key, MAX_KEY, "%s", k);
				g->jslot[i].neg = neg != 0;
				g->jslot[i].used = 1;
			}
		}
		else if (!strcmp(d, "maslots")) {
			for (int a = 1; a <= 8; a++) if (!config_tok_int(&sp, &g->maSlots[a])) goto bad_value;
		}
		else if (!strcmp(d, "mapacks")) {
			for (int a = 1; a <= 8; a++) if (!config_tok_bool(&sp, &g->maPacks[a])) goto bad_value;
		}
		else if (!strcmp(d, "pack")) {
			if (g->npack >= MAX_PACKF) { snprintf(err, errsz, "too many pack filters"); goto fail; }
			char *k = next_tok(&sp); if (!config_key_ok(k)) goto bad_value;
			snprintf(g->packKeys[g->npack++], MAX_KEY, "%s", k);
		}
		else if (!strcmp(d, "tagdef")) {
			if (g->ntags >= MAX_TAGS) { snprintf(err, errsz, "too many tags"); goto fail; }
			char *k = next_tok(&sp);
			int available;
			if (!config_key_ok(k) || !config_tok_bool(&sp, &available)
					|| !config_tok_int(&sp, &g->tagMinAnte[g->ntags])) goto bad_value;
			snprintf(g->tagKey[g->ntags], MAX_KEY, "%s", k);
			g->tagReqOk[g->ntags] = (uint8_t)available;
			g->ntags++;
		}
		else if (!strcmp(d, "vouchdef")) {
			if (g->nvouch >= MAX_VOUCH) { snprintf(err, errsz, "too many vouchers"); goto fail; }
			char *k = next_tok(&sp);
			int available;
			if (!config_key_ok(k) || !config_tok_bool(&sp, &available)) goto bad_value;
			snprintf(g->vouchKey[g->nvouch], MAX_KEY, "%s", k);
			g->vouchAvail[g->nvouch] = (uint8_t)available;
			g->vouchRouteAvail[g->nvouch] = (uint8_t)available;
			g->vouchPrereq[g->nvouch] = -1;
			g->nvouch++;
		}
		else if (!strcmp(d, "vouchroute")) {
			char *k = next_tok(&sp);
			int available, index = -1;
			char *requires = NULL;
			if (!config_key_ok(k) || !config_tok_bool(&sp, &available)
					|| !config_key_ok(requires = next_tok(&sp))) goto bad_value;
			for (int i = 0; i < g->nvouch; i++)
				if (!strcmp(g->vouchKey[i], k)) { index = i; break; }
			if (index < 0 || g->vouchRouteDefined[index]) {
				snprintf(err, errsz, "vouchroute must follow one matching vouchdef");
				goto fail;
			}
			g->vouchRouteDefined[index] = 1;
			g->vouchRouteAvail[index] = (uint8_t)available;
			if (strcmp(requires, "-"))
				snprintf(g->vouchPrereqKey[index], MAX_KEY, "%s", requires);
		}
		else if (!strcmp(d, "vouchowned")) {
			char *k = next_tok(&sp);
			int index = -1;
			if (!config_key_ok(k)) goto bad_value;
			for (int i = 0; i < g->nvouch; i++)
				if (!strcmp(g->vouchKey[i], k)) { index = i; break; }
			if (index < 0 || g->vouchInitiallyOwned[index]) goto bad_value;
			g->vouchInitiallyOwned[index] = 1;
		}
		else if (!strcmp(d, "jokerdef")) {
			int r, available;
			if (!config_tok_int(&sp, &r)) goto bad_value;
			if (r < 1 || r > 4 || g->njoker[r] >= MAX_JOKERS) { snprintf(err, errsz, "bad jokerdef"); goto fail; }
			char *k = next_tok(&sp);
			if (!config_key_ok(k) || !config_tok_bool(&sp, &available)) goto bad_value;
			snprintf(g->jokerKey[r][g->njoker[r]], MAX_KEY, "%s", k);
			g->jokerAvail[r][g->njoker[r]] = (uint8_t)available;
			g->njoker[r]++;
		}
		else if (!strcmp(d, "boostdef")) {
			if (g->nboost >= MAX_BOOST) { snprintf(err, errsz, "too many boosters"); goto fail; }
			char *k = next_tok(&sp);
			int buffoon, cards, available;
			double weight;
			if (!config_key_ok(k) || !config_tok_double(&sp, &weight)
					|| !config_tok_bool(&sp, &buffoon)
					|| !config_tok_int(&sp, &cards)) goto bad_value;
			snprintf(g->boostKey[g->nboost], MAX_KEY, "%s", k);
			g->boostW[g->nboost] = weight;
			g->boostBuf[g->nboost] = (uint8_t)buffoon;
			g->boostCards[g->nboost] = cards;
			char *sk = next_tok(&sp);
			if (!sk || (strcmp(sk, "A") && strcmp(sk, "S") && strcmp(sk, "N"))
					|| !config_tok_bool(&sp, &available)) goto bad_value;
			g->boostSoul[g->nboost] = !strcmp(sk, "A") ? 1 : !strcmp(sk, "S") ? 2 : 0;
			g->boostAvail[g->nboost] = (uint8_t)available;
			g->nboost++;
		}
		else if (!strcmp(d, "specialdef")) {
			if (!config_tok_bool(&sp, &g->soulAllowed)
					|| !config_tok_bool(&sp, &g->blackHoleAllowed)) goto bad_value;
			g->sawSpecialDef = 1;
		}
		else if (!strcmp(d, "check_ph") || !strcmp(d, "check_r13") || !strcmp(d, "check_pr") || !strcmp(d, "check_prn")) {
			if (g->nchecks >= MAX_CHECKS) continue;
			int i = g->nchecks++;
			if (!strcmp(d, "check_ph")) {
				g->checks[i].kind = CK_PH;
				char *s = next_tok(&sp);
				if (!config_key_ok(s)) goto bad_value;
				snprintf(g->checks[i].str, MAX_KEY, "%s", s);
			} else if (!strcmp(d, "check_r13")) {
				g->checks[i].kind = CK_R13;
				if (!config_tok_double(&sp, &g->checks[i].x)) goto bad_value;
			} else if (!strcmp(d, "check_pr")) {
				g->checks[i].kind = CK_PR;
				if (!config_tok_double(&sp, &g->checks[i].x)) goto bad_value;
			} else {
				g->checks[i].kind = CK_PRN;
				if (!config_tok_double(&sp, &g->checks[i].x)
						|| !config_tok_int(&sp, &g->checks[i].n)
						|| g->checks[i].n < 1) goto bad_value;
			}
			if (!config_tok_double(&sp, &g->checks[g->nchecks - 1].expect)) goto bad_value;
		}
		else if (!strcmp(d, "end")) { g->sawEnd = 1; break; }
		continue;
bad_value:
		snprintf(err, errsz, "invalid value for config directive '%s' on line %d", d, lineno);
		goto fail;
	}
	fclose(f);
	f = NULL;

	/* A garbled or truncated config must never degrade into "no filters,
	 * accept everything": demand the end marker and the parity checks the
	 * mod always writes. */
	if (!g->sawEnd) { snprintf(err, errsz, "config truncated (no end marker)"); return false; }
	const uint64_t requiredSingles = CS_THREADS | CS_MODEL | CS_ENTROPY | CS_SESSION
		| CS_SOUL | CS_LEGENDARY | CS_NEGLEG | CS_TAG | CS_VOUCHER
		| CS_VOUCHER_ANTE | CS_TAG_ANY | CS_LEG_ANY | CS_PACK_SLOTS
		| CS_MATCH_ANY | CS_MA_SLOTS | CS_MA_PACKS | CS_SPECIAL;
	if ((seenSingles & requiredSingles) != requiredSingles
			|| !sawJslot[0] || !sawJslot[1] || !sawJslot[2]) {
		snprintf(err, errsz, "config is missing required search settings"); return false;
	}
	if (g->nchecks < 8) { snprintf(err, errsz, "config has no parity checks"); return false; }
	if (g->modelver != MODELVER) {
		snprintf(err, errsz, "config modelver %d != helper model %d (rebuild native/brainstorm_native_search via native/build.sh)",
				g->modelver, MODELVER);
		return false;
	}
	if (!g->sawSpecialDef) {
		snprintf(err, errsz, "config has no Soul/Black Hole availability snapshot");
		return false;
	}
	if (g->threads < 1 || g->threads > 64 || !isfinite(g->entropy)
			|| g->soulCount < 0 || g->soulCount > 64
			|| (g->voucherAnte < -1 || g->voucherAnte > 8)
			|| g->packSlots < 0 || g->packSlots > 6) {
		snprintf(err, errsz, "config has an out-of-range search setting");
		return false;
	}
	if ((g->tag[0] && g->ntags == 0) || (g->voucher[0] && g->nvouch == 0)
			|| (g->legendary[0] && g->njoker[4] == 0)) {
		snprintf(err, errsz, "config is missing a pool required by an active filter");
		return false;
	}
	for (int i = 0; i < g->ntags; i++) if (g->tagMinAnte[i] < 0
			|| g->tagMinAnte[i] > MAX_SEARCH_ANTE) {
		snprintf(err, errsz, "config has an invalid tag minimum ante"); return false;
	}
	for (int i = 0; i < g->nvouch; i++) {
		if (!g->vouchPrereqKey[i][0]) continue;
		int prerequisite = -1;
		for (int j = 0; j < g->nvouch; j++)
			if (!strcmp(g->vouchKey[j], g->vouchPrereqKey[i])) {
				prerequisite = j; break;
			}
		if (prerequisite < 0 || prerequisite == i) {
			snprintf(err, errsz, "voucher %s has an invalid prerequisite %s",
					g->vouchKey[i], g->vouchPrereqKey[i]);
			return false;
		}
		g->vouchPrereq[i] = prerequisite;
	}

	/* booster cume (array order, matches the Lua float sum); card counts come
	 * from config.extra via the config -- name fallback is source-corrected
	 * (mega/jumbo are 4-card packs for Buffoon/Spectral, never 6). */
	g->nboostAvailable = 0;
	g->boostCume = 0.0;
	for (int i = 0; i < g->nboost; i++) {
		if (!isfinite(g->boostW[i]) || g->boostW[i] <= 0.0) {
			snprintf(err, errsz, "config has an invalid booster weight"); return false;
		}
		if (g->boostCards[i] < 0 || g->boostCards[i] > 64) {
			snprintf(err, errsz, "config has an invalid booster card count"); return false;
		}
		if (g->boostAvail[i]) {
			g->boostCume += g->boostW[i];
			int at = g->nboostAvailable++;
			g->boostAvailableIndex[at] = i;
			g->boostAvailableLower[at] = g->boostCume - g->boostW[i];
			g->boostAvailableCume[at] = g->boostCume;
		}
		if (g->boostCards[i] <= 0) {
			g->boostCards[i] = strstr(g->boostKey[i], "mega") ? 4 : (strstr(g->boostKey[i], "jumbo") ? 4 : 2);
		}
		/* Tag rewards force these centers directly, even when a challenge removes
		 * them from the random shop pool, so availability does not gate metadata. */
		if (!strcmp(g->boostKey[i], "p_arcana_mega_1") && g->boostSoul[i] == 1)
			g->tagRewardCards[1] = g->boostCards[i];
		if (!strcmp(g->boostKey[i], "p_spectral_normal_1") && g->boostSoul[i] == 2)
			g->tagRewardCards[2] = g->boostCards[i];
		if (!strcmp(g->boostKey[i], "p_buffoon_normal_1") && g->boostAvail[i])
			g->forceBuffoon = 1;
	}
	if (g->nboost == 0 || !isfinite(g->boostCume) || g->boostCume <= 0.0) {
		snprintf(err, errsz, "config has no available booster packs");
		return false;
	}
#ifdef BRAINSTORM_NATIVE_CORE_ONLY
	boost_build_soul_fraction_index(g);
#endif
	if (g->tagRewardCards[1] <= 0 || g->tagRewardCards[2] <= 0) {
		snprintf(err, errsz, "config is missing Charm/Ethereal reward-pack metadata");
		return false;
	}
	if (g->packSlots <= 0) g->packSlots = 2;
	/* joker-search derived state (mirrors checkMultiAnteJokerSearch setup).
	 * Wildcard targets ("*any"/"*common"/"*uncommon"/"*rare") match by rarity
	 * and never need a pool pick, so they contribute nothing to `wanted`. Any
	 * other "*" key behaves like the Lua side: an unknown specific key that
	 * can never match (NOT an error, so verdicts stay identical). */
	g->ntargets = 0;
	g->needNeg = 0;
	for (int t = 0; t < 3; t++) {
		g->jslot[t].wild = -1;
		if (!g->jslot[t].used) continue;
		g->ntargets++;
		if (g->jslot[t].neg) g->needNeg = 1;
		if (!strcmp(g->jslot[t].key, "*any")) g->jslot[t].wild = 0;
		else if (!strcmp(g->jslot[t].key, "*common")) g->jslot[t].wild = 1;
		else if (!strcmp(g->jslot[t].key, "*uncommon")) g->jslot[t].wild = 2;
		else if (!strcmp(g->jslot[t].key, "*rare")) g->jslot[t].wild = 3;
		if (g->jslot[t].wild >= 0) continue;
		int r = 0;
		for (int rr = 1; rr <= 3 && !r; rr++) {
			for (int i = 0; i < g->njoker[rr]; i++) {
				if (!strcmp(g->jokerKey[rr][i], g->jslot[t].key)) { r = rr; break; }
			}
		}
		g->jslot[t].rarity = r;
		if (r) g->wanted[r] = 1;
		else g->wanted[1] = g->wanted[2] = g->wanted[3] = 1; /* unknown key */
	}
	g->anyAnteActive = 0;
	for (int a = 1; a <= 8; a++) {
		if (g->maSlots[a] < 0) { snprintf(err, errsz, "ante slots cannot be negative"); return false; }
		g->maPacks[a] = g->maPacks[a] != 0;
		if (g->maSlots[a] > 0 || g->maPacks[a]) g->anyAnteActive = 1;
		if (g->maSlots[a] > MAX_SLOTS) { snprintf(err, errsz, "ante slots > %d", MAX_SLOTS); return false; }
	}
	if (g->ntags < 1 && g->tag[0]) { snprintf(err, errsz, "tag filter with empty tag pool"); return false; }
	if (g->voucher[0] && g->nvouch < 1) { snprintf(err, errsz, "voucher filter with empty pool"); return false; }
	/* First stream the filter chain touches, mirroring passes_prepared order
	 * exactly (incl. the legAnywhere block swap): its init hash is what the
	 * interleaved batch precomputes per candidate. */
	g->fsId = FS_NONE; g->fsAnte = 0; g->fsKey = NULL;
	int legAny = g->legAnywhere && g->legendary[0];
	if (!legAny && (g->soulCount > 0 || g->legendary[0])) { g->fsId = FS_SOUL; g->fsKey = K_SOULT[1]; }
	else if (g->tag[0]) { g->fsId = FS_TAG; g->fsKey = K_TAG[1]; }
	else if (legAny) { g->fsId = FS_LEGEND; g->fsKey = "Joker4"; }
	else if (g->npack > 0) { g->fsId = FS_PACK; g->fsAnte = 1; g->fsKey = K_SHOPPACK[1]; }
	else if (g->voucher[0]) { g->fsId = FS_VOUCH; g->fsAnte = 1; g->fsKey = K_VOUCHER[1]; }
	else if (g->ntargets > 0 && g->anyAnteActive) {
		for (int a = 1; a <= 8; a++) {
			if (g->maSlots[a] > 0) { g->fsId = FS_JCDT; g->fsAnte = a; g->fsKey = K_CDT[a]; break; }
			if (g->maPacks[a]) { g->fsId = FS_JPACK; g->fsAnte = a; g->fsKey = K_SHOPPACK[a]; break; }
		}
	}
	config_finalize_catalog(g);
	return true;
fail:
	if (f) fclose(f);
	return false;
}

/* Run the parity checks under the current g_seed_fma mode. */
static int run_checks(const Config *g, char *firstFail, size_t fsz) {
	int bad = 0;
	PRNG rs;
	for (int i = 0; i < g->nchecks; i++) {
		double got;
		switch (g->checks[i].kind) {
		case CK_PH: got = pseudohash_str(g->checks[i].str); break;
		case CK_R13: got = round13(g->checks[i].x); break;
		case CK_PR: lj_random_seed(&rs, g->checks[i].x); got = lj_random(&rs); break;
		default: lj_random_seed(&rs, g->checks[i].x); got = (double)lj_random_n(&rs, g->checks[i].n); break;
		}
		if (got != g->checks[i].expect) {
			if (!bad && firstFail) {
				snprintf(firstFail, fsz, "check %d kind %d: got %.17g want %.17g",
						i, g->checks[i].kind, got, g->checks[i].expect);
			}
			bad++;
		}
	}
	return bad;
}

/* Decide g_seed_fma from the checks. Both passing is unsafe: it means an old
 * snapshot lacks a probe that distinguishes the modes, so choosing either can
 * silently desync a later RNG stream. */
static bool calibrate(const Config *g, char *err, size_t errsz) {
	if (g->nchecks == 0) { g_seed_fma = 0; return true; } /* nothing to calibrate against */
	char plainFail[128] = "", fusedFail[128] = "";
	g_seed_fma = 0;
	int plainBad = run_checks(g, plainFail, sizeof plainFail);
	g_seed_fma = 1;
	int fusedBad = run_checks(g, fusedFail, sizeof fusedFail);
	if (!plainBad && fusedBad) { g_seed_fma = 0; return true; }
	if (plainBad && !fusedBad) { g_seed_fma = 1; return true; }
	if (!plainBad && !fusedBad) {
		snprintf(err, errsz, "parity checks are ambiguous; regenerate native_search.cfg with the updated mod");
		return false;
	}
	snprintf(err, errsz, "parity checks failed in both fp modes (plain: %s; fused: %s)",
			plainFail, fusedFail);
	return false;
}

/* ----------------------------------------------------- .bspool contract --
 * Shared between the exhaustive pool builder (which writes pools) and the
 * interactive searcher (which can restrict a search to one). The header is a
 * fixed-size zero-padded text block so a shared pool is self-describing:
 * versions, scanned range, fingerprints, AND the criteria that built it. */
#define BSPOOL_SCHEMA_LEGACY 1
#define BSPOOL_SCHEMA_BLOCKS 2
#define BSPOOL_SCHEMA_EVENTS 3
/* BSPOOL_SCHEMA names the latest legacy-compatible compressed generation used
 * by conversion. New scans select EVENTS explicitly in their PoolPlan. */
#define BSPOOL_SCHEMA BSPOOL_SCHEMA_BLOCKS
#define BSPOOL_HEADER_SIZE 1024
#define BSPOOL_HEADER_EVENTS_SIZE 8192
#define BSPOOL_HEADER_MAX_SIZE (256 * 1024)
#define BSPOOL_MAX_TAG_RULES MAX_POOL_ROUTE_RULES
#define BSPOOL_MAX_VOUCHER_RULES MAX_POOL_VOUCHER_RULES
#define BSPOOL_MAX_VOUCHER_EXCLUSIONS MAX_POOL_VOUCHER_EXCLUSIONS
#define BSPOOL_MAX_ANTE MAX_SEARCH_ANTE
#define BSPOOL_BLOCK_HEADER_SIZE 32
#define BSPOOL3_BLOCK_HEADER_SIZE 48
#define BSPOOL_BLOCK_MAX_RECORDS 8192
#define BSPOOL_BLOCK_MAX_PAYLOAD ((BSPOOL_BLOCK_MAX_RECORDS - 1) * 6)
#define BSPOOL3_BLOCK_MAX_METADATA (16 * 1024 * 1024)
#define BSPOOL_INDEX_ENTRY_SIZE 24
#define BSPOOL_FOOTER_SIZE 40
#define BSPOOL3_INDEX_ENTRY_SIZE 32
#define BSPOOL3_FOOTER_SIZE 80

enum {
	BSPOOL_ENCODING_U64 = 1,
	BSPOOL_ENCODING_DELTA_BLOCKS = 2,
	BSPOOL_ENCODING_DELTA_EVENTS = 3
};

typedef struct {
	int schema, modelver, complete, coverageComplete, headerBytes, encoding, mergedParts;
	uint64_t seedspace, rangeStart, rangeEnd, records, dataBytes;
	uint64_t catalogHash, criteriaHash;
	uint64_t familyId, segmentId, stageHash, lineageId, derivationId;
	uint64_t snapshotId, membershipDigest, metadataDigest;
	uint64_t scanCursor, inputCursor, parentSnapshotId, parentSegmentId;
	uint64_t parentRecords, parentDataBytes, inputRecordStart, inputRecordEnd;
	int parentCoverageComplete;
	char charset[64];
	int space;           /* SPACE_*, derived from charset */
	char label[136];     /* optional user-given pool name (may contain spaces) */
	char poolId[24];     /* short shareable fingerprint, hex */
	int refilterDepth;
	uint64_t sourceCriteriaHash, sourceRecords;
	char sourcePoolId[24];
	int sourceComplete, sourceCoverageComplete;
	int route; /* 1 = collect (tag blinds skipped), 0 = observe */
	/* Current-stage and inherited route-coverage provenance. Fast pools remain
	 * exact subsets, but deliberately omit automatic Omen-purchase rescue. */
	int legendaryRoutes;
	int legendaryRoutesExplicit;
	int routeLegendaryRoutes;
	int routeLegendaryRoutesExplicit;
	int ntagRules;
	struct { char key[MAX_KEY]; int minAnte, minPhase, maxAnte, maxPhase, minCount; }
		tagRules[BSPOOL_MAX_TAG_RULES];
	int nrouteTagRules;
	struct { char key[MAX_KEY]; int minAnte, minPhase, maxAnte, maxPhase, minCount, collect; }
		routeTagRules[BSPOOL_MAX_TAG_RULES];
	int nlegendaries; /* current-stage rule (0 or 1) */
	struct {
		char key[MAX_KEY];
		int minAnte, minPhase, maxAnte, maxPhase;
		int neg, source, humanLocation, soulDepth;
	} legendaries[1];
	int nrouteLegendRules;
	struct {
		char key[MAX_KEY];
		int minAnte, minPhase, maxAnte, maxPhase;
		int neg, source, humanLocation, soulDepth;
	}
		routeLegendRules[MAX_POOL_LEGEND_RULES];
	int nvoucherRules;
	struct { char key[MAX_KEY]; int minAnte, maxAnte; }
		voucherRules[BSPOOL_MAX_VOUCHER_RULES];
	int nrouteVoucherRules;
	struct { char key[MAX_KEY]; int minAnte, maxAnte; }
		routeVoucherRules[BSPOOL_MAX_VOUCHER_RULES];
	int nvoucherExclusions;
	char voucherExclusions[BSPOOL_MAX_VOUCHER_EXCLUSIONS][MAX_KEY];
	int nrouteVoucherExclusions;
	char routeVoucherExclusions[BSPOOL_MAX_VOUCHER_EXCLUSIONS][MAX_KEY];
} BspoolHeader;

static char *pool_tok(char **sp) {
	char *s = *sp;
	while (*s && isspace((unsigned char)*s)) s++;
	if (!*s) { *sp = s; return NULL; }
	char *out = s;
	while (*s && !isspace((unsigned char)*s)) s++;
	if (*s) *s++ = 0;
	*sp = s;
	return out;
}

static bool pool_header_u64(const char *s, int base, uint64_t *out) {
	if (!s || !*s || *s == '-') return false;
	errno = 0;
	char *end = NULL;
	unsigned long long v = strtoull(s, &end, base);
	if (errno || !end || *end) return false;
	*out = (uint64_t)v;
	return true;
}

static bool pool_header_int(const char *s, int *out) {
	if (!s || !*s) return false;
	errno = 0;
	char *end = NULL;
	long v = strtol(s, &end, 10);
	if (errno || !end || *end || v < INT_MIN || v > INT_MAX) return false;
	*out = (int)v;
	return true;
}

static bool pool_header_no_more(char **sp) { return pool_tok(sp) == NULL; }

static bool bspool_parse_phase(const char *s, int allowBoss, int *out) {
	if (!s) return false;
	if (allowBoss && (!strcmp(s, "boss") || !strcmp(s, "0")))
		*out = SOUL_PHASE_BOSS;
	else if (!strcmp(s, "small") || !strcmp(s, "sm") || !strcmp(s, "1"))
		*out = SOUL_PHASE_SMALL;
	else if (!strcmp(s, "big") || !strcmp(s, "2"))
		*out = SOUL_PHASE_BIG;
	else
		return false;
	return true;
}

static bool bspool_parse_source(const char *s, int *out) {
	if (!s || !strcmp(s, "any") || !strcmp(s, "0"))
		*out = 0;
	else if (!strcmp(s, "shop") || !strcmp(s, "1"))
		*out = SOUL_SOURCE_SHOP;
	else if (!strcmp(s, "charm") || !strcmp(s, "2"))
		*out = SOUL_SOURCE_CHARM;
	else if (!strcmp(s, "ethereal") || !strcmp(s, "3"))
		*out = SOUL_SOURCE_ETHEREAL;
	else
		return false;
	return true;
}

static int bspool_tag_position_count(int minAnte, int minPhase,
		int maxAnte, int maxPhase) {
	int count = 0;
	for (int ante = minAnte; ante <= maxAnte; ante++)
		for (int phase = SOUL_PHASE_SMALL; phase <= SOUL_PHASE_BIG; phase++)
			if (bspool_location_in_range(ante, phase,
					minAnte, minPhase, maxAnte, maxPhase)) count++;
	return count;
}

/* Legendary constraints from successive refilter stages describe the same
 * physical Soul #1/#2 sequence. Canonicalize them to at most one intersected
 * rule per (depth, key); conflicting exact-depth targets or disjoint Ante
 * windows mean the pool header cannot describe any valid seed on one
 * cumulative route. Either-depth (soulDepth 0) rules with different keys can
 * both be satisfied -- one per Soul -- so they stay separate entries. */
static bool bspool_add_legend_rule(BspoolHeader *h, const char *key,
		int minAnte, int minPhase, int maxAnte, int maxPhase,
		int neg, int source, int humanLocation, int soulDepth) {
	for (int i = 0; i < h->nrouteLegendRules; i++) {
		if (h->routeLegendRules[i].soulDepth != soulDepth) continue;
		if (strcmp(h->routeLegendRules[i].key, key)) {
			if (soulDepth == SOUL_DEPTH_ANY) continue;
			return false;
		}
		/* A legacy range names the RNG Ante stream, while a human range
		 * names the displayed blind/source. Keep unlike coordinate systems as
		 * separate cumulative constraints on the same Soul event. */
		if (h->routeLegendRules[i].humanLocation != humanLocation) continue;
		if (humanLocation) {
			if (bspool_route_position(minAnte, minPhase)
					> bspool_route_position(h->routeLegendRules[i].minAnte,
						h->routeLegendRules[i].minPhase)) {
				h->routeLegendRules[i].minAnte = minAnte;
				h->routeLegendRules[i].minPhase = minPhase;
			}
			if (bspool_route_position(maxAnte, maxPhase)
					< bspool_route_position(h->routeLegendRules[i].maxAnte,
						h->routeLegendRules[i].maxPhase)) {
				h->routeLegendRules[i].maxAnte = maxAnte;
				h->routeLegendRules[i].maxPhase = maxPhase;
			}
			if (h->routeLegendRules[i].source && source
					&& h->routeLegendRules[i].source != source) return false;
			if (!h->routeLegendRules[i].source) h->routeLegendRules[i].source = source;
		} else {
			if (minAnte > h->routeLegendRules[i].minAnte)
				h->routeLegendRules[i].minAnte = minAnte;
			if (maxAnte < h->routeLegendRules[i].maxAnte)
				h->routeLegendRules[i].maxAnte = maxAnte;
		}
		h->routeLegendRules[i].neg = h->routeLegendRules[i].neg || neg;
		return humanLocation
			? bspool_route_position(h->routeLegendRules[i].minAnte,
					h->routeLegendRules[i].minPhase)
				<= bspool_route_position(h->routeLegendRules[i].maxAnte,
					h->routeLegendRules[i].maxPhase)
			: h->routeLegendRules[i].minAnte <= h->routeLegendRules[i].maxAnte;
	}
	if (h->nrouteLegendRules >= MAX_POOL_LEGEND_RULES) return false;
	int i = h->nrouteLegendRules++;
	snprintf(h->routeLegendRules[i].key, MAX_KEY, "%s", key);
	h->routeLegendRules[i].minAnte = minAnte;
	h->routeLegendRules[i].minPhase = minPhase;
	h->routeLegendRules[i].maxAnte = maxAnte;
	h->routeLegendRules[i].maxPhase = maxPhase;
	h->routeLegendRules[i].neg = neg;
	h->routeLegendRules[i].source = source;
	h->routeLegendRules[i].humanLocation = humanLocation;
	h->routeLegendRules[i].soulDepth = soulDepth;
	return true;
}

static uint64_t pool_hash_update(uint64_t h, const void *data, size_t n) {
	const unsigned char *p = data;
	for (size_t i = 0; i < n; i++) {
		h ^= (uint64_t)p[i];
		h *= UINT64_C(1099511628211);
	}
	return h;
}

static bool pool_catalog_directive(const char *d) {
	return !strcmp(d, "modelver") || !strcmp(d, "tagdef")
		|| !strcmp(d, "vouchdef") || !strcmp(d, "vouchroute")
		|| !strcmp(d, "vouchowned")
		|| !strcmp(d, "jokerdef")
		|| !strcmp(d, "boostdef") || !strcmp(d, "specialdef")
		|| !strncmp(d, "check_", 6);
}

/* Fingerprint only model/pool/check data. Session entropy, current UI filters,
 * thread count, and other search-launch details do not affect pool truth and
 * must not invalidate a pool when Brainstorm refreshes the snapshot. */
static bool pool_hash_catalog_file(const char *path, uint64_t *out) {
	FILE *f = fopen(path, "r");
	if (!f) return false;
	uint64_t h = UINT64_C(1469598103934665603);
	char line[512];
	while (fgets(line, sizeof line, f)) {
		char copy[512];
		snprintf(copy, sizeof copy, "%s", line);
		char *sp = copy;
		char *d = pool_tok(&sp);
		if (!d || !pool_catalog_directive(d)) continue;
		for (char *t = d; t; t = pool_tok(&sp)) {
			h = pool_hash_update(h, t, strlen(t));
			const unsigned char separator = 0;
			h = pool_hash_update(h, &separator, 1);
		}
		const unsigned char newline = '\n';
		h = pool_hash_update(h, &newline, 1);
	}
	if (ferror(f)) { fclose(f); return false; }
	fclose(f);
	*out = h;
	return true;
}

/* Parse a .bspool header from an already-open file (rewinds to 0). Verifies
 * shape, schema, encoding, charset, and seed space here; callers add their
 * own model/records policy on top. */
static bool bspool_read_header(FILE *f, BspoolHeader *h, char *err, size_t errsz) {
	memset(h, 0, sizeof *h);
	h->legendaryRoutes = BSPOOL_LEGENDARY_ROUTES_FULL;
	h->routeLegendaryRoutes = BSPOOL_LEGENDARY_ROUTES_FULL;
	h->route = 1;
	char buf[BSPOOL_HEADER_MAX_SIZE + 1];
	if (bs_fseeko(f, 0, SEEK_SET) != 0) { snprintf(err, errsz, "cannot rewind pool"); return false; }
	size_t got = fread(buf, 1, BSPOOL_HEADER_SIZE, f);
	if (got != BSPOOL_HEADER_SIZE) { snprintf(err, errsz, "pool header is truncated"); return false; }
	buf[got] = 0;
	int peekSchema = 0;
	const char *firstNl = memchr(buf, '\n', got);
	size_t firstLen = firstNl ? (size_t)(firstNl - buf) : got;
	char firstLine[64];
	if (!firstLen || firstLen >= sizeof firstLine) {
		snprintf(err, errsz, "not a Brainstorm seed pool"); return false;
	}
	memcpy(firstLine, buf, firstLen); firstLine[firstLen] = 0;
	char *firstSp = firstLine;
	char *firstMagic = pool_tok(&firstSp), *firstVersion = pool_tok(&firstSp);
	if (!firstMagic || strcmp(firstMagic, "BRAINSTORM_SEED_POOL")
			|| !pool_header_int(firstVersion, &peekSchema) || pool_tok(&firstSp)) {
		snprintf(err, errsz, "not a Brainstorm seed pool"); return false;
	}
	size_t expectedHeader = BSPOOL_HEADER_SIZE;
	if (peekSchema == BSPOOL_SCHEMA_EVENTS) {
		uint64_t declared = 0;
		const char *line = buf, *prefixEnd = buf + got;
		while (line < prefixEnd) {
			const char *nl = memchr(line, '\n', (size_t)(prefixEnd - line));
			size_t len = nl ? (size_t)(nl - line) : (size_t)(prefixEnd - line);
			if (len && len < 64) {
				char copy[64];
				memcpy(copy, line, len); copy[len] = 0;
				char *sp = copy;
				char *d = pool_tok(&sp), *v = pool_tok(&sp);
				if (d && !strcmp(d, "header_bytes") && v && !pool_tok(&sp)
						&& pool_header_u64(v, 10, &declared)) break;
			}
			if (!nl) break;
			line = nl + 1;
		}
		if (declared < BSPOOL_HEADER_SIZE || declared > BSPOOL_HEADER_MAX_SIZE) {
			snprintf(err, errsz, "schema-3 pool has a missing or invalid header_bytes");
			return false;
		}
		expectedHeader = (size_t)declared;
	}
	if (expectedHeader > got) {
		size_t more = expectedHeader - got;
		if (fread(buf + got, 1, more, f) != more) {
			snprintf(err, errsz, "pool header is truncated"); return false;
		}
		got = expectedHeader;
	}
	buf[got] = 0;
	char *cur = buf;
	enum {
		HS_MAGIC = 1u << 0, HS_MODEL = 1u << 1, HS_ENCODING = 1u << 2,
		HS_CHARSET = 1u << 3, HS_SEEDSPACE = 1u << 4, HS_RANGE_START = 1u << 5,
		HS_RANGE_END = 1u << 6, HS_CATALOG = 1u << 7, HS_CRITERIA = 1u << 8,
		HS_RECORDS = 1u << 9, HS_DATA_BYTES = 1u << 10, HS_COMPLETE = 1u << 11,
		HS_HEADER_BYTES = 1u << 12, HS_COVERAGE_COMPLETE = 1u << 13
	};
	const unsigned required = HS_MAGIC | HS_MODEL | HS_ENCODING | HS_CHARSET
		| HS_SEEDSPACE | HS_RANGE_START | HS_RANGE_END | HS_CATALOG
		| HS_CRITERIA | HS_RECORDS | HS_COMPLETE | HS_HEADER_BYTES;
	unsigned seen = 0;
	int sawEnd = 0, malformed = 0, sawSoulDepthFor = -1;
	char encoding[32] = "";
	while (cur && *cur && !sawEnd) {
		char *nl = strchr(cur, '\n');
		if (!nl) break;
		*nl = 0;
		char *sp = cur;
		cur = nl + 1;
		char *d = pool_tok(&sp);
		if (!d) continue;
		char *v = NULL;
		if (!strcmp(d, "BRAINSTORM_SEED_POOL")) {
			v = pool_tok(&sp);
			if ((seen & HS_MAGIC) || !pool_header_int(v, &h->schema)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_MAGIC;
		}
		else if (!strcmp(d, "modelver")) {
			v = pool_tok(&sp); if ((seen & HS_MODEL) || !pool_header_int(v, &h->modelver)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_MODEL;
		}
		else if (!strcmp(d, "encoding")) {
			v = pool_tok(&sp); if ((seen & HS_ENCODING) || !v || strlen(v) >= sizeof encoding
					|| !pool_header_no_more(&sp)) malformed = 1;
			else snprintf(encoding, sizeof encoding, "%s", v); seen |= HS_ENCODING;
		}
		else if (!strcmp(d, "charset")) {
			v = pool_tok(&sp); if ((seen & HS_CHARSET) || !v || strlen(v) >= sizeof h->charset
					|| !pool_header_no_more(&sp)) malformed = 1;
			else snprintf(h->charset, sizeof h->charset, "%s", v); seen |= HS_CHARSET;
		}
		else if (!strcmp(d, "seedspace")) {
			v = pool_tok(&sp); if ((seen & HS_SEEDSPACE) || !pool_header_u64(v, 10, &h->seedspace)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_SEEDSPACE;
		}
		else if (!strcmp(d, "range_start")) {
			v = pool_tok(&sp); if ((seen & HS_RANGE_START) || !pool_header_u64(v, 10, &h->rangeStart)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_RANGE_START;
		}
		else if (!strcmp(d, "range_end")) {
			v = pool_tok(&sp); if ((seen & HS_RANGE_END) || !pool_header_u64(v, 10, &h->rangeEnd)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_RANGE_END;
		}
		else if (!strcmp(d, "catalog_hash")) {
			v = pool_tok(&sp); if ((seen & HS_CATALOG) || !pool_header_u64(v, 16, &h->catalogHash)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_CATALOG;
		}
		else if (!strcmp(d, "criteria_hash")) {
			v = pool_tok(&sp); if ((seen & HS_CRITERIA) || !pool_header_u64(v, 16, &h->criteriaHash)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_CRITERIA;
		}
		else if (!strcmp(d, "records")) {
			v = pool_tok(&sp); if ((seen & HS_RECORDS) || !pool_header_u64(v, 10, &h->records)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_RECORDS;
		}
		else if (!strcmp(d, "data_bytes")) {
			v = pool_tok(&sp); if ((seen & HS_DATA_BYTES) || !pool_header_u64(v, 10, &h->dataBytes)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_DATA_BYTES;
		}
		else if (!strcmp(d, "complete")) {
			v = pool_tok(&sp); if ((seen & HS_COMPLETE) || !pool_header_int(v, &h->complete)
					|| !pool_header_no_more(&sp) || (h->complete != 0 && h->complete != 1)) malformed = 1;
			seen |= HS_COMPLETE;
		}
		else if (!strcmp(d, "coverage_complete")) {
			v = pool_tok(&sp);
			if ((seen & HS_COVERAGE_COMPLETE)
					|| !pool_header_int(v, &h->coverageComplete)
					|| !pool_header_no_more(&sp)
					|| (h->coverageComplete != 0 && h->coverageComplete != 1)) malformed = 1;
			seen |= HS_COVERAGE_COMPLETE;
		}
		else if (!strcmp(d, "header_bytes")) {
			v = pool_tok(&sp); if ((seen & HS_HEADER_BYTES) || !pool_header_int(v, &h->headerBytes)
					|| !pool_header_no_more(&sp)) malformed = 1;
			seen |= HS_HEADER_BYTES;
		}
		else if (!strcmp(d, "tag_route")) {
			v = pool_tok(&sp);
			if (!v || (strcmp(v, "collect") && strcmp(v, "observe")) || !pool_header_no_more(&sp)) malformed = 1;
			else h->route = !strcmp(v, "collect");
		}
		else if (!strcmp(d, "legendary_routes")
				|| !strcmp(d, "route_legendary_routes")) {
			int inherited = !strcmp(d, "route_legendary_routes");
			v = pool_tok(&sp);
			int mode = BSPOOL_LEGENDARY_ROUTES_FULL;
			if ((inherited && h->routeLegendaryRoutesExplicit)
					|| (!inherited && h->legendaryRoutesExplicit)
					|| !v || !pool_header_no_more(&sp)) malformed = 1;
			else if (!strcmp(v, "full")) mode = BSPOOL_LEGENDARY_ROUTES_FULL;
			else if (!strcmp(v, "canonical_charm"))
				mode = BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM;
			else malformed = 1;
			if (!malformed) {
				if (inherited) {
					h->routeLegendaryRoutes = mode;
					h->routeLegendaryRoutesExplicit = 1;
				}
				else {
					h->legendaryRoutes = mode;
					h->legendaryRoutesExplicit = 1;
				}
			}
		}
		else if (!strcmp(d, "pool_id")) {
			v = pool_tok(&sp); if (!v || strlen(v) >= sizeof h->poolId || !pool_header_no_more(&sp)) malformed = 1;
			else snprintf(h->poolId, sizeof h->poolId, "%s", v);
		}
		else if (!strcmp(d, "family_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->familyId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "segment_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->segmentId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "stage_hash")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->stageHash)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "lineage_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->lineageId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "derivation_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->derivationId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "snapshot_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->snapshotId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "membership_digest")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->membershipDigest)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "metadata_digest")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->metadataDigest)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "scan_cursor")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->scanCursor)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "input_cursor")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->inputCursor)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "parent_snapshot_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->parentSnapshotId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "parent_segment_id")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->parentSegmentId)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "parent_records")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->parentRecords)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "parent_data_bytes")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->parentDataBytes)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "parent_coverage_complete")) {
			v = pool_tok(&sp); if (!pool_header_int(v, &h->parentCoverageComplete)
					|| (h->parentCoverageComplete != 0 && h->parentCoverageComplete != 1)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "input_record_start")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->inputRecordStart)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "input_record_end")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->inputRecordEnd)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "refilter_depth")) {
			v = pool_tok(&sp); if (!pool_header_int(v, &h->refilterDepth)
					|| h->refilterDepth < 0 || !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "source_criteria_hash")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 16, &h->sourceCriteriaHash)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "source_records")) {
			v = pool_tok(&sp); if (!pool_header_u64(v, 10, &h->sourceRecords)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "source_pool_id")) {
			v = pool_tok(&sp); if (!v || strlen(v) >= sizeof h->sourcePoolId
					|| !pool_header_no_more(&sp)) malformed = 1;
			else snprintf(h->sourcePoolId, sizeof h->sourcePoolId, "%s", v);
		}
		else if (!strcmp(d, "source_complete")) {
			v = pool_tok(&sp); if (!pool_header_int(v, &h->sourceComplete)
					|| (h->sourceComplete != 0 && h->sourceComplete != 1)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "source_coverage_complete")) {
			v = pool_tok(&sp); if (!pool_header_int(v, &h->sourceCoverageComplete)
					|| (h->sourceCoverageComplete != 0 && h->sourceCoverageComplete != 1)
					|| !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "merged_parts")) {
			v = pool_tok(&sp); if (!pool_header_int(v, &h->mergedParts)
					|| h->mergedParts < 0 || !pool_header_no_more(&sp)) malformed = 1;
		}
		else if (!strcmp(d, "label")) {
			/* rest of the line, spaces included */
			while (*sp == ' ' || *sp == '\t') sp++;
			snprintf(h->label, sizeof h->label, "%s", sp);
		}
		else if (!strcmp(d, "tag")) {
			if (h->ntagRules < BSPOOL_MAX_TAG_RULES) {
				char *k = pool_tok(&sp);
				char *values[6], *value; int nv = 0;
				while ((value = pool_tok(&sp)) != NULL && nv < 6) values[nv++] = value;
				int ia = 0, ip = SOUL_PHASE_SMALL, ib = 0, jp = SOUL_PHASE_BIG, ic = 0;
				bool valid = k && strlen(k) < MAX_KEY;
				if (valid && nv == 3) {
					valid = pool_header_int(values[0], &ia)
						&& pool_header_int(values[1], &ib)
						&& pool_header_int(values[2], &ic);
				} else if (valid && nv == 5) {
					valid = pool_header_int(values[0], &ia)
						&& bspool_parse_phase(values[1], 0, &ip)
						&& pool_header_int(values[2], &ib)
						&& bspool_parse_phase(values[3], 0, &jp)
						&& pool_header_int(values[4], &ic);
				} else valid = false;
				if (valid) {
					int i = h->ntagRules++;
					snprintf(h->tagRules[i].key, MAX_KEY, "%s", k);
					h->tagRules[i].minAnte = ia;
					h->tagRules[i].minPhase = ip;
					h->tagRules[i].maxAnte = ib;
					h->tagRules[i].maxPhase = jp;
					h->tagRules[i].minCount = ic;
				} else malformed = 1;
			} else malformed = 1;
		}
		else if (!strcmp(d, "route_tag")) {
			if (h->nrouteTagRules < BSPOOL_MAX_TAG_RULES) {
				char *mode = pool_tok(&sp), *k = pool_tok(&sp);
				char *values[6], *value; int nv = 0;
				while ((value = pool_tok(&sp)) != NULL && nv < 6) values[nv++] = value;
				int ia = 0, ip = SOUL_PHASE_SMALL, ib = 0, jp = SOUL_PHASE_BIG, ic = 0;
				bool valid = mode && k && strlen(k) < MAX_KEY
						&& (!strcmp(mode, "collect") || !strcmp(mode, "observe"));
				if (valid && nv == 3) {
					valid = pool_header_int(values[0], &ia)
						&& pool_header_int(values[1], &ib)
						&& pool_header_int(values[2], &ic);
				} else if (valid && nv == 5) {
					valid = pool_header_int(values[0], &ia)
						&& bspool_parse_phase(values[1], 0, &ip)
						&& pool_header_int(values[2], &ib)
						&& bspool_parse_phase(values[3], 0, &jp)
						&& pool_header_int(values[4], &ic);
				} else valid = false;
				if (valid) {
					int i = h->nrouteTagRules++;
					snprintf(h->routeTagRules[i].key, MAX_KEY, "%s", k);
					h->routeTagRules[i].minAnte = ia;
					h->routeTagRules[i].minPhase = ip;
					h->routeTagRules[i].maxAnte = ib;
					h->routeTagRules[i].maxPhase = jp;
					h->routeTagRules[i].minCount = ic;
					h->routeTagRules[i].collect = !strcmp(mode, "collect");
				} else malformed = 1;
			} else malformed = 1;
		}
		else if (!strcmp(d, "route_legendary")) {
			char *k = pool_tok(&sp), *values[8], *value; int nv = 0;
			while ((value = pool_tok(&sp)) != NULL && nv < 8) values[nv++] = value;
			int ia = 0, ip = SOUL_PHASE_BOSS, ib = 0, jp = SOUL_PHASE_BIG;
			int in = 0, source = 0, human = 0, id = 0;
			bool valid = k && strlen(k) < MAX_KEY;
			if (valid && nv == 4) {
				valid = pool_header_int(values[0], &ia)
					&& pool_header_int(values[1], &ib)
					&& pool_header_int(values[2], &in)
					&& pool_header_int(values[3], &id);
			} else if (valid && nv == 7) {
				human = 1;
				valid = pool_header_int(values[0], &ia)
					&& bspool_parse_phase(values[1], 1, &ip)
					&& pool_header_int(values[2], &ib)
					&& bspool_parse_phase(values[3], 1, &jp)
					&& pool_header_int(values[4], &in)
					&& bspool_parse_source(values[5], &source)
					&& pool_header_int(values[6], &id);
			} else valid = false;
			if (!valid || ia < 1 || ib < ia || ib > BSPOOL_MAX_ANTE
					|| (human && (bspool_route_position(ib, jp)
						< bspool_route_position(ia, ip)
						|| ib + (jp == SOUL_PHASE_BOSS ? 1 : 0) > BSPOOL_MAX_ANTE))
					|| (in != 0 && in != 1) || id < SOUL_DEPTH_ANY || id > 2
					|| !bspool_add_legend_rule(h, k, ia, ip, ib, jp,
						in, source, human, id)) malformed = 1;
		}
		else if (!strcmp(d, "legendary")) {
			char *k = pool_tok(&sp);
			char *values[7], *value; int nv = 0;
			while ((value = pool_tok(&sp)) != NULL && nv < 7) values[nv++] = value;
			int ia = 0, ip = SOUL_PHASE_BOSS, ib = 0, jp = SOUL_PHASE_BIG;
			int in = 0, source = 0, human = 0;
			bool valid = !h->nlegendaries && k && strlen(k) < MAX_KEY;
			if (valid && (nv == 2 || nv == 3)) {
				valid = pool_header_int(values[0], &ia)
					&& pool_header_int(values[1], &ib)
					&& (nv == 2 || pool_header_int(values[2], &in));
			} else if (valid && nv == 6) {
				human = 1;
				valid = pool_header_int(values[0], &ia)
					&& bspool_parse_phase(values[1], 1, &ip)
					&& pool_header_int(values[2], &ib)
					&& bspool_parse_phase(values[3], 1, &jp)
					&& pool_header_int(values[4], &in)
					&& bspool_parse_source(values[5], &source);
			} else valid = false;
			if (valid && (in == 0 || in == 1)) {
				int i = h->nlegendaries++;
				snprintf(h->legendaries[i].key, MAX_KEY, "%s", k);
				h->legendaries[i].minAnte = ia;
				h->legendaries[i].minPhase = ip;
				h->legendaries[i].maxAnte = ib;
				h->legendaries[i].maxPhase = jp;
				h->legendaries[i].neg = in;
				h->legendaries[i].source = source;
				h->legendaries[i].humanLocation = human;
					h->legendaries[i].soulDepth = 1;
				} else malformed = 1;
			}
		else if (!strcmp(d, "voucher") || !strcmp(d, "route_voucher")) {
			int route = !strcmp(d, "route_voucher");
			int *count = route ? &h->nrouteVoucherRules : &h->nvoucherRules;
			if (*count >= BSPOOL_MAX_VOUCHER_RULES) malformed = 1;
			else {
				char *k = pool_tok(&sp), *a = pool_tok(&sp), *b = pool_tok(&sp);
				int ia, ib;
				if (!k || strlen(k) >= MAX_KEY || !pool_header_int(a, &ia)
						|| !pool_header_int(b, &ib) || !pool_header_no_more(&sp)) malformed = 1;
				else {
					int i = (*count)++;
					if (route) {
						snprintf(h->routeVoucherRules[i].key, MAX_KEY, "%s", k);
						h->routeVoucherRules[i].minAnte = ia;
						h->routeVoucherRules[i].maxAnte = ib;
					} else {
						snprintf(h->voucherRules[i].key, MAX_KEY, "%s", k);
						h->voucherRules[i].minAnte = ia;
						h->voucherRules[i].maxAnte = ib;
					}
				}
			}
		}
		else if (!strcmp(d, "voucher_exclude")
				|| !strcmp(d, "route_voucher_exclude")) {
			int route = !strcmp(d, "route_voucher_exclude");
			int *count = route ? &h->nrouteVoucherExclusions : &h->nvoucherExclusions;
			char (*keys)[MAX_KEY] = route
					? h->routeVoucherExclusions : h->voucherExclusions;
			char *k = pool_tok(&sp);
			if (*count >= BSPOOL_MAX_VOUCHER_EXCLUSIONS || !k || strlen(k) >= MAX_KEY
					|| !pool_header_no_more(&sp)) malformed = 1;
			else snprintf(keys[(*count)++], MAX_KEY, "%s", k);
		}
		else if (!strcmp(d, "soul_depth")) {
			/* Applies to the preceding legendary line; at most one each. */
			v = pool_tok(&sp);
			int depth = 0;
			if (!h->nlegendaries || sawSoulDepthFor == h->nlegendaries - 1
					|| !v || !pool_header_no_more(&sp)
					|| !(!strcmp(v, "any") ? (depth = SOUL_DEPTH_ANY, true)
						: (pool_header_int(v, &depth)
							&& depth >= 1 && depth <= 2))) malformed = 1;
			else {
				sawSoulDepthFor = h->nlegendaries - 1;
				h->legendaries[sawSoulDepthFor].soulDepth = depth;
			}
		}
		else if (!strcmp(d, "end")) {
			if (!pool_header_no_more(&sp)) malformed = 1;
			sawEnd = 1;
		}
	}
	if (!(seen & HS_MAGIC)) { snprintf(err, errsz, "not a Brainstorm seed pool"); return false; }
	if (malformed || (seen & required) != required) {
		snprintf(err, errsz, "pool header is malformed or missing required metadata"); return false;
	}
	if (h->schema != BSPOOL_SCHEMA_LEGACY && h->schema != BSPOOL_SCHEMA_BLOCKS
			&& h->schema != BSPOOL_SCHEMA_EVENTS) {
		snprintf(err, errsz, "pool schema %d unsupported (want %d, %d, or %d)",
				h->schema, BSPOOL_SCHEMA_LEGACY, BSPOOL_SCHEMA_BLOCKS,
				BSPOOL_SCHEMA_EVENTS);
		return false;
	}
	if (!sawEnd) { snprintf(err, errsz, "pool header has no end marker"); return false; }
	/* Older schemas used `complete` for both local file finalization and
	 * exhaustive source coverage. New refilters can finish processing a
	 * paused source while remaining provisional, so the two facts are now
	 * distinct. Missing coverage_complete preserves the legacy meaning. */
	if (!(seen & HS_COVERAGE_COMPLETE)) h->coverageComplete = h->complete;
	if (h->coverageComplete && !h->complete) {
		snprintf(err, errsz, "unfinished pool cannot claim complete coverage"); return false;
	}
	if (!strcmp(encoding, "u64le")) h->encoding = BSPOOL_ENCODING_U64;
	else if (!strcmp(encoding, "delta-varint-blocks-v1")) h->encoding = BSPOOL_ENCODING_DELTA_BLOCKS;
	else if (!strcmp(encoding, "delta-varint-events-v1")) h->encoding = BSPOOL_ENCODING_DELTA_EVENTS;
	else { snprintf(err, errsz, "pool encoding '%s' is unsupported", encoding); return false; }
	if (h->schema == BSPOOL_SCHEMA_LEGACY && h->encoding != BSPOOL_ENCODING_U64) {
		snprintf(err, errsz, "legacy pool must use u64le encoding"); return false;
	}
	if (h->schema == BSPOOL_SCHEMA_BLOCKS && h->encoding != BSPOOL_ENCODING_DELTA_BLOCKS) {
		snprintf(err, errsz, "schema %d pool must use delta blocks", BSPOOL_SCHEMA_BLOCKS); return false;
	}
	if (h->schema == BSPOOL_SCHEMA_EVENTS && h->encoding != BSPOOL_ENCODING_DELTA_EVENTS) {
		snprintf(err, errsz, "schema %d pool must use event blocks", BSPOOL_SCHEMA_EVENTS); return false;
	}
	if (h->schema >= BSPOOL_SCHEMA_BLOCKS && !(seen & HS_DATA_BYTES)) {
		snprintf(err, errsz, "compressed pool is missing its committed byte count"); return false;
	}
	/* The charset decides which seed space the ranks index (the human-readable
	 * `space` header line is informational). Unknown charsets are refused: a
	 * newer format should fail loudly, never decode to wrong seeds. */
	if (!strcmp(h->charset, CHARSET)) h->space = SPACE_NATURAL;
	else if (!strcmp(h->charset, CHARSET_TOTAL)) h->space = SPACE_TOTAL;
	else if (!strcmp(h->charset, CHARSET_SETTABLE)) h->space = SPACE_SETTABLE;
	else { snprintf(err, errsz, "pool charset differs"); return false; }
	if (h->seedspace != space_size(h->space)) { snprintf(err, errsz, "pool seed space differs"); return false; }
	if (h->rangeStart >= h->rangeEnd || h->rangeEnd > h->seedspace
			|| h->records > h->rangeEnd - h->rangeStart) {
		snprintf(err, errsz, "pool range or record count is invalid");
		return false;
	}
	if (h->headerBytes != (int)expectedHeader) {
		snprintf(err, errsz, "pool header_bytes %d is invalid", h->headerBytes);
		return false;
	}
	/* `tag` rules describe the latest scan stage and inherit tag_route.
	 * `route_tag` carries earlier stages through refilters. Together they are
	 * the cumulative physical blind-skip route used by in-game overlays. */
	for (int i = 0; i < h->ntagRules; i++) {
		if (h->nrouteTagRules >= BSPOOL_MAX_TAG_RULES) {
			snprintf(err, errsz, "pool route has too many tag rules"); return false;
		}
		int j = h->nrouteTagRules++;
		snprintf(h->routeTagRules[j].key, MAX_KEY, "%s", h->tagRules[i].key);
		h->routeTagRules[j].minAnte = h->tagRules[i].minAnte;
		h->routeTagRules[j].minPhase = h->tagRules[i].minPhase;
		h->routeTagRules[j].maxAnte = h->tagRules[i].maxAnte;
		h->routeTagRules[j].maxPhase = h->tagRules[i].maxPhase;
		h->routeTagRules[j].minCount = h->tagRules[i].minCount;
		h->routeTagRules[j].collect = h->route;
	}
	for (int i = 0; i < h->nrouteTagRules; i++) {
		if (!h->routeTagRules[i].key[0] || h->routeTagRules[i].minAnte < 1
				|| h->routeTagRules[i].maxAnte < h->routeTagRules[i].minAnte
				|| h->routeTagRules[i].maxAnte > BSPOOL_MAX_ANTE
				|| h->routeTagRules[i].minPhase < SOUL_PHASE_SMALL
				|| h->routeTagRules[i].minPhase > SOUL_PHASE_BIG
				|| h->routeTagRules[i].maxPhase < SOUL_PHASE_SMALL
				|| h->routeTagRules[i].maxPhase > SOUL_PHASE_BIG
				|| bspool_route_position(h->routeTagRules[i].maxAnte,
						h->routeTagRules[i].maxPhase)
					< bspool_route_position(h->routeTagRules[i].minAnte,
						h->routeTagRules[i].minPhase)
				|| h->routeTagRules[i].minCount < 1
				|| h->routeTagRules[i].minCount > bspool_tag_position_count(
						h->routeTagRules[i].minAnte, h->routeTagRules[i].minPhase,
						h->routeTagRules[i].maxAnte, h->routeTagRules[i].maxPhase)) {
			snprintf(err, errsz, "pool has an invalid embedded tag route"); return false;
		}
	}
	for (int i = 0; i < h->nlegendaries; i++) {
		if (h->legendaries[i].minAnte < 1
				|| h->legendaries[i].maxAnte < h->legendaries[i].minAnte
				|| h->legendaries[i].maxAnte > BSPOOL_MAX_ANTE
				|| (h->legendaries[i].humanLocation
					&& (h->legendaries[i].minPhase < SOUL_PHASE_BOSS
						|| h->legendaries[i].minPhase > SOUL_PHASE_BIG
						|| h->legendaries[i].maxPhase < SOUL_PHASE_BOSS
						|| h->legendaries[i].maxPhase > SOUL_PHASE_BIG
						|| h->legendaries[i].source < 0
						|| h->legendaries[i].source > SOUL_SOURCE_ETHEREAL
						|| bspool_route_position(h->legendaries[i].maxAnte,
								h->legendaries[i].maxPhase)
							< bspool_route_position(h->legendaries[i].minAnte,
								h->legendaries[i].minPhase)
						|| h->legendaries[i].maxAnte
							+ (h->legendaries[i].maxPhase == SOUL_PHASE_BOSS ? 1 : 0)
							> BSPOOL_MAX_ANTE))
				|| h->legendaries[i].soulDepth < SOUL_DEPTH_ANY
				|| h->legendaries[i].soulDepth > 2) {
			snprintf(err, errsz, "pool has an invalid embedded legendary rule"); return false;
		}
		if (!bspool_add_legend_rule(h, h->legendaries[i].key,
				h->legendaries[i].minAnte, h->legendaries[i].minPhase,
				h->legendaries[i].maxAnte, h->legendaries[i].maxPhase,
				h->legendaries[i].neg, h->legendaries[i].source,
				h->legendaries[i].humanLocation, h->legendaries[i].soulDepth)) {
			snprintf(err, errsz, "pool has conflicting cumulative legendary rules"); return false;
		}
	}
	if ((h->legendaryRoutesExplicit || h->routeLegendaryRoutesExplicit)
			&& !h->nrouteLegendRules) {
		snprintf(err, errsz, "pool has Legendary route scope without a Legendary rule");
		return false;
	}
	for (int i = 0; i < h->nvoucherRules; i++) {
		if (h->nrouteVoucherRules >= BSPOOL_MAX_VOUCHER_RULES) {
			snprintf(err, errsz, "pool route has too many voucher rules"); return false;
		}
		int j = h->nrouteVoucherRules++;
		snprintf(h->routeVoucherRules[j].key, MAX_KEY, "%s", h->voucherRules[i].key);
		h->routeVoucherRules[j].minAnte = h->voucherRules[i].minAnte;
		h->routeVoucherRules[j].maxAnte = h->voucherRules[i].maxAnte;
	}
	for (int i = 0; i < h->nrouteVoucherRules; i++) {
		if (!h->routeVoucherRules[i].key[0]
				|| h->routeVoucherRules[i].minAnte < 1
				|| h->routeVoucherRules[i].maxAnte < h->routeVoucherRules[i].minAnte
				|| h->routeVoucherRules[i].maxAnte > BSPOOL_MAX_ANTE) {
			snprintf(err, errsz, "pool has an invalid embedded voucher route"); return false;
		}
	}
	for (int i = 0; i < h->nvoucherExclusions; i++) {
		int duplicate = 0;
		for (int j = 0; j < h->nrouteVoucherExclusions; j++)
			if (!strcmp(h->routeVoucherExclusions[j], h->voucherExclusions[i])) {
				duplicate = 1; break;
			}
		if (duplicate) continue;
		if (h->nrouteVoucherExclusions >= BSPOOL_MAX_VOUCHER_EXCLUSIONS) {
			snprintf(err, errsz, "pool route has too many voucher exclusions"); return false;
		}
		snprintf(h->routeVoucherExclusions[h->nrouteVoucherExclusions++],
				MAX_KEY, "%s", h->voucherExclusions[i]);
	}
	return true;
}

/* Schema 2 keeps small independently decodable blocks followed by a compact
 * index.  The index gives record-level random access without turning the
 * whole file into one compression stream, while incomplete scans can rebuild
 * the same index from committed block headers. */
typedef struct {
	uint64_t offset, firstRecord;
	uint32_t count, payloadBytes;
	uint32_t rankBytes, metadataBytes, associations;
} BspoolBlockIndex;

typedef struct {
	int fd, space, encoding;
	uint64_t records, dataOff, dataBytes, nblocks, rangeStart, rangeEnd;
	BspoolBlockIndex *blocks;
} BspoolReader;

typedef struct {
	unsigned char *bytes;
	size_t bytesCap;
	uint64_t *ranks;
	size_t ranksCap;
	uint64_t cachedBlock;
} BspoolScratch;

static uint32_t bspool_get_u32le(const unsigned char *p) {
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t bspool_get_u64le(const unsigned char *p) {
	uint64_t v = 0;
	for (int i = 0; i < 8; i++) v |= (uint64_t)p[i] << (8 * i);
	return v;
}

BS_MAYBE_UNUSED static void bspool_put_u32le(unsigned char *p, uint32_t v) {
	for (int i = 0; i < 4; i++) p[i] = (unsigned char)(v >> (8 * i));
}

BS_MAYBE_UNUSED static void bspool_put_u64le(unsigned char *p, uint64_t v) {
	for (int i = 0; i < 8; i++) p[i] = (unsigned char)(v >> (8 * i));
}

static uint32_t bspool_checksum(const unsigned char *p, size_t n) {
	uint32_t h = UINT32_C(2166136261);
	for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= UINT32_C(16777619); }
	return h;
}

/* CRC64-ECMA-182. Schema 3 covers the semantic block-header fields and both
 * payloads together so rank membership and occurrence metadata share one
 * atomic corruption boundary. */
static uint64_t bspool_crc64_update(uint64_t crc, const unsigned char *p, size_t n) {
	for (size_t i = 0; i < n; i++) {
		crc ^= (uint64_t)p[i] << 56;
		for (int bit = 0; bit < 8; bit++)
			crc = (crc & UINT64_C(0x8000000000000000))
					? (crc << 1) ^ UINT64_C(0x42f0e1eba9ea3693) : crc << 1;
	}
	return crc;
}

static bool bspool_varint_read(const unsigned char *p, size_t n, size_t *at,
		uint64_t *out) {
	uint64_t v = 0;
	int shift = 0;
	size_t start = *at;
	while (*at < n && shift <= 63) {
		unsigned char b = p[(*at)++];
		if (shift == 63 && (b & 0x7e)) return false;
		v |= (uint64_t)(b & 0x7f) << shift;
		if (!(b & 0x80)) {
			size_t minimal = 1;
			for (uint64_t x = v; x >= 128; x >>= 7) minimal++;
			if (*at - start != minimal) return false;
			*out = v; return true;
		}
		shift += 7;
	}
	return false;
}

/* Schema-3 metadata is block-local and inverted by canonical occurrence
 * descriptor. Each descriptor owns an ascending delta list of record indexes.
 * Unknown length-delimited descriptors remain safely skippable. */
static bool bspool_validate_metadata(const unsigned char *p, size_t n,
		uint32_t records, uint32_t expectedAssociations) {
	size_t at = 0;
	uint64_t descriptors = 0, associations = 0;
	if (!bspool_varint_read(p, n, &at, &descriptors) || descriptors > expectedAssociations) return false;
	const unsigned char *prior = NULL;
	size_t priorLen = 0;
	for (uint64_t d = 0; d < descriptors; d++) {
		uint64_t len = 0, matches = 0, index = 0;
		if (!bspool_varint_read(p, n, &at, &len) || !len || len > n - at) return false;
		const unsigned char *desc = p + at;
		if (prior) {
			size_t common = len < priorLen ? (size_t)len : priorLen;
			int cmp = memcmp(prior, desc, common);
			if (cmp > 0 || (cmp == 0 && priorLen >= len)) return false;
		}
		prior = desc; priorLen = (size_t)len; at += (size_t)len;
		if (!bspool_varint_read(p, n, &at, &matches) || !matches
				|| matches > records || associations > UINT32_MAX - matches) return false;
		if (!bspool_varint_read(p, n, &at, &index) || index >= records) return false;
		for (uint64_t i = 1; i < matches; i++) {
			uint64_t delta = 0;
			if (!bspool_varint_read(p, n, &at, &delta) || !delta
					|| index > UINT64_MAX - delta) return false;
			index += delta;
			if (index >= records) return false;
		}
		associations += matches;
	}
	return at == n && associations == expectedAssociations;
}

static bool bspool_scratch_bytes(BspoolScratch *s, size_t need) {
	if (need <= s->bytesCap) return need == 0 || s->bytes != NULL;
	unsigned char *p = realloc(s->bytes, need);
	if (!p) return false;
	s->bytes = p; s->bytesCap = need;
	return true;
}

static bool bspool_scratch_ranks(BspoolScratch *s, size_t need) {
	if (need <= s->ranksCap) return need == 0 || s->ranks != NULL;
	if (need > SIZE_MAX / sizeof *s->ranks) return false;
	uint64_t *p = realloc(s->ranks, need * sizeof *p);
	if (!p) return false;
	s->ranks = p; s->ranksCap = need;
	return true;
}

static void bspool_scratch_destroy(BspoolScratch *s) {
	free(s->bytes); free(s->ranks); memset(s, 0, sizeof *s);
}

typedef struct {
	uint32_t count, rankBytes, metadataBytes, associations, checksum, headerBytes;
	uint64_t first, last, crc64;
} BspoolBlockInfo;

static bool bspool_block_header(int fd, uint64_t off, int encoding,
		BspoolBlockInfo *info) {
	memset(info, 0, sizeof *info);
	if (encoding == BSPOOL_ENCODING_DELTA_EVENTS) {
		unsigned char raw[BSPOOL3_BLOCK_HEADER_SIZE];
		if (bs_pread(fd, raw, sizeof raw, (int64_t)off) != (int64_t)sizeof raw
				|| memcmp(raw, "BSP3", 4) || raw[4] != BSPOOL3_BLOCK_HEADER_SIZE
				|| raw[5] != 0 || raw[6] != 0 || raw[7] != 0) return false;
		info->headerBytes = BSPOOL3_BLOCK_HEADER_SIZE;
		info->count = bspool_get_u32le(raw + 8);
		info->rankBytes = bspool_get_u32le(raw + 12);
		info->metadataBytes = bspool_get_u32le(raw + 16);
		info->associations = bspool_get_u32le(raw + 20);
		info->first = bspool_get_u64le(raw + 24);
		info->last = bspool_get_u64le(raw + 32);
		info->crc64 = bspool_get_u64le(raw + 40);
		return info->count > 0 && info->count <= BSPOOL_BLOCK_MAX_RECORDS
				&& info->rankBytes <= (uint32_t)((info->count - 1u) * 6u)
				&& info->metadataBytes > 0
				&& info->metadataBytes <= BSPOOL3_BLOCK_MAX_METADATA
				&& info->rankBytes <= UINT32_MAX - info->metadataBytes;
	}
	unsigned char raw[BSPOOL_BLOCK_HEADER_SIZE];
	if (bs_pread(fd, raw, sizeof raw, (int64_t)off) != (int64_t)sizeof raw
			|| memcmp(raw, "BSP2", 4)) return false;
	info->headerBytes = BSPOOL_BLOCK_HEADER_SIZE;
	info->count = bspool_get_u32le(raw + 4);
	info->rankBytes = bspool_get_u32le(raw + 8);
	info->checksum = bspool_get_u32le(raw + 12);
	info->first = bspool_get_u64le(raw + 16);
	info->last = bspool_get_u64le(raw + 24);
	return info->count > 0 && info->count <= BSPOOL_BLOCK_MAX_RECORDS
			&& info->rankBytes <= (uint32_t)((info->count - 1u) * 6u);
}

static bool bspool_index_push(BspoolReader *r, uint64_t *cap,
		BspoolBlockIndex e) {
	if (r->nblocks == *cap) {
		uint64_t next = *cap ? *cap * 2 : 256;
		if (next < *cap || next > SIZE_MAX / sizeof *r->blocks) return false;
		BspoolBlockIndex *p = realloc(r->blocks, (size_t)next * sizeof *p);
		if (!p) return false;
		r->blocks = p; *cap = next;
	}
	r->blocks[r->nblocks++] = e;
	return true;
}

static bool bspool_reader_init(BspoolReader *r, int fd, const BspoolHeader *h,
		uint64_t fileBytes, char *err, size_t errsz) {
	memset(r, 0, sizeof *r);
	r->fd = fd; r->space = h->space; r->encoding = h->encoding;
	r->records = h->records; r->dataOff = (uint64_t)h->headerBytes;
	r->rangeStart = h->rangeStart; r->rangeEnd = h->rangeEnd;
	if (h->encoding == BSPOOL_ENCODING_U64) {
		if (h->records > (UINT64_MAX - r->dataOff) / 8u
				|| fileBytes < r->dataOff + h->records * 8u) {
			snprintf(err, errsz, "legacy pool is shorter than its committed record count");
			return false;
		}
		r->dataBytes = h->records * 8u;
		return true;
	}
	r->dataBytes = h->dataBytes;
	if (r->dataBytes > fileBytes || r->dataOff > fileBytes - r->dataBytes) {
		snprintf(err, errsz, "compressed pool data boundary is outside the file");
		return false;
	}
	if (h->complete) {
		uint32_t footerBytes = h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
				? BSPOOL3_FOOTER_SIZE : BSPOOL_FOOTER_SIZE;
		uint32_t indexEntryBytes = h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
				? BSPOOL3_INDEX_ENTRY_SIZE : BSPOOL_INDEX_ENTRY_SIZE;
		if (fileBytes < footerBytes) { snprintf(err, errsz, "compressed pool has no index footer"); return false; }
		unsigned char footer[BSPOOL3_FOOTER_SIZE] = { 0 };
		if (bs_pread(fd, footer, footerBytes, (int64_t)(fileBytes - footerBytes)) != (int64_t)footerBytes
				|| memcmp(footer, h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
						? "BSPIDX3\n" : "BSPIDX2\n", 8)) {
			snprintf(err, errsz, "compressed pool index footer is missing"); return false;
		}
		if (h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
				&& bspool_crc64_update(0, footer, 72) != bspool_get_u64le(footer + 72)) {
			snprintf(err, errsz, "event pool index footer checksum differs"); return false;
		}
		uint64_t indexOff = bspool_get_u64le(footer + 8);
		r->nblocks = bspool_get_u64le(footer + 16);
		uint64_t indexRecords = bspool_get_u64le(footer + 24);
		uint64_t footerDataBytes = bspool_get_u64le(footer + 32);
		uint64_t footerMembership = h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
				? bspool_get_u64le(footer + 40) : 0;
		uint64_t footerMetadata = h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
				? bspool_get_u64le(footer + 48) : 0;
		if (indexOff != r->dataOff + r->dataBytes || indexRecords != r->records
				|| footerDataBytes != r->dataBytes || r->nblocks > r->records
				|| (h->membershipDigest && footerMembership != h->membershipDigest)
				|| (h->metadataDigest && footerMetadata != h->metadataDigest)
				|| r->nblocks > SIZE_MAX / sizeof *r->blocks
				|| r->nblocks > (UINT64_MAX - indexOff - footerBytes) / indexEntryBytes
				|| indexOff + r->nblocks * indexEntryBytes + footerBytes != fileBytes) {
			snprintf(err, errsz, "compressed pool index metadata is inconsistent"); return false;
		}
		if (r->nblocks) {
			r->blocks = malloc((size_t)r->nblocks * sizeof *r->blocks);
			if (!r->blocks) { snprintf(err, errsz, "cannot allocate compressed pool index"); return false; }
		}
		unsigned char raw[BSPOOL3_INDEX_ENTRY_SIZE * 4096];
		uint64_t done = 0;
		while (done < r->nblocks) {
			uint64_t n = r->nblocks - done;
			if (n > 4096) n = 4096;
			size_t bytes = (size_t)n * indexEntryBytes;
			if (bs_pread(fd, raw, bytes, (int64_t)(indexOff + done * indexEntryBytes)) != (int64_t)bytes) {
				snprintf(err, errsz, "cannot read compressed pool index"); goto fail;
			}
			for (uint64_t i = 0; i < n; i++) {
				unsigned char *p = raw + i * indexEntryBytes;
				BspoolBlockIndex *e = &r->blocks[done + i];
				memset(e, 0, sizeof *e);
				e->offset = bspool_get_u64le(p);
				e->firstRecord = bspool_get_u64le(p + 8);
				e->count = bspool_get_u32le(p + 16);
				if (h->encoding == BSPOOL_ENCODING_DELTA_EVENTS) {
					e->rankBytes = bspool_get_u32le(p + 20);
					e->metadataBytes = bspool_get_u32le(p + 24);
					e->associations = bspool_get_u32le(p + 28);
					if (e->metadataBytes > BSPOOL3_BLOCK_MAX_METADATA
							|| e->rankBytes > UINT32_MAX - e->metadataBytes) {
						snprintf(err, errsz, "event pool index payload size overflows"); goto fail;
					}
					e->payloadBytes = e->rankBytes + e->metadataBytes;
				} else {
					e->payloadBytes = e->rankBytes = bspool_get_u32le(p + 20);
				}
			}
			done += n;
		}
	} else {
		uint64_t off = r->dataOff, end = r->dataOff + r->dataBytes, firstRecord = 0, cap = 0;
		while (off < end) {
			BspoolBlockInfo bi;
			if (!bspool_block_header(fd, off, h->encoding, &bi)
					|| bi.headerBytes > end - off
					|| bi.rankBytes > end - off - bi.headerBytes
					|| bi.metadataBytes > end - off - bi.headerBytes - bi.rankBytes) {
				snprintf(err, errsz, "compressed pool has a malformed committed block"); goto fail;
			}
			BspoolBlockIndex e = { .offset = off, .firstRecord = firstRecord,
					.count = bi.count, .payloadBytes = bi.rankBytes + bi.metadataBytes,
					.rankBytes = bi.rankBytes, .metadataBytes = bi.metadataBytes,
					.associations = bi.associations };
			if (!bspool_index_push(r, &cap, e)) { snprintf(err, errsz, "cannot allocate compressed pool index"); goto fail; }
			firstRecord += bi.count;
			off += bi.headerBytes + bi.rankBytes + bi.metadataBytes;
		}
		if (off != end || firstRecord != r->records) {
			snprintf(err, errsz, "compressed pool blocks do not match committed records"); goto fail;
		}
	}
	if ((r->records == 0) != (r->nblocks == 0)) { snprintf(err, errsz, "compressed pool index is empty or incomplete"); goto fail; }
	uint64_t expectedRecord = 0, expectedOffset = r->dataOff;
	uint32_t blockHeaderBytes = h->encoding == BSPOOL_ENCODING_DELTA_EVENTS
			? BSPOOL3_BLOCK_HEADER_SIZE : BSPOOL_BLOCK_HEADER_SIZE;
	for (uint64_t i = 0; i < r->nblocks; i++) {
		BspoolBlockIndex *e = &r->blocks[i];
		if (e->firstRecord != expectedRecord || !e->count
				|| e->count > BSPOOL_BLOCK_MAX_RECORDS
				|| e->rankBytes > (e->count - 1u) * 6u
				|| (h->encoding == BSPOOL_ENCODING_DELTA_EVENTS && !e->metadataBytes)
				|| e->payloadBytes != e->rankBytes + e->metadataBytes
				|| e->count > r->records - expectedRecord
				|| e->offset != expectedOffset || e->offset < r->dataOff
				|| e->offset > r->dataOff + r->dataBytes
				|| blockHeaderBytes > r->dataOff + r->dataBytes - e->offset
				|| e->payloadBytes > r->dataOff + r->dataBytes - e->offset - blockHeaderBytes) {
			snprintf(err, errsz, "compressed pool index entry is invalid"); goto fail;
		}
		expectedRecord += e->count;
		expectedOffset += blockHeaderBytes + e->payloadBytes;
	}
	if (expectedRecord != r->records || expectedOffset != r->dataOff + r->dataBytes) {
		snprintf(err, errsz, "compressed pool index record or byte count differs"); goto fail;
	}
	return true;
fail:
	free(r->blocks); memset(r, 0, sizeof *r); return false;
}

BS_MAYBE_UNUSED static void bspool_reader_destroy(BspoolReader *r) {
	free(r->blocks); memset(r, 0, sizeof *r);
}

static bool bspool_decode_block(const BspoolReader *r, uint64_t block,
		BspoolScratch *s) {
	if (s->cachedBlock == block && s->ranks) return true;
	if (block >= r->nblocks) return false;
	const BspoolBlockIndex *e = &r->blocks[block];
	BspoolBlockInfo bi;
	if (!bspool_block_header(r->fd, e->offset, r->encoding, &bi)
			|| bi.count != e->count || bi.rankBytes != e->rankBytes
			|| bi.metadataBytes != e->metadataBytes || bi.associations != e->associations
			|| bi.first < r->rangeStart || bi.last >= r->rangeEnd || bi.first > bi.last
			|| bi.rankBytes > SIZE_MAX - bi.metadataBytes
			|| !bspool_scratch_bytes(s, (size_t)bi.rankBytes + bi.metadataBytes)
			|| !bspool_scratch_ranks(s, bi.count)) return false;
	size_t payload = (size_t)bi.rankBytes + bi.metadataBytes;
	if (payload && bs_pread(r->fd, s->bytes, payload,
			(int64_t)(e->offset + bi.headerBytes)) != (int64_t)payload) return false;
	if (r->encoding == BSPOOL_ENCODING_DELTA_EVENTS) {
		unsigned char raw[BSPOOL3_BLOCK_HEADER_SIZE];
		if (bs_pread(r->fd, raw, sizeof raw, (int64_t)e->offset) != (int64_t)sizeof raw) return false;
		uint64_t crc = bspool_crc64_update(0, raw + 4, 36);
		crc = bspool_crc64_update(crc, s->bytes, payload);
		if (crc != bi.crc64 || !bspool_validate_metadata(s->bytes + bi.rankBytes,
				bi.metadataBytes, bi.count, bi.associations)) return false;
	} else if (bspool_checksum(s->bytes, bi.rankBytes) != bi.checksum) return false;
	s->ranks[0] = bi.first;
	size_t at = 0;
	for (uint32_t i = 1; i < bi.count; i++) {
		uint64_t delta = 0;
		if (!bspool_varint_read(s->bytes, bi.rankBytes, &at, &delta)
				|| delta == 0 || UINT64_MAX - s->ranks[i - 1] < delta) return false;
		s->ranks[i] = s->ranks[i - 1] + delta;
		if (s->ranks[i] >= r->rangeEnd) return false;
	}
	if (at != bi.rankBytes || s->ranks[bi.count - 1] != bi.last) return false;
	s->cachedBlock = block;
	return true;
}

static bool bspool_reader_read(const BspoolReader *r, uint64_t first,
		uint64_t count, uint64_t *out, BspoolScratch *s) {
	if (first > r->records || count > r->records - first) return false;
	if (!count) return true;
	if (r->encoding == BSPOOL_ENCODING_U64) {
		if (count > SIZE_MAX / 8u) return false;
		size_t bytes = (size_t)count * 8u;
		if (bytes == 0 || !bspool_scratch_bytes(s, bytes)
				|| bs_pread(r->fd, s->bytes, bytes, (int64_t)(r->dataOff + first * 8u)) != (int64_t)bytes) return false;
		for (uint64_t i = 0; i < count; i++) {
			out[i] = bspool_get_u64le(s->bytes + i * 8u);
			if (out[i] < r->rangeStart || out[i] >= r->rangeEnd) return false;
		}
		return true;
	}
	uint64_t done = 0;
	while (done < count) {
		uint64_t record = first + done, lo = 0, hi = r->nblocks;
		while (lo + 1 < hi) {
			uint64_t mid = lo + (hi - lo) / 2;
			if (r->blocks[mid].firstRecord <= record) lo = mid; else hi = mid;
		}
		if (!bspool_decode_block(r, lo, s)) return false;
		const BspoolBlockIndex *e = &r->blocks[lo];
		uint64_t within = record - e->firstRecord;
		uint64_t n = count - done;
		if (n > e->count - within) n = e->count - within;
		memcpy(out + done, s->ranks + within, (size_t)n * sizeof *out);
		done += n;
	}
	return true;
}

/* The external seed-pool builder includes this file as a core implementation
 * so both programs execute the exact same RNG/config/parity code.  Keep the
 * interactive first-hit modes out of that translation unit. */
#ifndef BRAINSTORM_NATIVE_CORE_ONLY

/* ------------------------------------------------------------- searching */
static _Atomic bool g_stop;
static _Atomic bool g_worker_failed;
static _Atomic unsigned long long g_tried;
static bs_mutex_t g_found_mtx = BS_MUTEX_INIT;
static bool g_found;
static char g_found_seed[9], g_found_label[64];
static char g_warn[256];

/* Active .bspool restriction: candidates come from the pool's decoded rank
 * records instead of the sequential full-space counter. Iteration starts at
 * an entropy-derived rotation and covers every record exactly once, so a
 * finished scan with no hit is a DEFINITIVE "nothing in this pool matches". */
typedef struct {
	BspoolReader reader;
	int space;                /* seed space the pool's ranks index */
	uint64_t records, rot;
	_Atomic uint64_t next;    /* rotated record index dealt to workers */
	_Atomic int live;         /* workers still scanning (exhaustion detect) */
	_Atomic bool failed;      /* allocation/decode failure: never exhaustion */
} PoolRun;
static PoolRun g_pool;
static bool g_pool_active;
static bool g_pool_coverage_complete;

typedef struct {
	const Config *g;
	int tid;
	uint64_t start;
} WorkerArgs;

/* Read `count` consecutive pool records starting at rotated index `first`
 * (wrapping at the record count) into ranks[]. pread keeps this thread-safe
 * without a shared file position. */
static bool pool_read_ranks(uint64_t first, uint64_t count, uint64_t *ranks,
		BspoolScratch *scratch) {
	uint64_t done = 0;
	while (done < count) {
		uint64_t at = (first + done) % g_pool.records;
		uint64_t run = count - done;
		if (at + run > g_pool.records) run = g_pool.records - at;
		if (!bspool_reader_read(&g_pool.reader, at, run, ranks + done, scratch)) return false;
		done += run;
	}
	return true;
}

static void record_hit(Ctx *c) {
	bs_mutex_lock(&g_found_mtx);
	if (!g_found) {
		g_found = true;
		memcpy(g_found_seed, c->seed, 9);
		snprintf(g_found_label, sizeof g_found_label, "%s", c->label);
	}
	bs_mutex_unlock(&g_found_mtx);
	atomic_store(&g_stop, true);
}

/* Pool-restricted worker: same batched evaluation pipeline as the full-space
 * worker, fed from pool records. Exits when the pool is fully dealt. */
static void *pool_worker(void *vp) {
	WorkerArgs *w = (WorkerArgs *)vp;
	const Config *g = w->g;
	const uint64_t PCHUNK = 16384;
	Ctx *c = calloc(1, sizeof(Ctx));
	uint64_t *ranks = malloc(PCHUNK * sizeof *ranks);
	BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
	if (!c || !ranks) {
		free(c); free(ranks);
		atomic_store(&g_pool.failed, true);
		atomic_store(&g_worker_failed, true);
		atomic_store(&g_stop, true);
		atomic_fetch_sub(&g_pool.live, 1);
		return NULL;
	}
	c->g = g;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	while (!atomic_load_explicit(&g_stop, memory_order_relaxed)) {
		uint64_t i0 = atomic_fetch_add(&g_pool.next, PCHUNK);
		if (i0 >= g_pool.records) break;
		uint64_t n = PCHUNK;
		if (i0 + n > g_pool.records) n = g_pool.records - i0;
		if (!pool_read_ranks(g_pool.rot + i0, n, ranks, &scratch)) {
			atomic_store(&g_pool.failed, true);
			atomic_store(&g_worker_failed, true);
			atomic_store(&g_stop, true);
			break;
		}
		/* drop corrupt out-of-space ranks instead of trusting them */
		uint64_t m = 0;
		for (uint64_t i = 0; i < n; i++) {
			if (ranks[i] < space_size(g_pool.space)) ranks[m++] = ranks[i];
		}
		uint64_t i = 0, done = 0;
		/* The batched hash needs uniform seed lengths within one ILV group.
		 * Pool ranks ascend between rotation wraps, so variable-length spaces
		 * arrive as long same-length runs: batch each uniform group (sorted
		 * dense pools frequently share suffixes too) and take the serial path
		 * only for the rare group that straddles a length boundary. */
		for (; i + ILV <= m
				&& !atomic_load_explicit(&g_stop, memory_order_relaxed); i += ILV) {
			int l0 = make_seed_in(g_pool.space, ranks[i], seeds[0]);
			int uniform = 1;
			for (int j = 1; j < ILV; j++) {
				if (make_seed_in(g_pool.space, ranks[i + (uint64_t)j], seeds[j]) != l0)
					uniform = 0;
			}
			if (uniform) {
				if (batch_seeds_share_suffix(seeds, l0)) {
					batch_hash_seed_shared(seeds, l0, hseed);
					if (g->fsKey) batch_hash_key_shared(g->fsKey, seeds, l0, hfirst);
				} else {
					batch_hash_seed_n(seeds, l0, hseed);
					if (g->fsKey) batch_hash_key_n(g->fsKey, seeds, l0, hfirst);
				}
				for (int j = 0; j < ILV; j++) {
					done++;
					if (passes_pre(c, seeds[j], hseed[j], g->fsKey ? hfirst[j] : 0.0)) {
						record_hit(c);
						break;
					}
				}
			} else {
				for (int j = 0; j < ILV
						&& !atomic_load_explicit(&g_stop, memory_order_relaxed); j++) {
					memcpy(c->seed, seeds[j], 9);
					done++;
					if (passes(c)) record_hit(c);
				}
			}
		}
		for (; i < m && !atomic_load_explicit(&g_stop, memory_order_relaxed); i++) {
			make_seed_in(g_pool.space, ranks[i], c->seed);
			done++;
			if (passes(c)) record_hit(c);
		}
		atomic_fetch_add_explicit(&g_tried, (unsigned long long)done, memory_order_relaxed);
	}
	bspool_scratch_destroy(&scratch);
	free(ranks);
	free(c->omenTrace);
	free(c);
	atomic_fetch_sub(&g_pool.live, 1);
	return NULL;
}

/* Full-space candidates are dealt to workers in contiguous chunks from this
 * shared counter (offset by the entropy-derived rotation). Contiguity is what
 * lets the ILV batch share the seed-suffix chain state: with the old
 * per-thread stride the eight candidates of a batch never shared a suffix. */
static _Atomic uint64_t g_space_next;

static void *worker(void *vp) {
	WorkerArgs *w = (WorkerArgs *)vp;
	const Config *g = w->g;
	Ctx *c = calloc(1, sizeof(Ctx));
	if (!c) {
		atomic_store(&g_worker_failed, true);
		atomic_store(&g_stop, true);
		return NULL;
	}
	c->g = g;
	const uint64_t BATCH = 16384; /* multiple of ILV */
	int kl = g->fsKey ? (int)strlen(g->fsKey) : 0;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV], sufS[ILV], sufK[ILV];
	SeedOdometer od;
	while (!atomic_load_explicit(&g_stop, memory_order_relaxed)) {
		uint64_t i0 = atomic_fetch_add(&g_space_next, BATCH);
		odometer_init(&od, SPACE_NATURAL, (w->start + i0) % SEEDSPACE);
		double sufSeed = hash_shared_suffix(od.seed, 8, 0);
		double sufKey = g->fsKey ? hash_shared_suffix(od.seed, 8, kl) : 0.0;
		uint64_t done = 0;
		for (uint64_t b = 0; b < BATCH; b += ILV) {
			for (int i = 0; i < ILV; i++) {
				memcpy(seeds[i], od.seed, 9);
				sufS[i] = sufSeed;
				sufK[i] = sufKey;
				if (odometer_next(&od)) {
					sufSeed = hash_shared_suffix(od.seed, 8, 0);
					if (g->fsKey) sufKey = hash_shared_suffix(od.seed, 8, kl);
				}
			}
			batch_hash_seed_pre(sufS, seeds, hseed);
			if (g->fsKey) batch_hash_key_pre(g->fsKey, kl, sufK, seeds, hfirst);
			for (int i = 0; i < ILV; i++) {
				done++;
				if (passes_pre(c, seeds[i], hseed[i], g->fsKey ? hfirst[i] : 0.0)) {
					record_hit(c);
					break;
				}
			}
			if (atomic_load_explicit(&g_stop, memory_order_relaxed)) break;
		}
		atomic_fetch_add_explicit(&g_tried, (unsigned long long)done, memory_order_relaxed);
	}
	free(c->omenTrace);
	free(c);
	return NULL;
}

/* "wb": the mod parses this file as exact bytes (LF only, all platforms). */
static void write_status(const char *path, const char *tmp, bool done, const char *emsg) {
	bool found;
	char foundSeed[sizeof g_found_seed], foundLabel[sizeof g_found_label];
	bs_mutex_lock(&g_found_mtx);
	found = g_found;
	if (found) {
		memcpy(foundSeed, g_found_seed, sizeof foundSeed);
		memcpy(foundLabel, g_found_label, sizeof foundLabel);
	}
	bs_mutex_unlock(&g_found_mtx);
	FILE *f = fopen(tmp, "wb");
	if (!f) return;
	fprintf(f, "P %llu\n", (unsigned long long)atomic_load(&g_tried));
	if (g_warn[0]) fprintf(f, "W %s\n", g_warn);
	if (emsg) fprintf(f, "E %s\n", emsg);
	if (found) fprintf(f, "R %s %s\n", foundSeed, foundLabel[0] ? foundLabel : "-");
	if (done) fprintf(f, "D\n");
	fclose(f);
	bs_rename_overwrite(tmp, path);
}

static double file_age_seconds(const char *path) {
	return bs_file_age_seconds(path);
}

/* Open + validate the config's .bspool. A profile/catalog difference is
 * fatal: membership proves the embedded criteria only against the snapshot
 * that built the pool, and overlay route composition depends on the same
 * ordered tag/booster pools. */
static bool pool_open(Config *g, const char *cfgPath, char *err, size_t errsz) {
	FILE *f = fopen(g->poolFile, "rb");
	if (!f) { snprintf(err, errsz, "pool: cannot open %s", g->poolFile); return false; }
	BspoolHeader h;
	char herr[192];
	if (!bspool_read_header(f, &h, herr, sizeof herr)) {
		snprintf(err, errsz, "pool: %s", herr);
		fclose(f);
		return false;
	}
	if (h.modelver != MODELVER) {
		snprintf(err, errsz, "pool: built with model %d, this helper is model %d", h.modelver, MODELVER);
		fclose(f);
		return false;
	}
	int64_t fsize = bs_file_size(f);
	if (fsize < 0) {
		snprintf(err, errsz, "pool: cannot stat %s", g->poolFile);
		fclose(f);
		return false;
	}
	/* The header's record/data boundaries are the last committed checkpoint;
	 * a compressed reader ignores any crash tail after that boundary. */
	uint64_t records = h.records;
	if (records == 0) { snprintf(err, errsz, "pool: no seed records"); fclose(f); return false; }
	uint64_t cfgHash = 0;
	if (!pool_hash_catalog_file(cfgPath, &cfgHash)) {
		snprintf(err, errsz, "pool: cannot fingerprint the current profile snapshot");
		fclose(f);
		return false;
	}
	if (cfgHash != h.catalogHash) {
		snprintf(err, errsz,
				"pool: profile/unlock snapshot differs; rebuild the pool with this native_search.cfg");
		fclose(f);
		return false;
	}
	g->npoolRouteRules = 0;
	for (int r = 0; r < h.nrouteTagRules; r++) {
		if (!h.routeTagRules[r].collect) continue;
		int idx = -1;
		for (int i = 0; i < g->ntags; i++) {
			if (!strcmp(g->tagKey[i], h.routeTagRules[r].key)) { idx = i; break; }
		}
		if (idx < 0 || g->npoolRouteRules >= MAX_POOL_ROUTE_RULES) {
			snprintf(err, errsz, "pool: embedded tag route is not available in this profile");
			fclose(f);
			return false;
		}
		int j = g->npoolRouteRules++;
		g->poolRouteRules[j].poolIndex = idx;
		g->poolRouteRules[j].minAnte = h.routeTagRules[r].minAnte;
		g->poolRouteRules[j].minPhase = h.routeTagRules[r].minPhase;
		g->poolRouteRules[j].maxAnte = h.routeTagRules[r].maxAnte;
		g->poolRouteRules[j].maxPhase = h.routeTagRules[r].maxPhase;
		g->poolRouteRules[j].minCount = h.routeTagRules[r].minCount;
		g->poolRouteRules[j].collect = 1;
	}
	g->npoolLegendRules = 0;
	g->poolLegendNeedsEdition = 0;
	g->poolLegendaryRoutes = h.legendaryRoutesExplicit
		? h.legendaryRoutes : h.routeLegendaryRoutes;
	for (int r = 0; r < h.nrouteLegendRules; r++) {
		int idx = -1;
		for (int i = 0; i < g->njoker[4]; i++) {
			if (!strcmp(g->jokerKey[4][i], h.routeLegendRules[r].key)) { idx = i; break; }
		}
		if (!g->soulAllowed || idx < 0 || !g->jokerAvail[4][idx]
				|| g->npoolLegendRules >= MAX_POOL_LEGEND_RULES) {
			snprintf(err, errsz, "pool: embedded legendary route is not available in this profile");
			fclose(f);
			return false;
		}
		int j = g->npoolLegendRules++;
		g->poolLegendRules[j].poolIndex = idx;
		g->poolLegendRules[j].minAnte = h.routeLegendRules[r].minAnte;
		g->poolLegendRules[j].minPhase = h.routeLegendRules[r].minPhase;
		g->poolLegendRules[j].maxAnte = h.routeLegendRules[r].maxAnte;
		g->poolLegendRules[j].maxPhase = h.routeLegendRules[r].maxPhase;
		g->poolLegendRules[j].neg = h.routeLegendRules[r].neg;
		g->poolLegendRules[j].source = h.routeLegendRules[r].source;
		g->poolLegendRules[j].humanLocation = h.routeLegendRules[r].humanLocation;
		g->poolLegendRules[j].soulDepth = h.routeLegendRules[r].soulDepth;
		if (g->poolLegendRules[j].neg) g->poolLegendNeedsEdition = 1;
	}
	g->npoolVoucherRules = 0;
	g->npoolVoucherExclusions = 0;
	g->poolVoucherMaxAnte = 0;
	if (h.nrouteVoucherRules) {
		if (g->nvouch < 1 || g->nvouch > 64) {
			snprintf(err, errsz,
					"pool: voucher routes require a catalog of 1 to 64 entries");
			fclose(f);
			return false;
		}
		for (int i = 0; i < g->nvouch; i++) if (!g->vouchRouteDefined[i]) {
			snprintf(err, errsz,
					"pool: voucher route catalog is missing; refresh the snapshot in Balatro");
			fclose(f);
			return false;
		}
	}
	for (int r = 0; r < h.nrouteVoucherRules; r++) {
		int idx = -1;
		for (int i = 0; i < g->nvouch; i++)
			if (!strcmp(g->vouchKey[i], h.routeVoucherRules[r].key)) {
				idx = i; break;
			}
		if (idx < 0 || !g->vouchRouteAvail[idx] || g->vouchInitiallyOwned[idx]
				|| h.routeVoucherRules[r].minAnte < 1
				|| h.routeVoucherRules[r].maxAnte < h.routeVoucherRules[r].minAnte
				|| h.routeVoucherRules[r].maxAnte > 8
				|| g->npoolVoucherRules >= MAX_POOL_VOUCHER_RULES) {
			snprintf(err, errsz,
					"pool: embedded voucher route is unavailable or outside Antes 1-8");
			fclose(f);
			return false;
		}
		int j = g->npoolVoucherRules++;
		g->poolVoucherRules[j].poolIndex = idx;
		g->poolVoucherRules[j].minAnte = h.routeVoucherRules[r].minAnte;
		g->poolVoucherRules[j].maxAnte = h.routeVoucherRules[r].maxAnte;
		if (g->poolVoucherRules[j].maxAnte > g->poolVoucherMaxAnte)
			g->poolVoucherMaxAnte = g->poolVoucherRules[j].maxAnte;
	}
	for (int r = 0; r < h.nrouteVoucherExclusions; r++) {
		int idx = -1;
		for (int i = 0; i < g->nvouch; i++)
			if (!strcmp(g->vouchKey[i], h.routeVoucherExclusions[r])) {
				idx = i; break;
			}
		if (idx < 0 || g->npoolVoucherExclusions >= MAX_POOL_VOUCHER_EXCLUSIONS) {
			snprintf(err, errsz, "pool: embedded voucher exclusion is unknown");
			fclose(f);
			return false;
		}
		g->poolVoucherExclusions[g->npoolVoucherExclusions++] = idx;
	}
	if (g->npoolVoucherRules) {
		for (int r = 0; r < g->npoolRouteRules; r++) {
			const char *key = g->tagKey[g->poolRouteRules[r].poolIndex];
			if (!strcmp(key, "tag_voucher") || !strcmp(key, "tag_double")) {
				snprintf(err, errsz,
						"pool: Voucher/Double Tag collection is not yet supported with voucher routes");
				fclose(f);
				return false;
			}
		}
		if (!strcmp(g->tag, "tag_voucher") || !strcmp(g->tag, "tag_double")) {
			snprintf(err, errsz,
					"pool: this active tag filter cannot be collected with a voucher route");
			fclose(f);
			return false;
		}
	}
	if (!h.coverageComplete) {
		snprintf(g_warn, sizeof g_warn, "pool coverage is incomplete (%llu records currently available)",
				(unsigned long long)records);
	}
	int fd = bs_dup(fileno(f));
	fclose(f);
	if (fd < 0) { snprintf(err, errsz, "pool: cannot keep %s open", g->poolFile); return false; }
	if (!bspool_reader_init(&g_pool.reader, fd, &h, (uint64_t)fsize, herr, sizeof herr)) {
		snprintf(err, errsz, "pool: %s", herr);
		bs_close(fd);
		return false;
	}
	g_pool.space = h.space;
	g_pool.records = records;
	g_pool_coverage_complete = h.coverageComplete != 0;
	g_pool_active = true;
	return true;
}

static int mode_search(Config *g, const char *cfgPath, const char *statusPath,
		const char *stopPath, const char *hbPath) {
	char tmp[1024];
	snprintf(tmp, sizeof tmp, "%s.tmp", statusPath);
	char err[256];
	if (!calibrate(g, err, sizeof err)) {
		fprintf(stderr, "%s\n", err);
		write_status(statusPath, tmp, true, err);
		return 1;
	}
	if (!batch_selftest(g)) {
		write_status(statusPath, tmp, true, "batch hash self-test failed");
		return 1;
	}
	if (g->poolFile[0] && !pool_open(g, cfgPath, err, sizeof err)) {
		fprintf(stderr, "%s\n", err);
		write_status(statusPath, tmp, true, err);
		return 1;
	}
	uint64_t entropy = splitmix64((uint64_t)(g->entropy * 4096.0) ^ 0xB5A7A7EDULL ^ (uint64_t)g->session);
	uint64_t start = entropy % SEEDSPACE;
	int n = g->threads < 1 ? 1 : (g->threads > 64 ? 64 : g->threads);
	atomic_store(&g_worker_failed, false);
	atomic_store(&g_space_next, 0);
	if (g_pool_active) {
		g_pool.rot = entropy % g_pool.records;
		atomic_init(&g_pool.next, 0);
		atomic_init(&g_pool.live, 0);
		atomic_init(&g_pool.failed, false);
	}
	bs_thread_t th[64];
	WorkerArgs wa[64];
	int made = 0;
	for (int i = 0; i < n; i++) {
		wa[i].g = g;
		wa[i].tid = i;
		wa[i].start = start;
		if (g_pool_active) atomic_fetch_add(&g_pool.live, 1);
		if (bs_thread_create(&th[i], g_pool_active ? pool_worker : worker, &wa[i]) != 0) {
			if (g_pool_active) atomic_fetch_sub(&g_pool.live, 1);
			atomic_store(&g_stop, true);
			for (int j = 0; j < made; j++) bs_thread_join(th[j]);
			write_status(statusPath, tmp, true, "worker thread creation failed");
			return 1;
		}
		made++;
	}
	time_t t0 = time(NULL);
	bool joined = false;
	while (!atomic_load(&g_stop)) {
		bs_sleep_ms(200);
		if (bs_file_exists(stopPath)) atomic_store(&g_stop, true);
		/* Dealing every recorded seed is definitive only when the pool covers
		 * its entire declared source. A paused source can prove no match among
		 * current records, never that future committed records contain none. */
		if (g_pool_active && atomic_load(&g_pool.live) == 0) {
			for (int i = 0; i < made; i++) bs_thread_join(th[i]);
			joined = true;
			if (g_found) break;
			if (atomic_load(&g_pool.failed)) {
				write_status(statusPath, tmp, true, "pool: record decode/read failed");
				return 1;
			}
			if (g_pool_coverage_complete) {
				write_status(statusPath, tmp, true,
						"pool: no seed in the pool matches the active filters");
				return 3;
			}
			write_status(statusPath, tmp, true,
					"pool: no matching seed among currently recorded seeds; source coverage is incomplete");
			return 4;
		}
		/* heartbeat: the mod touches hbPath every ~2s while searching; if the
		 * game died without writing the stop file, exit instead of orphaning. */
		if (difftime(time(NULL), t0) > 20 && file_age_seconds(hbPath) > 30.0) {
			atomic_store(&g_stop, true);
			write_status(statusPath, tmp, true, "heartbeat lost");
			for (int i = 0; i < made; i++) bs_thread_join(th[i]);
			return 2;
		}
		if (difftime(time(NULL), t0) > 6 * 3600) atomic_store(&g_stop, true); /* hard cap */
		write_status(statusPath, tmp, false, NULL);
	}
	if (!joined) {
		for (int i = 0; i < made; i++) bs_thread_join(th[i]);
	}
	if (atomic_load(&g_worker_failed)) {
		write_status(statusPath, tmp, true, g_pool_active
				? "pool: worker allocation or record read failed"
				: "native worker allocation failed");
		return 1;
	}
	write_status(statusPath, tmp, true, NULL);
	return 0;
}

/* Fixture mode deliberately runs the SAME batched pipeline as search, in
 * ILV-sized chunks (serial fallback for the remainder), so the equivalence
 * suite validates the interleaved hashing end-to-end. */
static int mode_fixture(const Config *g, const char *seedfile) {
	char err[256];
	if (!calibrate(g, err, sizeof err)) {
		fprintf(stderr, "CALIBRATE FAIL: %s\n", err);
		return 1;
	}
	if (!batch_selftest(g)) { fprintf(stderr, "batch hash self-test failed\n"); return 1; }
	FILE *f = fopen(seedfile, "r");
	if (!f) { fprintf(stderr, "cannot open %s\n", seedfile); return 1; }
	Ctx *c = calloc(1, sizeof(Ctx));
	if (!c) { fprintf(stderr, "cannot allocate fixture context\n"); fclose(f); return 1; }
	c->g = g;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	int nbuf = 0;
	char line[64];
	for (;;) {
		bool more = fgets(line, sizeof line, f) != NULL;
		int slen = 0;
		if (more) {
			slen = (int)strlen(line);
			while (slen > 0 && (line[slen - 1] == '\n' || line[slen - 1] == '\r'
					|| line[slen - 1] == ' ' || line[slen - 1] == '\t')) slen--;
			if (slen > 8) slen = 8;
		}
		if (more && slen == 8) {
			memcpy(seeds[nbuf], line, 8);
			seeds[nbuf][8] = 0;
			nbuf++;
		}
		/* Flush the batch when full, at EOF, and before any short seed so
		 * output order always matches input order (fixtures are diffed). */
		if (nbuf == ILV || ((!more || (slen > 0 && slen < 8)) && nbuf > 0)) {
			if (nbuf == ILV) {
				batch_hash_seed(seeds, hseed);
				if (g->fsKey) batch_hash_key(g->fsKey, seeds, hfirst);
				for (int i = 0; i < ILV; i++) {
					bool ok = passes_pre(c, seeds[i], hseed[i], g->fsKey ? hfirst[i] : 0.0);
					printf("%s %d %s\n", seeds[i], ok ? 1 : 0, (ok && c->label[0]) ? c->label : "-");
				}
			} else {
				for (int i = 0; i < nbuf; i++) {
					memcpy(c->seed, seeds[i], 9);
					bool ok = passes(c);
					printf("%s %d %s\n", c->seed, ok ? 1 : 0, (ok && c->label[0]) ? c->label : "-");
				}
			}
			nbuf = 0;
		}
		/* Short seeds (typed-in style, 1..7 chars) evaluate serially: the
		 * batched hash requires uniform lengths. */
		if (more && slen > 0 && slen < 8) {
			memcpy(c->seed, line, (size_t)slen);
			c->seed[slen] = 0;
			bool ok = passes(c);
			printf("%s %d %s\n", c->seed, ok ? 1 : 0, (ok && c->label[0]) ? c->label : "-");
		}
		if (!more) break;
	}
	free(c->omenTrace);
	free(c);
	fclose(f);
	return 0;
}

static int mode_verifychecks(const Config *g) {
	char err[256];
	if (!calibrate(g, err, sizeof err)) {
		printf("FAIL %s\n", err);
		return 1;
	}
	printf("OK mode=%s checks=%d\n", g_seed_fma ? "fma" : "plain", g->nchecks);
	return 0;
}

static int mode_bench(const Config *g0, int seconds) {
	Config g = *g0;
	char err[256];
	if (!calibrate(&g, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
	for (int pass = 0; pass < 2; pass++) {
		int n = pass == 0 ? 1 : g.threads;
		Config gb = g;
		gb.threads = n;
		atomic_store(&g_stop, false);
		atomic_store(&g_tried, 0);
		atomic_store(&g_space_next, 0);
		g_found = false;
		/* make it unfindable so we measure pure reject throughput */
		snprintf(gb.tag, MAX_KEY, "tag_bench_impossible");
		bs_thread_t th[64];
		WorkerArgs wa[64];
		for (int i = 0; i < n; i++) {
			wa[i].g = &gb; wa[i].tid = i; wa[i].start = 12345;
			bs_thread_create(&th[i], worker, &wa[i]);
		}
		bs_sleep_ms(1000u * (unsigned)seconds);
		atomic_store(&g_stop, true);
		for (int i = 0; i < n; i++) bs_thread_join(th[i]);
		printf("threads=%d seeds/sec=%.0f\n", n, (double)atomic_load(&g_tried) / seconds);
	}
	return 0;
}

int main(int argc, char **argv) {
	bs_platform_init();
	init_key_tables();
	if (argc < 3) {
		fprintf(stderr, "usage: %s search <cfg> <status> <stop> <hb> | fixture <cfg> <seeds> | verifychecks <cfg> | bench <cfg> <secs>\n", argv[0]);
		return 2;
	}
	static Config g;
	char err[256] = "";
	if (!load_config(argv[2], &g, err, sizeof err)) {
		fprintf(stderr, "config error: %s\n", err);
		if (!strcmp(argv[1], "search") && argc >= 4) {
			char tmp[1024];
			snprintf(tmp, sizeof tmp, "%s.tmp", argv[3]);
			write_status(argv[3], tmp, true, err[0] ? err : "config error");
		}
		return 1;
	}
	if (!strcmp(argv[1], "search") && argc == 6) return mode_search(&g, argv[2], argv[3], argv[4], argv[5]);
	if (!strcmp(argv[1], "fixture") && argc == 4) {
		/* Fixture candidates are supplied explicitly, but a configured pool is
		 * still opened so its embedded blind-skip route composes exactly as it
		 * does in an interactive restricted search. */
		if (g.poolFile[0] && !pool_open(&g, argv[2], err, sizeof err)) {
			fprintf(stderr, "%s\n", err);
			return 1;
		}
		int rc = mode_fixture(&g, argv[3]);
		if (g_pool_active) {
			int fd = g_pool.reader.fd;
			bspool_reader_destroy(&g_pool.reader);
			bs_close(fd);
		}
		return rc;
	}
	if (!strcmp(argv[1], "verifychecks")) return mode_verifychecks(&g);
	if (!strcmp(argv[1], "bench") && argc == 4) return mode_bench(&g, atoi(argv[3]));
	fprintf(stderr, "bad arguments\n");
	return 2;
}

#endif /* !BRAINSTORM_NATIVE_CORE_ONLY */
