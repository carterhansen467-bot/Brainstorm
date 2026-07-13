/* ========================================================================
 * Brainstorm exhaustive seed-pool builder
 * ------------------------------------------------------------------------
 * Standalone CLI built on the exact RNG/config/parity implementation used by
 * brainstorm_native_search.  It scans deterministic numeric ranges without
 * replacement, emits every match, and checkpoints at committed range
 * boundaries so interrupted runs can resume without duplicate records.
 *
 * The initial constraint compiler deliberately covers the route-sensitive
 * primitives already source-verified by Brainstorm:
 *   - multiple tag keys, each with an inclusive ante range and count;
 *   - the run's FIRST Soul yielding a chosen legendary in an ante range.
 * Matched tags can either be observed or collected.  Collecting selects the
 * first required occurrences as blind skips before physical packs/Souls are
 * simulated, matching Brainstorm's Model-3 shop layout.
 * ======================================================================== */

#define BRAINSTORM_NATIVE_CORE_ONLY
#include "brainstorm_native_search.c"

#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <signal.h>
#include <stdarg.h>

/* Schema, header size, and header parsing live in the shared core
 * (.bspool contract section of brainstorm_native_search.c) because the
 * interactive searcher consumes these files too. */
/* Criteria/state schema remains 1; .bspool file schema is independently at 2. */
#define POOL_SCHEMA 1
#define POOL_MAX_ANTE 39
#define POOL_MAX_TAG_RULES BSPOOL_MAX_TAG_RULES
#define POOL_LABEL 512
#define POOL_HEADER_SIZE BSPOOL_HEADER_SIZE
#define POOL_OUTPUT_BUFFER (64 * 1024)
_Static_assert(POOL_OUTPUT_BUFFER / 8 == BSPOOL_BLOCK_MAX_RECORDS,
		"pool writer and schema-2 block limit must agree");

typedef enum { POOL_BINARY = 0, POOL_TEXT = 1, POOL_COUNT = 2 } PoolFormat;

typedef struct {
	char key[MAX_KEY];
	int poolIndex;
	int minAnte, maxAnte, minCount;
} PoolTagRule;

typedef struct {
	int used;
	char key[MAX_KEY];
	int poolIndex;
	int minAnte, maxAnte;
	int requireNegative;
} PoolLegendaryRule;

typedef struct {
	int schema, sawEnd;
	int threads, resume, collectTags;
	int space; /* SPACE_NATURAL (default) or SPACE_TOTAL */
	char label[136]; /* optional shareable pool name; not part of criteriaHash */
	uint64_t start, count, checkpoint, chunk;
	int countAll;
	PoolFormat format;
	int ntagRules;
	PoolTagRule tagRules[POOL_MAX_TAG_RULES];
	PoolLegendaryRule legendary;
	int minTagAnte, maxTagAnte, maxAnte;
	int firstKind; /* 1 = Joker4, 2 = Tag<firstAnte> */
	int firstAnte;
	char firstKey[32];
	char kTag[POOL_MAX_ANTE + 1][16];
	char kShopPack[POOL_MAX_ANTE + 1][24];
	char kSoulT[POOL_MAX_ANTE + 1][24];
	char kSoulS[POOL_MAX_ANTE + 1][24];
	char kEdiSoul[POOL_MAX_ANTE + 1][24];
	uint64_t catalogHash, criteriaHash;
	int refilter, refilterDepth;
	uint64_t sourceCriteriaHash, sourceRecords, sourceRangeStart, sourceRangeEnd;
	char sourcePoolId[24];
	uint64_t outputRangeStart, outputRangeEnd;
	int inputFd;
	BspoolReader inputReader;
} PoolPlan;

/* Short shareable fingerprint shown by the builder UI and the in-game
 * selector: two people holding the same pool see the same id. Covers
 * everything that decides the pool's CONTENT (catalog, criteria, range,
 * space, committed records, completeness) -- and not the label, so renaming
 * doesn't change identity. Final once the scan completes. */
static void pool_compute_id(const PoolPlan *p, uint64_t records, int complete, char out[24]) {
	char buf[192];
	int n = snprintf(buf, sizeof buf, "%016" PRIx64 "%016" PRIx64 "%" PRIu64 "-%" PRIu64 "%s%" PRIu64 "%d",
			p->catalogHash, p->criteriaHash, p->outputRangeStart, p->outputRangeEnd,
			space_name(p->space), records, complete);
	uint64_t h = pool_hash_update(UINT64_C(1469598103934665603), buf, (size_t)n);
	snprintf(out, 24, "%016" PRIx64, h);
}

/* An exhaustive scan evaluates far more than 2^32 candidates per worker.
 * Use 64-bit generations here; the interactive helper's uint32 generation is
 * safe for its six-hour first-hit runs but would eventually wrap a full scan. */
typedef struct { double state; uint64_t gen; } PoolStream;

typedef struct {
	const Config *g;
	const PoolPlan *p;
	char seed[9];
	double hashedSeed;
	uint64_t gen;
	PRNG prng;
	PoolStream joker4;
	PoolStream jokerResample[MAX_RESAMPLE];
	PoolStream tag[POOL_MAX_ANTE + 1];
	PoolStream tagResample[POOL_MAX_ANTE + 1][MAX_RESAMPLE];
	PoolStream shopPack[POOL_MAX_ANTE + 1];
	PoolStream soulT[POOL_MAX_ANTE + 1], soulS[POOL_MAX_ANTE + 1];
	PoolStream ediSoul[POOL_MAX_ANTE + 1];
	uint64_t packsGen[POOL_MAX_ANTE + 1];
	int packsN[POOL_MAX_ANTE + 1], packIdx[POOL_MAX_ANTE + 1][6];
	uint8_t skipSm[POOL_MAX_ANTE + 1], skipBig[POOL_MAX_ANTE + 1];
	int forcedAnte;
} PoolCtx;

typedef struct {
	uint64_t cursor, outputBytes, matched, scanned;
	double elapsed;
	int done;
} PoolState;

static volatile sig_atomic_t poolSignalStop = 0;

static void pool_request_stop(void) {
	poolSignalStop = 1;
}

static bool pool_parse_u64(const char *s, uint64_t *out) {
	if (!s || !*s || *s == '-') return false;
	errno = 0;
	char *end = NULL;
	unsigned long long v = strtoull(s, &end, 10);
	if (errno || !end || *end) return false;
	*out = (uint64_t)v;
	return true;
}

static bool pool_parse_int(const char *s, int *out) {
	if (!s || !*s) return false;
	errno = 0;
	char *end = NULL;
	long v = strtol(s, &end, 10);
	if (errno || !end || *end || v < INT32_MIN || v > INT32_MAX) return false;
	*out = (int)v;
	return true;
}

static uint64_t pool_hash_plan(const PoolPlan *p) {
	uint64_t h = UINT64_C(1469598103934665603);
	char line[160];
	int n = snprintf(line, sizeof line, "poolver %d\nformat %d\ntag_route %d\n",
			p->schema, (int)p->format, p->collectTags);
	h = pool_hash_update(h, line, (size_t)n);
	/* Hashed only when non-default so every existing natural pool, .state
	 * file, and manifest keeps its criteria_hash. The label is deliberately
	 * NOT hashed: renaming a pool must not invalidate a resumable scan. */
	if (p->space != SPACE_NATURAL) {
		n = snprintf(line, sizeof line, "space %s\n", space_name(p->space));
		h = pool_hash_update(h, line, (size_t)n);
	}
	for (int i = 0; i < p->ntagRules; i++) {
		const PoolTagRule *r = &p->tagRules[i];
		n = snprintf(line, sizeof line, "tag %s %d %d %d\n",
				r->key, r->minAnte, r->maxAnte, r->minCount);
		h = pool_hash_update(h, line, (size_t)n);
	}
	if (p->legendary.used) {
		const PoolLegendaryRule *r = &p->legendary;
		n = snprintf(line, sizeof line, "legendary %s %d %d %d\n",
				r->key, r->minAnte, r->maxAnte, r->requireNegative);
		h = pool_hash_update(h, line, (size_t)n);
	}
	if (p->refilter) {
		n = snprintf(line, sizeof line, "source %016" PRIx64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %d\n",
				p->sourceCriteriaHash, p->sourceRecords, p->sourceRangeStart,
				p->sourceRangeEnd, p->space);
		h = pool_hash_update(h, line, (size_t)n);
	}
	return h;
}

static int pool_find_tag(const Config *g, const char *key) {
	for (int i = 0; i < g->ntags; i++) if (!strcmp(g->tagKey[i], key)) return i;
	return -1;
}

static int pool_find_legendary(const Config *g, const char *key) {
	for (int i = 0; i < g->njoker[4]; i++) if (!strcmp(g->jokerKey[4][i], key)) return i;
	return -1;
}

static bool pool_load_plan(const char *path, const Config *g, PoolPlan *p,
		char *err, size_t errsz) {
	memset(p, 0, sizeof *p);
	p->threads = g->threads;
	p->resume = 1;
	p->collectTags = 1;
	p->countAll = 1;
	p->checkpoint = UINT64_C(16777216);
	p->chunk = UINT64_C(16384);
	p->format = POOL_BINARY;
	p->minTagAnte = POOL_MAX_ANTE + 1;
	FILE *f = fopen(path, "r");
	if (!f) { snprintf(err, errsz, "cannot open criteria %s", path); return false; }
	char line[512];
	int lineno = 0;
	while (fgets(line, sizeof line, f)) {
		lineno++;
		size_t len = strlen(line);
		if (len == sizeof line - 1 && line[len - 1] != '\n') {
			snprintf(err, errsz, "criteria line %d is too long", lineno);
			goto fail;
		}
		char *sp = line;
		char *d = pool_tok(&sp);
		if (!d || d[0] == '#') continue;
		if (!strcmp(d, "poolver")) {
			if (!pool_parse_int(pool_tok(&sp), &p->schema)) goto bad_value;
		} else if (!strcmp(d, "threads")) {
			if (!pool_parse_int(pool_tok(&sp), &p->threads)) goto bad_value;
		} else if (!strcmp(d, "start")) {
			if (!pool_parse_u64(pool_tok(&sp), &p->start)) goto bad_value;
		} else if (!strcmp(d, "count")) {
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "all")) p->countAll = 1;
			else {
				p->countAll = 0;
				if (!pool_parse_u64(v, &p->count)) goto bad_value;
			}
		} else if (!strcmp(d, "checkpoint")) {
			if (!pool_parse_u64(pool_tok(&sp), &p->checkpoint)) goto bad_value;
		} else if (!strcmp(d, "chunk")) {
			if (!pool_parse_u64(pool_tok(&sp), &p->chunk)) goto bad_value;
		} else if (!strcmp(d, "resume")) {
			if (!pool_parse_int(pool_tok(&sp), &p->resume)) goto bad_value;
		} else if (!strcmp(d, "format")) {
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "binary")) p->format = POOL_BINARY;
			else if (v && !strcmp(v, "text")) p->format = POOL_TEXT;
			else if (v && !strcmp(v, "count")) p->format = POOL_COUNT;
			else goto bad_value;
		} else if (!strcmp(d, "tag_route")) {
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "collect")) p->collectTags = 1;
			else if (v && !strcmp(v, "observe")) p->collectTags = 0;
			else goto bad_value;
		} else if (!strcmp(d, "space")) {
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "natural")) p->space = SPACE_NATURAL;
			else if (v && !strcmp(v, "total")) p->space = SPACE_TOTAL;
			else goto bad_value;
		} else if (!strcmp(d, "label")) {
			/* rest of the line, spaces allowed; drop control characters so
			 * the label can't forge header/manifest lines */
			while (*sp == ' ' || *sp == '\t') sp++;
			size_t w = 0;
			for (; *sp && w + 1 < sizeof p->label; sp++) {
				if ((unsigned char)*sp >= 32) p->label[w++] = *sp;
			}
			while (w && p->label[w - 1] == ' ') w--;
			p->label[w] = 0;
			continue;
		} else if (!strcmp(d, "tag")) {
			if (p->ntagRules >= POOL_MAX_TAG_RULES) {
				snprintf(err, errsz, "criteria line %d: too many tag rules (max %d)", lineno, POOL_MAX_TAG_RULES);
				goto fail;
			}
			PoolTagRule *r = &p->tagRules[p->ntagRules++];
			char *key = pool_tok(&sp);
			if (!key || strlen(key) >= sizeof r->key) goto bad_value;
			snprintf(r->key, sizeof r->key, "%s", key);
			if (!pool_parse_int(pool_tok(&sp), &r->minAnte)
					|| !pool_parse_int(pool_tok(&sp), &r->maxAnte)
					|| !pool_parse_int(pool_tok(&sp), &r->minCount)) goto bad_value;
		} else if (!strcmp(d, "legendary")) {
			if (p->legendary.used) {
				snprintf(err, errsz, "criteria line %d: only one first-Soul legendary rule is supported", lineno);
				goto fail;
			}
			PoolLegendaryRule *r = &p->legendary;
			r->used = 1;
			char *key = pool_tok(&sp);
			if (!key || strlen(key) >= sizeof r->key) goto bad_value;
			snprintf(r->key, sizeof r->key, "%s", key);
			if (!pool_parse_int(pool_tok(&sp), &r->minAnte)
					|| !pool_parse_int(pool_tok(&sp), &r->maxAnte)) goto bad_value;
			char *neg = pool_tok(&sp);
			if (neg && !pool_parse_int(neg, &r->requireNegative)) goto bad_value;
		} else if (!strcmp(d, "end")) {
			p->sawEnd = 1;
			break;
		} else {
			snprintf(err, errsz, "criteria line %d: unknown directive '%s'", lineno, d);
			goto fail;
		}
		if (pool_tok(&sp)) {
			snprintf(err, errsz, "criteria line %d: unexpected extra value", lineno);
			goto fail;
		}
		continue;
bad_value:
		snprintf(err, errsz, "criteria line %d: invalid value for '%s'", lineno, d);
		goto fail;
	}
	fclose(f);
	f = NULL;

	if (!p->sawEnd) { snprintf(err, errsz, "criteria truncated (no end marker)"); return false; }
	if (p->schema != POOL_SCHEMA) { snprintf(err, errsz, "criteria poolver %d != scanner schema %d", p->schema, POOL_SCHEMA); return false; }
	uint64_t seedspace = space_size(p->space);
	if (p->start >= seedspace) { snprintf(err, errsz, "start is outside the %s seed space (%" PRIu64 " seeds)", space_name(p->space), seedspace); return false; }
	if (p->countAll) p->count = seedspace - p->start;
	if (!p->count || p->count > seedspace - p->start) { snprintf(err, errsz, "count extends outside the %s seed space (%" PRIu64 " seeds)", space_name(p->space), seedspace); return false; }
	if (p->threads == 0) {
		int ncpu = bs_cpu_count();
		p->threads = ncpu > 1 ? ncpu - 1 : 1;
	}
	if (p->threads < 1 || p->threads > 64) { snprintf(err, errsz, "threads must be 0..64"); return false; }
	if (p->chunk < ILV || p->chunk > UINT64_C(1073741824)) { snprintf(err, errsz, "chunk must be between %d and 1073741824", ILV); return false; }
	p->chunk -= p->chunk % ILV;
	if (!p->checkpoint) { snprintf(err, errsz, "checkpoint must be positive"); return false; }
	if (p->checkpoint < p->chunk) p->checkpoint = p->chunk;
	p->resume = !!p->resume;
	p->collectTags = !!p->collectTags;
	if (p->ntagRules == 0 && !p->legendary.used) { snprintf(err, errsz, "criteria has no predicates"); return false; }

	for (int i = 0; i < p->ntagRules; i++) {
		PoolTagRule *r = &p->tagRules[i];
		if (r->minAnte < 1 || r->maxAnte < r->minAnte || r->maxAnte > POOL_MAX_ANTE
				|| r->minCount < 1 || r->minCount > 2 * (r->maxAnte - r->minAnte + 1)) {
			snprintf(err, errsz, "bad range/count for tag rule %s", r->key);
			return false;
		}
		r->poolIndex = pool_find_tag(g, r->key);
		if (r->poolIndex < 0) { snprintf(err, errsz, "tag %s is not in the snapshot pool", r->key); return false; }
		if (!g->tagReqOk[r->poolIndex]) { snprintf(err, errsz, "tag %s is locked in this snapshot", r->key); return false; }
		if (g->tagMinAnte[r->poolIndex] > r->maxAnte) { snprintf(err, errsz, "tag %s cannot appear by ante %d", r->key, r->maxAnte); return false; }
		if (r->minAnte < p->minTagAnte) p->minTagAnte = r->minAnte;
		if (r->maxAnte > p->maxTagAnte) p->maxTagAnte = r->maxAnte;
	}
	if (p->legendary.used) {
		PoolLegendaryRule *r = &p->legendary;
		if (r->minAnte < 1 || r->maxAnte < r->minAnte || r->maxAnte > POOL_MAX_ANTE) {
			snprintf(err, errsz, "bad range for legendary rule %s", r->key);
			return false;
		}
		r->requireNegative = !!r->requireNegative;
		r->poolIndex = pool_find_legendary(g, r->key);
		if (r->poolIndex < 0 || !g->jokerAvail[4][r->poolIndex]) {
			snprintf(err, errsz, "legendary %s is unavailable in this snapshot", r->key);
			return false;
		}
		p->maxAnte = r->maxAnte;
		p->firstKind = 1;
		snprintf(p->firstKey, sizeof p->firstKey, "Joker4");
	} else {
		p->firstKind = 2;
		p->firstAnte = p->minTagAnte;
		snprintf(p->firstKey, sizeof p->firstKey, "Tag%d", p->firstAnte);
	}
	if (p->maxTagAnte > p->maxAnte) p->maxAnte = p->maxTagAnte;
	for (int a = 1; a <= p->maxAnte; a++) {
		snprintf(p->kTag[a], sizeof p->kTag[a], "Tag%d", a);
		snprintf(p->kShopPack[a], sizeof p->kShopPack[a], "shop_pack%d", a);
		snprintf(p->kSoulT[a], sizeof p->kSoulT[a], "soul_Tarot%d", a);
		snprintf(p->kSoulS[a], sizeof p->kSoulS[a], "soul_Spectral%d", a);
		snprintf(p->kEdiSoul[a], sizeof p->kEdiSoul[a], "edisou%d", a);
	}
	return true;
fail:
	if (f) fclose(f);
	return false;
}

static inline double pool_stream_next(PoolCtx *c, PoolStream *s, const char *key) {
	if (s->gen != c->gen) {
		s->state = pseudohash_ks(key, c->seed);
		s->gen = c->gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashedSeed) / 2.0;
}

static inline double pool_psr(PoolCtx *c, double seedval) {
	lj_random_seed(&c->prng, seedval);
	return lj_random(&c->prng);
}

static inline int pool_psr_n(PoolCtx *c, double seedval, int n) {
	lj_random_seed(&c->prng, seedval);
	return lj_random_n(&c->prng, n);
}

static double pool_resample_next(PoolCtx *c, PoolStream *streams, const char *base, int it) {
	if (it - 2 >= MAX_RESAMPLE) return NAN;
	PoolStream *s = &streams[it - 2];
	if (s->gen != c->gen) {
		char key[64];
		snprintf(key, sizeof key, "%s_resample%d", base, it);
		s->state = pseudohash_ks(key, c->seed);
		s->gen = c->gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashedSeed) / 2.0;
}

static int pool_pick_culled(PoolCtx *c, PoolStream *first, const char *firstKey,
		PoolStream *resamples, const uint8_t *avail, int n) {
	int idx = pool_psr_n(c, pool_stream_next(c, first, firstKey), n);
	int it = 1;
	while (idx > 0 && !avail[idx - 1]) {
		it++;
		double sv = pool_resample_next(c, resamples, firstKey, it);
		if (isnan(sv)) return -1;
		idx = pool_psr_n(c, sv, n);
	}
	return idx > 0 ? idx - 1 : -1;
}

static int pool_roll_tag(PoolCtx *c, int ante) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	int idx = pool_psr_n(c, pool_stream_next(c, &c->tag[ante], p->kTag[ante]), g->ntags);
	int it = 1;
	while (idx > 0 && !(g->tagReqOk[idx - 1]
			&& (g->tagMinAnte[idx - 1] == 0 || g->tagMinAnte[idx - 1] <= ante))) {
		it++;
		double sv = pool_resample_next(c, c->tagResample[ante], p->kTag[ante], it);
		if (isnan(sv)) return -1;
		idx = pool_psr_n(c, sv, g->ntags);
	}
	return idx > 0 ? idx - 1 : -1;
}

static void pool_label_add(char *label, size_t cap, const char *fmt, ...) {
	if (!label || !cap) return;
	size_t used = strlen(label);
	if (used >= cap - 1) return;
	va_list ap;
	va_start(ap, fmt);
	vsnprintf(label + used, cap - used, fmt, ap);
	va_end(ap);
}

static bool pool_check_tags(PoolCtx *c, char *label, size_t labelCap) {
	const PoolPlan *p = c->p;
	int counts[POOL_MAX_TAG_RULES] = { 0 };
	bool wroteRule[POOL_MAX_TAG_RULES] = { false };
	int remaining = p->ntagRules;
	for (int ante = p->minTagAnte; ante <= p->maxTagAnte; ante++) {
		for (int blind = 0; blind < 2; blind++) {
			int idx = pool_roll_tag(c, ante);
			if (idx < 0) return false;
			for (int r = 0; r < p->ntagRules; r++) {
				const PoolTagRule *rule = &p->tagRules[r];
				if (counts[r] >= rule->minCount || ante < rule->minAnte || ante > rule->maxAnte
						|| idx != rule->poolIndex) continue;
				counts[r]++;
				if (p->collectTags) {
					if (blind == 0) c->skipSm[ante] = 1;
					else c->skipBig[ante] = 1;
				}
				if (wroteRule[r]) {
					pool_label_add(label, labelCap, ",A%d%s", ante, blind == 0 ? "Sm" : "Big");
				} else {
					pool_label_add(label, labelCap, "%s%s=A%d%s",
							label && label[0] ? " " : "", rule->key, ante, blind == 0 ? "Sm" : "Big");
				}
				wroteRule[r] = true;
				if (counts[r] == rule->minCount) remaining--;
			}
		}
		for (int r = 0; r < p->ntagRules; r++) {
			if (p->tagRules[r].maxAnte == ante && counts[r] < p->tagRules[r].minCount) return false;
		}
		if (remaining == 0) return true;
	}
	return remaining == 0;
}

static int pool_pack_max_slots(const PoolCtx *c, int ante) {
	int shops = (ante >= 2 ? 3 : 2) - (c->skipSm[ante] ? 1 : 0) - (c->skipBig[ante] ? 1 : 0);
	return shops * 2;
}

static void pool_sim_packs(PoolCtx *c, int ante) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	if (c->packsGen[ante] != c->gen) {
		c->packsGen[ante] = c->gen;
		c->packsN[ante] = 0;
		if (ante == c->forcedAnte) c->packIdx[ante][c->packsN[ante]++] = PACK_FORCED;
	}
	int max = pool_pack_max_slots(c, ante);
	while (c->packsN[ante] < max) {
		double poll = pool_psr(c, pool_stream_next(c, &c->shopPack[ante], p->kShopPack[ante])) * g->boostCume;
		double cumulative = 0.0;
		int pick = -1;
		for (int i = 0; i < g->nboost; i++) {
			cumulative += g->boostW[i];
			if (cumulative >= poll && cumulative - g->boostW[i] <= poll) { pick = i; break; }
		}
		c->packIdx[ante][c->packsN[ante]++] = pick;
	}
}

static bool pool_precheck_legendary(PoolCtx *c) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	if (!p->legendary.used) return true;
	int idx = pool_pick_culled(c, &c->joker4, "Joker4", c->jokerResample,
			g->jokerAvail[4], g->njoker[4]);
	return idx == p->legendary.poolIndex;
}

static bool pool_check_first_soul(PoolCtx *c, char *label, size_t labelCap) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	const PoolLegendaryRule *rule = &p->legendary;
	bool blackHoleFound = false;
	for (int ante = 1; ante <= rule->maxAnte; ante++) {
		pool_sim_packs(c, ante);
		for (int slot = 0; slot < c->packsN[ante]; slot++) {
			int pi = c->packIdx[ante][slot];
			if (pi < 0 || !g->boostSoul[pi]) continue;
			int soulKind = g->boostSoul[pi];
			int cards = g->boostCards[pi];
			PoolStream *stream = soulKind == 1 ? &c->soulT[ante] : &c->soulS[ante];
			const char *key = soulKind == 1 ? p->kSoulT[ante] : p->kSoulS[ante];
			for (int card = 0; card < cards; card++) {
				bool soul = pool_psr(c, pool_stream_next(c, stream, key)) > 0.997;
				if (soulKind == 2 && !blackHoleFound) {
					bool blackHole = pool_psr(c, pool_stream_next(c, stream, key)) > 0.997;
					if (blackHole) { blackHoleFound = true; soul = false; }
				}
				if (!soul) continue;
				if (ante < rule->minAnte) return false;
				if (rule->requireNegative
						&& pool_psr(c, pool_stream_next(c, &c->ediSoul[ante], p->kEdiSoul[ante])) <= 0.997) return false;
				pool_label_add(label, labelCap, "%s%s=A%dP%d",
						label && label[0] ? " " : "", rule->key, ante, slot + 1);
				return true;
			}
		}
	}
	return false;
}

static bool pool_evaluate_pre(PoolCtx *c, const char seed[9], double hseed,
		double hfirst, char *label, size_t labelCap) {
	const PoolPlan *p = c->p;
	memcpy(c->seed, seed, 9);
	c->gen++;
	c->hashedSeed = hseed;
	memset(c->skipSm, 0, sizeof c->skipSm);
	memset(c->skipBig, 0, sizeof c->skipBig);
	if (label && labelCap) label[0] = 0;
	if (p->firstKind == 1) {
		c->joker4.state = hfirst;
		c->joker4.gen = c->gen;
	} else {
		c->tag[p->firstAnte].state = hfirst;
		c->tag[p->firstAnte].gen = c->gen;
	}
	/* Specific legendary selection is independent of tag/pack streams and
	 * rejects ~80% of vanilla candidates before the expensive route walk. */
	if (!pool_precheck_legendary(c)) return false;
	if (p->ntagRules && !pool_check_tags(c, label, labelCap)) return false;
	c->forcedAnte = 1;
	for (int ante = 1; ante <= p->maxAnte; ante++) {
		if (pool_pack_max_slots(c, ante) > 0) { c->forcedAnte = ante; break; }
	}
	if (p->legendary.used && !pool_check_first_soul(c, label, labelCap)) return false;
	return true;
}

static bool pool_batch_selftest(const PoolPlan *p) {
	char seeds[ILV][9];
	double hs[ILV], hf[ILV];
	uint64_t k = UINT64_C(987654321);
	for (int round = 0; round < 4; round++) {
		for (int i = 0; i < ILV; i++) {
			make_seed(k % SEEDSPACE, seeds[i]);
			k += UINT64_C(104729);
		}
		batch_hash_seed(seeds, hs);
		batch_hash_key(p->firstKey, seeds, hf);
		for (int i = 0; i < ILV; i++) {
			if (hs[i] != pseudohash_ks("", seeds[i])) return false;
			if (hf[i] != pseudohash_ks(p->firstKey, seeds[i])) return false;
		}
	}
	/* Total-space batches run at every seed length; prove each length's
	 * batched hash against the serial reference too. */
	for (int slen = 1; slen <= 8; slen++) {
		uint64_t base = 0;
		for (int l = 1; l < slen; l++) base = (base + 1) * CHARSET_TOTAL_N;
		for (int i = 0; i < ILV; i++) {
			if (make_seed_in(SPACE_TOTAL, base + (uint64_t)i * 31 % 36, seeds[i]) != slen) return false;
		}
		batch_hash_seed_n(seeds, slen, hs);
		batch_hash_key_n(p->firstKey, seeds, slen, hf);
		for (int i = 0; i < ILV; i++) {
			if (hs[i] != pseudohash_ks("", seeds[i])) return false;
			if (hf[i] != pseudohash_ks(p->firstKey, seeds[i])) return false;
		}
	}
	return true;
}

typedef struct {
	const Config *g;
	const PoolPlan *p;
	_Atomic uint64_t next;
	uint64_t end;
	_Atomic uint64_t scanned, matched;
	_Atomic bool ioError;
	FILE *out;
	bs_mutex_t outMutex;
} PoolScanShared;

static bool pool_read_input_batch(const PoolScanShared *s, uint64_t first,
		uint64_t count, uint64_t ranks[ILV], BspoolScratch *scratch) {
	return bspool_reader_read(&s->p->inputReader, first, count, ranks, scratch);
}

static int pool_rank_compare(const void *a, const void *b) {
	uint64_t x, y;
	memcpy(&x, a, sizeof x); memcpy(&y, b, sizeof y);
	return x < y ? -1 : x > y;
}

static size_t pool_encode_rank_block(unsigned char *buf, uint32_t count,
		unsigned char *encoded, unsigned char header[BSPOOL_BLOCK_HEADER_SIZE]) {
	qsort(buf, count, 8u, pool_rank_compare);
	uint64_t first, last;
	memcpy(&first, buf, 8); memcpy(&last, buf + (size_t)(count - 1) * 8u, 8);
	size_t out = 0;
	uint64_t prior = first;
	for (uint32_t i = 1; i < count; i++) {
		uint64_t rank;
		memcpy(&rank, buf + (size_t)i * 8u, 8);
		uint64_t delta = rank - prior;
		do {
			unsigned char b = (unsigned char)(delta & 0x7f);
			delta >>= 7;
			if (delta) b |= 0x80;
			encoded[out++] = b;
		} while (delta);
		prior = rank;
	}
	memset(header, 0, BSPOOL_BLOCK_HEADER_SIZE);
	memcpy(header, "BSP2", 4);
	bspool_put_u32le(header + 4, count);
	bspool_put_u32le(header + 8, (uint32_t)out);
	bspool_put_u32le(header + 12, bspool_checksum(encoded, out));
	bspool_put_u64le(header + 16, first);
	bspool_put_u64le(header + 24, last);
	return out;
}

static bool pool_flush_hits(PoolScanShared *s, unsigned char *buf,
		unsigned char *encoded, size_t *used) {
	if (!*used || s->p->format == POOL_COUNT) { *used = 0; return true; }
	const unsigned char *writeBuf = buf;
	size_t writeBytes = *used;
	unsigned char header[BSPOOL_BLOCK_HEADER_SIZE];
	if (s->p->format == POOL_BINARY) {
		uint32_t count = (uint32_t)(*used / 8u);
		size_t out = pool_encode_rank_block(buf, count, encoded, header);
		writeBuf = encoded; writeBytes = out;
	}
	bs_mutex_lock(&s->outMutex);
	size_t wroteHeader = s->p->format == POOL_BINARY
			? fwrite(header, 1, sizeof header, s->out) : sizeof header;
	size_t wrote = fwrite(writeBuf, 1, writeBytes, s->out);
	bs_mutex_unlock(&s->outMutex);
	if (wroteHeader != sizeof header || wrote != writeBytes) {
		atomic_store(&s->ioError, true);
		return false;
	}
	*used = 0;
	return true;
}

static bool pool_buffer_hit(PoolScanShared *s, unsigned char *buf,
		unsigned char *encoded, size_t *used, uint64_t rank, const char seed[9]) {
	size_t slen = strlen(seed); /* 8 in the natural space, 1..8 in total */
	size_t record = s->p->format == POOL_BINARY ? 8u : slen + 1;
	if (*used + record > POOL_OUTPUT_BUFFER && !pool_flush_hits(s, buf, encoded, used)) return false;
	if (s->p->format == POOL_BINARY) {
		memcpy(buf + *used, &rank, 8); *used += 8;
	} else if (s->p->format == POOL_TEXT) {
		memcpy(buf + *used, seed, slen);
		*used += slen;
		buf[(*used)++] = '\n';
	}
	return true;
}

static void *pool_scan_worker(void *arg) {
	PoolScanShared *s = arg;
	PoolCtx *c = calloc(1, sizeof *c);
	unsigned char *outbuf = malloc(POOL_OUTPUT_BUFFER);
	unsigned char *encoded = malloc(POOL_OUTPUT_BUFFER);
	BspoolScratch inputScratch = { .cachedBlock = UINT64_MAX };
	if (!c || !outbuf || !encoded) {
		free(c); free(outbuf); free(encoded);
		atomic_store(&s->ioError, true);
		return NULL;
	}
	c->g = s->g;
	c->p = s->p;
	size_t outUsed = 0;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	uint64_t ranks[ILV];
	while (!atomic_load(&s->ioError)) {
		uint64_t begin = atomic_fetch_add(&s->next, s->p->chunk);
		if (begin >= s->end) break;
		uint64_t end = begin + s->p->chunk;
		if (end > s->end) end = s->end;
		uint64_t rank = begin;
		uint64_t chunkMatched = 0;
		int space = s->p->space;
		for (; rank + ILV <= end; rank += ILV) {
			if (s->p->refilter && !pool_read_input_batch(s, rank, ILV, ranks, &inputScratch)) {
				atomic_store(&s->ioError, true); goto done;
			}
			int l0 = 0, uniform = 1;
			for (int i = 0; i < ILV; i++) {
				uint64_t candidate = s->p->refilter ? ranks[i] : rank + (uint64_t)i;
				int slen = make_seed_in(space, candidate, seeds[i]);
				if (i == 0) l0 = slen;
				else if (slen != l0) uniform = 0;
				ranks[i] = candidate;
			}
			if (uniform) {
				batch_hash_seed_n(seeds, l0, hseed);
				batch_hash_key_n(s->p->firstKey, seeds, l0, hfirst);
			} else {
				for (int i = 0; i < ILV; i++) {
					hseed[i] = pseudohash_ks("", seeds[i]);
					hfirst[i] = pseudohash_ks(s->p->firstKey, seeds[i]);
				}
			}
			for (int i = 0; i < ILV; i++) {
				if (pool_evaluate_pre(c, seeds[i], hseed[i], hfirst[i], NULL, 0)) {
					chunkMatched++;
					if (!pool_buffer_hit(s, outbuf, encoded, &outUsed, ranks[i], seeds[i])) goto done;
				}
			}
		}
		for (; rank < end; rank++) {
			if (s->p->refilter && !pool_read_input_batch(s, rank, 1, ranks, &inputScratch)) {
				atomic_store(&s->ioError, true); goto done;
			}
			uint64_t candidate = s->p->refilter ? ranks[0] : rank;
			make_seed_in(space, candidate, seeds[0]);
			double hs = pseudohash_ks("", seeds[0]);
			double hf = pseudohash_ks(s->p->firstKey, seeds[0]);
			if (pool_evaluate_pre(c, seeds[0], hs, hf, NULL, 0)) {
				chunkMatched++;
				if (!pool_buffer_hit(s, outbuf, encoded, &outUsed, candidate, seeds[0])) goto done;
			}
		}
		atomic_fetch_add(&s->matched, chunkMatched);
		atomic_fetch_add(&s->scanned, end - begin);
	}
done:
	(void)pool_flush_hits(s, outbuf, encoded, &outUsed);
	bspool_scratch_destroy(&inputScratch);
	free(encoded);
	free(outbuf);
	free(c);
	return NULL;
}

static bool pool_write_header(FILE *f, const PoolPlan *p, uint64_t records,
		uint64_t dataBytes, int complete, char *err, size_t errsz) {
	if (p->format != POOL_BINARY) return true;
	unsigned char buf[POOL_HEADER_SIZE];
	memset(buf, 0, sizeof buf);
	/* The header embeds the criteria that built the pool so a shared .bspool
	 * is self-describing without its .manifest sidecar, and so the in-game
	 * consumer can later compose the pool's tag route with overlay filters. */
	char poolId[24];
	pool_compute_id(p, records, complete, poolId);
	int n = snprintf((char *)buf, sizeof buf,
			"BRAINSTORM_SEED_POOL %d\n"
			"modelver %d\nencoding delta-varint-blocks-v1\ncharset %s\nseedspace %" PRIu64 "\n"
			"space %s\n"
			"range_start %" PRIu64 "\nrange_end %" PRIu64 "\n"
			"catalog_hash %016" PRIx64 "\ncriteria_hash %016" PRIx64 "\n"
			"pool_id %s\n"
			"tag_route %s\n",
			BSPOOL_SCHEMA, MODELVER, space_charset(p->space), space_size(p->space),
			space_name(p->space),
			p->outputRangeStart, p->outputRangeEnd, p->catalogHash, p->criteriaHash,
			poolId,
			p->collectTags ? "collect" : "observe");
	if (n < 0 || n >= (int)sizeof buf) { snprintf(err, errsz, "binary header overflow"); return false; }
	if (p->label[0]) {
		int lw = snprintf((char *)buf + n, sizeof buf - (size_t)n, "label %s\n", p->label);
		if (lw < 0 || lw >= (int)(sizeof buf - (size_t)n)) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += lw;
	}
	for (int i = 0; i < p->ntagRules; i++) {
		const PoolTagRule *r = &p->tagRules[i];
		int w = snprintf((char *)buf + n, sizeof buf - (size_t)n, "tag %s %d %d %d\n",
				r->key, r->minAnte, r->maxAnte, r->minCount);
		if (w < 0 || w >= (int)(sizeof buf - (size_t)n)) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += w;
	}
	if (p->legendary.used) {
		const PoolLegendaryRule *r = &p->legendary;
		int w = snprintf((char *)buf + n, sizeof buf - (size_t)n, "legendary %s %d %d %d\n",
				r->key, r->minAnte, r->maxAnte, r->requireNegative);
		if (w < 0 || w >= (int)(sizeof buf - (size_t)n)) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += w;
	}
	if (p->refilter) {
		int w = snprintf((char *)buf + n, sizeof buf - (size_t)n,
				"refilter_depth %d\nsource_criteria_hash %016" PRIx64 "\nsource_records %" PRIu64 "\nsource_pool_id %s\n",
				p->refilterDepth, p->sourceCriteriaHash, p->sourceRecords,
				p->sourcePoolId[0] ? p->sourcePoolId : "-");
		if (w < 0 || w >= (int)(sizeof buf - (size_t)n)) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += w;
	}
	int w = snprintf((char *)buf + n, sizeof buf - (size_t)n,
			"records %" PRIu64 "\ndata_bytes %" PRIu64 "\ncomplete %d\nheader_bytes %d\nend\n",
			records, dataBytes, complete, POOL_HEADER_SIZE);
	if (w < 0 || w >= (int)(sizeof buf - (size_t)n)) { snprintf(err, errsz, "binary header overflow"); return false; }
	n += w;
	int64_t at = bs_ftello(f);
	if (bs_fseeko(f, 0, SEEK_SET) != 0 || fwrite(buf, 1, sizeof buf, f) != sizeof buf
			|| fflush(f) != 0 || bs_fsync_file(f) != 0 || bs_fseeko(f, at, SEEK_SET) != 0) {
		snprintf(err, errsz, "cannot update binary header: %s", strerror(errno));
		return false;
	}
	return true;
}

static bool pool_append_index(FILE *f, int space, uint64_t records,
		uint64_t dataBytes, uint64_t *finalBytes, char *err, size_t errsz) {
	if (fflush(f) != 0) { snprintf(err, errsz, "cannot flush pool data before indexing"); return false; }
	BspoolHeader h;
	memset(&h, 0, sizeof h);
	h.headerBytes = POOL_HEADER_SIZE; h.encoding = BSPOOL_ENCODING_DELTA_BLOCKS;
	h.space = space; h.records = records; h.dataBytes = dataBytes;
	BspoolReader r;
	if (!bspool_reader_init(&r, fileno(f), &h, POOL_HEADER_SIZE + dataBytes, err, errsz)) return false;
	uint64_t indexOff = POOL_HEADER_SIZE + dataBytes;
	if (bs_fseeko(f, (int64_t)indexOff, SEEK_SET) != 0) {
		snprintf(err, errsz, "cannot seek to compressed pool index"); bspool_reader_destroy(&r); return false;
	}
	unsigned char raw[BSPOOL_INDEX_ENTRY_SIZE * 4096];
	uint64_t done = 0;
	while (done < r.nblocks) {
		uint64_t n = r.nblocks - done;
		if (n > 4096) n = 4096;
		for (uint64_t i = 0; i < n; i++) {
			const BspoolBlockIndex *e = &r.blocks[done + i];
			unsigned char *q = raw + i * BSPOOL_INDEX_ENTRY_SIZE;
			bspool_put_u64le(q, e->offset);
			bspool_put_u64le(q + 8, e->firstRecord);
			bspool_put_u32le(q + 16, e->count);
			bspool_put_u32le(q + 20, e->payloadBytes);
		}
		size_t bytes = (size_t)n * BSPOOL_INDEX_ENTRY_SIZE;
		if (fwrite(raw, 1, bytes, f) != bytes) {
			snprintf(err, errsz, "cannot write compressed pool index"); bspool_reader_destroy(&r); return false;
		}
		done += n;
	}
	unsigned char footer[BSPOOL_FOOTER_SIZE];
	memset(footer, 0, sizeof footer); memcpy(footer, "BSPIDX2\n", 8);
	bspool_put_u64le(footer + 8, indexOff);
	bspool_put_u64le(footer + 16, r.nblocks);
	bspool_put_u64le(footer + 24, records);
	bspool_put_u64le(footer + 32, dataBytes);
	uint64_t blocks = r.nblocks;
	bspool_reader_destroy(&r);
	if (fwrite(footer, 1, sizeof footer, f) != sizeof footer || fflush(f) != 0 || bs_fsync_file(f) != 0) {
		snprintf(err, errsz, "cannot commit compressed pool index"); return false;
	}
	int64_t pos = bs_ftello(f);
	if (pos < 0 || (uint64_t)pos != indexOff + blocks * BSPOOL_INDEX_ENTRY_SIZE + BSPOOL_FOOTER_SIZE) {
		snprintf(err, errsz, "compressed pool index size accounting failed"); return false;
	}
	*finalBytes = (uint64_t)pos;
	return true;
}

static bool pool_write_state(const char *path, const PoolPlan *p, const PoolState *s,
		char *err, size_t errsz) {
	char tmp[1024];
	if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) {
		snprintf(err, errsz, "state path is too long"); return false;
	}
	FILE *f = fopen(tmp, "w");
	if (!f) { snprintf(err, errsz, "cannot write state: %s", strerror(errno)); return false; }
	fprintf(f, "BRAINSTORM_SEED_POOL_STATE %d\n", POOL_SCHEMA);
	fprintf(f, "catalog_hash %016" PRIx64 "\ncriteria_hash %016" PRIx64 "\n", p->catalogHash, p->criteriaHash);
	fprintf(f, "range_start %" PRIu64 "\nrange_end %" PRIu64 "\n", p->start, p->start + p->count);
	fprintf(f, "cursor %" PRIu64 "\noutput_bytes %" PRIu64 "\n", s->cursor, s->outputBytes);
	fprintf(f, "matched %" PRIu64 "\nscanned %" PRIu64 "\nelapsed_seconds %.9f\ndone %d\nend\n",
			s->matched, s->scanned, s->elapsed, s->done);
	if (fflush(f) != 0 || bs_fsync_file(f) != 0 || fclose(f) != 0 || bs_rename_overwrite(tmp, path) != 0) {
		snprintf(err, errsz, "cannot commit state: %s", strerror(errno));
		return false;
	}
	return true;
}

static bool pool_load_state(const char *path, const PoolPlan *p, PoolState *s,
		char *err, size_t errsz) {
	FILE *f = fopen(path, "r");
	if (!f) return false;
	memset(s, 0, sizeof *s);
	int version = 0, sawEnd = 0;
	uint64_t ch = 0, qh = 0, rs = 0, re = 0;
	char line[256];
	while (fgets(line, sizeof line, f)) {
		char *sp = line;
		char *d = pool_tok(&sp), *v = pool_tok(&sp);
		if (!d) continue;
		if (!strcmp(d, "BRAINSTORM_SEED_POOL_STATE")) { if (!pool_parse_int(v, &version)) goto bad; }
		else if (!strcmp(d, "catalog_hash")) ch = strtoull(v, NULL, 16);
		else if (!strcmp(d, "criteria_hash")) qh = strtoull(v, NULL, 16);
		else if (!strcmp(d, "range_start")) { if (!pool_parse_u64(v, &rs)) goto bad; }
		else if (!strcmp(d, "range_end")) { if (!pool_parse_u64(v, &re)) goto bad; }
		else if (!strcmp(d, "cursor")) { if (!pool_parse_u64(v, &s->cursor)) goto bad; }
		else if (!strcmp(d, "output_bytes")) { if (!pool_parse_u64(v, &s->outputBytes)) goto bad; }
		else if (!strcmp(d, "matched")) { if (!pool_parse_u64(v, &s->matched)) goto bad; }
		else if (!strcmp(d, "scanned")) { if (!pool_parse_u64(v, &s->scanned)) goto bad; }
		else if (!strcmp(d, "elapsed_seconds")) s->elapsed = v ? strtod(v, NULL) : 0.0;
		else if (!strcmp(d, "done")) { if (!pool_parse_int(v, &s->done)) goto bad; }
		else if (!strcmp(d, "end")) sawEnd = 1;
	}
	fclose(f);
	if (version != POOL_SCHEMA || !sawEnd || ch != p->catalogHash || qh != p->criteriaHash
			|| rs != p->start || re != p->start + p->count || s->cursor < rs || s->cursor > re) {
		snprintf(err, errsz, "state does not match this model, criteria, or range");
		return false;
	}
	return true;
bad:
	fclose(f);
	snprintf(err, errsz, "malformed state file");
	return false;
}

static bool pool_write_manifest(const char *path, const Config *g, const PoolPlan *p,
		const PoolState *s, char *err, size_t errsz) {
	FILE *f = fopen(path, "w");
	if (!f) { snprintf(err, errsz, "cannot write manifest: %s", strerror(errno)); return false; }
	double rate = s->scanned ? (double)s->matched / (double)s->scanned : 0.0;
	double projected = rate * (double)space_size(p->space);
	char poolId[24];
	pool_compute_id(p, s->matched, s->done, poolId);
	fprintf(f, "BRAINSTORM_SEED_POOL_MANIFEST %d\n", POOL_SCHEMA);
	fprintf(f, "modelver %d\nfp_mode %s\n", MODELVER, g_seed_fma ? "fma" : "plain");
	fprintf(f, "catalog_hash %016" PRIx64 "\ncriteria_hash %016" PRIx64 "\n", p->catalogHash, p->criteriaHash);
	fprintf(f, "pool_id %s\n", poolId);
	if (p->label[0]) fprintf(f, "label %s\n", p->label);
	fprintf(f, "charset %s\nseedspace %" PRIu64 "\nspace %s\n",
			space_charset(p->space), space_size(p->space), space_name(p->space));
	fprintf(f, "range_start %" PRIu64 "\nrange_end %" PRIu64 "\n", p->outputRangeStart, p->outputRangeEnd);
	fprintf(f, "scanned %" PRIu64 "\nmatched %" PRIu64 "\ncomplete %d\n", s->scanned, s->matched, s->done);
	fprintf(f, "format %s\nrecord_order block-sorted\ntag_route %s\n",
			p->format == POOL_BINARY ? "delta-varint-blocks-v1" : p->format == POOL_TEXT ? "seed-text" : "count-only",
			p->collectTags ? "collect-first-required" : "observe");
	if (p->refilter) fprintf(f,
			"refilter_depth %d\ninput_records %" PRIu64 "\nsource_criteria_hash %016" PRIx64 "\nsource_pool_id %s\n",
			p->refilterDepth, p->sourceRecords, p->sourceCriteriaHash,
			p->sourcePoolId[0] ? p->sourcePoolId : "-");
	for (int i = 0; i < p->ntagRules; i++) {
		const PoolTagRule *r = &p->tagRules[i];
		fprintf(f, "tag %s %d %d %d\n", r->key, r->minAnte, r->maxAnte, r->minCount);
	}
	if (p->legendary.used) fprintf(f, "first_soul_legendary %s %d %d %d\n",
			p->legendary.key, p->legendary.minAnte, p->legendary.maxAnte, p->legendary.requireNegative);
	fprintf(f, "elapsed_seconds %.6f\nseeds_per_second %.3f\nmatch_rate %.12g\n", s->elapsed,
			s->elapsed > 0.0 ? (double)s->scanned / s->elapsed : 0.0, rate);
	if (!p->refilter) {
		double expectedDeltaBytes = 1.0;
		if (rate > 0.0 && rate < 1.0) {
			double threshold = 128.0;
			while (threshold <= (double)space_size(p->space)) {
				expectedDeltaBytes += pow(1.0 - rate, threshold - 1.0);
				threshold *= 128.0;
			}
		}
		double projectedCompressed = projected * (expectedDeltaBytes + 0.01)
				+ POOL_HEADER_SIZE + BSPOOL_FOOTER_SIZE;
		fprintf(f, "projected_full_matches %.0f\nprojected_u64_bytes %.0f\n"
				"projected_compressed_bytes %.0f\nexpected_compressed_bytes_per_record %.6f\n",
				projected, projected * 8.0, projectedCompressed, expectedDeltaBytes + 0.01);
	}
	if (p->format == POOL_BINARY && s->matched && s->outputBytes >= POOL_HEADER_SIZE) {
		fprintf(f, "compressed_file_bytes %" PRIu64 "\nbytes_per_record %.6f\n",
				s->outputBytes, (double)(s->outputBytes - POOL_HEADER_SIZE) / (double)s->matched);
	}
	fprintf(f, "end\n");
	if (fclose(f) != 0) { snprintf(err, errsz, "cannot close manifest: %s", strerror(errno)); return false; }
	return true;
}

static double pool_now(void) {
	return bs_monotonic_seconds();
}

static int pool_mode_scan(const Config *g, const PoolPlan *p, const char *output) {
	char err[256];
	if (!calibrate(g, err, sizeof err)) { fprintf(stderr, "calibration failed: %s\n", err); return 1; }
	if (!pool_batch_selftest(p)) { fprintf(stderr, "batch hash self-test failed\n"); return 1; }
	char statePath[1024], manifestPath[1024];
	if (snprintf(statePath, sizeof statePath, "%s.state", output) >= (int)sizeof statePath
			|| snprintf(manifestPath, sizeof manifestPath, "%s.manifest", output) >= (int)sizeof manifestPath) {
		fprintf(stderr, "output path is too long\n"); return 1;
	}
	PoolState state = { .cursor = p->start };
	FILE *out = NULL;
	bool stateExists = bs_file_exists(statePath);
	if (p->resume && !stateExists && p->format != POOL_COUNT && bs_file_exists(output)) {
		fprintf(stderr, "output exists without resumable state; set 'resume 0' to replace it\n");
		return 1;
	}
	if (p->resume && stateExists) {
		if (!pool_load_state(statePath, p, &state, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
		if (p->format != POOL_COUNT) {
			out = fopen(output, "r+b");
			if (!out) { fprintf(stderr, "cannot open output for resume: %s\n", strerror(errno)); return 1; }
			if (bs_ftruncate_file(out, (int64_t)state.outputBytes) != 0
					|| bs_fseeko(out, (int64_t)state.outputBytes, SEEK_SET) != 0) {
				fprintf(stderr, "cannot restore committed output boundary: %s\n", strerror(errno)); fclose(out); return 1;
			}
		}
		fprintf(stderr, "resuming at %s %" PRIu64 " (%" PRIu64 " already scanned)\n",
				p->refilter ? "input record" : "rank", state.cursor, state.scanned);
	} else {
		if (stateExists && p->resume) { fprintf(stderr, "existing state could not be resumed\n"); return 1; }
		if (p->format != POOL_COUNT) {
			out = fopen(output, "w+b");
			if (!out) { fprintf(stderr, "cannot create output: %s\n", strerror(errno)); return 1; }
			if (p->format == POOL_BINARY) {
				state.outputBytes = POOL_HEADER_SIZE;
				if (bs_fseeko(out, POOL_HEADER_SIZE, SEEK_SET) != 0
						|| !pool_write_header(out, p, 0, 0, 0, err, sizeof err)) {
					fprintf(stderr, "%s\n", err); fclose(out); return 1;
				}
			}
		}
		if (!pool_write_state(statePath, p, &state, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); if (out) fclose(out); return 1;
		}
	}
	if (state.done) {
		fprintf(stderr, "pool is already complete (%" PRIu64 " matches)\n", state.matched);
		if (out) fclose(out);
		return 0;
	}

	bs_install_stop_handler(pool_request_stop);
	double priorElapsed = state.elapsed;
	double started = pool_now();
	while (state.cursor < p->start + p->count && !poolSignalStop) {
		uint64_t epochEnd = state.cursor + p->checkpoint;
		if (epochEnd < state.cursor || epochEnd > p->start + p->count) epochEnd = p->start + p->count;
		PoolScanShared shared;
		memset(&shared, 0, sizeof shared);
		shared.g = g; shared.p = p; shared.end = epochEnd; shared.out = out;
		atomic_init(&shared.next, state.cursor);
		atomic_init(&shared.scanned, 0);
		atomic_init(&shared.matched, 0);
		atomic_init(&shared.ioError, false);
		bs_mutex_init(&shared.outMutex);
		bs_thread_t threads[64];
		int made = 0;
		for (int i = 0; i < p->threads; i++) {
			if (bs_thread_create(&threads[i], pool_scan_worker, &shared) != 0) {
				atomic_store(&shared.ioError, true);
				break;
			}
			made++;
		}
		for (int i = 0; i < made; i++) bs_thread_join(threads[i]);
		bs_mutex_destroy(&shared.outMutex);
		if (atomic_load(&shared.ioError)) {
			fprintf(stderr, "scan aborted because a worker or output write failed\n");
			if (out) fclose(out);
			return 1;
		}
		uint64_t epochScanned = atomic_load(&shared.scanned);
		uint64_t epochMatched = atomic_load(&shared.matched);
		if (epochScanned != epochEnd - state.cursor) {
			fprintf(stderr, "internal range accounting failure\n"); if (out) fclose(out); return 1;
		}
		state.cursor = epochEnd;
		state.scanned += epochScanned;
		state.matched += epochMatched;
		state.elapsed = priorElapsed + (pool_now() - started);
		if (out) {
			if (fflush(out) != 0 || bs_fsync_file(out) != 0) {
				fprintf(stderr, "cannot commit output: %s\n", strerror(errno)); fclose(out); return 1;
			}
			int64_t pos = bs_ftello(out);
			if (pos < 0) { fprintf(stderr, "cannot read output position\n"); fclose(out); return 1; }
			state.outputBytes = (uint64_t)pos;
			if (!pool_write_header(out, p, state.matched,
					state.outputBytes - POOL_HEADER_SIZE, 0, err, sizeof err)) {
				fprintf(stderr, "%s\n", err); fclose(out); return 1;
			}
		}
		if (!pool_write_state(statePath, p, &state, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); if (out) fclose(out); return 1;
		}
		fprintf(stderr, "scanned=%" PRIu64 "/%" PRIu64 " matches=%" PRIu64 " rate=%.0f/s\n",
				state.scanned, p->count, state.matched, state.elapsed > 0 ? (double)state.scanned / state.elapsed : 0.0);
	}
	state.done = state.cursor == p->start + p->count;
	state.elapsed = priorElapsed + (pool_now() - started);
	if (out) {
		uint64_t dataBytes = p->format == POOL_BINARY
				? state.outputBytes - POOL_HEADER_SIZE : state.outputBytes;
		if (p->format == POOL_BINARY && state.done
				&& !pool_append_index(out, p->space, state.matched, dataBytes,
						&state.outputBytes, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); fclose(out); return 1;
		}
		if (!pool_write_header(out, p, state.matched, dataBytes, state.done, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); fclose(out); return 1;
		}
		if (fclose(out) != 0) { fprintf(stderr, "cannot close output: %s\n", strerror(errno)); return 1; }
	}
	if (!pool_write_state(statePath, p, &state, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
	if (!pool_write_manifest(manifestPath, g, p, &state, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
	if (state.done) {
		fprintf(stderr, "complete: scanned=%" PRIu64 " matched=%" PRIu64 " elapsed=%.3fs\n", state.scanned, state.matched, state.elapsed);
		return 0;
	}
	fprintf(stderr, "stopped cleanly at %s %" PRIu64 "; rerun the same command to resume\n",
			p->refilter ? "input record" : "rank", state.cursor);
	return 130;
}

static bool pool_prepare_refilter(PoolPlan *p, const char *input,
		const char *output, FILE **inputFile, char *err, size_t errsz) {
	FILE *f = fopen(input, "rb");
	if (!f) { snprintf(err, errsz, "cannot open input pool %s: %s", input, strerror(errno)); return false; }
	FILE *existingOutput = fopen(output, "rb");
	if (existingOutput) {
		bool same = bs_same_file(f, existingOutput);
		fclose(existingOutput);
		if (same) {
			snprintf(err, errsz, "input and output pool resolve to the same file");
			fclose(f); return false;
		}
	}
	BspoolHeader h;
	if (!bspool_read_header(f, &h, err, errsz)) { fclose(f); return false; }
	if (h.modelver != MODELVER) {
		snprintf(err, errsz, "input pool model %d != scanner model %d", h.modelver, MODELVER);
		fclose(f); return false;
	}
	if (!h.complete) {
		snprintf(err, errsz, "input pool is incomplete; finish its scan before refiltering");
		fclose(f); return false;
	}
	if (h.catalogHash != p->catalogHash) {
		snprintf(err, errsz, "input pool was built from a different pool/unlock snapshot");
		fclose(f); return false;
	}
	int64_t size = bs_file_size(f);
	if (size < 0 || (h.encoding == BSPOOL_ENCODING_U64
			&& (uint64_t)size != (uint64_t)h.headerBytes + h.records * 8u)
			|| !bspool_reader_init(&p->inputReader, fileno(f), &h,
					(uint64_t)(size < 0 ? 0 : size), err, errsz)) {
		if (!err[0]) snprintf(err, errsz, "input pool size does not match its committed record count");
		fclose(f); return false;
	}
	if (!h.records) {
		snprintf(err, errsz, "input pool has no seed records");
		bspool_reader_destroy(&p->inputReader);
		fclose(f); return false;
	}
	p->refilter = 1;
	p->refilterDepth = h.refilterDepth + 1;
	p->sourceCriteriaHash = h.criteriaHash;
	p->sourceRecords = h.records;
	p->sourceRangeStart = h.rangeStart;
	p->sourceRangeEnd = h.rangeEnd;
	snprintf(p->sourcePoolId, sizeof p->sourcePoolId, "%s", h.poolId);
	p->space = h.space;
	p->start = 0;
	p->count = h.records;
	p->countAll = 0;
	p->outputRangeStart = h.rangeStart;
	p->outputRangeEnd = h.rangeEnd;
	p->inputFd = fileno(f);
	*inputFile = f;
	return true;
}

static int pool_mode_fixture(const Config *g, const PoolPlan *p, const char *seedfile) {
	char err[256];
	if (!calibrate(g, err, sizeof err)) { fprintf(stderr, "calibration failed: %s\n", err); return 1; }
	if (!pool_batch_selftest(p)) { fprintf(stderr, "batch hash self-test failed\n"); return 1; }
	FILE *f = fopen(seedfile, "r");
	if (!f) { fprintf(stderr, "cannot open %s\n", seedfile); return 1; }
	PoolCtx *c = calloc(1, sizeof *c);
	if (!c) { fclose(f); return 1; }
	c->g = g; c->p = p;
	char line[128], seed[9], label[POOL_LABEL];
	while (fgets(line, sizeof line, f)) {
		size_t slen = strlen(line);
		while (slen > 0 && (line[slen - 1] == '\n' || line[slen - 1] == '\r'
				|| line[slen - 1] == ' ' || line[slen - 1] == '\t')) slen--;
		if (slen == 0) continue;
		if (slen > 8) slen = 8;
		memcpy(seed, line, slen); seed[slen] = 0;
		double hs = pseudohash_ks("", seed);
		double hf = pseudohash_ks(p->firstKey, seed);
		bool ok = pool_evaluate_pre(c, seed, hs, hf, label, sizeof label);
		printf("%s %d %s\n", seed, ok ? 1 : 0, ok && label[0] ? label : "-");
	}
	free(c);
	fclose(f);
	return 0;
}

static int pool_mode_export(const char *input, const char *output) {
	FILE *in = fopen(input, "rb");
	if (!in) { fprintf(stderr, "cannot open %s: %s\n", input, strerror(errno)); return 1; }
	BspoolHeader h;
	char err[192];
	if (!bspool_read_header(in, &h, err, sizeof err)) {
		fprintf(stderr, "%s\n", err); fclose(in); return 1;
	}
	if (h.modelver != MODELVER) {
		fprintf(stderr, "warning: pool model %d, this tool is model %d\n", h.modelver, MODELVER);
	}
	uint64_t records = h.records;
	int complete = h.complete;
	int64_t fileBytes = bs_file_size(in);
	BspoolReader reader;
	if (fileBytes < 0 || !bspool_reader_init(&reader, fileno(in), &h,
			(uint64_t)(fileBytes < 0 ? 0 : fileBytes), err, sizeof err)) {
		fprintf(stderr, "%s\n", fileBytes < 0 ? "cannot stat pool" : err); fclose(in); return 1;
	}
	/* "wb": exported seed lists are diffed/grepped byte-exactly by the tests */
	FILE *out = !strcmp(output, "-") ? stdout : fopen(output, "wb");
	if (!out) { fprintf(stderr, "cannot create %s: %s\n", output, strerror(errno)); bspool_reader_destroy(&reader); fclose(in); return 1; }
	uint64_t ranks[16384];
	BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
	for (uint64_t record = 0; record < records;) {
		uint64_t n = records - record;
		if (n > 16384) n = 16384;
		if (!bspool_reader_read(&reader, record, n, ranks, &scratch)) {
			fprintf(stderr, "cannot decode pool at record %" PRIu64 "\n", record);
			bspool_scratch_destroy(&scratch); bspool_reader_destroy(&reader);
			if (out != stdout) fclose(out); fclose(in); return 1;
		}
		for (uint64_t i = 0; i < n; i++) {
			char seed[9];
			make_seed_in(h.space, ranks[i], seed);
			fprintf(out, "%s\n", seed);
		}
		record += n;
	}
	bspool_scratch_destroy(&scratch); bspool_reader_destroy(&reader);
	if (out != stdout && fclose(out) != 0) { fprintf(stderr, "cannot close export\n"); fclose(in); return 1; }
	fclose(in);
	fprintf(stderr, "exported %" PRIu64 " seeds%s (space %s, pool_id %s%s%s)\n",
			records, complete ? " from a complete pool" : " from an incomplete pool",
			space_name(h.space), h.poolId[0] ? h.poolId : "-",
			h.label[0] ? ", label " : "", h.label);
	return 0;
}

static bool pool_write_converted_header(FILE *f, const unsigned char original[POOL_HEADER_SIZE],
		uint64_t records, uint64_t dataBytes, int complete, char *err, size_t errsz) {
	unsigned char out[POOL_HEADER_SIZE];
	char work[POOL_HEADER_SIZE + 1];
	memcpy(work, original, POOL_HEADER_SIZE); work[POOL_HEADER_SIZE] = 0;
	memset(out, 0, sizeof out);
	size_t used = 0;
	char *line = work;
	while (line && *line) {
		char *nl = strchr(line, '\n');
		if (nl) *nl = 0;
		char copy[POOL_HEADER_SIZE + 1];
		snprintf(copy, sizeof copy, "%s", line);
		char *sp = copy;
		char *d = pool_tok(&sp);
		if (!d || !strcmp(d, "end")) break;
		const char *replacement = NULL;
		char generated[96];
		if (!strcmp(d, "BRAINSTORM_SEED_POOL")) {
			snprintf(generated, sizeof generated, "BRAINSTORM_SEED_POOL %d", BSPOOL_SCHEMA);
			replacement = generated;
		} else if (!strcmp(d, "encoding")) replacement = "encoding delta-varint-blocks-v1";
		else if (!strcmp(d, "records") || !strcmp(d, "data_bytes") || !strcmp(d, "complete")
				|| !strcmp(d, "header_bytes")) replacement = NULL;
		else replacement = line;
		if (replacement) {
			size_t n = strlen(replacement);
			if (n + 1 > sizeof out - used) { snprintf(err, errsz, "converted pool header overflow"); return false; }
			memcpy(out + used, replacement, n); used += n; out[used++] = '\n';
		}
		line = nl ? nl + 1 : NULL;
	}
	char tail[192];
	int n = snprintf(tail, sizeof tail,
			"records %" PRIu64 "\ndata_bytes %" PRIu64 "\ncomplete %d\nheader_bytes %d\nend\n",
			records, dataBytes, complete, POOL_HEADER_SIZE);
	if (n < 0 || (size_t)n > sizeof out - used) { snprintf(err, errsz, "converted pool header overflow"); return false; }
	memcpy(out + used, tail, (size_t)n);
	int64_t at = bs_ftello(f);
	if (bs_fseeko(f, 0, SEEK_SET) != 0 || fwrite(out, 1, sizeof out, f) != sizeof out
			|| fflush(f) != 0 || bs_fsync_file(f) != 0 || bs_fseeko(f, at, SEEK_SET) != 0) {
		snprintf(err, errsz, "cannot update converted pool header: %s", strerror(errno)); return false;
	}
	return true;
}

static int pool_mode_convert(const char *input, const char *output) {
	FILE *in = fopen(input, "rb");
	if (!in) { fprintf(stderr, "cannot open %s: %s\n", input, strerror(errno)); return 1; }
	FILE *existing = fopen(output, "rb");
	if (existing) {
		bool same = bs_same_file(in, existing);
		fclose(existing);
		fprintf(stderr, "%s\n", same ? "input and output pool resolve to the same file"
				: "output already exists; choose a new filename");
		fclose(in); return 1;
	}
	BspoolHeader h;
	char err[256] = "";
	if (!bspool_read_header(in, &h, err, sizeof err)) { fprintf(stderr, "%s\n", err); fclose(in); return 1; }
	if (!h.complete) { fprintf(stderr, "finish the input pool before converting it\n"); fclose(in); return 1; }
	if (h.encoding == BSPOOL_ENCODING_DELTA_BLOCKS) {
		fprintf(stderr, "pool is already block-compressed; no output was created\n"); fclose(in); return 1;
	}
	int64_t inputBytes = bs_file_size(in);
	if (inputBytes < 0 || (uint64_t)inputBytes != (uint64_t)h.headerBytes + h.records * 8u) {
		fprintf(stderr, "legacy pool size does not match its committed record count\n"); fclose(in); return 1;
	}
	BspoolReader reader;
	if (!bspool_reader_init(&reader, fileno(in), &h, (uint64_t)inputBytes, err, sizeof err)) {
		fprintf(stderr, "%s\n", err); fclose(in); return 1;
	}
	unsigned char original[POOL_HEADER_SIZE];
	if (bs_fseeko(in, 0, SEEK_SET) != 0 || fread(original, 1, sizeof original, in) != sizeof original) {
		fprintf(stderr, "cannot preserve input pool header\n"); bspool_reader_destroy(&reader); fclose(in); return 1;
	}
	FILE *out = fopen(output, "w+b");
	if (!out) { fprintf(stderr, "cannot create %s: %s\n", output, strerror(errno)); bspool_reader_destroy(&reader); fclose(in); return 1; }
	if (bs_fseeko(out, POOL_HEADER_SIZE, SEEK_SET) != 0
			|| !pool_write_converted_header(out, original, h.records, 0, 0, err, sizeof err)) {
		fprintf(stderr, "%s\n", err); fclose(out); bspool_reader_destroy(&reader); fclose(in); return 1;
	}
	uint64_t ranks[POOL_OUTPUT_BUFFER / 8];
	unsigned char encoded[POOL_OUTPUT_BUFFER], header[BSPOOL_BLOCK_HEADER_SIZE];
	BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
	uint64_t record = 0;
	while (record < h.records) {
		uint64_t n = h.records - record;
		if (n > POOL_OUTPUT_BUFFER / 8) n = POOL_OUTPUT_BUFFER / 8;
		if (!bspool_reader_read(&reader, record, n, ranks, &scratch)) {
			fprintf(stderr, "cannot decode input at record %" PRIu64 "\n", record); goto fail;
		}
		size_t payload = pool_encode_rank_block((unsigned char *)ranks, (uint32_t)n, encoded, header);
		if (fwrite(header, 1, sizeof header, out) != sizeof header
				|| fwrite(encoded, 1, payload, out) != payload) {
			fprintf(stderr, "cannot write compressed output: %s\n", strerror(errno)); goto fail;
		}
		record += n;
		if ((record & UINT64_C(0x3ffffff)) == 0 || record == h.records)
			fprintf(stderr, "converted=%" PRIu64 "/%" PRIu64 "\n", record, h.records);
	}
	{
		int64_t dataEnd = bs_ftello(out);
		uint64_t finalBytes = 0;
		if (dataEnd < POOL_HEADER_SIZE || !pool_append_index(out, h.space, h.records,
				(uint64_t)dataEnd - POOL_HEADER_SIZE, &finalBytes, err, sizeof err)
				|| !pool_write_converted_header(out, original, h.records,
						(uint64_t)dataEnd - POOL_HEADER_SIZE, 1, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); goto fail;
		}
		if (fclose(out) != 0) { out = NULL; fprintf(stderr, "cannot close converted pool\n"); goto fail_no_out; }
		out = NULL;
		bspool_scratch_destroy(&scratch); bspool_reader_destroy(&reader); fclose(in);
		fprintf(stderr, "compressed %" PRIu64 " records: %.3f GB -> %.3f GB (%.1f%% smaller)\n",
				h.records, (double)inputBytes / 1e9, (double)finalBytes / 1e9,
				100.0 * (1.0 - (double)finalBytes / (double)inputBytes));
		return 0;
	}
fail:
	if (out) fclose(out);
fail_no_out:
	bspool_scratch_destroy(&scratch); bspool_reader_destroy(&reader); fclose(in);
	return 1;
}

static void pool_usage(const char *prog) {
	fprintf(stderr,
			"usage:\n"
			"  %s scan <native-snapshot.cfg> <pool-criteria.cfg> <output>\n"
			"  %s refilter <native-snapshot.cfg> <pool-criteria.cfg> <input.bspool> <output.bspool>\n"
			"  %s fixture <native-snapshot.cfg> <pool-criteria.cfg> <seed-file>\n"
			"  %s export <input.bspool> <output.txt|->\n"
			"  %s convert <legacy-input.bspool> <compressed-output.bspool>\n",
			prog, prog, prog, prog, prog);
}

int main(int argc, char **argv) {
	bs_platform_init();
	if (argc == 4 && !strcmp(argv[1], "export")) return pool_mode_export(argv[2], argv[3]);
	if (argc == 4 && !strcmp(argv[1], "convert")) return pool_mode_convert(argv[2], argv[3]);
	int refilter = argc == 6 && !strcmp(argv[1], "refilter");
	if ((!refilter && argc != 5) || (strcmp(argv[1], "scan") && strcmp(argv[1], "fixture") && !refilter)) {
		pool_usage(argv[0]);
		return 2;
	}
	if (refilter && !strcmp(argv[4], argv[5])) {
		fprintf(stderr, "input and output pool must be different files\n");
		return 2;
	}
	static Config catalog;
	static PoolPlan plan;
	char err[256] = "";
	if (!load_config(argv[2], &catalog, err, sizeof err)) {
		fprintf(stderr, "snapshot error: %s\n", err);
		return 1;
	}
	if (!pool_hash_catalog_file(argv[2], &plan.catalogHash)) {
		fprintf(stderr, "cannot fingerprint snapshot %s\n", argv[2]);
		return 1;
	}
	uint64_t catalogHash = plan.catalogHash;
	if (!pool_load_plan(argv[3], &catalog, &plan, err, sizeof err)) {
		fprintf(stderr, "criteria error: %s\n", err);
		return 1;
	}
	plan.catalogHash = catalogHash;
	plan.outputRangeStart = plan.start;
	plan.outputRangeEnd = plan.start + plan.count;
	FILE *inputFile = NULL;
	if (refilter && !pool_prepare_refilter(&plan, argv[4], argv[5], &inputFile, err, sizeof err)) {
		fprintf(stderr, "input pool error: %s\n", err);
		return 1;
	}
	plan.criteriaHash = pool_hash_plan(&plan);
	if (!strcmp(argv[1], "scan")) return pool_mode_scan(&catalog, &plan, argv[4]);
	if (refilter) {
		int rc = pool_mode_scan(&catalog, &plan, argv[5]);
		bspool_reader_destroy(&plan.inputReader);
		fclose(inputFile);
		return rc;
	}
	return pool_mode_fixture(&catalog, &plan, argv[4]);
}
