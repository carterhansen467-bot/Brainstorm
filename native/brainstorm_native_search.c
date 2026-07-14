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
#define MAX_CHECKS 64
#define MAX_SLOTS 64      /* shop slots per ante */
#define MAX_RESAMPLE 64   /* persistent resample streams per base key */
#define MAX_SEQ 80        /* per-ante joker sequence entries */

static const double LUA_PI = 3.14159265358979323846; /* == math.pi bits */

/* Lua 5.1 `x % 1` == x - floor(x/1)*1; exact for our non-negative x. */
static inline double lua_mod1(double x) { return x - floor(x); }

/* ------------------------------------------------------------- round13 --
 * Port of the mod's round13 (Brainstorm_reroll.lua): bit-identical stand-in
 * for math.abs(tonumber(string.format("%.13f", x))), x in [0,1). The rare
 * ambiguous band falls back to the exact decimal path; snprintf/strtod here
 * and LuaJIT's lj_strfmt/lj_strscan are both correctly rounded, so they
 * agree everywhere (also enforced by check_r13 parity lines). */
static double round13(double x) {
	double q = x * 1e13;
	double n = floor(q);
	double f = q - n;
	if (f > 0.5015) {
		n += 1.0;
	} else if (f > 0.4985) {
		char buf[48];
		snprintf(buf, sizeof buf, "%.13f", x);
		return fabs(strtod(buf, NULL));
	}
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

typedef union { uint64_t u64; double d; } U64double;

/* g_seed_fma: how the game's LuaJIT binary rounded d*pi+e in random_seed.
 * 1 = fused (single rounding), 0 = separate mul+add. Decided at startup by
 * the parity checks; -1 = undecided (both behave identically on the checks,
 * which means either is correct for the tested domain -- default plain). */
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

/* ------------------------------------------------------------ pseudohash --
 * Balatro's pseudohash over key..seed (Lua-level math: no contraction). */
static double pseudohash_ks(const char *key, const char *seed) {
	char buf[96];
	size_t kl = strlen(key);
	size_t sl = strlen(seed); /* 8 for natural seeds; 1..8 in a total-space pool */
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
#define MAX_POOL_LEGEND_RULES 2
#define MAX_SEARCH_ANTE 39

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
	int njoker[5]; char jokerKey[5][MAX_JOKERS][MAX_KEY]; uint8_t jokerAvail[5][MAX_JOKERS];
	int nboost; char boostKey[MAX_BOOST][MAX_KEY]; double boostW[MAX_BOOST];
	uint8_t boostBuf[MAX_BOOST], boostAvail[MAX_BOOST]; int boostCards[MAX_BOOST];
	uint8_t boostSoul[MAX_BOOST]; /* 0 none, 1 Arcana(Tarot), 2 Spectral */
	int tagRewardCards[3]; /* indexed by boostSoul kind: Charm=1, Ethereal=2 */
	double boostCume;
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
	struct { int poolIndex, minAnte, maxAnte, minCount, collect; }
		poolRouteRules[MAX_POOL_ROUTE_RULES];
	int npoolLegendRules;
	struct { int poolIndex, minAnte, maxAnte, neg, soulDepth; }
		poolLegendRules[MAX_POOL_LEGEND_RULES];
} Config;

enum { CK_PH = 1, CK_R13, CK_PR, CK_PRN };
enum { FS_NONE = 0, FS_SOUL, FS_TAG, FS_PACK, FS_VOUCH, FS_JCDT, FS_JPACK };

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

/* The natural space above is what the game GENERATES. The "total" space is
 * everything its seed box ACCEPTS typed in: 0-9 A-Z (0 and O included),
 * lengths 1..8. Ranks order seeds shortest-first; within a length the digit
 * order is little-endian, same convention as make_seed. Only .bspool files
 * carry a space choice -- live full-space searches stay natural, since they
 * hunt seeds the game can actually deal. */
static const char CHARSET_TOTAL[] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";
#define CHARSET_TOTAL_N 36
static const uint64_t SEEDSPACE_TOTAL = 2901713047668ULL; /* 36^1 + ... + 36^8 */

enum { SPACE_NATURAL = 0, SPACE_TOTAL = 1 };

/* the last two are used by the pool-builder translation unit only */
#if defined(__GNUC__) || defined(__clang__)
#define BS_MAYBE_UNUSED __attribute__((unused))
#else
#define BS_MAYBE_UNUSED
#endif
static uint64_t space_size(int space) {
	return space == SPACE_TOTAL ? SEEDSPACE_TOTAL : SEEDSPACE;
}
BS_MAYBE_UNUSED static const char *space_charset(int space) {
	return space == SPACE_TOTAL ? CHARSET_TOTAL : CHARSET;
}
BS_MAYBE_UNUSED static const char *space_name(int space) {
	return space == SPACE_TOTAL ? "total" : "natural";
}

/* rank -> seed in a given space; returns the seed's length. The length clamp
 * only matters for corrupt out-of-space ranks (callers validate first). */
static int make_seed_in(int space, uint64_t k, char out[9]) {
	if (space != SPACE_TOTAL) {
		make_seed(k, out);
		return 8;
	}
	uint64_t block = CHARSET_TOTAL_N;
	int len = 1;
	while (len < 8 && k >= block) {
		k -= block;
		block *= CHARSET_TOTAL_N;
		len++;
	}
	for (int i = 0; i < len; i++) {
		out[i] = CHARSET_TOTAL[k % CHARSET_TOTAL_N];
		k /= CHARSET_TOTAL_N;
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
typedef struct { double state; uint32_t gen; } Stream;

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
	uint32_t gen;
	PRNG prng;
	Stream joker4;
	Stream tagS[MAX_SEARCH_ANTE + 1], soulT[MAX_SEARCH_ANTE + 1];
	Stream soulS[MAX_SEARCH_ANTE + 1], edisouA[MAX_SEARCH_ANTE + 1];
	Stream voucher[9], shop_pack[MAX_SEARCH_ANTE + 1];
	Stream cdt[9], rarity_sho[9], rarity_buf[9], edisho[9], edibuf[9];
	Stream joker_sho[4][9], joker_buf[4][9];
	Stream resample[RBASES][MAX_RESAMPLE];
	Stream tagResample[MAX_SEARCH_ANTE + 1][MAX_RESAMPLE];
	int tagRoll[MAX_SEARCH_ANTE + 1][2];
	uint8_t tagRollDone[MAX_SEARCH_ANTE + 1][2];
	uint32_t packs_gen[MAX_SEARCH_ANTE + 1];
	int packs_n[MAX_SEARCH_ANTE + 1], pack_idx[MAX_SEARCH_ANTE + 1][6];
	/* per-candidate blind-skip assumption (mirrors skipsFromFilters /
	 * setPackSkipAssumption): a skipped blind's shop never opens, so its two
	 * get_pack picks never roll. forcedAnte = first ante that still opens a
	 * shop; that shop's slot 1 is the run's forced normal Buffoon. */
	uint8_t skipSm[MAX_SEARCH_ANTE + 1], skipBig[MAX_SEARCH_ANTE + 1]; int forcedAnte;
	/* Soul-capable pack opened immediately in place of a collected blind's shop:
	 * 1 = Charm/Mega Arcana, 2 = Ethereal/Spectral. A plain skipped blind is 0. */
	uint8_t rewardSm[MAX_SEARCH_ANTE + 1], rewardBig[MAX_SEARCH_ANTE + 1];
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
static const char *K_SOULS[9] = { 0, "soul_Spectral1", "soul_Spectral2", "soul_Spectral3", "soul_Spectral4",
	"soul_Spectral5", "soul_Spectral6", "soul_Spectral7", "soul_Spectral8" };
static const char *K_EDISOUA[9] = { 0, "edisou1", "edisou2", "edisou3", "edisou4",
	"edisou5", "edisou6", "edisou7", "edisou8" };

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
		char kb[64];
		snprintf(kb, sizeof kb, "%s_resample%d", RBASE_KEY[base], it);
		s->state = pseudohash_ks(kb, c->seed);
		s->gen = c->gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashed_seed) / 2.0;
}

/* pseudorandom(seed) / pseudorandom_element index (worker fast path). */
static inline double psr(Ctx *c, double seedval) {
	lj_random_seed(&c->prng, seedval);
	return lj_random(&c->prng);
}
static inline int psr_n(Ctx *c, double seedval, int n) {
	lj_random_seed(&c->prng, seedval);
	return lj_random_n(&c->prng, n);
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

static void resolve_forced_pack_ante(Ctx *c) {
	c->forcedAnte = 1;
	for (int a = 1; a <= MAX_SEARCH_ANTE; a++) {
		if (pack_max_slots(c, a) > 0) { c->forcedAnte = a; break; }
	}
}

static void sim_packs(Ctx *c, int a, int count) {
	const Config *g = c->g;
	if (a < 1 || a > MAX_SEARCH_ANTE) return;
	if (c->packs_gen[a] != c->gen) {
		c->packs_gen[a] = c->gen;
		c->packs_n[a] = 0;
		if (g->forceBuffoon && a == c->forcedAnte) {
			c->pack_idx[a][0] = PACK_FORCED;
			c->packs_n[a] = 1;
		}
	}
	int max = pack_max_slots(c, a);
	if (count > max) count = max;
	char dynamicKey[24];
	const char *packKey = a <= 8 ? K_SHOPPACK[a] : dynamicKey;
	if (a > 8) snprintf(dynamicKey, sizeof dynamicKey, "shop_pack%d", a);
	while (c->packs_n[a] < count) {
		double poll = psr(c, stream_next(c, &c->shop_pack[a], packKey)) * g->boostCume;
		double it = 0.0;
		int pick = -1;
		for (int i = 0; i < g->nboost; i++) {
			if (!g->boostAvail[i]) continue;
			it += g->boostW[i];
			if (it >= poll && it - g->boostW[i] <= poll) { pick = i; break; }
		}
		c->pack_idx[a][c->packs_n[a]++] = pick;
	}
}

/* Charm/Ethereal tags do more than remove a shop: skip_blind immediately
 * opens their reward pack at that blind choice, before the next reachable
 * shop. Those packs consume the same per-Ante Soul streams as shop packs. */
static int tag_reward_kind(const Config *g, int tagIndex) {
	if (tagIndex < 0 || tagIndex >= g->ntags) return 0;
	if (!strcmp(g->tagKey[tagIndex], "tag_charm")) return 1;
	if (!strcmp(g->tagKey[tagIndex], "tag_ethereal")) return 2;
	return 0;
}

static int tag_reward_kind_key(const char *key) {
	if (key && !strcmp(key, "tag_charm")) return 1;
	if (key && !strcmp(key, "tag_ethereal")) return 2;
	return 0;
}

typedef struct {
	int soulKind, cards;
	int shopSlot; /* 1-based flattened physical shop-pack slot; 0 for tag reward */
	int blind;    /* -1 shop, 0 Small reward, 1 Big reward */
} SoulPackEvent;

static void append_shop_pair(const Ctx *c, int ante, int *cursor,
		SoulPackEvent events[8], int *n) {
	const Config *g = c->g;
	for (int i = 0; i < 2 && *cursor < c->packs_n[ante]; i++) {
		int slot = (*cursor)++;
		int pi = c->pack_idx[ante][slot];
		SoulPackEvent *e = &events[(*n)++];
		e->soulKind = pi >= 0 ? g->boostSoul[pi] : 0;
		e->cards = pi >= 0 ? g->boostCards[pi] : 0;
		e->shopSlot = slot + 1;
		e->blind = -1;
	}
}

static void append_tag_reward(const Ctx *c, int kind, int blind,
		SoulPackEvent events[8], int *n) {
	if (!kind) return;
	SoulPackEvent *e = &events[(*n)++];
	e->soulKind = kind;
	e->cards = c->g->tagRewardCards[kind];
	e->shopSlot = 0;
	e->blind = blind;
}

/* Build one Ante's opened-pack timeline. Antes 2+ begin with the previous
 * boss's entry shop; each played Small/Big then contributes its two shop pack
 * offers, while a skipped Charm/Ethereal blind contributes its immediate tag
 * reward instead. Shop-pack RNG advances remain compressed across skips. */
static int soul_pack_events(Ctx *c, int ante, SoulPackEvent events[8]) {
	sim_packs(c, ante, 6);
	int cursor = 0, n = 0;
	if (ante >= 2) append_shop_pair(c, ante, &cursor, events, &n);
	if (c->skipSm[ante]) append_tag_reward(c, c->rewardSm[ante], 0, events, &n);
	else append_shop_pair(c, ante, &cursor, events, &n);
	if (c->skipBig[ante]) append_tag_reward(c, c->rewardBig[ante], 1, events, &n);
	else append_shop_pair(c, ante, &cursor, events, &n);
	return n;
}

static void soul_event_location(char *out, size_t outsz, int ante,
		const SoulPackEvent *event) {
	if (event->blind < 0) {
		snprintf(out, outsz, "LegA%dP%d", ante, event->shopSlot);
	} else {
		snprintf(out, outsz, "LegA%d%s%s", ante,
				event->soulKind == 1 ? "Charm" : "Ethereal",
				event->blind == 0 ? "Sm" : "Big");
	}
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
			if (g->needNeg) neg = psr(c, stream_next(c, &c->edisho[a], K_EDISHO[a])) > 0.997;
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
			if (g->needNeg) neg = psr(c, stream_next(c, &c->edibuf[a], K_EDIBUF[a])) > 0.997;
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
	int prev = -1;
	bool any = false;
	for (int a = 1; a <= max_ante; a++) {
		int idx = pick_culled(c, &c->voucher[a], K_VOUCHER[a], RB_VOUCHER(a),
				g->vouchAvail, g->nvouch, prev);
		if (idx < 0) return false;
		if (any_to) {
			if (!strcmp(g->vouchKey[idx], g->voucher)) any = true;
		} else if (a == mode) {
			return !strcmp(g->vouchKey[idx], g->voucher);
		}
		prev = idx;
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
	char key[32];
	snprintf(key, sizeof key, "Tag%d", a);
	int idx = psr_n(c, stream_next(c, &c->tagS[a], key), g->ntags);
	int it = 1;
	while (idx > 0 && !(g->tagReqOk[idx - 1] && (g->tagMinAnte[idx - 1] == 0 || g->tagMinAnte[idx - 1] <= a))) {
		it++;
		if (it - 2 >= MAX_RESAMPLE) return -1;
		char rkey[48];
		snprintf(rkey, sizeof rkey, "Tag%d_resample%d", a, it);
		double sv = stream_next(c, &c->tagResample[a][it - 2], rkey);
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
			int needRoll = 0;
			for (int r = 0; r < g->npoolRouteRules; r++) {
				if (g->poolRouteRules[r].collect && counts[r] < g->poolRouteRules[r].minCount
						&& a >= g->poolRouteRules[r].minAnte
						&& a <= g->poolRouteRules[r].maxAnte) { needRoll = 1; break; }
			}
			if (!needRoll) continue;
			int idx = roll_tag_at(c, a, blind);
			if (idx < 0) return false;
			for (int r = 0; r < g->npoolRouteRules; r++) {
				if (!g->poolRouteRules[r].collect
						|| counts[r] >= g->poolRouteRules[r].minCount
						|| a < g->poolRouteRules[r].minAnte
						|| a > g->poolRouteRules[r].maxAnte
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

/* Pool criteria and active overlay filters must each read their RNG streams
 * from the beginning, while sharing the final merged blind-skip route. Reset
 * only the streams touched by the pool's cumulative Soul oracle; tag-roll
 * caches and skip arrays intentionally survive. */
static void reset_pool_legend_streams(Ctx *c) {
	memset(&c->joker4, 0, sizeof c->joker4);
	memset(c->resample[RB_JOKER4], 0, sizeof c->resample[RB_JOKER4]);
	memset(c->shop_pack, 0, sizeof c->shop_pack);
	memset(c->soulT, 0, sizeof c->soulT);
	memset(c->soulS, 0, sizeof c->soulS);
	memset(c->edisouA, 0, sizeof c->edisouA);
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
	int needDepth = 1, maxAnte = 0;
	for (int i = 0; i < g->npoolLegendRules; i++) {
		if (g->poolLegendRules[i].soulDepth > needDepth)
			needDepth = g->poolLegendRules[i].soulDepth;
		if (g->poolLegendRules[i].maxAnte > maxAnte)
			maxAnte = g->poolLegendRules[i].maxAnte;
	}
	int second = -1;
	if (needDepth == 2) {
		second = pick_culled(c, &c->joker4, "Joker4", RB_JOKER4,
				g->jokerAvail[4], g->njoker[4], first);
		if (second < 0) return false;
	}
	for (int i = 0; i < g->npoolLegendRules; i++) {
		int chosen = g->poolLegendRules[i].soulDepth == 1 ? first : second;
		if (chosen != g->poolLegendRules[i].poolIndex) return false;
	}

	int found = 0, eventAnte[3] = { 0 };
	double eventEdition[3] = { 0.0 };
	for (int ante = 1; ante <= maxAnte && found < needDepth; ante++) {
		SoulPackEvent packs[8];
		int npacks = soul_pack_events(c, ante, packs);
		for (int slot = 0; slot < npacks && found < needDepth; slot++) {
			int kind = packs[slot].soulKind, cards = packs[slot].cards;
			if (!kind) continue;
			Stream *stream = kind == 1 ? &c->soulT[ante] : &c->soulS[ante];
			char soulKey[32];
			snprintf(soulKey, sizeof soulKey, "soul_%s%d",
					kind == 1 ? "Tarot" : "Spectral", ante);
			bool soulInPack = false, blackHoleInPack = false;
			for (int card = 0; card < cards && found < needDepth; card++) {
				bool soul = false;
				if (!soulInPack)
					soul = psr(c, stream_next(c, stream, soulKey)) > 0.997;
				if (kind == 2 && !blackHoleInPack) {
					bool blackHole = psr(c, stream_next(c, stream, soulKey)) > 0.997;
					if (blackHole) {
						if (g->blackHoleAllowed) blackHoleInPack = true;
						soul = false;
					}
				}
				if (!soul) continue;
				soulInPack = true;
				found++;
				eventAnte[found] = ante;
				char editionKey[24];
				snprintf(editionKey, sizeof editionKey, "edisou%d", ante);
				eventEdition[found] = psr(c,
						stream_next(c, &c->edisouA[ante], editionKey));
			}
		}
	}
	if (found < needDepth) return false;
	for (int i = 0; i < g->npoolLegendRules; i++) {
		int depth = g->poolLegendRules[i].soulDepth;
		if (eventAnte[depth] < g->poolLegendRules[i].minAnte
				|| eventAnte[depth] > g->poolLegendRules[i].maxAnte
				|| (g->poolLegendRules[i].neg && eventEdition[depth] <= 0.997)) return false;
	}
	return true;
}

/* checkLegendaryAnywhere: the run's FIRST Soul across chronological collected
 * Charm/Ethereal rewards and reachable shop packs in antes 1-8. */
static bool check_legendary_anywhere(Ctx *c, const char **locOut) {
	const Config *g = c->g;
	if (!g->soulAllowed) return false;
	for (int a = 1; a <= 8; a++) {
		SoulPackEvent packs[8];
		int npacks = soul_pack_events(c, a, packs);
		for (int slot = 0; slot < npacks; slot++) {
			int sk = packs[slot].soulKind;
			if (!sk) continue;
			int ncards = packs[slot].cards;
			Stream *ss = (sk == 1) ? &c->soulT[a] : &c->soulS[a];
			const char *skey = (sk == 1) ? K_SOULT[a] : K_SOULS[a];
			bool soul_in_pack = false, bh_in_pack = false;
			for (int card = 0; card < ncards; card++) {
				bool soul = false;
				if (!soul_in_pack)
					soul = psr(c, stream_next(c, ss, skey)) > 0.997;
				if (sk == 2 && !bh_in_pack) {
					bool bh = psr(c, stream_next(c, ss, skey)) > 0.997;
					if (bh) {
						/* A banned Black Hole still consumes its roll and
						 * overwrites a same-card Soul, but it never enters the
						 * pack and therefore cannot suppress later BH rolls. */
						if (g->blackHoleAllowed) bh_in_pack = true;
						soul = false; /* black hole overwrites the soul */
					}
				}
				if (soul) {
					int idx = pick_culled(c, &c->joker4, "Joker4", RB_JOKER4,
							g->jokerAvail[4], g->njoker[4], -1);
					if (idx < 0) return false;
					if (strcmp(g->jokerKey[4][idx], g->legendary)) return false;
					if (g->negLegendary
							&& psr(c, stream_next(c, &c->edisouA[a], K_EDISOUA[a])) <= 0.997) {
						return false;
					}
					soul_event_location(c->legloc, sizeof c->legloc, a, &packs[slot]);
					*locOut = c->legloc;
					return true;
				}
			}
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
	memset(c->tagRollDone, 0, sizeof c->tagRollDone);
	bool legAnywhere = g->legAnywhere && g->legendary[0];
	const char *tagLoc = NULL, *legLoc = NULL;
	char tagLocBuf[12];

	/* 1) soul / legendary, ante-1 charm-tag convention (replaced by the
	 * anywhere pack-scan below when the toggle is on) */
	if (!legAnywhere && (g->soulCount > 0 || g->legendary[0])) {
		if (!g->soulAllowed) return false;
		int needed = g->soulCount > 0 ? g->soulCount : 1;
		bool last = false;
		for (int i = 0; i < needed; i++) {
			bool found = false;
			for (int j = 0; j < g->tagRewardCards[1]; j++) {
				if (psr(c, stream_next(c, &c->soulT[1], K_SOULT[1])) > 0.997) found = true;
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
			if (g->negLegendary && psr(c, stream_next(c, &c->edisouA[1], K_EDISOUA[1])) <= 0.997) return false;
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
		int reward = tag_reward_kind_key(g->tag);
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
	resolve_forced_pack_ante(c);
	if (g->npoolLegendRules) {
		reset_pool_legend_streams(c);
		if (!check_pool_legend_rules(c)) return false;
		reset_pool_legend_streams(c);
	}
	/* 2.5) legendary ANYWHERE: first Soul across antes 1-8 */
	if (legAnywhere) {
		if (!check_legendary_anywhere(c, &legLoc)) return false;
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
 * total-space callers group same-length candidates before batching). */
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

static Stream *first_stream(Ctx *c) {
	const Config *g = c->g;
	switch (g->fsId) {
	case FS_SOUL: return &c->soulT[1];
	case FS_TAG: return &c->tagS[1];
	case FS_PACK: return &c->shop_pack[1];
	case FS_VOUCH: return &c->voucher[1];
	case FS_JCDT: return &c->cdt[g->fsAnte];
	case FS_JPACK: return &c->shop_pack[g->fsAnte];
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

static bool load_config(const char *path, Config *g, char *err, size_t errsz) {
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
			g->nvouch++;
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

	/* booster cume (array order, matches the Lua float sum); card counts come
	 * from config.extra via the config -- name fallback is source-corrected
	 * (mega/jumbo are 4-card packs for Buffoon/Spectral, never 6). */
	g->boostCume = 0.0;
	for (int i = 0; i < g->nboost; i++) {
		if (!isfinite(g->boostW[i]) || g->boostW[i] <= 0.0) {
			snprintf(err, errsz, "config has an invalid booster weight"); return false;
		}
		if (g->boostCards[i] < 0 || g->boostCards[i] > 64) {
			snprintf(err, errsz, "config has an invalid booster card count"); return false;
		}
		if (g->boostAvail[i]) g->boostCume += g->boostW[i];
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
	if (g->nboost == 0 || g->boostCume <= 0.0) {
		snprintf(err, errsz, "config has no available booster packs");
		return false;
	}
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
	else if (legAny) { g->fsId = FS_PACK; g->fsAnte = 1; g->fsKey = K_SHOPPACK[1]; }
	else if (g->npack > 0) { g->fsId = FS_PACK; g->fsAnte = 1; g->fsKey = K_SHOPPACK[1]; }
	else if (g->voucher[0]) { g->fsId = FS_VOUCH; g->fsAnte = 1; g->fsKey = K_VOUCHER[1]; }
	else if (g->ntargets > 0 && g->anyAnteActive) {
		for (int a = 1; a <= 8; a++) {
			if (g->maSlots[a] > 0) { g->fsId = FS_JCDT; g->fsAnte = a; g->fsKey = K_CDT[a]; break; }
			if (g->maPacks[a]) { g->fsId = FS_JPACK; g->fsAnte = a; g->fsKey = K_SHOPPACK[a]; break; }
		}
	}
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

/* Decide g_seed_fma from the checks. Returns false if neither mode works. */
static bool calibrate(const Config *g, char *err, size_t errsz) {
	if (g->nchecks == 0) { g_seed_fma = 0; return true; } /* nothing to calibrate against */
	char ff[128];
	g_seed_fma = 0;
	if (run_checks(g, ff, sizeof ff) == 0) return true;
	g_seed_fma = 1;
	if (run_checks(g, ff, sizeof ff) == 0) return true;
	snprintf(err, errsz, "parity checks failed in both fp modes (%s)", ff);
	return false;
}

/* ----------------------------------------------------- .bspool contract --
 * Shared between the exhaustive pool builder (which writes pools) and the
 * interactive searcher (which can restrict a search to one). The header is a
 * fixed-size zero-padded text block so a shared pool is self-describing:
 * versions, scanned range, fingerprints, AND the criteria that built it. */
#define BSPOOL_SCHEMA_LEGACY 1
#define BSPOOL_SCHEMA 2
#define BSPOOL_HEADER_SIZE 1024
#define BSPOOL_MAX_TAG_RULES MAX_POOL_ROUTE_RULES
#define BSPOOL_MAX_ANTE MAX_SEARCH_ANTE
#define BSPOOL_BLOCK_HEADER_SIZE 32
#define BSPOOL_BLOCK_MAX_RECORDS 8192
#define BSPOOL_BLOCK_MAX_PAYLOAD ((BSPOOL_BLOCK_MAX_RECORDS - 1) * 6)
#define BSPOOL_INDEX_ENTRY_SIZE 24
#define BSPOOL_FOOTER_SIZE 40

enum { BSPOOL_ENCODING_U64 = 1, BSPOOL_ENCODING_DELTA_BLOCKS = 2 };

typedef struct {
	int schema, modelver, complete, headerBytes, encoding, mergedParts;
	uint64_t seedspace, rangeStart, rangeEnd, records, dataBytes;
	uint64_t catalogHash, criteriaHash;
	char charset[64];
	int space;           /* SPACE_NATURAL / SPACE_TOTAL, derived from charset */
	char label[136];     /* optional user-given pool name (may contain spaces) */
	char poolId[24];     /* short shareable fingerprint, hex */
	int refilterDepth;
	int route; /* 1 = collect (tag blinds skipped), 0 = observe */
	int ntagRules;
	struct { char key[MAX_KEY]; int minAnte, maxAnte, minCount; } tagRules[BSPOOL_MAX_TAG_RULES];
	int nrouteTagRules;
	struct { char key[MAX_KEY]; int minAnte, maxAnte, minCount, collect; }
		routeTagRules[BSPOOL_MAX_TAG_RULES];
	struct { int used; char key[MAX_KEY]; int minAnte, maxAnte, neg, soulDepth; } legendary;
	int nrouteLegendRules;
	struct { char key[MAX_KEY]; int minAnte, maxAnte, neg, soulDepth; }
		routeLegendRules[MAX_POOL_LEGEND_RULES];
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

/* Legendary constraints from successive refilter stages describe the same
 * physical Soul #1/#2 sequence. Canonicalize them to at most one intersected
 * rule per depth; conflicting targets or disjoint Ante windows mean the pool
 * header cannot describe any valid seed on one cumulative route. */
static bool bspool_add_legend_rule(BspoolHeader *h, const char *key,
		int minAnte, int maxAnte, int neg, int soulDepth) {
	for (int i = 0; i < h->nrouteLegendRules; i++) {
		if (h->routeLegendRules[i].soulDepth != soulDepth) continue;
		if (strcmp(h->routeLegendRules[i].key, key)) return false;
		if (minAnte > h->routeLegendRules[i].minAnte)
			h->routeLegendRules[i].minAnte = minAnte;
		if (maxAnte < h->routeLegendRules[i].maxAnte)
			h->routeLegendRules[i].maxAnte = maxAnte;
		h->routeLegendRules[i].neg = h->routeLegendRules[i].neg || neg;
		return h->routeLegendRules[i].minAnte <= h->routeLegendRules[i].maxAnte;
	}
	if (h->nrouteLegendRules >= MAX_POOL_LEGEND_RULES) return false;
	int i = h->nrouteLegendRules++;
	snprintf(h->routeLegendRules[i].key, MAX_KEY, "%s", key);
	h->routeLegendRules[i].minAnte = minAnte;
	h->routeLegendRules[i].maxAnte = maxAnte;
	h->routeLegendRules[i].neg = neg;
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
		|| !strcmp(d, "vouchdef") || !strcmp(d, "jokerdef")
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
	h->route = 1;
	h->legendary.soulDepth = 1;
	char buf[BSPOOL_HEADER_SIZE + 1];
	if (bs_fseeko(f, 0, SEEK_SET) != 0) { snprintf(err, errsz, "cannot rewind pool"); return false; }
	size_t got = fread(buf, 1, BSPOOL_HEADER_SIZE, f);
	if (got != BSPOOL_HEADER_SIZE) { snprintf(err, errsz, "pool header is truncated"); return false; }
	buf[got] = 0;
	char *cur = buf;
	enum {
		HS_MAGIC = 1u << 0, HS_MODEL = 1u << 1, HS_ENCODING = 1u << 2,
		HS_CHARSET = 1u << 3, HS_SEEDSPACE = 1u << 4, HS_RANGE_START = 1u << 5,
		HS_RANGE_END = 1u << 6, HS_CATALOG = 1u << 7, HS_CRITERIA = 1u << 8,
		HS_RECORDS = 1u << 9, HS_DATA_BYTES = 1u << 10, HS_COMPLETE = 1u << 11,
		HS_HEADER_BYTES = 1u << 12
	};
	const unsigned required = HS_MAGIC | HS_MODEL | HS_ENCODING | HS_CHARSET
		| HS_SEEDSPACE | HS_RANGE_START | HS_RANGE_END | HS_CATALOG
		| HS_CRITERIA | HS_RECORDS | HS_COMPLETE | HS_HEADER_BYTES;
	unsigned seen = 0;
	int sawEnd = 0, malformed = 0, sawSoulDepth = 0;
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
		else if (!strcmp(d, "pool_id")) {
			v = pool_tok(&sp); if (!v || strlen(v) >= sizeof h->poolId || !pool_header_no_more(&sp)) malformed = 1;
			else snprintf(h->poolId, sizeof h->poolId, "%s", v);
		}
		else if (!strcmp(d, "refilter_depth")) {
			v = pool_tok(&sp); if (!pool_header_int(v, &h->refilterDepth)
					|| h->refilterDepth < 0 || !pool_header_no_more(&sp)) malformed = 1;
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
				char *a = pool_tok(&sp), *b = pool_tok(&sp), *c = pool_tok(&sp);
				int ia, ib, ic;
				if (k && strlen(k) < MAX_KEY && pool_header_int(a, &ia)
						&& pool_header_int(b, &ib) && pool_header_int(c, &ic)
						&& pool_header_no_more(&sp)) {
					snprintf(h->tagRules[h->ntagRules].key, MAX_KEY, "%s", k);
					h->tagRules[h->ntagRules].minAnte = ia;
					h->tagRules[h->ntagRules].maxAnte = ib;
					h->tagRules[h->ntagRules].minCount = ic;
					h->ntagRules++;
				} else malformed = 1;
			} else malformed = 1;
		}
		else if (!strcmp(d, "route_tag")) {
			if (h->nrouteTagRules < BSPOOL_MAX_TAG_RULES) {
				char *mode = pool_tok(&sp), *k = pool_tok(&sp);
				char *a = pool_tok(&sp), *b = pool_tok(&sp), *c = pool_tok(&sp);
				int ia, ib, ic;
				if (mode && k && strlen(k) < MAX_KEY && pool_header_int(a, &ia)
						&& pool_header_int(b, &ib) && pool_header_int(c, &ic)
						&& (!strcmp(mode, "collect") || !strcmp(mode, "observe"))
						&& pool_header_no_more(&sp)) {
					int i = h->nrouteTagRules++;
					snprintf(h->routeTagRules[i].key, MAX_KEY, "%s", k);
					h->routeTagRules[i].minAnte = ia;
					h->routeTagRules[i].maxAnte = ib;
					h->routeTagRules[i].minCount = ic;
					h->routeTagRules[i].collect = !strcmp(mode, "collect");
				} else malformed = 1;
			} else malformed = 1;
		}
		else if (!strcmp(d, "route_legendary")) {
			char *k = pool_tok(&sp), *a = pool_tok(&sp), *b = pool_tok(&sp);
			char *n = pool_tok(&sp), *depth = pool_tok(&sp);
			int ia, ib, in, id;
			if (!k || strlen(k) >= MAX_KEY || !pool_header_int(a, &ia)
					|| !pool_header_int(b, &ib) || !pool_header_int(n, &in)
					|| !pool_header_int(depth, &id) || !pool_header_no_more(&sp)
					|| ia < 1 || ib < ia || ib > BSPOOL_MAX_ANTE
					|| (in != 0 && in != 1) || id < 1 || id > 2
					|| !bspool_add_legend_rule(h, k, ia, ib, in, id)) malformed = 1;
		}
		else if (!strcmp(d, "legendary")) {
			char *k = pool_tok(&sp);
			char *a = pool_tok(&sp), *b = pool_tok(&sp), *n = pool_tok(&sp);
			int ia, ib, in = 0;
			if (!h->legendary.used && k && strlen(k) < MAX_KEY
					&& pool_header_int(a, &ia) && pool_header_int(b, &ib)
					&& (!n || pool_header_int(n, &in)) && (in == 0 || in == 1)
					&& pool_header_no_more(&sp)) {
				h->legendary.used = 1;
				snprintf(h->legendary.key, MAX_KEY, "%s", k);
				h->legendary.minAnte = ia;
				h->legendary.maxAnte = ib;
				h->legendary.neg = in;
			} else malformed = 1;
		}
		else if (!strcmp(d, "soul_depth")) {
			v = pool_tok(&sp);
			if (sawSoulDepth || !pool_header_int(v, &h->legendary.soulDepth)
					|| !pool_header_no_more(&sp)) malformed = 1;
			sawSoulDepth = 1;
		}
		else if (!strcmp(d, "end")) {
			if (!pool_header_no_more(&sp)) malformed = 1;
			sawEnd = 1;
		}
	}
	if (!(seen & HS_MAGIC)) { snprintf(err, errsz, "not a Brainstorm seed pool"); return false; }
	if (malformed || (seen & required) != required
			|| (sawSoulDepth && !h->legendary.used)) {
		snprintf(err, errsz, "pool header is malformed or missing required metadata"); return false;
	}
	if (h->schema != BSPOOL_SCHEMA_LEGACY && h->schema != BSPOOL_SCHEMA) {
		snprintf(err, errsz, "pool schema %d unsupported (want %d or %d)",
				h->schema, BSPOOL_SCHEMA_LEGACY, BSPOOL_SCHEMA);
		return false;
	}
	if (!sawEnd) { snprintf(err, errsz, "pool header has no end marker"); return false; }
	if (!strcmp(encoding, "u64le")) h->encoding = BSPOOL_ENCODING_U64;
	else if (!strcmp(encoding, "delta-varint-blocks-v1")) h->encoding = BSPOOL_ENCODING_DELTA_BLOCKS;
	else { snprintf(err, errsz, "pool encoding '%s' is unsupported", encoding); return false; }
	if (h->schema == BSPOOL_SCHEMA_LEGACY && h->encoding != BSPOOL_ENCODING_U64) {
		snprintf(err, errsz, "legacy pool must use u64le encoding"); return false;
	}
	if (h->schema == BSPOOL_SCHEMA && h->encoding != BSPOOL_ENCODING_DELTA_BLOCKS) {
		snprintf(err, errsz, "schema %d pool must use delta blocks", BSPOOL_SCHEMA); return false;
	}
	if (h->schema == BSPOOL_SCHEMA && !(seen & HS_DATA_BYTES)) {
		snprintf(err, errsz, "compressed pool is missing its committed byte count"); return false;
	}
	/* The charset decides which seed space the ranks index (the human-readable
	 * `space` header line is informational). Unknown charsets are refused: a
	 * newer format should fail loudly, never decode to wrong seeds. */
	if (!strcmp(h->charset, CHARSET)) h->space = SPACE_NATURAL;
	else if (!strcmp(h->charset, CHARSET_TOTAL)) h->space = SPACE_TOTAL;
	else { snprintf(err, errsz, "pool charset differs"); return false; }
	if (h->seedspace != space_size(h->space)) { snprintf(err, errsz, "pool seed space differs"); return false; }
	if (h->rangeStart >= h->rangeEnd || h->rangeEnd > h->seedspace
			|| h->records > h->rangeEnd - h->rangeStart) {
		snprintf(err, errsz, "pool range or record count is invalid");
		return false;
	}
	if (h->headerBytes != BSPOOL_HEADER_SIZE) {
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
		h->routeTagRules[j].maxAnte = h->tagRules[i].maxAnte;
		h->routeTagRules[j].minCount = h->tagRules[i].minCount;
		h->routeTagRules[j].collect = h->route;
	}
	for (int i = 0; i < h->nrouteTagRules; i++) {
		if (!h->routeTagRules[i].key[0] || h->routeTagRules[i].minAnte < 1
				|| h->routeTagRules[i].maxAnte < h->routeTagRules[i].minAnte
				|| h->routeTagRules[i].maxAnte > BSPOOL_MAX_ANTE
				|| h->routeTagRules[i].minCount < 1
				|| h->routeTagRules[i].minCount > 2 * (h->routeTagRules[i].maxAnte
						- h->routeTagRules[i].minAnte + 1)) {
			snprintf(err, errsz, "pool has an invalid embedded tag route"); return false;
		}
	}
	if (h->legendary.used && (h->legendary.minAnte < 1
			|| h->legendary.maxAnte < h->legendary.minAnte
			|| h->legendary.maxAnte > BSPOOL_MAX_ANTE
			|| h->legendary.soulDepth < 1 || h->legendary.soulDepth > 2)) {
		snprintf(err, errsz, "pool has an invalid embedded legendary rule"); return false;
	}
	if (h->legendary.used && !bspool_add_legend_rule(h, h->legendary.key,
			h->legendary.minAnte, h->legendary.maxAnte, h->legendary.neg,
			h->legendary.soulDepth)) {
		snprintf(err, errsz, "pool has conflicting cumulative legendary rules"); return false;
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

static bool bspool_block_header(int fd, uint64_t off, uint32_t *count,
		uint32_t *payloadBytes, uint32_t *checksum, uint64_t *first,
		uint64_t *last) {
	unsigned char raw[BSPOOL_BLOCK_HEADER_SIZE];
	if (bs_pread(fd, raw, sizeof raw, (int64_t)off) != (int64_t)sizeof raw
			|| memcmp(raw, "BSP2", 4)) return false;
	*count = bspool_get_u32le(raw + 4);
	*payloadBytes = bspool_get_u32le(raw + 8);
	*checksum = bspool_get_u32le(raw + 12);
	*first = bspool_get_u64le(raw + 16);
	*last = bspool_get_u64le(raw + 24);
	return *count > 0 && *count <= BSPOOL_BLOCK_MAX_RECORDS
			&& *payloadBytes <= (uint32_t)((*count - 1u) * 6u);
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
		if (fileBytes < BSPOOL_FOOTER_SIZE) { snprintf(err, errsz, "compressed pool has no index footer"); return false; }
		unsigned char footer[BSPOOL_FOOTER_SIZE];
		if (bs_pread(fd, footer, sizeof footer, (int64_t)(fileBytes - sizeof footer)) != (int64_t)sizeof footer
				|| memcmp(footer, "BSPIDX2\n", 8)) {
			snprintf(err, errsz, "compressed pool index footer is missing"); return false;
		}
		uint64_t indexOff = bspool_get_u64le(footer + 8);
		r->nblocks = bspool_get_u64le(footer + 16);
		uint64_t indexRecords = bspool_get_u64le(footer + 24);
		uint64_t footerDataBytes = bspool_get_u64le(footer + 32);
		if (indexOff != r->dataOff + r->dataBytes || indexRecords != r->records
				|| footerDataBytes != r->dataBytes || r->nblocks > r->records
				|| r->nblocks > SIZE_MAX / sizeof *r->blocks
				|| r->nblocks > (UINT64_MAX - indexOff - BSPOOL_FOOTER_SIZE) / BSPOOL_INDEX_ENTRY_SIZE
				|| indexOff + r->nblocks * BSPOOL_INDEX_ENTRY_SIZE + BSPOOL_FOOTER_SIZE != fileBytes) {
			snprintf(err, errsz, "compressed pool index metadata is inconsistent"); return false;
		}
		if (r->nblocks) {
			r->blocks = malloc((size_t)r->nblocks * sizeof *r->blocks);
			if (!r->blocks) { snprintf(err, errsz, "cannot allocate compressed pool index"); return false; }
		}
		unsigned char raw[BSPOOL_INDEX_ENTRY_SIZE * 4096];
		uint64_t done = 0;
		while (done < r->nblocks) {
			uint64_t n = r->nblocks - done;
			if (n > 4096) n = 4096;
			size_t bytes = (size_t)n * BSPOOL_INDEX_ENTRY_SIZE;
			if (bs_pread(fd, raw, bytes, (int64_t)(indexOff + done * BSPOOL_INDEX_ENTRY_SIZE)) != (int64_t)bytes) {
				snprintf(err, errsz, "cannot read compressed pool index"); goto fail;
			}
			for (uint64_t i = 0; i < n; i++) {
				unsigned char *p = raw + i * BSPOOL_INDEX_ENTRY_SIZE;
				BspoolBlockIndex *e = &r->blocks[done + i];
				e->offset = bspool_get_u64le(p);
				e->firstRecord = bspool_get_u64le(p + 8);
				e->count = bspool_get_u32le(p + 16);
				e->payloadBytes = bspool_get_u32le(p + 20);
			}
			done += n;
		}
	} else {
		uint64_t off = r->dataOff, end = r->dataOff + r->dataBytes, firstRecord = 0, cap = 0;
		while (off < end) {
			uint32_t count, payload, checksum; uint64_t first, last;
			if (end - off < BSPOOL_BLOCK_HEADER_SIZE
					|| !bspool_block_header(fd, off, &count, &payload, &checksum, &first, &last)
					|| payload > end - off - BSPOOL_BLOCK_HEADER_SIZE) {
				snprintf(err, errsz, "compressed pool has a malformed committed block"); goto fail;
			}
			BspoolBlockIndex e = { off, firstRecord, count, payload };
			if (!bspool_index_push(r, &cap, e)) { snprintf(err, errsz, "cannot allocate compressed pool index"); goto fail; }
			firstRecord += count;
			off += BSPOOL_BLOCK_HEADER_SIZE + payload;
		}
		if (off != end || firstRecord != r->records) {
			snprintf(err, errsz, "compressed pool blocks do not match committed records"); goto fail;
		}
	}
	if ((r->records == 0) != (r->nblocks == 0)) { snprintf(err, errsz, "compressed pool index is empty or incomplete"); goto fail; }
	uint64_t expectedRecord = 0, expectedOffset = r->dataOff;
	for (uint64_t i = 0; i < r->nblocks; i++) {
		BspoolBlockIndex *e = &r->blocks[i];
		if (e->firstRecord != expectedRecord || !e->count
				|| e->count > BSPOOL_BLOCK_MAX_RECORDS
				|| e->payloadBytes > (e->count - 1u) * 6u
				|| e->count > r->records - expectedRecord
				|| e->offset != expectedOffset || e->offset < r->dataOff
				|| e->offset > r->dataOff + r->dataBytes
				|| BSPOOL_BLOCK_HEADER_SIZE > r->dataOff + r->dataBytes - e->offset
				|| e->payloadBytes > r->dataOff + r->dataBytes - e->offset - BSPOOL_BLOCK_HEADER_SIZE) {
			snprintf(err, errsz, "compressed pool index entry is invalid"); goto fail;
		}
		expectedRecord += e->count;
		expectedOffset += BSPOOL_BLOCK_HEADER_SIZE + e->payloadBytes;
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
	uint32_t count, payload, checksum; uint64_t first, last;
	if (!bspool_block_header(r->fd, e->offset, &count, &payload, &checksum, &first, &last)
			|| count != e->count || payload != e->payloadBytes
			|| first < r->rangeStart || last >= r->rangeEnd || first > last
			|| !bspool_scratch_bytes(s, payload) || !bspool_scratch_ranks(s, count)) return false;
	if (payload && bs_pread(r->fd, s->bytes, payload,
			(int64_t)(e->offset + BSPOOL_BLOCK_HEADER_SIZE)) != (int64_t)payload) return false;
	if (bspool_checksum(s->bytes, payload) != checksum) return false;
	s->ranks[0] = first;
	size_t at = 0;
	for (uint32_t i = 1; i < count; i++) {
		uint64_t delta = 0; int shift = 0, sawEnd = 0;
		while (at < payload && shift <= 63) {
			unsigned char b = s->bytes[at++];
			if (shift == 63 && (b & 0x7e)) return false;
			delta |= (uint64_t)(b & 0x7f) << shift;
			if (!(b & 0x80)) { sawEnd = 1; break; }
			shift += 7;
		}
		if (!sawEnd || delta == 0 || UINT64_MAX - s->ranks[i - 1] < delta) return false;
		s->ranks[i] = s->ranks[i - 1] + delta;
		if (s->ranks[i] >= r->rangeEnd) return false;
	}
	if (at != payload || s->ranks[count - 1] != last) return false;
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
		uint64_t i = 0;
		/* The batched hash needs uniform seed lengths; total-space pool
		 * records mix lengths, so those go through the serial path below
		 * (pool searches are record-bounded -- batching is a full-space
		 * scan optimization, not a correctness requirement). */
		for (; g_pool.space == SPACE_NATURAL && i + ILV <= m
				&& !atomic_load_explicit(&g_stop, memory_order_relaxed); i += ILV) {
			for (int j = 0; j < ILV; j++) make_seed(ranks[i + (uint64_t)j], seeds[j]);
			batch_hash_seed(seeds, hseed);
			if (g->fsKey) batch_hash_key(g->fsKey, seeds, hfirst);
			for (int j = 0; j < ILV; j++) {
				if (passes_pre(c, seeds[j], hseed[j], g->fsKey ? hfirst[j] : 0.0)) {
					record_hit(c);
					break;
				}
			}
		}
		for (; i < m && !atomic_load_explicit(&g_stop, memory_order_relaxed); i++) {
			make_seed_in(g_pool.space, ranks[i], c->seed);
			if (passes(c)) record_hit(c);
		}
		atomic_fetch_add_explicit(&g_tried, (unsigned long long)m, memory_order_relaxed);
	}
	bspool_scratch_destroy(&scratch);
	free(ranks);
	free(c);
	atomic_fetch_sub(&g_pool.live, 1);
	return NULL;
}

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
	uint64_t k = w->start + (uint64_t)w->tid;
	uint64_t step = (uint64_t)g->threads;
	const int BATCH = 16384; /* multiple of ILV */
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	while (!atomic_load_explicit(&g_stop, memory_order_relaxed)) {
		for (int b = 0; b < BATCH; b += ILV) {
			for (int i = 0; i < ILV; i++) {
				make_seed(k % SEEDSPACE, seeds[i]);
				k += step;
			}
			batch_hash_seed(seeds, hseed);
			if (g->fsKey) batch_hash_key(g->fsKey, seeds, hfirst);
			for (int i = 0; i < ILV; i++) {
				if (passes_pre(c, seeds[i], hseed[i], g->fsKey ? hfirst[i] : 0.0)) {
					bs_mutex_lock(&g_found_mtx);
					if (!g_found) {
						g_found = true;
						memcpy(g_found_seed, c->seed, 9);
						snprintf(g_found_label, sizeof g_found_label, "%s", c->label);
					}
					bs_mutex_unlock(&g_found_mtx);
					atomic_store(&g_stop, true);
					break;
				}
			}
			if (atomic_load_explicit(&g_stop, memory_order_relaxed)) break;
		}
		atomic_fetch_add_explicit(&g_tried, (unsigned long long)BATCH, memory_order_relaxed);
	}
	free(c);
	return NULL;
}

/* "wb": the mod parses this file as exact bytes (LF only, all platforms). */
static void write_status(const char *path, const char *tmp, bool done, const char *emsg) {
	FILE *f = fopen(tmp, "wb");
	if (!f) return;
	fprintf(f, "P %llu\n", (unsigned long long)atomic_load(&g_tried));
	if (g_warn[0]) fprintf(f, "W %s\n", g_warn);
	if (emsg) fprintf(f, "E %s\n", emsg);
	if (g_found) fprintf(f, "R %s %s\n", g_found_seed, g_found_label[0] ? g_found_label : "-");
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
		g->poolRouteRules[j].maxAnte = h.routeTagRules[r].maxAnte;
		g->poolRouteRules[j].minCount = h.routeTagRules[r].minCount;
		g->poolRouteRules[j].collect = 1;
	}
	g->npoolLegendRules = 0;
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
		g->poolLegendRules[j].maxAnte = h.routeLegendRules[r].maxAnte;
		g->poolLegendRules[j].neg = h.routeLegendRules[r].neg;
		g->poolLegendRules[j].soulDepth = h.routeLegendRules[r].soulDepth;
	}
	if (!h.complete) {
		snprintf(g_warn, sizeof g_warn, "pool scan is incomplete (%llu records committed)",
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
		/* a fully dealt pool with no hit is a definitive verdict, not a retry */
		if (g_pool_active && atomic_load(&g_pool.live) == 0) {
			for (int i = 0; i < made; i++) bs_thread_join(th[i]);
			joined = true;
			if (g_found) break;
			if (atomic_load(&g_pool.failed)) {
				write_status(statusPath, tmp, true, "pool: record decode/read failed");
				return 1;
			}
			write_status(statusPath, tmp, true,
					"pool: no seed in the pool matches the active filters");
			return 3;
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
