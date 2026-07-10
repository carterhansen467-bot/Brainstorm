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
#include <math.h>
#include <stdatomic.h>
#include "platform.h" /* threads, positional IO, signals: POSIX or Win32 */

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
static double pseudohash_ks(const char *key, const char *seed8) {
	char buf[96];
	size_t kl = strlen(key);
	memcpy(buf, key, kl);
	memcpy(buf + kl, seed8, 8);
	int len = (int)kl + 8;
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
#define MODELVER 4 /* RNG/shop model + config protocol version; must match the
                    * config's modelver. 4 adds the poolfile directive -- an
                    * older binary would silently IGNORE it and search the full
                    * space, so the handshake must reject, not degrade. */

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
	uint8_t boostBuf[MAX_BOOST]; int boostCards[MAX_BOOST];
	uint8_t boostSoul[MAX_BOOST]; /* 0 none, 1 Arcana(Tarot), 2 Spectral */
	double boostCume;
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
	Stream tagS[9], soulT[9], soulS[9], edisouA[9];
	Stream voucher[9], shop_pack[9], cdt[9], rarity_sho[9], rarity_buf[9], edisho[9], edibuf[9];
	Stream joker_sho[4][9], joker_buf[4][9];
	Stream resample[RBASES][MAX_RESAMPLE];
	uint32_t packs_gen[9]; int packs_n[9]; int pack_idx[9][6];
	/* per-candidate blind-skip assumption (mirrors skipsFromFilters /
	 * setPackSkipAssumption): a skipped blind's shop never opens, so its two
	 * get_pack picks never roll. forcedAnte = first ante that still opens a
	 * shop; that shop's slot 1 is the run's forced normal Buffoon. */
	uint8_t skipSm[9], skipBig[9]; int forcedAnte;
	char label[64];
	char legloc[16];
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
	int shops = (a >= 2 ? 3 : 2) - (c->skipSm[a] ? 1 : 0) - (c->skipBig[a] ? 1 : 0);
	return 2 * shops;
}

static void sim_packs(Ctx *c, int a, int count) {
	const Config *g = c->g;
	if (c->packs_gen[a] != c->gen) {
		c->packs_gen[a] = c->gen;
		c->packs_n[a] = 0;
		if (a == c->forcedAnte) {
			c->pack_idx[a][0] = PACK_FORCED;
			c->packs_n[a] = 1;
		}
	}
	int max = pack_max_slots(c, a);
	if (count > max) count = max;
	while (c->packs_n[a] < count) {
		double poll = psr(c, stream_next(c, &c->shop_pack[a], K_SHOPPACK[a])) * g->boostCume;
		double it = 0.0;
		int pick = -1;
		for (int i = 0; i < g->nboost; i++) {
			it += g->boostW[i];
			if (it >= poll && it - g->boostW[i] <= poll) { pick = i; break; }
		}
		c->pack_idx[a][c->packs_n[a]++] = pick;
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
static int roll_tag(Ctx *c, int a) {
	const Config *g = c->g;
	int idx = psr_n(c, stream_next(c, &c->tagS[a], K_TAG[a]), g->ntags);
	int it = 1;
	while (idx > 0 && !(g->tagReqOk[idx - 1] && (g->tagMinAnte[idx - 1] == 0 || g->tagMinAnte[idx - 1] <= a))) {
		it++;
		double sv = resample_next(c, RB_TAG(a), it);
		if (isnan(sv)) return -1;
		idx = psr_n(c, sv, g->ntags);
	}
	return idx > 0 ? idx - 1 : -1;
}

/* checkLegendaryAnywhere: the run's FIRST Soul across antes 1-8 (see the Lua
 * original for the full source-verified model: per-card 'soul_'..type..ante
 * rolls, Spectral cards roll twice with black-hole OVERWRITING the soul,
 * later spectral cards skip the second roll once a black hole exists, first
 * Soul's legendary = first bare-'Joker4' advance, edition = 'edisou'..ante). */
static bool check_legendary_anywhere(Ctx *c, const char **locOut) {
	const Config *g = c->g;
	bool bh_found = false;
	for (int a = 1; a <= 8; a++) {
		sim_packs(c, a, 6);
		for (int slot = 0; slot < c->packs_n[a]; slot++) {
			int pi = c->pack_idx[a][slot];
			if (pi < 0) continue; /* forced buffoon / none: not soulable */
			int sk = g->boostSoul[pi];
			if (!sk) continue;
			int ncards = g->boostCards[pi];
			Stream *ss = (sk == 1) ? &c->soulT[a] : &c->soulS[a];
			const char *skey = (sk == 1) ? K_SOULT[a] : K_SOULS[a];
			for (int card = 0; card < ncards; card++) {
				bool soul = psr(c, stream_next(c, ss, skey)) > 0.997;
				if (sk == 2 && !bh_found) {
					bool bh = psr(c, stream_next(c, ss, skey)) > 0.997;
					if (bh) {
						bh_found = true;
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
					snprintf(c->legloc, sizeof c->legloc, "LegA%dP%d", a, slot + 1);
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
	bool legAnywhere = g->legAnywhere && g->legendary[0];
	const char *tagLoc = NULL, *legLoc = NULL;
	char tagLocBuf[12];

	/* 1) soul / legendary, ante-1 charm-tag convention (replaced by the
	 * anywhere pack-scan below when the toggle is on) */
	if (!legAnywhere && (g->soulCount > 0 || g->legendary[0])) {
		int needed = g->soulCount > 0 ? g->soulCount : 1;
		bool last = false;
		for (int i = 0; i < needed; i++) {
			bool found = false;
			for (int j = 0; j < 5; j++) {
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
				int idx = roll_tag(c, a);
				if (idx < 0) return false;
				if (!strcmp(g->tagKey[idx], g->tag)) {
					snprintf(tagLocBuf, sizeof tagLocBuf, "TagA%dSm", a);
					tagLoc = tagLocBuf;
					break;
				}
				idx = roll_tag(c, a);
				if (idx < 0) return false;
				if (!strcmp(g->tagKey[idx], g->tag)) {
					snprintf(tagLocBuf, sizeof tagLocBuf, "TagA%dBig", a);
					tagLoc = tagLocBuf;
					break;
				}
			}
			if (!tagLoc) return false;
		} else {
			int idx = roll_tag(c, 1);
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
	if (!legAnywhere && (g->soulCount > 0 || g->legendary[0])) c->skipSm[1] = 1;
	if (g->tag[0]) {
		if (g->tagAnywhere) {
			int a = atoi(tagLoc + 4); /* "TagA<n>Sm|Big" */
			if (a >= 1 && a <= 8) {
				if (tagLoc[strlen(tagLoc) - 1] == 'm') c->skipSm[a] = 1;
				else c->skipBig[a] = 1;
			}
		} else {
			c->skipSm[1] = 1;
		}
	}
	c->forcedAnte = 1;
	for (int a = 1; a <= 8; a++) {
		if (pack_max_slots(c, a) > 0) { c->forcedAnte = a; break; }
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

static void batch_hash_seed(const char seeds[ILV][9], double *out) {
	double num[ILV];
	for (int i = 0; i < ILV; i++) num[i] = 1.0;
	for (int pos = 8; pos >= 1; pos--) {
		double pi_pos = LUA_PI * (double)pos; /* same product as the serial code */
		for (int i = 0; i < ILV; i++) {
			double x = (1.1239285023 / num[i]) * (double)(unsigned char)seeds[i][pos - 1] * LUA_PI + pi_pos;
			num[i] = x - floor(x);
		}
	}
	for (int i = 0; i < ILV; i++) out[i] = num[i];
}

static void batch_hash_key(const char *key, const char seeds[ILV][9], double *out) {
	int kl = (int)strlen(key);
	int len = kl + 8;
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
static char *next_tok(char **sp) { return strsep(sp, " \t"); }
static int tok_int(char **sp) { char *t = next_tok(sp); return t ? atoi(t) : 0; }

static bool load_config(const char *path, Config *g, char *err, size_t errsz) {
	memset(g, 0, sizeof *g);
	g->threads = 1;
	FILE *f = fopen(path, "r");
	if (!f) { snprintf(err, errsz, "cannot open config %s", path); return false; }
	char line[512];
	while (fgets(line, sizeof line, f)) {
		size_t L = strlen(line);
		while (L && (line[L - 1] == '\n' || line[L - 1] == '\r')) line[--L] = 0;
		if (!L || line[0] == '#') continue;
		char *sp = line;
		char *d = next_tok(&sp);
		if (!d) continue;
		if (!strcmp(d, "threads")) g->threads = atoi(next_tok(&sp));
		else if (!strcmp(d, "modelver")) g->modelver = tok_int(&sp);
		else if (!strcmp(d, "entropy")) g->entropy = strtod(next_tok(&sp), NULL);
		else if (!strcmp(d, "session")) g->session = atol(next_tok(&sp));
		else if (!strcmp(d, "soul")) g->soulCount = atoi(next_tok(&sp));
		else if (!strcmp(d, "legendary")) { char *v = next_tok(&sp); if (strcmp(v, "-")) snprintf(g->legendary, MAX_KEY, "%s", v); }
		else if (!strcmp(d, "neglegendary")) g->negLegendary = atoi(next_tok(&sp));
		else if (!strcmp(d, "tag")) { char *v = next_tok(&sp); if (strcmp(v, "-")) snprintf(g->tag, MAX_KEY, "%s", v); }
		else if (!strcmp(d, "voucher")) { char *v = next_tok(&sp); if (strcmp(v, "-")) snprintf(g->voucher, MAX_KEY, "%s", v); }
		else if (!strcmp(d, "voucherante")) g->voucherAnte = atoi(next_tok(&sp));
		else if (!strcmp(d, "taganywhere")) g->tagAnywhere = atoi(next_tok(&sp));
		else if (!strcmp(d, "leganywhere")) g->legAnywhere = atoi(next_tok(&sp));
		else if (!strcmp(d, "packslots")) g->packSlots = atoi(next_tok(&sp));
		else if (!strcmp(d, "matchany")) g->matchAny = atoi(next_tok(&sp));
		else if (!strcmp(d, "poolfile")) {
			/* rest of the line verbatim: mod paths contain spaces */
			char *v = sp;
			while (v && (*v == ' ' || *v == '\t')) v++;
			if (v && *v) snprintf(g->poolFile, sizeof g->poolFile, "%s", v);
			sp = NULL;
		}
		else if (!strcmp(d, "jslot")) {
			int i = atoi(next_tok(&sp)) - 1;
			char *k = next_tok(&sp);
			int neg = atoi(next_tok(&sp));
			if (i >= 0 && i < 3 && strcmp(k, "-")) {
				snprintf(g->jslot[i].key, MAX_KEY, "%s", k);
				g->jslot[i].neg = neg;
				g->jslot[i].used = 1;
			}
		}
		else if (!strcmp(d, "maslots")) { for (int a = 1; a <= 8; a++) g->maSlots[a] = tok_int(&sp); }
		else if (!strcmp(d, "mapacks")) { for (int a = 1; a <= 8; a++) g->maPacks[a] = tok_int(&sp); }
		else if (!strcmp(d, "pack")) {
			if (g->npack < MAX_PACKF) snprintf(g->packKeys[g->npack++], MAX_KEY, "%s", next_tok(&sp));
		}
		else if (!strcmp(d, "tagdef")) {
			if (g->ntags >= MAX_TAGS) { snprintf(err, errsz, "too many tags"); goto fail; }
			char *k = next_tok(&sp);
			snprintf(g->tagKey[g->ntags], MAX_KEY, "%s", k);
			g->tagReqOk[g->ntags] = (uint8_t)tok_int(&sp);
			g->tagMinAnte[g->ntags] = tok_int(&sp);
			g->ntags++;
		}
		else if (!strcmp(d, "vouchdef")) {
			if (g->nvouch >= MAX_VOUCH) { snprintf(err, errsz, "too many vouchers"); goto fail; }
			char *k = next_tok(&sp);
			snprintf(g->vouchKey[g->nvouch], MAX_KEY, "%s", k);
			g->vouchAvail[g->nvouch] = (uint8_t)atoi(next_tok(&sp));
			g->nvouch++;
		}
		else if (!strcmp(d, "jokerdef")) {
			int r = atoi(next_tok(&sp));
			if (r < 1 || r > 4 || g->njoker[r] >= MAX_JOKERS) { snprintf(err, errsz, "bad jokerdef"); goto fail; }
			char *k = next_tok(&sp);
			snprintf(g->jokerKey[r][g->njoker[r]], MAX_KEY, "%s", k);
			g->jokerAvail[r][g->njoker[r]] = (uint8_t)atoi(next_tok(&sp));
			g->njoker[r]++;
		}
		else if (!strcmp(d, "boostdef")) {
			if (g->nboost >= MAX_BOOST) { snprintf(err, errsz, "too many boosters"); goto fail; }
			char *k = next_tok(&sp);
			snprintf(g->boostKey[g->nboost], MAX_KEY, "%s", k);
			g->boostW[g->nboost] = strtod(next_tok(&sp), NULL);
			g->boostBuf[g->nboost] = (uint8_t)tok_int(&sp);
			g->boostCards[g->nboost] = tok_int(&sp);
			char *sk = next_tok(&sp);
			g->boostSoul[g->nboost] = (sk && sk[0] == 'A') ? 1 : (sk && sk[0] == 'S') ? 2 : 0;
			g->nboost++;
		}
		else if (!strcmp(d, "check_ph") || !strcmp(d, "check_r13") || !strcmp(d, "check_pr") || !strcmp(d, "check_prn")) {
			if (g->nchecks >= MAX_CHECKS) continue;
			int i = g->nchecks++;
			if (!strcmp(d, "check_ph")) {
				g->checks[i].kind = CK_PH;
				snprintf(g->checks[i].str, MAX_KEY, "%s", next_tok(&sp));
			} else if (!strcmp(d, "check_r13")) {
				g->checks[i].kind = CK_R13;
				g->checks[i].x = strtod(next_tok(&sp), NULL);
			} else if (!strcmp(d, "check_pr")) {
				g->checks[i].kind = CK_PR;
				g->checks[i].x = strtod(next_tok(&sp), NULL);
			} else {
				g->checks[i].kind = CK_PRN;
				g->checks[i].x = strtod(next_tok(&sp), NULL);
				g->checks[i].n = atoi(next_tok(&sp));
			}
			g->checks[g->nchecks - 1].expect = strtod(next_tok(&sp), NULL);
		}
		else if (!strcmp(d, "end")) { g->sawEnd = 1; break; }
	}
	fclose(f);
	f = NULL;

	/* A garbled or truncated config must never degrade into "no filters,
	 * accept everything": demand the end marker and the parity checks the
	 * mod always writes. */
	if (!g->sawEnd) { snprintf(err, errsz, "config truncated (no end marker)"); return false; }
	if (g->nchecks < 8) { snprintf(err, errsz, "config has no parity checks"); return false; }
	if (g->modelver != MODELVER) {
		snprintf(err, errsz, "config modelver %d != helper model %d (rebuild native/brainstorm_native_search via native/build.sh)",
				g->modelver, MODELVER);
		return false;
	}

	/* booster cume (array order, matches the Lua float sum); card counts come
	 * from config.extra via the config -- name fallback is source-corrected
	 * (mega/jumbo are 4-card packs for Buffoon/Spectral, never 6). */
	g->boostCume = 0.0;
	for (int i = 0; i < g->nboost; i++) {
		g->boostCume += g->boostW[i];
		if (g->boostCards[i] <= 0) {
			g->boostCards[i] = strstr(g->boostKey[i], "mega") ? 4 : (strstr(g->boostKey[i], "jumbo") ? 4 : 2);
		}
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
#define BSPOOL_SCHEMA 1
#define BSPOOL_HEADER_SIZE 1024
#define BSPOOL_MAX_TAG_RULES 16

typedef struct {
	int schema, modelver, complete, headerBytes;
	uint64_t seedspace, rangeStart, rangeEnd, records;
	uint64_t catalogHash, criteriaHash;
	char charset[64];
	int route; /* 1 = collect (tag blinds skipped), 0 = observe */
	int ntagRules;
	struct { char key[MAX_KEY]; int minAnte, maxAnte, minCount; } tagRules[BSPOOL_MAX_TAG_RULES];
	struct { int used; char key[MAX_KEY]; int minAnte, maxAnte, neg; } legendary;
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
		|| !strcmp(d, "boostdef") || !strncmp(d, "check_", 6);
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
	char buf[BSPOOL_HEADER_SIZE + 1];
	if (fseeko(f, 0, SEEK_SET) != 0) { snprintf(err, errsz, "cannot rewind pool"); return false; }
	size_t got = fread(buf, 1, BSPOOL_HEADER_SIZE, f);
	if (got < 64) { snprintf(err, errsz, "pool header is truncated"); return false; }
	buf[got] = 0;
	char *cur = buf;
	int sawMagic = 0, sawEnd = 0;
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
			h->schema = v ? atoi(v) : 0;
			sawMagic = 1;
		}
		else if (!strcmp(d, "modelver")) { v = pool_tok(&sp); h->modelver = v ? atoi(v) : 0; }
		else if (!strcmp(d, "encoding")) { v = pool_tok(&sp); if (v) snprintf(encoding, sizeof encoding, "%s", v); }
		else if (!strcmp(d, "charset")) { v = pool_tok(&sp); if (v) snprintf(h->charset, sizeof h->charset, "%s", v); }
		else if (!strcmp(d, "seedspace")) { v = pool_tok(&sp); h->seedspace = v ? strtoull(v, NULL, 10) : 0; }
		else if (!strcmp(d, "range_start")) { v = pool_tok(&sp); h->rangeStart = v ? strtoull(v, NULL, 10) : 0; }
		else if (!strcmp(d, "range_end")) { v = pool_tok(&sp); h->rangeEnd = v ? strtoull(v, NULL, 10) : 0; }
		else if (!strcmp(d, "catalog_hash")) { v = pool_tok(&sp); h->catalogHash = v ? strtoull(v, NULL, 16) : 0; }
		else if (!strcmp(d, "criteria_hash")) { v = pool_tok(&sp); h->criteriaHash = v ? strtoull(v, NULL, 16) : 0; }
		else if (!strcmp(d, "records")) { v = pool_tok(&sp); h->records = v ? strtoull(v, NULL, 10) : 0; }
		else if (!strcmp(d, "complete")) { v = pool_tok(&sp); h->complete = v ? atoi(v) : 0; }
		else if (!strcmp(d, "header_bytes")) { v = pool_tok(&sp); h->headerBytes = v ? atoi(v) : 0; }
		else if (!strcmp(d, "tag_route")) { v = pool_tok(&sp); h->route = (v && !strcmp(v, "observe")) ? 0 : 1; }
		else if (!strcmp(d, "tag")) {
			if (h->ntagRules < BSPOOL_MAX_TAG_RULES) {
				char *k = pool_tok(&sp);
				char *a = pool_tok(&sp), *b = pool_tok(&sp), *c = pool_tok(&sp);
				if (k && a && b && c) {
					snprintf(h->tagRules[h->ntagRules].key, MAX_KEY, "%s", k);
					h->tagRules[h->ntagRules].minAnte = atoi(a);
					h->tagRules[h->ntagRules].maxAnte = atoi(b);
					h->tagRules[h->ntagRules].minCount = atoi(c);
					h->ntagRules++;
				}
			}
		}
		else if (!strcmp(d, "legendary")) {
			char *k = pool_tok(&sp);
			char *a = pool_tok(&sp), *b = pool_tok(&sp), *n = pool_tok(&sp);
			if (k && a && b) {
				h->legendary.used = 1;
				snprintf(h->legendary.key, MAX_KEY, "%s", k);
				h->legendary.minAnte = atoi(a);
				h->legendary.maxAnte = atoi(b);
				h->legendary.neg = n ? atoi(n) : 0;
			}
		}
		else if (!strcmp(d, "end")) sawEnd = 1;
	}
	if (!sawMagic) { snprintf(err, errsz, "not a Brainstorm seed pool"); return false; }
	if (h->schema != BSPOOL_SCHEMA) { snprintf(err, errsz, "pool schema %d unsupported (want %d)", h->schema, BSPOOL_SCHEMA); return false; }
	if (!sawEnd) { snprintf(err, errsz, "pool header has no end marker"); return false; }
	if (strcmp(encoding, "u64le")) { snprintf(err, errsz, "pool encoding is not u64le"); return false; }
	if (strcmp(h->charset, CHARSET)) { snprintf(err, errsz, "pool charset differs"); return false; }
	if (h->seedspace != SEEDSPACE) { snprintf(err, errsz, "pool seed space differs"); return false; }
	if (h->headerBytes < 64 || h->headerBytes > BSPOOL_HEADER_SIZE) {
		snprintf(err, errsz, "pool header_bytes %d is invalid", h->headerBytes);
		return false;
	}
	return true;
}

/* The external seed-pool builder includes this file as a core implementation
 * so both programs execute the exact same RNG/config/parity code.  Keep the
 * interactive first-hit modes out of that translation unit. */
#ifndef BRAINSTORM_NATIVE_CORE_ONLY

/* ------------------------------------------------------------- searching */
static _Atomic bool g_stop;
static _Atomic unsigned long long g_tried;
static pthread_mutex_t g_found_mtx = PTHREAD_MUTEX_INITIALIZER;
static bool g_found;
static char g_found_seed[9], g_found_label[64];
static char g_warn[256];

/* Active .bspool restriction: candidates come from the pool's u64le rank
 * records instead of the sequential full-space counter. Iteration starts at
 * an entropy-derived rotation and covers every record exactly once, so a
 * finished scan with no hit is a DEFINITIVE "nothing in this pool matches". */
typedef struct {
	int fd;
	uint64_t records, rot, dataOff;
	_Atomic uint64_t next;    /* rotated record index dealt to workers */
	_Atomic int live;         /* workers still scanning (exhaustion detect) */
} PoolRun;
static PoolRun g_pool;
static bool g_pool_active;

typedef struct {
	const Config *g;
	int tid;
	uint64_t start;
} WorkerArgs;

/* Read `count` consecutive pool records starting at rotated index `first`
 * (wrapping at the record count) into ranks[]. bs_pread (pread / overlapped
 * ReadFile) keeps this thread-safe without a shared file position. */
static bool pool_read_ranks(uint64_t first, uint64_t count, uint64_t *ranks,
		unsigned char *raw) {
	uint64_t done = 0;
	while (done < count) {
		uint64_t at = (first + done) % g_pool.records;
		uint64_t run = count - done;
		if (at + run > g_pool.records) run = g_pool.records - at;
		uint64_t bytes = run * 8;
		int64_t got = bs_pread(g_pool.fd, raw, bytes, g_pool.dataOff + at * 8);
		if (got != (int64_t)bytes) return false;
		for (uint64_t i = 0; i < run; i++) {
			uint64_t rank = 0;
			for (int b = 0; b < 8; b++) rank |= (uint64_t)raw[i * 8 + b] << (8 * b);
			ranks[done + i] = rank;
		}
		done += run;
	}
	return true;
}

static void record_hit(Ctx *c) {
	pthread_mutex_lock(&g_found_mtx);
	if (!g_found) {
		g_found = true;
		memcpy(g_found_seed, c->seed, 9);
		snprintf(g_found_label, sizeof g_found_label, "%s", c->label);
	}
	pthread_mutex_unlock(&g_found_mtx);
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
	unsigned char *raw = malloc(PCHUNK * 8);
	if (!c || !ranks || !raw) {
		free(c); free(ranks); free(raw);
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
		if (!pool_read_ranks(g_pool.rot + i0, n, ranks, raw)) {
			if (!g_warn[0]) snprintf(g_warn, sizeof g_warn, "pool record read failed; scan is partial");
			break;
		}
		/* drop corrupt out-of-space ranks instead of trusting them */
		uint64_t m = 0;
		for (uint64_t i = 0; i < n; i++) {
			if (ranks[i] < SEEDSPACE) ranks[m++] = ranks[i];
		}
		uint64_t i = 0;
		for (; i + ILV <= m && !atomic_load_explicit(&g_stop, memory_order_relaxed); i += ILV) {
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
			make_seed(ranks[i], c->seed);
			if (passes(c)) record_hit(c);
		}
		atomic_fetch_add_explicit(&g_tried, (unsigned long long)m, memory_order_relaxed);
	}
	free(raw);
	free(ranks);
	free(c);
	atomic_fetch_sub(&g_pool.live, 1);
	return NULL;
}

static void *worker(void *vp) {
	WorkerArgs *w = (WorkerArgs *)vp;
	const Config *g = w->g;
	Ctx *c = calloc(1, sizeof(Ctx));
	if (!c) return NULL;
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
					pthread_mutex_lock(&g_found_mtx);
					if (!g_found) {
						g_found = true;
						memcpy(g_found_seed, c->seed, 9);
						snprintf(g_found_label, sizeof g_found_label, "%s", c->label);
					}
					pthread_mutex_unlock(&g_found_mtx);
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

/* "wb" + bs_rename: byte-identical LF status on every platform (the CRT
 * would write \r\n in text mode) committed atomically (plain rename does not
 * overwrite on Windows). */
static void write_status(const char *path, const char *tmp, bool done, const char *emsg) {
	FILE *f = fopen(tmp, "wb");
	if (!f) return;
	fprintf(f, "P %llu\n", (unsigned long long)atomic_load(&g_tried));
	if (g_warn[0]) fprintf(f, "W %s\n", g_warn);
	if (emsg) fprintf(f, "E %s\n", emsg);
	if (g_found) fprintf(f, "R %s %s\n", g_found_seed, g_found_label[0] ? g_found_label : "-");
	if (done) fprintf(f, "D\n");
	fclose(f);
	bs_rename(tmp, path);
}

static double file_age_seconds(const char *path) {
	bs_stat_t st;
	if (bs_stat(path, &st) != 0) return 1e9;
	return difftime(time(NULL), st.st_mtime);
}

/* Open + validate the config's .bspool. Fatal problems come back as an error
 * message (the mod stops a pool search rather than degrading to a full-space
 * search); a catalog fingerprint difference is only a warning because every
 * hit is still re-verified against the CURRENT profile's filters. */
static bool pool_open(const Config *g, const char *cfgPath, char *err, size_t errsz) {
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
	bs_stat_t st; /* 64-bit st_size: pools grow past 2 GiB */
	if (bs_fstat(fileno(f), &st) != 0) {
		snprintf(err, errsz, "pool: cannot stat %s", g->poolFile);
		fclose(f);
		return false;
	}
	/* trust the committed record count, clamped to what the file really holds
	 * (a crash tail past the last checkpoint is not committed data) */
	uint64_t avail = st.st_size > h.headerBytes ? ((uint64_t)st.st_size - (uint64_t)h.headerBytes) / 8 : 0;
	uint64_t records = h.records < avail ? h.records : avail;
	if (records == 0) { snprintf(err, errsz, "pool: no seed records"); fclose(f); return false; }
	uint64_t cfgHash = 0;
	if (pool_hash_catalog_file(cfgPath, &cfgHash) && cfgHash != h.catalogHash) {
		snprintf(g_warn, sizeof g_warn,
				"pool was built on a different pool/unlock snapshot; hits are re-verified in-game");
	}
	if (!h.complete) {
		snprintf(g_warn, sizeof g_warn, "pool scan is incomplete (%llu records committed)",
				(unsigned long long)records);
	}
	int fd = dup(fileno(f));
	fclose(f);
	if (fd < 0) { snprintf(err, errsz, "pool: cannot keep %s open", g->poolFile); return false; }
	g_pool.fd = fd;
	g_pool.records = records;
	g_pool.dataOff = (uint64_t)h.headerBytes;
	g_pool_active = true;
	return true;
}

static int mode_search(const Config *g, const char *cfgPath, const char *statusPath,
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
	if (g_pool_active) {
		g_pool.rot = entropy % g_pool.records;
		atomic_init(&g_pool.next, 0);
		atomic_init(&g_pool.live, n);
	}
	pthread_t th[64];
	WorkerArgs wa[64];
	for (int i = 0; i < n; i++) {
		wa[i].g = g;
		wa[i].tid = i;
		wa[i].start = start;
		pthread_create(&th[i], NULL, g_pool_active ? pool_worker : worker, &wa[i]);
	}
	time_t t0 = time(NULL);
	bool joined = false;
	while (!atomic_load(&g_stop)) {
		bs_sleep_ms(200);
		if (access(stopPath, F_OK) == 0) atomic_store(&g_stop, true);
		/* a fully dealt pool with no hit is a definitive verdict, not a retry */
		if (g_pool_active && atomic_load(&g_pool.live) == 0) {
			for (int i = 0; i < n; i++) pthread_join(th[i], NULL);
			joined = true;
			if (g_found) break;
			write_status(statusPath, tmp, true,
					"pool: no seed in the pool matches the active filters");
			return 3;
		}
		/* heartbeat: the mod touches hbPath every ~2s while searching; if the
		 * game died without writing the stop file, exit instead of orphaning. */
		if (difftime(time(NULL), t0) > 20 && file_age_seconds(hbPath) > 30.0) {
			atomic_store(&g_stop, true);
			write_status(statusPath, tmp, true, "heartbeat lost");
			for (int i = 0; i < n; i++) pthread_join(th[i], NULL);
			return 2;
		}
		if (difftime(time(NULL), t0) > 6 * 3600) atomic_store(&g_stop, true); /* hard cap */
		write_status(statusPath, tmp, false, NULL);
	}
	if (!joined) {
		for (int i = 0; i < n; i++) pthread_join(th[i], NULL);
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
	c->g = g;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	int nbuf = 0;
	char line[64];
	for (;;) {
		bool more = fgets(line, sizeof line, f) != NULL;
		if (more && strlen(line) >= 8) {
			memcpy(seeds[nbuf], line, 8);
			seeds[nbuf][8] = 0;
			nbuf++;
		}
		if (nbuf == ILV || (!more && nbuf > 0)) {
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
		pthread_t th[64];
		WorkerArgs wa[64];
		for (int i = 0; i < n; i++) {
			wa[i].g = &gb; wa[i].tid = i; wa[i].start = 12345;
			pthread_create(&th[i], NULL, worker, &wa[i]);
		}
		bs_sleep_ms((unsigned)seconds * 1000u);
		atomic_store(&g_stop, true);
		for (int i = 0; i < n; i++) pthread_join(th[i], NULL);
		printf("threads=%d seeds/sec=%.0f\n", n, (double)atomic_load(&g_tried) / seconds);
	}
	return 0;
}

int main(int argc, char **argv) {
	bs_stdio_binary(); /* fixture output is diffed byte-for-byte cross-platform */
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
	if (!strcmp(argv[1], "fixture") && argc == 4) return mode_fixture(&g, argv[3]);
	if (!strcmp(argv[1], "verifychecks")) return mode_verifychecks(&g);
	if (!strcmp(argv[1], "bench") && argc == 4) return mode_bench(&g, atoi(argv[3]));
	fprintf(stderr, "bad arguments\n");
	return 2;
}

#endif /* !BRAINSTORM_NATIVE_CORE_ONLY */
