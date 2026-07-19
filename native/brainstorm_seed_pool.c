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
 *   - one legendary rule over the run's first two Souls, with an ante range
 *     and a Soul search depth: 1 (the first Soul only, default) or "any"
 *     (up to two Souls deep -- the first Soul, or failing that the second).
 *     Whenever the target must come from Soul #2 the route assumes Soul #1
 *     is used and its non-target legendary remains owned. Legacy exclusive
 *     soul_depth 2 pools remain readable and refilterable.
 * Matched tags can either be observed or collected.  Collecting selects the
 * first required occurrences as blind skips before physical packs/Souls are
 * simulated. Model 6 also opens collected Charm/Ethereal reward packs at the
 * skipped blind, in chronological order with the shops that remain reachable.
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
#define POOL_MAX_VOUCHER_RULES 8
#define POOL_MAX_VOUCHER_EXCLUSIONS 16
#define POOL_MAX_VOUCHER_ANTE 8
#define POOL_LABEL 512
#define POOL_HEADER_SIZE BSPOOL_HEADER_SIZE
#define POOL_OUTPUT_BUFFER (64 * 1024)
#define POOL_HASH_INIT UINT64_C(1469598103934665603)
#define POOL_STATE_SCHEMA 3
#define POOL_SOUL_EVENTS_PER_ANTE 8
#define POOL_SOUL_CARDS_PER_EVENT 64
#define POOL_SOUL_CARDS_PER_ANTE \
	(POOL_SOUL_EVENTS_PER_ANTE * POOL_SOUL_CARDS_PER_EVENT)
_Static_assert(POOL_OUTPUT_BUFFER / 8 == BSPOOL_BLOCK_MAX_RECORDS,
		"pool writer and schema-2 block limit must agree");

typedef enum { POOL_BINARY = 0, POOL_TEXT = 1, POOL_COUNT = 2 } PoolFormat;

typedef struct {
	char key[MAX_KEY];
	int poolIndex;
	int minAnte, minPhase, maxAnte, maxPhase, minCount, collect;
} PoolTagRule;

typedef struct {
	int used;
	char key[MAX_KEY];
	int poolIndex;
	int minAnte, minPhase, maxAnte, maxPhase;
	int requireNegative;
	int source; /* 0 any, otherwise SOUL_SOURCE_* */
	int humanLocation; /* 0 preserves legacy RNG-Ante interpretation */
	int soulDepth; /* 1 (default), 2, or SOUL_DEPTH_ANY (either Soul) */
} PoolLegendaryRule;

typedef struct {
	char key[MAX_KEY];
	int poolIndex, minAnte, maxAnte;
} PoolVoucherRule;

static const char *pool_soul_depth_str(int depth) {
	return depth == SOUL_DEPTH_ANY ? "any" : depth == 2 ? "2" : "1";
}

typedef struct {
	int schema, sawEnd;
	int outputSchema, headerBytes;
	int threads, resume, collectTags;
	int legendaryRoutes, legendaryRoutesExplicit;
	int baseLegendaryRoutes; /* source pool's effective cumulative policy */
	int inheritedFastLegendaryRoutes; /* sticky: any source stage was a subset */
	int space; /* SPACE_NATURAL (default), SPACE_SETTABLE, or SPACE_TOTAL */
	char label[136]; /* optional shareable pool name; not part of criteriaHash */
	uint64_t start, count, checkpoint, chunk;
	int countAll;
	PoolFormat format;
	int ntagRules;
	PoolTagRule tagRules[POOL_MAX_TAG_RULES];
	int nbaseTagRules;
	PoolTagRule baseTagRules[POOL_MAX_TAG_RULES];
	int nbaseLegendaryRules;
	PoolLegendaryRule baseLegendaryRules[MAX_POOL_LEGEND_RULES];
	int nbaseVoucherRules;
	PoolVoucherRule baseVoucherRules[POOL_MAX_VOUCHER_RULES];
	int nbaseVoucherExclusions;
	int baseVoucherExclusions[POOL_MAX_VOUCHER_EXCLUSIONS];
	char baseVoucherExclusionKeys[POOL_MAX_VOUCHER_EXCLUSIONS][MAX_KEY];
	uint64_t voucherExclusionMask; /* derived; not part of criteria identity */
	int nlegendary; /* current-stage rule (0 or 1) */
	PoolLegendaryRule legendary[1];
	int nvoucherRules;
	PoolVoucherRule voucherRules[POOL_MAX_VOUCHER_RULES];
	int nvoucherExclusions;
	int voucherExclusions[POOL_MAX_VOUCHER_EXCLUSIONS];
	char voucherExclusionKeys[POOL_MAX_VOUCHER_EXCLUSIONS][MAX_KEY];
	int maxVoucherAnte;
	int minTagAnte, maxTagAnte, maxAnte;
	int firstKind; /* 1 = Joker4, 2 = Tag<firstAnte>, 3 = Voucher1 */
	int firstAnte;
	char firstKey[32];
	int legendaryNeedsEdition; /* derived; not part of criteria identity */
	int simpleFirstSoul, simpleSoulMinAnte, simpleSoulMaxAnte; /* derived hot path */
	int vectorFirstGate, vectorGateTarget; /* derived hot path */
	uint64_t catalogHash, criteriaHash, stageHash;
	uint64_t familyId, segmentId, lineageId, derivationId;
	int refilter, refilterDepth;
	uint64_t sourceCriteriaHash, sourceRecords, sourceRangeStart, sourceRangeEnd;
	uint64_t sourceDataBytes, sourceMembershipDigest, sourceSnapshotId;
	uint64_t sourceFamilyId, sourceSegmentId, sourceLineageId;
	int sourceComplete, sourceCoverageComplete;
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
	int coverageComplete = complete && (!p->refilter || p->sourceCoverageComplete);
	int n = snprintf(buf, sizeof buf, "%016" PRIx64 "%016" PRIx64 "%" PRIu64 "-%" PRIu64 "%s%" PRIu64 "%d",
			p->catalogHash, p->criteriaHash, p->outputRangeStart, p->outputRangeEnd,
			space_name(p->space), records, coverageComplete);
	uint64_t h = pool_hash_update(UINT64_C(1469598103934665603), buf, (size_t)n);
	snprintf(out, 24, "%016" PRIx64, h);
}

/* High-throughput scans can cross 2^32 candidates per worker well inside the
 * six-hour search cap. Keep every lazy-stream generation at 64 bits in both
 * native engines so an epoch can never alias a prior candidate in practice. */
typedef struct { double state; uint64_t gen; } PoolStream;

typedef struct PoolOmenTrace PoolOmenTrace;
typedef struct PoolSoulTape PoolSoulTape;

typedef struct {
	const Config *g;
	const PoolPlan *p;
	char seed[9];
	uint8_t seedLen;
	double hashedSeed;
	uint64_t gen;
	/* Independent epoch for resettable shop-pack/Soul streams. Alternate tag
	 * and Omen routes restart those streams frequently within one candidate;
	 * advancing an epoch avoids clearing several kilobytes each time. */
	uint64_t soulWalkGen;
	/* pseudohash(key..seed) walks the seed before the key.  For every distinct
	 * key length, cache that exact post-seed chain state once per candidate;
	 * Tag/Voucher/pack/Soul streams of the same length then hash only their key
	 * bytes instead of repeating all seed-byte divisions. */
	uint64_t hashSeedPrefixMask;
	double hashSeedPrefix[MAX_KEY];
	PRNG prng;
	PoolStream joker4;
	PoolStream jokerResample[MAX_RESAMPLE];
	PoolStream tag[POOL_MAX_ANTE + 1];
	PoolStream tagResample[POOL_MAX_ANTE + 1][MAX_RESAMPLE];
	PoolStream voucher[POOL_MAX_VOUCHER_ANTE + 1];
	PoolStream voucherResample[POOL_MAX_VOUCHER_ANTE + 1][MAX_RESAMPLE];
	/* Omen/Soul routes forbid Ante reducers, so each VoucherN stream is read
	 * exactly once per route. Cache that first main/resample draw sequence per
	 * seed instead of re-hashing identical keys in every DFS sibling. */
	uint64_t voucherRawGen[POOL_MAX_VOUCHER_ANTE + 1];
	uint8_t voucherRawN[POOL_MAX_VOUCHER_ANTE + 1];
	uint8_t voucherRaw[POOL_MAX_VOUCHER_ANTE + 1][MAX_RESAMPLE + 1];
	uint8_t voucherVisits[POOL_MAX_VOUCHER_ANTE + 1];
	int tagRoll[POOL_MAX_ANTE + 1][2];
	uint8_t tagRollDone[POOL_MAX_ANTE + 1][2];
	PoolStream shopPack[POOL_MAX_ANTE + 1];
	/* Raw shop_pack draws are route-independent. Skips consume a shorter prefix
	 * and the forced Buffoon consumes no draw, so alternate Charm/Omen walks can
	 * replay this per-candidate prefix without rehashing or reseeding. */
	uint64_t shopPackRawGen[POOL_MAX_ANTE + 1];
	uint8_t shopPackRawN[POOL_MAX_ANTE + 1];
	uint16_t shopPackRaw[POOL_MAX_ANTE + 1][6];
	uint64_t shopPackPrefillGen;
	uint8_t shopPackPrefillMaxAnte;
	PoolSoulTape *soulTape;
	uint64_t packsGen[POOL_MAX_ANTE + 1];
	int packsN[POOL_MAX_ANTE + 1];
	uint16_t packClass[POOL_MAX_ANTE + 1][6];
	uint8_t skipSm[POOL_MAX_ANTE + 1], skipBig[POOL_MAX_ANTE + 1];
	uint8_t rewardSm[POOL_MAX_ANTE + 1], rewardBig[POOL_MAX_ANTE + 1];
	uint64_t voucherPurchased;
	uint8_t voucherPurchaseAnte[MAX_VOUCH], voucherPurchaseVisit[MAX_VOUCH];
	uint8_t charmRequired;
	int forcedAnte;
	int firstLegendaryIdx, secondLegendaryIdx;
	/* -1 unknown, 0 proven unreachable, 1 reachable for the current no-extra-
	 * Charm Omen probe. Used only to prune the immediately following Charm
	 * fallback; it is never carried across candidates. */
	int omenRoutePossible;
	/* Allocated only after a canonical/Charm miss reaches exhaustive Omen
	 * recovery. Normal tag, voucher, and fast-exact scans pay no memory cost. */
	PoolOmenTrace *omenTrace;
	/* Per-rule Soul depth after either-depth resolution (base rules first,
	 * then current-stage rules; set by pool_precheck_all_legendaries). */
	int legendResolved[MAX_POOL_LEGEND_RULES + 2];
} PoolCtx;

#define POOL_TAROT_TAPE_WORDS ((POOL_SOUL_CARDS_PER_ANTE + 63) / 64)
#define POOL_SPECTRAL_TAPE_WORDS ((POOL_SOUL_CARDS_PER_ANTE * 2 + 63) / 64)
#define POOL_OMEN_TAPE_BITS ((POOL_MAX_ANTE + 1) * POOL_SOUL_CARDS_PER_ANTE)
#define POOL_OMEN_TAPE_WORDS ((POOL_OMEN_TAPE_BITS + 63) / 64)

/* Threshold-only random tape shared by every Soul replay for one candidate.
 * Each keyed stream is still advanced serially and seeded exactly like Lua;
 * route-local cursors merely reuse already-observed independent draws. */
struct PoolSoulTape {
	uint64_t gen;
	PoolStream tarotState[POOL_MAX_ANTE + 1];
	PoolStream spectralState[POOL_MAX_ANTE + 1];
	PoolStream editionState[POOL_MAX_ANTE + 1];
	PoolStream omenState;
	uint16_t tarotFilled[POOL_MAX_ANTE + 1];
	uint16_t spectralFilled[POOL_MAX_ANTE + 1];
	uint8_t editionFilled[POOL_MAX_ANTE + 1];
	uint32_t omenFilled;
	uint64_t tarotHit[POOL_MAX_ANTE + 1][POOL_TAROT_TAPE_WORDS];
	uint64_t spectralHit[POOL_MAX_ANTE + 1][POOL_SPECTRAL_TAPE_WORDS];
	uint64_t editionNegative[POOL_MAX_ANTE + 1];
	uint64_t omenConvert[POOL_OMEN_TAPE_WORDS];
};

#define POOL_EVENT_BLOCK_RECORDS 1024
#define POOL_MAX_OCCURRENCES 160
enum { POOL_META_TAG = 1, POOL_META_LEGENDARY = 2, POOL_META_VOUCHER = 3 };
enum {
	POOL_META_NEGATIVE = 1u << 0,
	POOL_META_CHARM_REQUIRED = 1u << 1,
	POOL_META_PURCHASED = 1u << 2
};

typedef struct {
	uint16_t keyIndex;
	uint8_t kind, ante, phase, source, ordinal, flags;
} PoolOccurrence;

typedef struct {
	uint8_t count;
	PoolOccurrence occurrence[POOL_MAX_OCCURRENCES];
} PoolMetadata;

static bool pool_metadata_add(PoolMetadata *m, PoolOccurrence value) {
	if (!m) return true;
	for (uint8_t i = 0; i < m->count; i++) {
		const PoolOccurrence *x = &m->occurrence[i];
		if (x->keyIndex == value.keyIndex && x->kind == value.kind
				&& x->ante == value.ante && x->phase == value.phase
				&& x->source == value.source && x->ordinal == value.ordinal
				&& x->flags == value.flags) return true;
	}
	if (m->count >= POOL_MAX_OCCURRENCES) return false;
	m->occurrence[m->count++] = value;
	return true;
}

typedef struct {
	uint64_t cursor, outputBytes, matched, scanned, membershipDigest, metadataDigest;
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

static bool pool_parse_phase(const char *s, int allowBoss, int *out) {
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

static bool pool_parse_source(const char *s, int *out) {
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

static const char *pool_phase_str(int phase) {
	return phase == SOUL_PHASE_BOSS ? "boss"
		: phase == SOUL_PHASE_SMALL ? "small" : "big";
}

static const char *pool_source_str(int source) {
	return source == SOUL_SOURCE_SHOP ? "shop"
		: source == SOUL_SOURCE_CHARM ? "charm"
		: source == SOUL_SOURCE_ETHEREAL ? "ethereal" : "any";
}

static int pool_route_position(int ante, int phase) {
	/* Human route order is Small -> Big -> Boss. The enum values are grouped
	 * for pack-source handling (Boss=0), so do not use them as chronology. */
	int order = phase == SOUL_PHASE_SMALL ? 0
		: phase == SOUL_PHASE_BIG ? 1 : 2;
	return ante * 3 + order;
}

static bool pool_location_in_range(int ante, int phase,
		int minAnte, int minPhase, int maxAnte, int maxPhase) {
	int position = pool_route_position(ante, phase);
	return position >= pool_route_position(minAnte, minPhase)
		&& position <= pool_route_position(maxAnte, maxPhase);
}

static bool pool_parse_hex64(const char *s, uint64_t *out) {
	if (!s || !*s || *s == '-') return false;
	errno = 0;
	char *end = NULL;
	unsigned long long v = strtoull(s, &end, 16);
	if (errno || !end || *end) return false;
	*out = (uint64_t)v;
	return true;
}

static bool pool_parse_double(const char *s, double *out) {
	if (!s || !*s) return false;
	errno = 0;
	char *end = NULL;
	double v = strtod(s, &end);
	if (errno || !end || *end || !isfinite(v)) return false;
	*out = v;
	return true;
}

static uint64_t pool_hash_stage(const PoolPlan *p) {
	uint64_t h = POOL_HASH_INIT;
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
	/* Fast route coverage is a different membership predicate and therefore a
	 * different resumable pool identity. Omit the default so every existing
	 * exhaustive criteria/state fingerprint remains byte-for-byte stable. */
	if (p->legendaryRoutesExplicit
			&& p->legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM) {
		n = snprintf(line, sizeof line, "legendary_routes canonical_charm\n");
		h = pool_hash_update(h, line, (size_t)n);
	}
	for (int i = 0; i < p->ntagRules; i++) {
		const PoolTagRule *r = &p->tagRules[i];
		if (r->minPhase == SOUL_PHASE_SMALL && r->maxPhase == SOUL_PHASE_BIG)
			n = snprintf(line, sizeof line, "tag %s %d %d %d\n",
					r->key, r->minAnte, r->maxAnte, r->minCount);
		else
			n = snprintf(line, sizeof line, "tag %s %d %s %d %s %d\n",
					r->key, r->minAnte, pool_phase_str(r->minPhase),
					r->maxAnte, pool_phase_str(r->maxPhase), r->minCount);
		h = pool_hash_update(h, line, (size_t)n);
	}
	for (int i = 0; i < p->nlegendary; i++) {
		const PoolLegendaryRule *r = &p->legendary[i];
		if (r->humanLocation)
			n = snprintf(line, sizeof line, "legendary %s %d %s %d %s %d %s\n",
					r->key, r->minAnte, pool_phase_str(r->minPhase),
					r->maxAnte, pool_phase_str(r->maxPhase), r->requireNegative,
					pool_source_str(r->source));
		else
			n = snprintf(line, sizeof line, "legendary %s %d %d %d\n",
					r->key, r->minAnte, r->maxAnte, r->requireNegative);
		h = pool_hash_update(h, line, (size_t)n);
		/* Preserve every existing depth-1 pool/state fingerprint. */
		if (r->soulDepth != 1) {
			n = snprintf(line, sizeof line, "soul_depth %s\n",
					pool_soul_depth_str(r->soulDepth));
			h = pool_hash_update(h, line, (size_t)n);
		}
	}
	for (int i = 0; i < p->nvoucherRules; i++) {
		const PoolVoucherRule *r = &p->voucherRules[i];
		n = snprintf(line, sizeof line, "voucher %s %d %d\n",
				r->key, r->minAnte, r->maxAnte);
		h = pool_hash_update(h, line, (size_t)n);
	}
	for (int i = 0; i < p->nvoucherExclusions; i++) {
		n = snprintf(line, sizeof line, "voucher_exclude %s\n",
				p->voucherExclusionKeys[i]);
		h = pool_hash_update(h, line, (size_t)n);
	}
	return h;
}

static uint64_t pool_hash_plan(const PoolPlan *p) {
	uint64_t h = pool_hash_stage(p);
	char line[160];
	int n;
	if (p->refilter) {
		n = snprintf(line, sizeof line, "source %016" PRIx64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %d\n",
				p->sourceCriteriaHash, p->sourceRecords, p->sourceRangeStart,
				p->sourceRangeEnd, p->space);
		h = pool_hash_update(h, line, (size_t)n);
		/* Keep every existing complete-source refilter fingerprint stable. A
		 * provisional snapshot is a different input truth even if its current
		 * record count happens to match another source. */
		if (!p->sourceCoverageComplete) {
			n = snprintf(line, sizeof line, "source_incomplete %s\n",
					p->sourcePoolId[0] ? p->sourcePoolId : "-");
			h = pool_hash_update(h, line, (size_t)n);
		}
	}
	return h;
}

static uint64_t pool_hash_fields(const char *kind, uint64_t a, uint64_t b,
		uint64_t c, uint64_t d) {
	char text[128];
	int n = snprintf(text, sizeof text, "%s:%016" PRIx64 ":%016" PRIx64
			":%016" PRIx64 ":%016" PRIx64, kind, a, b, c, d);
	return pool_hash_update(POOL_HASH_INIT, text, (size_t)n);
}

static void pool_set_identity(PoolPlan *p) {
	p->stageHash = pool_hash_stage(p);
	if (p->refilter) {
		p->familyId = p->sourceFamilyId ? p->sourceFamilyId
				: pool_hash_fields("family-fallback", p->catalogHash,
						p->sourceCriteriaHash, (uint64_t)p->space, 0);
		uint64_t parentLineage = p->sourceLineageId ? p->sourceLineageId
				: pool_hash_fields("lineage-fallback", p->familyId,
						p->sourceCriteriaHash, 0, 0);
		p->lineageId = pool_hash_fields("refilter", parentLineage,
				p->stageHash, p->sourceSnapshotId, 0);
	} else {
		p->familyId = pool_hash_fields("family", p->catalogHash, p->stageHash,
				(uint64_t)p->space, space_size(p->space));
		p->lineageId = pool_hash_fields("root", p->familyId, p->stageHash, 0, 0);
	}
	p->segmentId = pool_hash_fields("segment", p->lineageId,
			p->outputRangeStart, p->outputRangeEnd, (uint64_t)p->space);
	p->derivationId = pool_hash_fields(p->refilter ? "derive-refilter" : "derive-scan",
			p->lineageId, p->segmentId, p->sourceSnapshotId, p->stageHash);
}

static uint64_t pool_snapshot_id(const PoolPlan *p, uint64_t records,
		uint64_t dataBytes, uint64_t membershipDigest) {
	return pool_hash_fields("snapshot", p->segmentId, records, dataBytes,
			membershipDigest);
}

static bool pool_hash_fd_region(int fd, uint64_t offset, uint64_t bytes,
		uint64_t *digest) {
	unsigned char buf[64 * 1024];
	uint64_t h = POOL_HASH_INIT;
	while (bytes) {
		size_t n = bytes < sizeof buf ? (size_t)bytes : sizeof buf;
		if (offset > (uint64_t)INT64_MAX
				|| bs_pread(fd, buf, n, (int64_t)offset) != (int64_t)n) return false;
		h = pool_hash_update(h, buf, n);
		offset += n; bytes -= n;
	}
	*digest = h;
	return true;
}

static int pool_find_tag(const Config *g, const char *key) {
	for (int i = 0; i < g->ntags; i++) if (!strcmp(g->tagKey[i], key)) return i;
	return -1;
}

static int pool_find_legendary(const Config *g, const char *key) {
	for (int i = 0; i < g->njoker[4]; i++) if (!strcmp(g->jokerKey[4][i], key)) return i;
	return -1;
}

static int pool_find_voucher(const Config *g, const char *key) {
	for (int i = 0; i < g->nvouch; i++) if (!strcmp(g->vouchKey[i], key)) return i;
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
	p->chunk = UINT64_C(2048);
	p->format = POOL_BINARY;
	p->outputSchema = BSPOOL_SCHEMA_EVENTS;
	p->headerBytes = BSPOOL_HEADER_EVENTS_SIZE;
	p->legendaryRoutes = BSPOOL_LEGENDARY_ROUTES_FULL;
	p->baseLegendaryRoutes = BSPOOL_LEGENDARY_ROUTES_FULL;
	p->minTagAnte = POOL_MAX_ANTE + 1;
	p->legendary[0].soulDepth = 1;
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
		} else if (!strcmp(d, "legendary_routes")) {
			if (p->legendaryRoutesExplicit) goto bad_value;
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "full"))
				p->legendaryRoutes = BSPOOL_LEGENDARY_ROUTES_FULL;
			else if (v && !strcmp(v, "canonical_charm"))
				p->legendaryRoutes = BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM;
			else goto bad_value;
			p->legendaryRoutesExplicit = 1;
		} else if (!strcmp(d, "space")) {
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "natural")) p->space = SPACE_NATURAL;
			else if (v && !strcmp(v, "settable")) p->space = SPACE_SETTABLE;
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
			r->minPhase = SOUL_PHASE_SMALL;
			r->maxPhase = SOUL_PHASE_BIG;
			char *key = pool_tok(&sp);
			if (!key || strlen(key) >= sizeof r->key) goto bad_value;
			snprintf(r->key, sizeof r->key, "%s", key);
			char *values[6]; int nv = 0;
			for (char *v = pool_tok(&sp); v && nv < 6; v = pool_tok(&sp)) values[nv++] = v;
			if (nv == 3) {
				if (!pool_parse_int(values[0], &r->minAnte)
						|| !pool_parse_int(values[1], &r->maxAnte)
						|| !pool_parse_int(values[2], &r->minCount)) goto bad_value;
			} else if (nv == 5) {
				if (!pool_parse_int(values[0], &r->minAnte)
						|| !pool_parse_phase(values[1], 0, &r->minPhase)
						|| !pool_parse_int(values[2], &r->maxAnte)
						|| !pool_parse_phase(values[3], 0, &r->maxPhase)
						|| !pool_parse_int(values[4], &r->minCount)) goto bad_value;
			} else goto bad_value;
		} else if (!strcmp(d, "legendary")) {
			if (p->nlegendary) {
				snprintf(err, errsz, "criteria line %d: only one legendary rule is supported", lineno);
				goto fail;
			}
			PoolLegendaryRule *r = &p->legendary[p->nlegendary++];
			r->used = 1;
			r->minPhase = SOUL_PHASE_BOSS;
			r->maxPhase = SOUL_PHASE_BIG;
			char *key = pool_tok(&sp);
			if (!key || strlen(key) >= sizeof r->key) goto bad_value;
			snprintf(r->key, sizeof r->key, "%s", key);
			char *values[7]; int nv = 0;
			for (char *v = pool_tok(&sp); v && nv < 7; v = pool_tok(&sp)) values[nv++] = v;
			if (nv == 2 || nv == 3) {
				if (!pool_parse_int(values[0], &r->minAnte)
						|| !pool_parse_int(values[1], &r->maxAnte)
						|| (nv == 3 && !pool_parse_int(values[2], &r->requireNegative))) goto bad_value;
			} else if (nv == 6) {
				r->humanLocation = 1;
				if (!pool_parse_int(values[0], &r->minAnte)
						|| !pool_parse_phase(values[1], 1, &r->minPhase)
						|| !pool_parse_int(values[2], &r->maxAnte)
						|| !pool_parse_phase(values[3], 1, &r->maxPhase)
						|| !pool_parse_int(values[4], &r->requireNegative)
						|| !pool_parse_source(values[5], &r->source)) goto bad_value;
			} else goto bad_value;
		} else if (!strcmp(d, "voucher")) {
			if (p->nvoucherRules >= POOL_MAX_VOUCHER_RULES) {
				snprintf(err, errsz, "criteria line %d: too many voucher rules (max %d)",
						lineno, POOL_MAX_VOUCHER_RULES);
				goto fail;
			}
			PoolVoucherRule *r = &p->voucherRules[p->nvoucherRules++];
			char *key = pool_tok(&sp);
			if (!key || strlen(key) >= sizeof r->key
					|| !pool_parse_int(pool_tok(&sp), &r->minAnte)
					|| !pool_parse_int(pool_tok(&sp), &r->maxAnte)) goto bad_value;
			snprintf(r->key, sizeof r->key, "%s", key);
		} else if (!strcmp(d, "voucher_exclude")) {
			if (p->nvoucherExclusions >= POOL_MAX_VOUCHER_EXCLUSIONS) {
				snprintf(err, errsz, "criteria line %d: too many voucher exclusions (max %d)",
						lineno, POOL_MAX_VOUCHER_EXCLUSIONS);
				goto fail;
			}
			char *key = pool_tok(&sp);
			if (!key) goto bad_value;
			int index = pool_find_voucher(g, key);
			if (index < 0) goto bad_value;
			for (int i = 0; i < p->nvoucherExclusions; i++)
				if (p->voucherExclusions[i] == index) goto bad_value;
			int slot = p->nvoucherExclusions++;
			p->voucherExclusions[slot] = index;
			snprintf(p->voucherExclusionKeys[slot], MAX_KEY, "%s", key);
		} else if (!strcmp(d, "soul_depth")) {
			/* Applies to the most recent legendary rule (or, for
			 * compatibility with older single-rule files, the first rule
			 * even when the directive precedes its legendary line). */
			PoolLegendaryRule *r =
				&p->legendary[p->nlegendary ? p->nlegendary - 1 : 0];
			char *v = pool_tok(&sp);
			if (v && !strcmp(v, "any")) r->soulDepth = SOUL_DEPTH_ANY;
			else if (!pool_parse_int(v, &r->soulDepth)) goto bad_value;
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
		p->threads = ncpu > 64 ? 64 : (ncpu > 0 ? ncpu : 1);
	}
	if (p->threads < 1 || p->threads > 64) { snprintf(err, errsz, "threads must be 0..64"); return false; }
	if (p->chunk < ILV || p->chunk > UINT64_C(1073741824)) { snprintf(err, errsz, "chunk must be between %d and 1073741824", ILV); return false; }
	p->chunk -= p->chunk % ILV;
	if (!p->checkpoint) { snprintf(err, errsz, "checkpoint must be positive"); return false; }
	if (p->checkpoint < p->chunk) p->checkpoint = p->chunk;
	p->resume = !!p->resume;
	p->collectTags = !!p->collectTags;
	if (p->ntagRules == 0 && !p->nlegendary && !p->nvoucherRules) {
		snprintf(err, errsz, "criteria has no predicates"); return false;
	}
	if (p->nvoucherExclusions && !p->nvoucherRules) {
		snprintf(err, errsz, "voucher_exclude requires at least one voucher rule");
		return false;
	}
	if (!p->nlegendary && p->legendary[0].soulDepth != 1) {
		snprintf(err, errsz, "soul_depth requires a legendary rule");
		return false;
	}
	if (p->legendaryRoutesExplicit && !p->nlegendary) {
		snprintf(err, errsz, "legendary_routes requires a legendary rule");
		return false;
	}

	for (int i = 0; i < p->ntagRules; i++) {
		PoolTagRule *r = &p->tagRules[i];
		r->collect = p->collectTags;
		if (r->minAnte < 1 || r->maxAnte < r->minAnte || r->maxAnte > POOL_MAX_ANTE
				|| pool_route_position(r->maxAnte, r->maxPhase)
						< pool_route_position(r->minAnte, r->minPhase)) {
			snprintf(err, errsz, "bad range for tag rule %s", r->key);
			return false;
		}
		int possible = 0;
		for (int ante = r->minAnte; ante <= r->maxAnte; ante++)
			for (int phase = SOUL_PHASE_SMALL; phase <= SOUL_PHASE_BIG; phase++)
				if (pool_location_in_range(ante, phase, r->minAnte, r->minPhase,
						r->maxAnte, r->maxPhase)) possible++;
		if (r->minCount < 1 || r->minCount > possible) {
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
	if (p->nlegendary) {
		if (!g->soulAllowed) {
			snprintf(err, errsz, "The Soul is banned in this snapshot");
			return false;
		}
		for (int i = 0; i < p->nlegendary; i++) {
			PoolLegendaryRule *r = &p->legendary[i];
			int rngMax = r->maxAnte + (r->humanLocation
					&& r->maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
			if (r->minAnte < 1 || r->maxAnte < r->minAnte || r->maxAnte > POOL_MAX_ANTE
					|| (r->humanLocation && pool_route_position(r->maxAnte, r->maxPhase)
							< pool_route_position(r->minAnte, r->minPhase))
					|| rngMax > POOL_MAX_ANTE) {
				snprintf(err, errsz, "bad range for legendary rule %s", r->key);
				return false;
			}
			if (r->soulDepth != SOUL_DEPTH_ANY
					&& (r->soulDepth < 1 || r->soulDepth > 2)) {
				snprintf(err, errsz, "soul_depth must be 1, 2, or any");
				return false;
			}
			r->requireNegative = !!r->requireNegative;
			r->poolIndex = pool_find_legendary(g, r->key);
			if (r->poolIndex < 0 || !g->jokerAvail[4][r->poolIndex]) {
				snprintf(err, errsz, "legendary %s is unavailable in this snapshot", r->key);
				return false;
			}
			if (rngMax > p->maxAnte) p->maxAnte = rngMax;
		}
		p->firstKind = 1;
		snprintf(p->firstKey, sizeof p->firstKey, "Joker4");
	} else if (p->ntagRules) {
		p->firstKind = 2;
		p->firstAnte = p->minTagAnte;
		snprintf(p->firstKey, sizeof p->firstKey, "Tag%d", p->firstAnte);
	} else {
		p->firstKind = 3;
		snprintf(p->firstKey, sizeof p->firstKey, "Voucher1");
	}
	if (p->nvoucherRules) {
		if (g->nvouch > 64) {
			snprintf(err, errsz,
					"voucher routes support at most 64 catalog entries (snapshot has %d)",
					g->nvouch);
			return false;
		}
		for (int i = 0; i < g->nvouch; i++) {
			if (!g->vouchRouteDefined[i]) {
				snprintf(err, errsz,
						"voucher route catalog is missing; refresh native_search.cfg in Balatro");
				return false;
			}
		}
		for (int i = 0; i < p->nvoucherRules; i++) {
			PoolVoucherRule *r = &p->voucherRules[i];
			if (r->minAnte < 1 || r->maxAnte < r->minAnte
					|| r->maxAnte > POOL_MAX_VOUCHER_ANTE) {
				snprintf(err, errsz, "bad range for voucher rule %s", r->key);
				return false;
			}
			r->poolIndex = pool_find_voucher(g, r->key);
			if (r->poolIndex < 0 || !g->vouchRouteAvail[r->poolIndex]) {
				snprintf(err, errsz, "voucher %s is unavailable in this snapshot", r->key);
				return false;
			}
			if (g->vouchInitiallyOwned[r->poolIndex]) {
				snprintf(err, errsz,
						"voucher %s is already owned at the start of this snapshot", r->key);
				return false;
			}
			if (r->maxAnte > p->maxVoucherAnte) p->maxVoucherAnte = r->maxAnte;
		}
		if (p->maxVoucherAnte > p->maxAnte) p->maxAnte = p->maxVoucherAnte;
		for (int i = 0; i < p->ntagRules; i++) {
			const PoolTagRule *r = &p->tagRules[i];
			if (r->collect && (!strcmp(r->key, "tag_voucher")
					|| !strcmp(r->key, "tag_double"))) {
				snprintf(err, errsz,
						"collected Voucher/Double Tags are not yet supported with voucher routes");
				return false;
			}
		}
	}
	if (p->maxTagAnte > p->maxAnte) p->maxAnte = p->maxTagAnte;
	return true;
fail:
	if (f) fclose(f);
	return false;
}

static inline double pool_pseudohash_ks_n(PoolCtx *c, const char *key, int kl) {
	if (kl < 0 || kl >= MAX_KEY) return pseudohash_ks(key, c->seed);
	uint64_t bit = UINT64_C(1) << kl;
	double num;
	if (!(c->hashSeedPrefixMask & bit)) {
		num = 1.0;
		for (int si = (int)c->seedLen - 1; si >= 0; si--) {
			int pos = kl + si + 1;
			num = lua_mod1((1.1239285023 / num)
					* (double)(unsigned char)c->seed[si] * LUA_PI
					+ LUA_PI * (double)pos);
		}
		c->hashSeedPrefix[kl] = num;
		c->hashSeedPrefixMask |= bit;
	} else {
		num = c->hashSeedPrefix[kl];
	}
	for (int pos = kl; pos >= 1; pos--) {
		num = lua_mod1((1.1239285023 / num)
				* (double)(unsigned char)key[pos - 1] * LUA_PI
				+ LUA_PI * (double)pos);
	}
	return num;
}

static inline double pool_pseudohash_ks(PoolCtx *c, const char *key) {
	return pool_pseudohash_ks_n(c, key, (int)strlen(key));
}

static inline double pool_stream_next_gen_n(PoolCtx *c, PoolStream *s,
		const char *key, int keyLen, uint64_t gen) {
	if (s->gen != gen) {
		s->state = pool_pseudohash_ks_n(c, key, keyLen);
		s->gen = gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashedSeed) / 2.0;
}

static inline double pool_stream_next_gen(PoolCtx *c, PoolStream *s,
		const char *key, uint64_t gen) {
	return pool_stream_next_gen_n(c, s, key, (int)strlen(key), gen);
}

static inline double pool_stream_next(PoolCtx *c, PoolStream *s, const char *key) {
	return pool_stream_next_gen(c, s, key, c->gen);
}

static inline double pool_stream_next_n(PoolCtx *c, PoolStream *s,
		const char *key, int keyLen) {
	return pool_stream_next_gen_n(c, s, key, keyLen, c->gen);
}

static inline double pool_psr(PoolCtx *c, double seedval) {
	(void)c;
	return lj_random_seed_one(seedval);
}

static inline int pool_psr_n(PoolCtx *c, double seedval, int n) {
	(void)c;
	return lj_random_seed_one_n(seedval, n);
}

static inline int pool_psr_gt_997(PoolCtx *c, double seedval) {
	(void)c;
	return lj_random_seed_gt_997(seedval);
}

static inline int pool_psr_gt_08(PoolCtx *c, double seedval) {
	(void)c;
	return lj_random_seed_gt_08(seedval);
}

static inline void pool_tape_store(uint64_t *bits, uint32_t at, int value) {
	uint64_t mask = UINT64_C(1) << (at & 63u);
	if (value) bits[at >> 6] |= mask;
	else bits[at >> 6] &= ~mask;
}

static inline int pool_tape_load(const uint64_t *bits, uint32_t at) {
	return (int)((bits[at >> 6] >> (at & 63u)) & 1u);
}

static bool pool_soul_tape_prepare(PoolCtx *c) {
	if (!c->soulTape) c->soulTape = calloc(1, sizeof *c->soulTape);
	PoolSoulTape *t = c->soulTape;
	if (!t) return false;
	if (t->gen == c->gen) return true;
	t->gen = c->gen;
	memset(t->tarotFilled, 0, sizeof t->tarotFilled);
	memset(t->spectralFilled, 0, sizeof t->spectralFilled);
	memset(t->editionFilled, 0, sizeof t->editionFilled);
	t->omenFilled = 0;
	return true;
}

#ifdef BRAINSTORM_VERIFY_SOUL_TAPE
static int pool_soul_tape_reference(PoolCtx *c, const char *key,
		uint32_t at, double threshold) {
	PoolStream state = { 0 };
	double value = 0.0;
	for (uint32_t i = 0; i <= at; i++)
		value = pool_psr(c, pool_stream_next(c, &state, key));
	return value > threshold;
}
#define POOL_VERIFY_TAPE_VALUE(c_, key_, at_, threshold_, got_) do { \
	int reference_ = pool_soul_tape_reference((c_), (key_), (at_), (threshold_)); \
	if ((got_) != reference_) { \
		fprintf(stderr, "Soul tape mismatch seed=%s key=%s at=%u cached=%d reference=%d\n", \
				(c_)->seed, (key_), (unsigned)(at_), (got_), reference_); \
		abort(); \
	} \
} while (0)
#else
#define POOL_VERIFY_TAPE_VALUE(c_, key_, at_, threshold_, got_) ((void)0)
#endif

static int pool_soul_tape_tarot(PoolCtx *c, int ante, uint16_t at) {
	PoolSoulTape *t = c->soulTape;
	if (ante < 1 || ante > POOL_MAX_ANTE || at >= POOL_SOUL_CARDS_PER_ANTE)
		return 0;
	while (t->tarotFilled[ante] <= at) {
		uint16_t pos = t->tarotFilled[ante]++;
		int hit = pool_psr_gt_997(c, pool_stream_next_n(c,
				&t->tarotState[ante], KT_SOULT[ante], KL_SOULT[ante]));
		pool_tape_store(t->tarotHit[ante], pos, hit);
		POOL_VERIFY_TAPE_VALUE(c, KT_SOULT[ante], pos, 0.997, hit);
	}
	return pool_tape_load(t->tarotHit[ante], at);
}

static int pool_soul_tape_spectral(PoolCtx *c, int ante, uint16_t at) {
	PoolSoulTape *t = c->soulTape;
	if (ante < 1 || ante > POOL_MAX_ANTE
			|| at >= POOL_SOUL_CARDS_PER_ANTE * 2) return 0;
	while (t->spectralFilled[ante] <= at) {
		uint16_t pos = t->spectralFilled[ante]++;
		int hit = pool_psr_gt_997(c, pool_stream_next_n(c,
				&t->spectralState[ante], KT_SOULS[ante], KL_SOULS[ante]));
		pool_tape_store(t->spectralHit[ante], pos, hit);
		POOL_VERIFY_TAPE_VALUE(c, KT_SOULS[ante], pos, 0.997, hit);
	}
	return pool_tape_load(t->spectralHit[ante], at);
}

static int pool_soul_tape_edition(PoolCtx *c, int ante, uint8_t at) {
	PoolSoulTape *t = c->soulTape;
	if (ante < 1 || ante > POOL_MAX_ANTE || at >= 64) return 0;
	while (t->editionFilled[ante] <= at) {
		uint8_t pos = t->editionFilled[ante]++;
		int negative = pool_psr_gt_997(c, pool_stream_next_n(c,
				&t->editionState[ante], KT_EDISOU[ante], KL_EDISOU[ante]));
		pool_tape_store(&t->editionNegative[ante], pos, negative);
		POOL_VERIFY_TAPE_VALUE(c, KT_EDISOU[ante], pos, 0.997, negative);
	}
	return pool_tape_load(&t->editionNegative[ante], at);
}

static int pool_soul_tape_omen(PoolCtx *c, uint32_t at) {
	PoolSoulTape *t = c->soulTape;
	if (at >= POOL_OMEN_TAPE_BITS) return 0;
	while (t->omenFilled <= at) {
		uint32_t pos = t->omenFilled++;
		int convert = pool_psr_gt_08(c, pool_stream_next_n(c,
				&t->omenState, "omen_globe", 10));
		pool_tape_store(t->omenConvert, pos, convert);
		POOL_VERIFY_TAPE_VALUE(c, "omen_globe", pos, 0.8, convert);
	}
	return pool_tape_load(t->omenConvert, at);
}

/* rsKeys: one precomputed "<base>_resample<it>" row from init_key_tables()
 * (RS_RBASE / RS_TAG / RS_VOUCHER); hot loops must not rebuild key strings. */
static double pool_resample_next(PoolCtx *c, PoolStream *streams,
		const char (*rsKeys)[24], const uint8_t *rsKeyLen, int it) {
	if (it - 2 >= MAX_RESAMPLE) return NAN;
	PoolStream *s = &streams[it - 2];
	if (s->gen != c->gen) {
		s->state = pool_pseudohash_ks_n(c, rsKeys[it - 2], rsKeyLen[it - 2]);
		s->gen = c->gen;
	}
	s->state = round13(lua_mod1(2.134453429141 + s->state * 1.72431234));
	return (s->state + c->hashedSeed) / 2.0;
}

static int pool_pick_culled(PoolCtx *c, PoolStream *first, const char *firstKey,
		int firstKeyLen, const char (*rsKeys)[24], const uint8_t *rsKeyLen,
		PoolStream *resamples, const uint8_t *avail, int n) {
	int idx = pool_psr_n(c, pool_stream_next_n(c, first, firstKey, firstKeyLen), n);
	int it = 1;
	while (idx > 0 && !avail[idx - 1]) {
		it++;
		double sv = pool_resample_next(c, resamples, rsKeys, rsKeyLen, it);
		if (isnan(sv)) return -1;
		idx = pool_psr_n(c, sv, n);
	}
	return idx > 0 ? idx - 1 : -1;
}

static int pool_roll_tag_at(PoolCtx *c, int ante, int blind) {
	if (ante < 1 || ante > POOL_MAX_ANTE || blind < 0 || blind > 1) return -1;
	if (c->tagRollDone[ante][blind]) return c->tagRoll[ante][blind];
	if (blind == 1 && !c->tagRollDone[ante][0]
			&& pool_roll_tag_at(c, ante, 0) < 0) return -1;
	const Config *g = c->g;
	int idx = pool_psr_n(c, pool_stream_next_n(c, &c->tag[ante],
			KT_TAG[ante], KL_TAG[ante]), g->ntags);
	int it = 1;
	while (idx > 0 && !(g->tagReqOk[idx - 1]
			&& (g->tagMinAnte[idx - 1] == 0 || g->tagMinAnte[idx - 1] <= ante))) {
		it++;
		double sv = pool_resample_next(c, c->tagResample[ante], RS_TAG[ante],
				KL_RS_TAG[ante], it);
		if (isnan(sv)) return -1;
		idx = pool_psr_n(c, sv, g->ntags);
	}
	idx = idx > 0 ? idx - 1 : -1;
	c->tagRoll[ante][blind] = idx;
	c->tagRollDone[ante][blind] = 1;
	return idx;
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

/* Refiltering is an intersection on one physical route, not merely a second
 * independent predicate pass. Replay the source pool's cumulative collected
 * tag rules so the new Soul/pack criteria see the same missing shops. */
static bool pool_apply_base_route(PoolCtx *c, PoolMetadata *metadata) {
	const PoolPlan *p = c->p;
	int counts[POOL_MAX_TAG_RULES] = { 0 };
	for (int ante = 1; ante <= p->maxAnte; ante++) {
		for (int blind = 0; blind < 2; blind++) {
			int phase = blind == 0 ? SOUL_PHASE_SMALL : SOUL_PHASE_BIG;
			int needRoll = 0;
			for (int r = 0; r < p->nbaseTagRules; r++) {
				const PoolTagRule *rule = &p->baseTagRules[r];
				if (pool_location_in_range(ante, phase, rule->minAnte, rule->minPhase,
						rule->maxAnte, rule->maxPhase)
						&& (metadata || (rule->collect && counts[r] < rule->minCount))) {
					needRoll = 1; break;
				}
			}
			if (!needRoll) continue;
			int idx = pool_roll_tag_at(c, ante, blind);
			if (idx < 0) return false;
			for (int r = 0; r < p->nbaseTagRules; r++) {
				const PoolTagRule *rule = &p->baseTagRules[r];
				if (!pool_location_in_range(ante, phase, rule->minAnte, rule->minPhase,
						rule->maxAnte, rule->maxPhase)
						|| idx != rule->poolIndex) continue;
				if (!pool_metadata_add(metadata, (PoolOccurrence) {
						.kind = POOL_META_TAG, .keyIndex = (uint16_t)rule->poolIndex,
						.ante = (uint8_t)ante,
						.phase = (uint8_t)phase,
				})) return false;
				if (!rule->collect || counts[r] >= rule->minCount) continue;
				counts[r]++;
				int reward = tag_reward_kind(c->g, idx);
				if (blind == 0) {
					c->skipSm[ante] = 1;
					if (reward) c->rewardSm[ante] = (uint8_t)reward;
				} else {
					c->skipBig[ante] = 1;
					if (reward) c->rewardBig[ante] = (uint8_t)reward;
				}
			}
		}
	}
	return true;
}

static bool pool_check_tags(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata) {
	const PoolPlan *p = c->p;
	int counts[POOL_MAX_TAG_RULES] = { 0 };
	bool wroteRule[POOL_MAX_TAG_RULES] = { false };
	int remaining = p->ntagRules;
	for (int ante = p->minTagAnte; ante <= p->maxTagAnte; ante++) {
		for (int blind = 0; blind < 2; blind++) {
			int phase = blind == 0 ? SOUL_PHASE_SMALL : SOUL_PHASE_BIG;
			int needRoll = 0;
			for (int r = 0; r < p->ntagRules; r++) {
				const PoolTagRule *rule = &p->tagRules[r];
				if ((counts[r] < rule->minCount || metadata || label)
						&& pool_location_in_range(ante, phase,
							rule->minAnte, rule->minPhase,
							rule->maxAnte, rule->maxPhase)) {
					needRoll = 1;
					break;
				}
			}
			if (needRoll) {
				int idx = pool_roll_tag_at(c, ante, blind);
				if (idx < 0) return false;
				for (int r = 0; r < p->ntagRules; r++) {
					const PoolTagRule *rule = &p->tagRules[r];
					if (!pool_location_in_range(ante, phase,
							rule->minAnte, rule->minPhase,
							rule->maxAnte, rule->maxPhase)
							|| idx != rule->poolIndex) continue;
					if (!pool_metadata_add(metadata, (PoolOccurrence) {
							.kind = POOL_META_TAG,
							.keyIndex = (uint16_t)rule->poolIndex,
							.ante = (uint8_t)ante,
							.phase = (uint8_t)phase,
					})) return false;
					if (counts[r] >= rule->minCount) continue;
					counts[r]++;
					if (rule->collect) {
						int reward = tag_reward_kind(c->g, idx);
						if (blind == 0) {
							c->skipSm[ante] = 1;
							if (reward) c->rewardSm[ante] = (uint8_t)reward;
						} else {
							c->skipBig[ante] = 1;
							if (reward) c->rewardBig[ante] = (uint8_t)reward;
						}
					}
					if (wroteRule[r]) {
						pool_label_add(label, labelCap, ",A%d%s", ante,
								blind == 0 ? "Sm" : "Big");
					} else {
						pool_label_add(label, labelCap, "%s%s=A%d%s",
								label && label[0] ? " " : "", rule->key,
								ante, blind == 0 ? "Sm" : "Big");
					}
					wroteRule[r] = true;
					if (counts[r] == rule->minCount) remaining--;
				}
			}
			int position = pool_route_position(ante, phase);
			for (int r = 0; r < p->ntagRules; r++) {
				const PoolTagRule *rule = &p->tagRules[r];
				if (counts[r] < rule->minCount
						&& position >= pool_route_position(rule->maxAnte,
							rule->maxPhase)) return false;
			}
			/* Count/text/BSP2 callers need only the first collected occurrences;
			 * schema-3 metadata and fixture labels deliberately keep replaying the
			 * rest of each requested window. */
			if (!remaining && !metadata && !label) return true;
		}
	}
	return remaining == 0;
}

/* ------------------------------------------------------- voucher routes --
 * Voucher offers are rolled after the old shop has been destroyed, so an
 * unbought offer remains eligible on later Antes. Buying changes the dynamic
 * pool (owned vouchers disappear; upgrades whose prerequisite is now owned
 * become eligible). Hieroglyph/Petroglyph reduce the displayed Ante by one;
 * the following Boss restores it, causing another call to the SAME VoucherN
 * stream at its next advance. Explore skip first and retain the minimum-buy
 * route. Exclusions forbid only the buy edge, never the offer itself. */

typedef struct {
	PoolStream main;
	PoolStream resample[MAX_RESAMPLE];
	int nresample;
	uint8_t visits;
} PoolVoucherUndo;

#define POOL_VOUCHER_MEMO_CAP 1024
typedef struct {
	uint64_t purchased;
	uint32_t matched;
	uint16_t ante;
	uint8_t omenAnte, used;
} PoolVoucherMemoEntry;

typedef struct {
	PoolVoucherMemoEntry entry[POOL_VOUCHER_MEMO_CAP];
} PoolVoucherMemo;

typedef struct {
	int found, purchases;
	int maxAnte, requireOmen, requireSouls, forbidAnteReducers;
	int omenIndex;
	uint16_t omenActivationMask;
	uint64_t purchased;
	PoolMetadata metadata;
	PoolVoucherMemo *memo;
} PoolVoucherBest;

static bool pool_voucher_memo_seen(PoolCtx *c, PoolVoucherBest *best,
		int ante, uint64_t purchased, uint32_t matched) {
	if (!best->memo || (purchased & c->g->vouchReducerMask)) return false;
	uint8_t omenAnte = best->omenIndex >= 0 && best->omenIndex < MAX_VOUCH
			? c->voucherPurchaseAnte[best->omenIndex] : 0;
	uint64_t key = purchased ^ ((uint64_t)matched << 32)
			^ ((uint64_t)(unsigned)ante << 56) ^ ((uint64_t)omenAnte << 48);
	size_t slot = (size_t)splitmix64(key) & (POOL_VOUCHER_MEMO_CAP - 1u);
	for (size_t probe = 0; probe < POOL_VOUCHER_MEMO_CAP; probe++) {
		PoolVoucherMemoEntry *e = &best->memo->entry[slot];
		if (!e->used) {
			*e = (PoolVoucherMemoEntry) {
				.purchased = purchased, .matched = matched,
				.ante = (uint16_t)ante, .omenAnte = omenAnte, .used = 1,
			};
			return false;
		}
		if (e->purchased == purchased && e->matched == matched
				&& e->ante == ante && e->omenAnte == omenAnte) return true;
		slot = (slot + 1u) & (POOL_VOUCHER_MEMO_CAP - 1u);
	}
	/* A full table merely disables pruning; it must never reject a route. */
	return false;
}

static bool pool_check_all_souls(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata);

static void pool_reset_soul_walk(PoolCtx *c) {
	c->soulWalkGen++;
	/* A zero generation is the calloc/overflow sentinel used by untouched
	 * stream entries. Overflow is practically unreachable, but recovering here
	 * keeps the epoch invariant exact even for unbounded embedded use. */
	if (!c->soulWalkGen) {
		memset(c->packsGen, 0, sizeof c->packsGen);
		c->soulWalkGen = 1;
	}
}

static int pool_voucher_rule_count(const PoolPlan *p) {
	return p->nbaseVoucherRules + p->nvoucherRules;
}

static const PoolVoucherRule *pool_voucher_rule_at(const PoolPlan *p, int i) {
	return i < p->nbaseVoucherRules ? &p->baseVoucherRules[i]
			: &p->voucherRules[i - p->nbaseVoucherRules];
}

static bool pool_voucher_purchase_excluded(const PoolPlan *p, int index) {
	if (index >= 0 && index < 64)
		return (p->voucherExclusionMask & (UINT64_C(1) << index)) != 0;
	for (int i = 0; i < p->nbaseVoucherExclusions; i++)
		if (p->baseVoucherExclusions[i] == index) return true;
	for (int i = 0; i < p->nvoucherExclusions; i++)
		if (p->voucherExclusions[i] == index) return true;
	return false;
}

/* Return the first draw from VoucherN (position 0) or its independent
 * VoucherN_resample<K> key. This is valid only for routes that cannot repeat
 * an Ante: every branch then observes the same first keyed draw, while its
 * purchased mask merely decides which raw catalog values are accepted. */
static int pool_route_raw_voucher(PoolCtx *c, int ante, int position) {
	if (ante < 1 || ante > POOL_MAX_VOUCHER_ANTE
			|| position < 0 || position > MAX_RESAMPLE) return -1;
	if (c->voucherRawGen[ante] != c->gen) {
		c->voucherRawGen[ante] = c->gen;
		c->voucherRawN[ante] = 0;
	}
	while (c->voucherRawN[ante] <= position) {
		int at = c->voucherRawN[ante];
		const char *key = at == 0 ? KT_VOUCHER[ante]
				: RS_VOUCHER[ante][at - 1];
		int keyLen = at == 0 ? KL_VOUCHER[ante]
				: KL_RS_VOUCHER[ante][at - 1];
		double state = pool_pseudohash_ks_n(c, key, keyLen);
		state = round13(lua_mod1(2.134453429141 + state * 1.72431234));
		int index = pool_psr_n(c, (state + c->hashedSeed) / 2.0, c->g->nvouch);
		if (index < 1 || index > c->g->nvouch) return -1;
		c->voucherRaw[ante][at] = (uint8_t)index;
		c->voucherRawN[ante]++;
	}
	return c->voucherRaw[ante][position];
}

/* Reducer-free routes see one immutable raw VoucherN sequence per Ante. */
static int pool_route_raw_offer(PoolCtx *c, int ante, uint64_t availMask) {
	if (!availMask) return -1;
	for (int position = 0; position <= MAX_RESAMPLE; position++) {
		int raw = pool_route_raw_voucher(c, ante, position);
		if (raw < 1 || raw > c->g->nvouch) return -1;
		int index = raw - 1;
		if (availMask & (UINT64_C(1) << index)) return index;
	}
	return -1;
}

/* The reducer-free Omen frontier almost always reaches every configured Ante.
 * Fill its mandatory main raw column ante-wise, batching independent one-shot
 * TW223 reseeds.  Later replay/Charm attempts reuse the cache; eager resample
 * columns and interleaved key hashing were benchmarked and rejected. */
static bool pool_prefill_omen_voucher_raw(PoolCtx *c, int maxAnte) {
	double seedval[PRNG_BATCH_MAX];
	int result[PRNG_BATCH_MAX], anteOf[PRNG_BATCH_MAX];
	int count = 0;
	for (int ante = 1; ante <= maxAnte; ante++) {
		if (c->voucherRawGen[ante] != c->gen) {
			c->voucherRawGen[ante] = c->gen;
			c->voucherRawN[ante] = 0;
		}
		if (c->voucherRawN[ante] > 0) continue;
		anteOf[count++] = ante;
	}
	if (!count) return true;
	double advanced[PRNG_BATCH_MAX];
	for (int lane = 0; lane < count; lane++) {
		int ante = anteOf[lane];
		double state = pool_pseudohash_ks_n(c, KT_VOUCHER[ante],
				KL_VOUCHER[ante]);
		advanced[lane] = lua_mod1(2.134453429141 + state * 1.72431234);
	}
	for (int lane = 0; lane < count; lane++)
		seedval[lane] = (round13(advanced[lane]) + c->hashedSeed) / 2.0;
	if (count == 1) result[0] = pool_psr_n(c, seedval[0], c->g->nvouch);
	else lj_random_seed_one_n_batch(seedval, count, c->g->nvouch, result);
	for (int lane = 0; lane < count; lane++) {
#ifdef BRAINSTORM_VERIFY_VOUCHER_RAW_CACHE
		int reference = pool_psr_n(c, seedval[lane], c->g->nvouch);
			if (result[lane] != reference) {
				fprintf(stderr,
						"voucher raw batch mismatch seed=%s ante=%d position=0 batched=%d reference=%d\n",
						c->seed, anteOf[lane],
						result[lane], reference);
				abort();
			}
#endif
		if (result[lane] < 1 || result[lane] > c->g->nvouch) return false;
		int ante = anteOf[lane];
		c->voucherRaw[ante][0] = (uint8_t)result[lane];
		c->voucherRawN[ante] = 1;
	}
	return true;
}

/* The common exhaustive fallback has no explicit voucher predicate and
 * forbids Ante reducers. Enumerate its complete skip/buy frontier once, retain
 * the generic DFS's minimum-purchase/skip-first winner for every possible
 * Omen purchase Ante, then intersect those exact timings with the Soul walk.
 * At Ante 8 there are at most 2^8 states, so fixed stack storage is sufficient.
 * Unrelated purchases remain present: removing an offer can change which later
 * raw value survives resampling, and purchasing a base can unlock an upgrade. */
typedef struct {
	uint64_t purchased, availMask;
	uint16_t buyMask, order;
	uint8_t purchases, omenAnte;
} PoolOmenRouteState;

typedef struct {
	uint16_t feasibleMask;
	uint8_t found[POOL_MAX_VOUCHER_ANTE + 1];
	PoolOmenRouteState best[POOL_MAX_VOUCHER_ANTE + 1];
} PoolOmenRoutes;

static int pool_omen_state_better(const PoolOmenRouteState *a,
		const PoolOmenRouteState *b) {
	return a->purchases < b->purchases
			|| (a->purchases == b->purchases && a->order < b->order);
}

static bool pool_collect_omen_routes(PoolCtx *c, int maxAnte,
		PoolOmenRoutes *routes) {
	const Config *g = c->g;
	memset(routes, 0, sizeof *routes);
	if (maxAnte < 1 || maxAnte > POOL_MAX_VOUCHER_ANTE
			|| g->omenVoucherIdx < 0 || g->omenVoucherIdx >= g->nvouch
			|| g->nvouch > 64) return false;
	uint64_t omenBit = UINT64_C(1) << g->omenVoucherIdx;
	/* Starting Omen already participated in the failed canonical/Charm walks;
	 * this branch represents a physical purchase and therefore has no Ante-0
	 * timing to test. */
	if (g->vouchInitiallyOwnedMask & omenBit) return false;
	uint64_t forbidden = c->p->voucherExclusionMask | g->vouchReducerMask;
	if (g->omenPrereqMask & (forbidden | ~g->vouchRouteEligibleMask)) return false;
	if (!pool_prefill_omen_voucher_raw(c, maxAnte)) return false;
	/* Keep the live frontier in structure-of-arrays form.  The search touches
	 * purchased/available masks for every state but needs the materialized
	 * route record only when Omen is bought; avoiding copies of the padded
	 * 24-byte record keeps the complete 2^8 frontier in a much smaller working
	 * set. */
	enum { FRONTIER_CAP = 1 << POOL_MAX_VOUCHER_ANTE };
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
			int index = pool_route_raw_offer(c, ante, availMask);
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
				/* The generic skip-first search cannot buy anything after its only
				 * goal is owned.  Preserve its later-offer validity checks, but
				 * finalize this non-branching tail without carrying it through every
				 * remaining frontier array. */
				int valid = 1;
				for (int later = ante + 1; later <= maxAnte; later++) {
					if (pool_route_raw_offer(c, later, boughtAvail) < 0) {
						valid = 0;
						break;
					}
				}
				if (!valid) continue;
				PoolOmenRouteState state = {
					.purchased = bought, .availMask = boughtAvail,
					.buyMask = boughtMask,
					.order = (uint16_t)(boughtOrder << (maxAnte - ante)),
					.purchases = (uint8_t)__builtin_popcount((unsigned)boughtMask),
					.omenAnte = (uint8_t)ante,
				};
				if (!routes->found[ante]
						|| pool_omen_state_better(&state, &routes->best[ante])) {
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

static const PoolOmenRouteState *pool_best_omen_route(
		const PoolOmenRoutes *routes, uint16_t timings) {
	const PoolOmenRouteState *best = NULL;
	for (int ante = 1; ante <= POOL_MAX_VOUCHER_ANTE; ante++) {
		if (!(timings & (UINT16_C(1) << (ante - 1))) || !routes->found[ante])
			continue;
		const PoolOmenRouteState *candidate = &routes->best[ante];
		if (!best || pool_omen_state_better(candidate, best)) best = candidate;
	}
	return best;
}

static bool pool_materialize_omen_route(PoolCtx *c,
		const PoolOmenRouteState *state, int maxAnte,
		const PoolMetadata *base, PoolVoucherBest *best) {
	const Config *g = c->g;
	uint64_t purchased = g->vouchInitiallyOwnedMask;
	uint64_t availMask = g->vouchInitialAvailMask;
	PoolMetadata metadata = *base;
	for (int ante = 1; ante <= maxAnte; ante++) {
		int index = pool_route_raw_offer(c, ante, availMask);
		if (index < 0) return false;
		if (!(state->buyMask & (UINT16_C(1) << (ante - 1)))) continue;
		int purchasePhase = ante == 1
				? (c->skipSm[1] ? SOUL_PHASE_BIG : SOUL_PHASE_SMALL)
				: SOUL_PHASE_BOSS;
		if (!pool_metadata_add(&metadata, (PoolOccurrence) {
				.kind = POOL_META_VOUCHER, .keyIndex = (uint16_t)index,
				.ante = (uint8_t)ante, .phase = (uint8_t)purchasePhase,
				.source = SOUL_SOURCE_SHOP, .ordinal = 1,
				.flags = POOL_META_PURCHASED,
		})) return false;
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
	best->metadata = metadata;
	return true;
}

static int pool_roll_route_voucher(PoolCtx *c, int ante, uint64_t purchased,
		PoolVoucherUndo *undo, int firstVisitOnly) {
	const Config *g = c->g;
	if (ante < 1 || ante > POOL_MAX_VOUCHER_ANTE
			|| g->nvouch < 1 || g->nvouch > 64) {
		*undo = (PoolVoucherUndo){0};
		return -1;
	}
	const char *key = KT_VOUCHER[ante];
	undo->main = c->voucher[ante];
	undo->nresample = 0;
	undo->visits = c->voucherVisits[ante];
	c->voucherVisits[ante]++;

	/* Bit i is available iff route-eligible, not yet purchased, and its
	 * prerequisite (if any) is owned -- identical to the old per-entry
	 * catalog walk, assembled from masks precomputed at config load. */
	uint64_t unlocked = 0;
	for (uint64_t owned = purchased; owned; owned &= owned - 1)
		unlocked |= g->vouchUnlocksMask[__builtin_ctzll(owned)];
	uint64_t availMask = g->vouchRouteEligibleMask & ~purchased
			& (g->vouchNoPrereqMask | unlocked);
	if (!availMask) return -1;

	int idx = firstVisitOnly ? pool_route_raw_voucher(c, ante, 0)
			: pool_psr_n(c, pool_stream_next_n(c, &c->voucher[ante], key,
					KL_VOUCHER[ante]), g->nvouch);
	int it = 1;
	while (idx > 0 && !(availMask >> (idx - 1) & 1)) {
		it++;
		if (it - 2 >= MAX_RESAMPLE) return -1;
		if (firstVisitOnly) {
			idx = pool_route_raw_voucher(c, ante, it - 1);
		} else {
			undo->resample[undo->nresample++] = c->voucherResample[ante][it - 2];
			double value = pool_resample_next(c, c->voucherResample[ante],
					RS_VOUCHER[ante], KL_RS_VOUCHER[ante], it);
			if (isnan(value)) return -1;
			idx = pool_psr_n(c, value, g->nvouch);
		}
	}
	int result = idx > 0 ? idx - 1 : -1;
#ifdef BRAINSTORM_VERIFY_VOUCHER_RAW_CACHE
	if (firstVisitOnly) {
		PoolStream verifyMain = undo->main;
		PoolStream verifyResample[MAX_RESAMPLE];
		memcpy(verifyResample, c->voucherResample[ante], sizeof verifyResample);
		int verify = pool_psr_n(c, pool_stream_next_n(c, &verifyMain, key,
				KL_VOUCHER[ante]), g->nvouch);
		int verifyIt = 1;
		while (verify > 0 && !(availMask >> (verify - 1) & 1)) {
			verifyIt++;
			if (verifyIt - 2 >= MAX_RESAMPLE) { verify = -1; break; }
			double value = pool_resample_next(c, verifyResample,
					RS_VOUCHER[ante], KL_RS_VOUCHER[ante], verifyIt);
			if (isnan(value)) { verify = -1; break; }
			verify = pool_psr_n(c, value, g->nvouch);
		}
		int verifyResult = verify > 0 ? verify - 1 : -1;
		if (result != verifyResult) {
			fprintf(stderr, "voucher raw-cache mismatch seed=%s ante=%d cached=%d reference=%d\n",
					c->seed, ante, result, verifyResult);
			abort();
		}
	}
#endif
	return result;
}

static void pool_undo_route_voucher(PoolCtx *c, int ante,
		const PoolVoucherUndo *undo) {
	c->voucher[ante] = undo->main;
	c->voucherVisits[ante] = undo->visits;
	for (int i = 0; i < undo->nresample; i++)
		c->voucherResample[ante][i] = undo->resample[i];
}

static void pool_search_voucher_route(PoolCtx *c, int ante, uint64_t purchased,
		uint32_t matched, int purchases, PoolMetadata *route,
		PoolVoucherBest *best) {
	const PoolPlan *p = c->p;
	const Config *g = c->g;
	/* Reducer-free Omen recovery has at most one purchase per remaining Ante.
	 * Every unowned voucher in Omen's prerequisite chain must be bought, so an
	 * overlong chain proves this branch impossible before any RNG work. Cycles
	 * stop the count and merely under-prune an already impossible catalog. */
	if (best->requireOmen && best->forbidAnteReducers
			&& best->omenIndex >= 0
			&& !(purchased & (UINT64_C(1) << best->omenIndex))) {
		int missing = 0, index = best->omenIndex;
		uint64_t seen = 0;
		while (index >= 0 && index < g->nvouch
				&& !(purchased & (UINT64_C(1) << index))
				&& !(seen & (UINT64_C(1) << index))) {
			seen |= UINT64_C(1) << index;
			missing++;
			index = g->vouchPrereq[index];
		}
		if (missing > best->maxAnte - ante + 1) return;
	}
	int nrules = pool_voucher_rule_count(p);
	uint32_t all = nrules == 32 ? UINT32_MAX : ((UINT32_C(1) << nrules) - 1u);
	if (best->found && purchases > best->purchases) return;
	for (int i = 0; i < nrules; i++) {
		const PoolVoucherRule *r = pool_voucher_rule_at(p, i);
		if (!(matched & (UINT32_C(1) << i)) && ante > r->maxAnte) return;
	}
	if (pool_voucher_memo_seen(c, best, ante, purchased, matched)) return;
	if (ante > best->maxAnte) {
		if (matched == all && (!best->found || purchases < best->purchases)) {
			if (best->requireOmen
					&& (best->omenIndex < 0
						|| !(purchased & (UINT64_C(1) << best->omenIndex)))) return;
			if (best->requireSouls && best->requireOmen) {
				int purchaseAnte = best->omenIndex >= 0
						? c->voucherPurchaseAnte[best->omenIndex] : 0;
				if (purchaseAnte < 1 || purchaseAnte > POOL_MAX_VOUCHER_ANTE
						|| !(best->omenActivationMask & (UINT16_C(1) << (purchaseAnte - 1))))
					return;
			} else if (best->requireSouls) {
				PoolMetadata trial = *route;
				c->voucherPurchased = purchased;
				pool_reset_soul_walk(c);
				if (!pool_check_all_souls(c, NULL, 0, &trial)) {
					pool_reset_soul_walk(c);
					return;
				}
				pool_reset_soul_walk(c);
			}
			best->found = 1;
			best->purchases = purchases;
			best->purchased = purchased;
			best->metadata = *route;
		}
		return;
	}

	PoolVoucherUndo undo;
	int index = pool_roll_route_voucher(c, ante, purchased, &undo,
			best->forbidAnteReducers);
	if (index < 0) {
		pool_undo_route_voucher(c, ante, &undo);
		return;
	}
	uint8_t visit = c->voucherVisits[ante];
	/* The initial A1 voucher has no Boss-entry shop. If both A1 blinds are
	 * skipped, it was rolled but is never physically offered. Every later (or
	 * repeated) generation is visible immediately in a Boss-entry shop. */
	int visible = ante != 1 || visit != 1 || !c->skipSm[1] || !c->skipBig[1];
	uint32_t offeredMask = 0;
	for (int i = 0; visible && i < nrules; i++) {
		const PoolVoucherRule *r = pool_voucher_rule_at(p, i);
		if (index == r->poolIndex && ante >= r->minAnte && ante <= r->maxAnte)
			offeredMask |= UINT32_C(1) << i;
	}
	uint32_t nextMatched = matched | offeredMask;
	int before = route->count;
	if (offeredMask && !pool_metadata_add(route, (PoolOccurrence) {
			.kind = POOL_META_VOUCHER, .keyIndex = (uint16_t)index,
			.ante = (uint8_t)ante, .source = SOUL_SOURCE_SHOP,
			.ordinal = visit,
	})) {
		pool_undo_route_voucher(c, ante, &undo);
		return;
	}
	int afterOffer = route->count;

	/* Once all targets have appeared, buying anything else cannot improve the
	 * minimum route. Continue skip-only so metadata still records every later
	 * target occurrence inside the requested windows. */
	pool_search_voucher_route(c, ante + 1, purchased, nextMatched,
			purchases, route, best);
	route->count = (uint8_t)afterOffer;

	int needMore = nextMatched != all
			|| (best->requireOmen && (best->omenIndex < 0
				|| !(purchased & (UINT64_C(1) << best->omenIndex))));
	if (visible && needMore && (!best->found || purchases + 1 <= best->purchases)
			&& !pool_voucher_purchase_excluded(p, index)) {
		/* The scoped mixed model composes only Omen into the Soul timeline.
		 * A minimum voucher-target route may use an Ante reducer while the
		 * canonical Legendary replay stays unchanged; Omen fallback and tag
		 * routes conservatively reject that unresolved timeline interaction. */
		if (best->forbidAnteReducers && g->vouchIsReducer[index])
			goto skip_buy;
		int purchasePhase = ante == 1 && visit == 1
				? (c->skipSm[1] ? SOUL_PHASE_BIG : SOUL_PHASE_SMALL)
				: SOUL_PHASE_BOSS;
		if (pool_metadata_add(route, (PoolOccurrence) {
				.kind = POOL_META_VOUCHER, .keyIndex = (uint16_t)index,
				.ante = (uint8_t)ante, .phase = (uint8_t)purchasePhase,
				.source = SOUL_SOURCE_SHOP,
				.ordinal = visit, .flags = POOL_META_PURCHASED,
		})) {
			uint8_t oldPurchaseAnte = c->voucherPurchaseAnte[index];
			uint8_t oldPurchaseVisit = c->voucherPurchaseVisit[index];
			c->voucherPurchaseAnte[index] = (uint8_t)ante;
			c->voucherPurchaseVisit[index] = visit;
			int repeatsAnte = g->vouchIsReducer[index];
			pool_search_voucher_route(c, repeatsAnte ? ante : ante + 1,
					purchased | (UINT64_C(1) << index), nextMatched,
					purchases + 1, route, best);
			c->voucherPurchaseAnte[index] = oldPurchaseAnte;
			c->voucherPurchaseVisit[index] = oldPurchaseVisit;
		}
		route->count = (uint8_t)afterOffer;
	}
skip_buy:
	route->count = (uint8_t)before;
	pool_undo_route_voucher(c, ante, &undo);
}

static uint16_t pool_omen_activation_mask(PoolCtx *c, int omenIndex,
		int routeMaxAnte, uint16_t candidates);

static bool pool_check_vouchers_mode(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata, int requireOmen, int requireSouls,
		int routeMaxAnte) {
	if (requireOmen && requireSouls) c->omenRoutePossible = -1;
	const PoolPlan *p = c->p;
	int nrules = pool_voucher_rule_count(p);
	if (!nrules && !requireOmen && !requireSouls) return true;
	if (nrules > 31 || c->g->nvouch > 64) return false;
	PoolMetadata route;
	if (metadata) route = *metadata;
	else route.count = 0;
	/* Initialized only when the generic DFS is actually entered. The common
	 * no-rule Omen frontier must not clear this 16 KiB scratch per fallback. */
	PoolVoucherMemo memo;
	PoolVoucherBest best;
	best.found = 0;
	best.purchases = INT_MAX;
	best.maxAnte = routeMaxAnte;
	best.requireOmen = requireOmen;
	best.requireSouls = requireSouls;
	/* This route-policy restriction must survive the cheap Omen probe,
	 * which disables repeated Soul validation but still models the same
	 * unresolved Soul/tag timeline. */
	best.forbidAnteReducers = requireSouls || p->nbaseTagRules || p->ntagRules;
	best.omenIndex = c->g->omenVoucherIdx;
	best.omenActivationMask = 0;
	best.purchased = 0;
	best.memo = &memo;
	if (best.maxAnte < p->maxVoucherAnte) best.maxAnte = p->maxVoucherAnte;
	if (best.maxAnte < 1 || best.maxAnte > POOL_MAX_VOUCHER_ANTE) return false;
	if (requireOmen && best.omenIndex < 0) return false;
	uint64_t initialPurchased = c->g->vouchInitiallyOwnedMask;
	/* With no voucher predicates and no requested Omen purchase, the minimum
	 * route buys nothing.  Validate the Soul branch directly instead of
	 * requiring an unrelated voucher offer to exist in every Ante.  This also
	 * covers challenge snapshots where every voucher is banned/already owned. */
	if (!nrules && !requireOmen) {
		c->voucherPurchased = initialPurchased;
		PoolMetadata trial = route;
		pool_reset_soul_walk(c);
		bool passed = pool_check_all_souls(c, NULL, 0, &trial);
		pool_reset_soul_walk(c);
		return passed;
	}
	int haveBest = 0;
	if (requireOmen && requireSouls) {
		if (!nrules && best.forbidAnteReducers) {
			PoolOmenRoutes routes;
			if (!pool_collect_omen_routes(c, best.maxAnte, &routes)) {
				/* Adding a targeted Charm skip can only hide the initial A1
				 * voucher; it cannot reveal an offer or buy edge. */
				c->omenRoutePossible = 0;
				return false;
			}
			c->omenRoutePossible = 1;
#ifdef BRAINSTORM_VERIFY_OMEN_FRONTIER
			for (int ante = 1; ante <= best.maxAnte; ante++) {
				uint16_t bit = UINT16_C(1) << (ante - 1);
				PoolVoucherBest reference = best;
				reference.omenActivationMask = bit;
				PoolMetadata referenceRoute = route;
				memset(&memo, 0, sizeof memo);
				pool_search_voucher_route(c, 1, initialPurchased, 0, 0,
						&referenceRoute, &reference);
				int frontierFound = routes.found[ante] != 0;
				if (frontierFound != (reference.found != 0)) {
					fprintf(stderr, "Omen frontier reach mismatch seed=%s ante=%d fast=%d reference=%d\n",
							c->seed, ante, frontierFound, reference.found != 0);
					abort();
				}
				if (frontierFound) {
					PoolVoucherBest materialized = { 0 };
					if (!pool_materialize_omen_route(c, &routes.best[ante],
							best.maxAnte, &route, &materialized)
							|| materialized.purchases != reference.purchases
							|| materialized.purchased != reference.purchased
							|| materialized.metadata.count != reference.metadata.count
							|| memcmp(materialized.metadata.occurrence,
								reference.metadata.occurrence,
								(size_t)reference.metadata.count
										* sizeof(PoolOccurrence))) {
						fprintf(stderr, "Omen frontier route mismatch seed=%s ante=%d\n",
								c->seed, ante);
						abort();
					}
				}
			}
#endif
			/* Testing one timing directly and then initializing the shared trace
			 * on its usual miss duplicates a complete physical Soul walk.  Evaluate
			 * the exact feasible timing set through one trace instead. */
			uint16_t validTimings = pool_omen_activation_mask(c,
					best.omenIndex, best.maxAnte, routes.feasibleMask);
			if (!validTimings) return false;
			const PoolOmenRouteState *chosen = pool_best_omen_route(&routes,
					validTimings);
			if (!chosen || !pool_materialize_omen_route(c, chosen,
					best.maxAnte, &route, &best)) return false;
			haveBest = 1;
		} else {
			/* Explicit voucher predicates retain the generic combined DFS. Test
			 * its minimum route first, then constrain a second search to timings
			 * that actually satisfy the Soul rules. */
			PoolVoucherBest probe = best;
			probe.requireSouls = 0;
			PoolMetadata probeRoute = route;
			memset(&memo, 0, sizeof memo);
			pool_search_voucher_route(c, 1, initialPurchased, 0, 0,
					&probeRoute, &probe);
			if (!probe.found) {
				c->omenRoutePossible = 0;
				return false;
			}
			c->omenRoutePossible = 1;
			int probeAnte = 0;
			for (int i = 0; i < probe.metadata.count; i++) {
				const PoolOccurrence *o = &probe.metadata.occurrence[i];
				if (o->kind == POOL_META_VOUCHER && o->keyIndex == best.omenIndex
						&& (o->flags & POOL_META_PURCHASED)) {
					probeAnte = o->ante;
					break;
				}
			}
			if (!probeAnte) return false;
			uint16_t probeBit = UINT16_C(1) << (probeAnte - 1);
			if (pool_omen_activation_mask(c, best.omenIndex,
					best.maxAnte, probeBit)) {
				best = probe;
				haveBest = 1;
			} else {
				uint16_t allCandidates = (UINT16_C(1) << best.maxAnte) - 1u;
				best.omenActivationMask = pool_omen_activation_mask(c,
						best.omenIndex, best.maxAnte, allCandidates);
				if (!best.omenActivationMask) return false;
			}
		}
	}
	if (!haveBest) {
		memset(&memo, 0, sizeof memo);
		pool_search_voucher_route(c, 1, initialPurchased, 0, 0, &route, &best);
	}
	if (!best.found) return false;
	if (metadata) *metadata = best.metadata;
	c->voucherPurchased = best.purchased;
	for (int i = 0; i < best.metadata.count; i++) {
		const PoolOccurrence *o = &best.metadata.occurrence[i];
		if (o->kind == POOL_META_VOUCHER && (o->flags & POOL_META_PURCHASED)
				&& o->keyIndex < MAX_VOUCH) {
			c->voucherPurchaseAnte[o->keyIndex] = o->ante;
			c->voucherPurchaseVisit[o->keyIndex] = o->ordinal;
		}
	}

	for (int i = p->nbaseVoucherRules; i < nrules; i++) {
		const PoolVoucherRule *r = pool_voucher_rule_at(p, i);
		int wrote = 0;
		for (int j = 0; j < best.metadata.count; j++) {
			const PoolOccurrence *o = &best.metadata.occurrence[j];
			if (o->kind != POOL_META_VOUCHER || o->keyIndex != r->poolIndex
					|| (o->flags & POOL_META_PURCHASED)
					|| o->ante < r->minAnte || o->ante > r->maxAnte) continue;
			pool_label_add(label, labelCap, "%s%sA%dV%d",
					wrote ? "," : (label && label[0] ? " " : ""),
					wrote ? "" : r->key, o->ante, o->ordinal);
			wrote = 1;
		}
	}
	if (best.purchases) {
		pool_label_add(label, labelCap, "%sBuyRoute=", label && label[0] ? " " : "");
		int wrote = 0;
		for (int i = 0; i < best.metadata.count; i++) {
			const PoolOccurrence *o = &best.metadata.occurrence[i];
			if (o->kind != POOL_META_VOUCHER || !(o->flags & POOL_META_PURCHASED)) continue;
			const char *key = o->keyIndex < c->g->nvouch ? c->g->vouchKey[o->keyIndex] : "?";
			pool_label_add(label, labelCap, "%s%s@A%dV%d",
					wrote ? "," : "", key, o->ante, o->ordinal);
			wrote = 1;
		}
	}
	return true;
}

static bool pool_check_vouchers(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata) {
	return pool_check_vouchers_mode(c, label, labelCap, metadata,
			0, 0, c->p->maxVoucherAnte);
}

static int pool_pack_max_slots(const PoolCtx *c, int ante) {
	int shops = (ante >= 2 ? 3 : 2) - (c->skipSm[ante] ? 1 : 0) - (c->skipBig[ante] ? 1 : 0);
	return shops * 2;
}

static void pool_resolve_forced_ante(PoolCtx *c) {
	c->forcedAnte = 1;
	for (int ante = 1; ante <= c->p->maxAnte; ante++) {
		if (pool_pack_max_slots(c, ante) > 0) {
			c->forcedAnte = ante;
			break;
		}
	}
}

static void pool_sim_packs(PoolCtx *c, int ante) {
	const Config *g = c->g;
	if (c->packsGen[ante] != c->soulWalkGen) {
		c->packsGen[ante] = c->soulWalkGen;
		c->packsN[ante] = 0;
		if (g->forceBuffoon && ante == c->forcedAnte)
			c->packClass[ante][c->packsN[ante]++] = 0;
	}
	int max = pool_pack_max_slots(c, ante);
	if (c->shopPackRawGen[ante] != c->gen) {
		c->shopPackRawGen[ante] = c->gen;
		c->shopPackRawN[ante] = 0;
	}
	int forced = g->forceBuffoon && ante == c->forcedAnte;
	int rawNeeded = max - forced;
	int missing = rawNeeded - c->shopPackRawN[ante];
	if (missing > 0) {
		double seedval[PRNG_BATCH_MAX];
		uint64_t word[PRNG_BATCH_MAX];
		for (int lane = 0; lane < missing; lane++)
			seedval[lane] = pool_stream_next_n(c,
					&c->shopPack[ante], KT_SHOPPACK[ante], KL_SHOPPACK[ante]);
		lj_random_seed_word_batch(seedval, missing, word);
		for (int lane = 0; lane < missing; lane++) {
#ifdef BRAINSTORM_VERIFY_SHOP_PACK_BATCH
			U64double randomBits = { .u64 = (word[lane]
					& UINT64_C(0x000fffffffffffff))
					| UINT64_C(0x3ff0000000000000) };
			double random = randomBits.d - 1.0;
			double reference = pool_psr(c, seedval[lane]);
			if (random != reference) {
				fprintf(stderr,
						"shop-pack RNG batch mismatch seed=%s ante=%d lane=%d\n",
						c->seed, ante, lane);
				abort();
			}
#endif
			uint16_t cls = boost_pick_soul_fraction(g, word[lane]);
			c->shopPackRaw[ante][c->shopPackRawN[ante]++] = cls;
		}
	}
	while (c->packsN[ante] < max) {
		int rawAt = c->packsN[ante] - forced;
		c->packClass[ante][c->packsN[ante]++] = c->shopPackRaw[ante][rawAt];
	}
}

/* The ordinary first-Soul predicate almost always reaches its full Ante
 * window, and every alternate route reuses the same route-independent raw
 * shop_pack sequence.  Fill that small prefix column-wise: advances for
 * different Ante streams are independent, so the CPU can overlap their
 * round13 chains instead of completing up to six dependent advances for one
 * Ante before starting the next. */
static void pool_prefill_shop_pack_raw(PoolCtx *c, int maxAnte) {
	const Config *g = c->g;
	if (maxAnte > POOL_MAX_ANTE) maxAnte = POOL_MAX_ANTE;
	int firstAnte = 1;
	if (c->shopPackPrefillGen == c->gen
			&& c->shopPackPrefillMaxAnte >= maxAnte) return;
	if (c->shopPackPrefillGen == c->gen)
		firstAnte = c->shopPackPrefillMaxAnte + 1;
	for (int ante = firstAnte; ante <= maxAnte; ante++) {
		if (c->shopPackRawGen[ante] != c->gen) {
			c->shopPackRawGen[ante] = c->gen;
			c->shopPackRawN[ante] = 0;
		}
	}
	for (int at = 0; at < 6; at++) {
		for (int block = firstAnte; block <= maxAnte; block += PRNG_BATCH_MAX) {
			double advanced[PRNG_BATCH_MAX], seedval[PRNG_BATCH_MAX];
			uint64_t word[PRNG_BATCH_MAX];
			PoolStream *streamOf[PRNG_BATCH_MAX];
			uint8_t anteOf[PRNG_BATCH_MAX];
			int count = 0;
			int blockEnd = block + PRNG_BATCH_MAX - 1;
			if (blockEnd > maxAnte) blockEnd = maxAnte;
			for (int ante = block; ante <= blockEnd; ante++) {
				int cap = ante == 1 ? 4 - !!g->forceBuffoon : 6;
				if (at >= cap || c->shopPackRawN[ante] > at) continue;
				PoolStream *stream = &c->shopPack[ante];
				if (stream->gen != c->gen) {
					stream->state = pool_pseudohash_ks_n(c, KT_SHOPPACK[ante],
							KL_SHOPPACK[ante]);
					stream->gen = c->gen;
				}
				streamOf[count] = stream;
				anteOf[count++] = (uint8_t)ante;
			}
			if (!count) continue;
			for (int lane = 0; lane < count; lane++)
				advanced[lane] = lua_mod1(2.134453429141
						+ streamOf[lane]->state * 1.72431234);
			for (int lane = 0; lane < count; lane++) {
				double state = round13(advanced[lane]);
				streamOf[lane]->state = state;
				seedval[lane] = (state + c->hashedSeed) / 2.0;
			}
			lj_random_seed_word_batch(seedval, count, word);
			for (int lane = 0; lane < count; lane++) {
				int ante = anteOf[lane];
#ifdef BRAINSTORM_VERIFY_SHOP_PACK_BATCH
				U64double randomBits = { .u64 = (word[lane]
						& UINT64_C(0x000fffffffffffff))
						| UINT64_C(0x3ff0000000000000) };
				double random = randomBits.d - 1.0;
				double reference = pool_psr(c, seedval[lane]);
				if (random != reference) {
					fprintf(stderr,
							"shop-pack wave RNG mismatch seed=%s ante=%d lane=%d\n",
							c->seed, ante, lane);
					abort();
				}
#endif
				uint16_t cls = boost_pick_soul_fraction(g, word[lane]);
				c->shopPackRaw[ante][c->shopPackRawN[ante]++] = cls;
			}
		}
	}
	c->shopPackPrefillGen = c->gen;
	c->shopPackPrefillMaxAnte = (uint8_t)maxAnte;
}

static void pool_append_shop_pair(const PoolCtx *c, int ante, int humanAnte,
		int phase, int *cursor,
		SoulPackEvent events[8], int *n) {
	for (int i = 0; i < 2 && *cursor < c->packsN[ante]; i++) {
		int slot = (*cursor)++;
		uint16_t cls = c->packClass[ante][slot];
		int soulKind = cls >> 8;
		if (!soulKind) continue;
		SoulPackEvent *e = &events[(*n)++];
		e->soulKind = (uint8_t)soulKind;
		e->cards = (uint8_t)(cls & 0xffu);
		e->humanAnte = (uint8_t)humanAnte;
		e->phase = (uint8_t)phase;
		e->source = SOUL_SOURCE_SHOP;
	}
}

static void pool_append_tag_reward(const PoolCtx *c, int ante, int kind, int blind,
		SoulPackEvent events[8], int *n) {
	if (!kind) return;
	SoulPackEvent *e = &events[(*n)++];
	e->soulKind = (uint8_t)kind;
	e->cards = (uint8_t)c->g->tagRewardCards[kind];
	e->humanAnte = (uint8_t)ante;
	e->phase = (uint8_t)(blind == 0 ? SOUL_PHASE_SMALL : SOUL_PHASE_BIG);
	e->source = (uint8_t)(kind == 1 ? SOUL_SOURCE_CHARM : SOUL_SOURCE_ETHEREAL);
}

static int pool_soul_pack_events(PoolCtx *c, int ante, SoulPackEvent events[8]) {
	pool_sim_packs(c, ante);
	int cursor = 0, n = 0;
	if (ante >= 2) pool_append_shop_pair(c, ante, ante - 1,
			SOUL_PHASE_BOSS, &cursor, events, &n);
	if (c->skipSm[ante]) pool_append_tag_reward(c, ante, c->rewardSm[ante], 0, events, &n);
	else pool_append_shop_pair(c, ante, ante, SOUL_PHASE_SMALL, &cursor, events, &n);
	if (c->skipBig[ante]) pool_append_tag_reward(c, ante, c->rewardBig[ante], 1, events, &n);
	else pool_append_shop_pair(c, ante, ante, SOUL_PHASE_BIG, &cursor, events, &n);
	return n;
}

static void pool_soul_event_label(char *out, size_t outsz,
		const SoulPackEvent *event) {
	const char *phase = event->phase == SOUL_PHASE_BOSS ? "Boss"
			: event->phase == SOUL_PHASE_SMALL ? "Sm" : "Big";
	const char *source = event->source == SOUL_SOURCE_SHOP ? "Shop"
			: event->source == SOUL_SOURCE_CHARM ? "Charm" : "Ethereal";
	snprintf(out, outsz, "A%d%s%s", event->humanAnte, source, phase);
}

static int pool_legend_rule_count(const PoolPlan *p) {
	return p->nbaseLegendaryRules + p->nlegendary;
}

static bool pool_voucher_owned_for_soul_event(const PoolCtx *c, int voucherIndex,
		const SoulPackEvent *event) {
	if (voucherIndex < 0 || voucherIndex >= c->g->nvouch) return false;
	if (c->g->vouchInitiallyOwned[voucherIndex]) return true;
	if (!(c->voucherPurchased & (UINT64_C(1) << voucherIndex))) return false;
	int ante = c->voucherPurchaseAnte[voucherIndex];
	int visit = c->voucherPurchaseVisit[voucherIndex];
	if (!ante || !visit) return false;
	/* Combined Soul routes deliberately do not buy Ante reducers, so every
	 * purchase is either the initial A1 offer (first reachable blind shop) or
	 * a later voucher's Boss-entry shop. Voucher identities were generated
	 * before the purchase, while pack contents are opened afterward. */
	int purchaseAnte, purchasePhase;
	if (ante == 1 && visit == 1) {
		purchaseAnte = 1;
		purchasePhase = c->skipSm[1] ? SOUL_PHASE_BIG : SOUL_PHASE_SMALL;
	} else if (visit == 1 && ante >= 2) {
		purchaseAnte = ante - 1;
		purchasePhase = SOUL_PHASE_BOSS;
	} else {
		return false;
	}
	return pool_route_position(event->humanAnte, event->phase)
			>= pool_route_position(purchaseAnte, purchasePhase);
}

static const PoolLegendaryRule *pool_legend_rule_at(const PoolPlan *p, int i) {
	return i < p->nbaseLegendaryRules ? &p->baseLegendaryRules[i]
			: &p->legendary[i - p->nbaseLegendaryRules];
}

static void pool_finalize_hot_plan(const Config *g, PoolPlan *p) {
	(void)g;
	p->voucherExclusionMask = 0;
	for (int i = 0; i < p->nbaseVoucherExclusions; i++)
		if (p->baseVoucherExclusions[i] >= 0 && p->baseVoucherExclusions[i] < 64)
			p->voucherExclusionMask |= UINT64_C(1) << p->baseVoucherExclusions[i];
	for (int i = 0; i < p->nvoucherExclusions; i++)
		if (p->voucherExclusions[i] >= 0 && p->voucherExclusions[i] < 64)
			p->voucherExclusionMask |= UINT64_C(1) << p->voucherExclusions[i];
	p->legendaryNeedsEdition = 0;
	for (int i = 0; i < pool_legend_rule_count(p); i++)
		if (pool_legend_rule_at(p, i)->requireNegative)
			p->legendaryNeedsEdition = 1;
	p->simpleFirstSoul = 0;
	if (pool_legend_rule_count(p) == 1) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, 0);
		if (r->soulDepth == 1 && !r->humanLocation && !r->requireNegative) {
			p->simpleFirstSoul = 1;
			p->simpleSoulMinAnte = r->minAnte;
			p->simpleSoulMaxAnte = r->maxAnte;
		}
	}
	/* The batched first gate can only reject a lane on evidence the scalar
	 * precheck would also reject on: the very first Joker4 draw. That draw
	 * fully decides the precheck only when every cumulative rule pins Soul #1
	 * (soul_depth 1); ANY/2 rules keep a second, stream-dependent chance. */
	p->vectorFirstGate = 0;
	p->vectorGateTarget = -2;
	if (pool_legend_rule_count(p) && p->firstKind == 1) {
		int allFirst = 1;
		for (int i = 0; i < pool_legend_rule_count(p); i++)
			if (pool_legend_rule_at(p, i)->soulDepth != 1) allFirst = 0;
		if (allFirst) {
			p->vectorFirstGate = 1;
			p->vectorGateTarget = pool_legend_rule_at(p, 0)->poolIndex;
			for (int i = 1; i < pool_legend_rule_count(p); i++)
				if (pool_legend_rule_at(p, i)->poolIndex != p->vectorGateTarget)
					p->vectorGateTarget = -2; /* contradictory rules: no lane passes */
		}
	}
	const char *gateEnv = getenv("BRAINSTORM_VECTOR_GATE");
	if (gateEnv && !strcmp(gateEnv, "0")) p->vectorFirstGate = 0;
}

/* All refilter stages constrain one cumulative Soul sequence. Resolve the
 * owned legendary for Soul #1 and (when needed) Soul #2 once, then apply every
 * source/current rule to those same picks. Either-depth rules resolve
 * deterministically: the exclusive Soul #2 pick can never repeat Soul #1's
 * legendary, so the target is at depth 1 iff it IS the first pick. */
static bool pool_precheck_legendaries_after_first(PoolCtx *c, int first) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	int nrules = pool_legend_rule_count(p);
	c->firstLegendaryIdx = first;
	c->secondLegendaryIdx = -1;
	int needSecond = 0;
	for (int i = 0; i < nrules; i++) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
		int d = r->soulDepth;
		if (d == SOUL_DEPTH_ANY) d = r->poolIndex == first ? 1 : 2;
		c->legendResolved[i] = d;
		if (d == 2) needSecond = 1;
	}
	int second = -1;
	if (needSecond) {
		uint8_t avail[MAX_JOKERS];
		memcpy(avail, g->jokerAvail[4], sizeof avail);
		avail[first] = 0;
		second = pool_pick_culled(c, &c->joker4, "Joker4", 6,
				RS_RBASE[RB_JOKER4], KL_RS_RBASE[RB_JOKER4],
				c->jokerResample, avail, g->njoker[4]);
		if (second < 0) return false;
		c->secondLegendaryIdx = second;
	}
	for (int i = 0; i < nrules; i++) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
		if ((c->legendResolved[i] == 1 ? first : second) != r->poolIndex) return false;
	}
	return true;
}

static bool pool_precheck_all_legendaries(PoolCtx *c) {
	const Config *g = c->g;
	if (!pool_legend_rule_count(c->p)) return true;
	int first = pool_pick_culled(c, &c->joker4, "Joker4", 6,
			RS_RBASE[RB_JOKER4], KL_RS_RBASE[RB_JOKER4],
			c->jokerResample, g->jokerAvail[4], g->njoker[4]);
	return first >= 0 && pool_precheck_legendaries_after_first(c, first);
}

/* Lane-parallel prefilter over one hashed ILV group: replay only the first
 * Joker4 draw (identical stream advance, reseed, and pick arithmetic) and
 * clear lanes it already proves the scalar precheck must reject. Survivors
 * re-enter pool_evaluate_pre unchanged, so this stage owns no route state and
 * a culled-catalog resample leaves the lane undecided rather than guessing. */
static void pool_first_gate_batch(const Config *g, const PoolPlan *p,
		const double hseed[ILV], const double hfirst[ILV],
		uint8_t survive[ILV]) {
	double sv[ILV], frac[ILV];
	/* round13's data-dependent rounding branch is a coin flip per lane, so
	 * the scalar helper mispredicts constantly here. Evaluate its fast path
	 * branch-free across the group (fl + 1.0 is exact, matching the helper's
	 * increment) and patch the rare near-half-way lanes with the exact form. */
	for (int i = 0; i < ILV; i++) {
		double x = lua_mod1(2.134453429141 + hfirst[i] * 1.72431234);
		double q = x * 1e13, fl = floor(q), f = q - fl;
		frac[i] = f;
		sv[i] = ((fl + (double)(f > 0.5015)) / 1e13 + hseed[i]) / 2.0;
	}
	for (int i = 0; i < ILV; i++) {
		if (frac[i] > 0.4985 && frac[i] <= 0.5015) {
			double x = lua_mod1(2.134453429141 + hfirst[i] * 1.72431234);
			sv[i] = (round13_exact(x) + hseed[i]) / 2.0;
		}
	}
	uint64_t word[4][PRNG_BATCH_MAX];
	lj_random_seed_components_batch(sv, ILV, word);
	int n = g->njoker[4];
	const uint8_t *avail = g->jokerAvail[4];
	for (int i = 0; i < ILV; i++) {
		/* Output bits 44..51 bound the 52-bit draw to [high, high+1)/256.
		 * math.random(n)'s bucket is decided whenever that whole interval
		 * floors to one bucket; the strict 1/256 gap to the next integer
		 * also dwarfs the half-ulp of the scalar path's d*n rounding, so a
		 * decided bucket always equals the scalar pick. Boundary-straddling
		 * lanes stay undecided and rerun the full scalar precheck. */
		unsigned high = (unsigned)(uint8_t)((prng_first_hi_0(word[0][i])
				^ prng_first_hi_1(word[1][i])
				^ prng_first_hi_2(word[2][i])
				^ prng_first_hi_3(word[3][i])) >> 44);
		unsigned bucket = (high * (unsigned)n) >> 8;
		if (bucket != ((high + 1u) * (unsigned)n) >> 8) survive[i] = 1;
		else if (!avail[bucket]) survive[i] = 1; /* resample: undecided */
		else survive[i] = (int)bucket == p->vectorGateTarget;
	}
}

/* Count/decision scans overwhelmingly use one ordinary first-Soul Ante range.
 * The general cumulative oracle below carries per-rule arrays, locations,
 * editions, and two-depth bookkeeping that this predicate cannot observe.
 * Keep only per-Ante stream cursors and return on the first physical Soul. */
static bool pool_check_simple_first_soul(PoolCtx *c) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	if (!pool_soul_tape_prepare(c)) return false;
	int prefillEnd = p->simpleSoulMaxAnte;
	if (prefillEnd > PRNG_BATCH_MAX) prefillEnd = PRNG_BATCH_MAX;
	pool_prefill_shop_pack_raw(c, prefillEnd);
	uint32_t omenAt = 0;
	int omenIndex = g->omenVoucherIdx;
	for (int ante = 1; ante <= p->simpleSoulMaxAnte; ante++) {
		uint16_t tarotAt = 0, spectralAt = 0;
		SoulPackEvent packs[8];
		int npacks = pool_soul_pack_events(c, ante, packs);
		for (int slot = 0; slot < npacks; slot++) {
			int soulKind = packs[slot].soulKind;
			if (!soulKind) continue;
			bool omenOwned = pool_voucher_owned_for_soul_event(c,
					omenIndex, &packs[slot]);
			bool soulInPack = false, blackHoleInPack = false;
			for (int card = 0; card < packs[slot].cards; card++) {
				int contentKind = soulKind;
				if (soulKind == 1 && omenOwned
						&& pool_soul_tape_omen(c, omenAt++)) contentKind = 2;
				bool soul = false;
				if (!soulInPack) {
					if (contentKind == 1)
						soul = pool_soul_tape_tarot(c, ante, tarotAt++);
					else
						soul = pool_soul_tape_spectral(c, ante, spectralAt++);
				}
				if (contentKind == 2 && !blackHoleInPack) {
					bool blackHole = pool_soul_tape_spectral(c,
							ante, spectralAt++);
					if (blackHole) {
						if (g->blackHoleAllowed) blackHoleInPack = true;
						soul = false;
					}
				}
				if (!soul) continue;
				soulInPack = true;
				return ante >= p->simpleSoulMinAnte;
			}
		}
	}
	return false;
}

/* Locate the first two Souls once on the final cumulative collected-tag
 * route, including the per-pack Soul/Black-Hole gates and the edition roll
 * consumed whenever a Soul is used. Every inherited and current rule is then
 * checked against the corresponding exact Soul event. */
static bool pool_check_all_souls(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	if (!label && !metadata && p->simpleFirstSoul)
		return pool_check_simple_first_soul(c);
	int nrules = pool_legend_rule_count(p), needDepth = 0, maxAnte = 0;
	if (!nrules) return true;
	if (!pool_soul_tape_prepare(c)) return false;
	for (int i = 0; i < nrules; i++) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
		if (c->legendResolved[i] > needDepth) needDepth = c->legendResolved[i];
		int rngMax = r->maxAnte + (r->humanLocation
				&& r->maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
		if (rngMax > maxAnte) maxAnte = rngMax;
	}
	int found = 0, eventAnte[3] = { 0 };
	SoulPackEvent eventPack[3] = { 0 };
	uint8_t eventNegative[3] = { 0 };
	uint16_t tarotAt[POOL_MAX_ANTE + 1] = { 0 };
	uint16_t spectralAt[POOL_MAX_ANTE + 1] = { 0 };
	uint8_t editionAt[POOL_MAX_ANTE + 1] = { 0 };
	uint32_t omenAt = 0;
	int needEdition = metadata != NULL || p->legendaryNeedsEdition;
	int omenIndex = g->omenVoucherIdx;
	for (int ante = 1; ante <= maxAnte && found < needDepth; ante++) {
		SoulPackEvent packs[8];
		int npacks = pool_soul_pack_events(c, ante, packs);
		for (int slot = 0; slot < npacks && found < needDepth; slot++) {
			int soulKind = packs[slot].soulKind, cards = packs[slot].cards;
			if (!soulKind) continue;
			bool omenOwned = pool_voucher_owned_for_soul_event(c, omenIndex, &packs[slot]);
			bool soulInPack = false, blackHoleInPack = false;
			for (int card = 0; card < cards && found < needDepth; card++) {
				int contentKind = soulKind;
				if (soulKind == 1 && omenOwned
						&& pool_soul_tape_omen(c, omenAt++))
					contentKind = 2;
				bool soul = false;
				if (!soulInPack) {
					if (contentKind == 1)
						soul = pool_soul_tape_tarot(c, ante, tarotAt[ante]++);
					else
						soul = pool_soul_tape_spectral(c, ante, spectralAt[ante]++);
				}
				if (contentKind == 2 && !blackHoleInPack) {
					bool blackHole = pool_soul_tape_spectral(c, ante,
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
				if (needEdition)
					eventNegative[found] = (uint8_t)pool_soul_tape_edition(c,
							ante, editionAt[ante]++);
				int legendaryIndex = found == 1 ? c->firstLegendaryIdx : c->secondLegendaryIdx;
				if (legendaryIndex < 0) return false;
				if (metadata && !pool_metadata_add(metadata, (PoolOccurrence) {
						.keyIndex = (uint16_t)legendaryIndex,
						.kind = POOL_META_LEGENDARY,
						.ante = (uint8_t)packs[slot].humanAnte,
						.phase = (uint8_t)packs[slot].phase,
						.source = (uint8_t)packs[slot].source,
						.ordinal = (uint8_t)found,
						.flags = (eventNegative[found] ? POOL_META_NEGATIVE : 0)
								| (c->charmRequired ? POOL_META_CHARM_REQUIRED : 0),
				})) return false;
			}
		}
	}
	if (found < needDepth) return false;
	for (int i = 0; i < nrules; i++) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
		int depth = c->legendResolved[i];
		if (r->humanLocation) {
			const SoulPackEvent *event = &eventPack[depth];
			if (!pool_location_in_range(event->humanAnte, event->phase,
					r->minAnte, r->minPhase, r->maxAnte, r->maxPhase)
					|| (r->source && event->source != r->source)) return false;
		} else if (eventAnte[depth] < r->minAnte || eventAnte[depth] > r->maxAnte) {
			return false;
		}
		if (r->requireNegative && !eventNegative[depth]) return false;
	}
	if (p->nlegendary && label) {
		char loc1[32], loc2[32];
		pool_soul_event_label(loc1, sizeof loc1, &eventPack[1]);
		int currentSecond = 0;
		for (int i = p->nbaseLegendaryRules; i < nrules; i++)
			if (c->legendResolved[i] == 2) currentSecond = 1;
		if (currentSecond) {
			const char *firstKey = c->firstLegendaryIdx >= 0
					&& c->firstLegendaryIdx < g->njoker[4]
					? g->jokerKey[4][c->firstLegendaryIdx] : "?";
			const char *secondKey = c->secondLegendaryIdx >= 0
					&& c->secondLegendaryIdx < g->njoker[4]
					? g->jokerKey[4][c->secondLegendaryIdx] : "?";
			pool_soul_event_label(loc2, sizeof loc2, &eventPack[2]);
			pool_label_add(label, labelCap,
					"%sSoul1(%s)=%s Soul2(%s)=%s",
					label && label[0] ? " " : "", firstKey, loc1, secondKey, loc2);
		} else {
			pool_label_add(label, labelCap, "%s%s=%s",
					label && label[0] ? " " : "", p->legendary[0].key, loc1);
		}
	}
	return true;
}

/* Omen timing variants share both the per-candidate random tape and one compact
 * physical pack timeline for the current skip/reward route. */
struct PoolOmenTrace {
	uint64_t routeGen;
	int routeMaxAnte, forcedAnte;
	uint8_t routeSkipSm[POOL_MAX_ANTE + 1];
	uint8_t routeSkipBig[POOL_MAX_ANTE + 1];
	uint8_t routeRewardSm[POOL_MAX_ANTE + 1];
	uint8_t routeRewardBig[POOL_MAX_ANTE + 1];
	uint8_t eventCount[POOL_MAX_ANTE + 1];
	SoulPackEvent event[POOL_MAX_ANTE + 1][POOL_SOUL_EVENTS_PER_ANTE];
};

static bool pool_soul_requirements(const PoolCtx *c, int *needDepth,
		int *maxAnte) {
	const PoolPlan *p = c->p;
	int nrules = pool_legend_rule_count(p);
	*needDepth = 0;
	*maxAnte = 0;
	for (int i = 0; i < nrules; i++) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
		if (c->legendResolved[i] > *needDepth) *needDepth = c->legendResolved[i];
		int rngMax = r->maxAnte + (r->humanLocation
				&& r->maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
		if (rngMax > *maxAnte) *maxAnte = rngMax;
	}
	return nrules > 0 && *needDepth >= 1 && *needDepth <= 2
			&& *maxAnte >= 1 && *maxAnte <= POOL_MAX_ANTE;
}

static bool pool_omen_trace_same_route(const PoolCtx *c,
		const PoolOmenTrace *t, int maxAnte) {
	size_t bytes = (size_t)(maxAnte + 1) * sizeof(uint8_t);
	return t->routeGen == c->gen && t->routeMaxAnte == maxAnte
			&& t->forcedAnte == c->forcedAnte
			&& !memcmp(t->routeSkipSm, c->skipSm, bytes)
			&& !memcmp(t->routeSkipBig, c->skipBig, bytes)
			&& !memcmp(t->routeRewardSm, c->rewardSm, bytes)
			&& !memcmp(t->routeRewardBig, c->rewardBig, bytes);
}

static bool pool_omen_trace_prepare(PoolCtx *c, PoolOmenTrace *t,
		int *needDepthOut, int *maxAnteOut) {
	int needDepth, maxAnte;
	if (!pool_soul_requirements(c, &needDepth, &maxAnte)) return false;
	if (!pool_soul_tape_prepare(c)) return false;
	*needDepthOut = needDepth;
	*maxAnteOut = maxAnte;
	if (pool_omen_trace_same_route(c, t, maxAnte)) return true;

	pool_reset_soul_walk(c);
	for (int ante = 1; ante <= maxAnte; ante++) {
		int n = pool_soul_pack_events(c, ante, t->event[ante]);
		if (n < 0 || n > POOL_SOUL_EVENTS_PER_ANTE) goto fail;
		t->eventCount[ante] = (uint8_t)n;
		for (int i = 0; i < n; i++) {
			const SoulPackEvent *e = &t->event[ante][i];
			if (e->cards > POOL_SOUL_CARDS_PER_EVENT) goto fail;
		}
	}
	t->routeMaxAnte = maxAnte;
	t->forcedAnte = c->forcedAnte;
	size_t bytes = (size_t)(maxAnte + 1) * sizeof(uint8_t);
	memcpy(t->routeSkipSm, c->skipSm, bytes);
	memcpy(t->routeSkipBig, c->skipBig, bytes);
	memcpy(t->routeRewardSm, c->rewardSm, bytes);
	memcpy(t->routeRewardBig, c->rewardBig, bytes);
	t->routeGen = c->gen;
	pool_reset_soul_walk(c);
	return true;

fail:
	pool_reset_soul_walk(c);
	return false;
}

static bool pool_omen_trace_owned_at(const PoolCtx *c, int purchaseAnte,
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
	return pool_route_position(event->humanAnte, event->phase)
			>= pool_route_position(humanAnte, phase);
}

static bool pool_omen_trace_matches(PoolCtx *c, const PoolOmenTrace *t,
		int purchaseAnte, int needDepth, int maxAnte) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	uint16_t tarotAt[POOL_MAX_ANTE + 1] = { 0 };
	uint16_t spectralAt[POOL_MAX_ANTE + 1] = { 0 };
	uint8_t editionAt[POOL_MAX_ANTE + 1] = { 0 };
	uint32_t omenAt = 0;
	int found = 0, eventAnte[3] = { 0 };
	SoulPackEvent eventPack[3] = { 0 };
	uint8_t eventNegative[3] = { 0 };
	for (int ante = 1; ante <= maxAnte && found < needDepth; ante++) {
		for (int slot = 0; slot < t->eventCount[ante] && found < needDepth; slot++) {
			const SoulPackEvent *event = &t->event[ante][slot];
			int soulKind = event->soulKind;
			if (!soulKind) continue;
			bool omenOwned = pool_omen_trace_owned_at(c, purchaseAnte, event);
			bool soulInPack = false, blackHoleInPack = false;
			for (int card = 0; card < event->cards && found < needDepth; card++) {
				int contentKind = soulKind;
				if (soulKind == 1 && omenOwned
						&& pool_soul_tape_omen(c, omenAt++)) contentKind = 2;
				bool soul = false;
				if (!soulInPack) {
					if (contentKind == 1)
						soul = pool_soul_tape_tarot(c, ante, tarotAt[ante]++);
					else
						soul = pool_soul_tape_spectral(c, ante, spectralAt[ante]++);
				}
				if (contentKind == 2 && !blackHoleInPack) {
					bool blackHole = pool_soul_tape_spectral(c, ante,
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
				eventPack[found] = *event;
				if (p->legendaryNeedsEdition) {
					eventNegative[found] = (uint8_t)pool_soul_tape_edition(c,
							ante, editionAt[ante]++);
				}
			}
		}
	}
	if (found < needDepth) return false;
	for (int i = 0; i < pool_legend_rule_count(p); i++) {
		const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
		int depth = c->legendResolved[i];
		if (r->humanLocation) {
			const SoulPackEvent *event = &eventPack[depth];
			if (!pool_location_in_range(event->humanAnte, event->phase,
					r->minAnte, r->minPhase, r->maxAnte, r->maxPhase)
					|| (r->source && event->source != r->source)) return false;
		} else if (eventAnte[depth] < r->minAnte || eventAnte[depth] > r->maxAnte) {
			return false;
		}
		if (r->requireNegative && !eventNegative[depth]) return false;
	}
	return true;
}

static uint16_t pool_omen_activation_mask_reference(PoolCtx *c, int omenIndex,
		int routeMaxAnte, uint16_t candidates) {
	uint64_t oldPurchased = c->voucherPurchased;
	uint8_t oldAnte = c->voucherPurchaseAnte[omenIndex];
	uint8_t oldVisit = c->voucherPurchaseVisit[omenIndex];
	uint16_t mask = 0;
	for (int ante = 1; ante <= routeMaxAnte; ante++) {
		if (!(candidates & (UINT16_C(1) << (ante - 1)))) continue;
		c->voucherPurchased = oldPurchased | (UINT64_C(1) << omenIndex);
		c->voucherPurchaseAnte[omenIndex] = (uint8_t)ante;
		c->voucherPurchaseVisit[omenIndex] = 1;
		pool_reset_soul_walk(c);
		if (pool_check_all_souls(c, NULL, 0, NULL))
			mask |= UINT16_C(1) << (ante - 1);
	}
	c->voucherPurchased = oldPurchased;
	c->voucherPurchaseAnte[omenIndex] = oldAnte;
	c->voucherPurchaseVisit[omenIndex] = oldVisit;
	pool_reset_soul_walk(c);
	return mask;
}

static uint16_t pool_omen_activation_mask(PoolCtx *c, int omenIndex,
		int routeMaxAnte, uint16_t candidates) {
	if (omenIndex < 0 || omenIndex >= c->g->nvouch) return 0;
	/* The adaptive short-range path often probes just its minimum voucher
	 * route. A direct walk is cheaper than initializing the shared trace for
	 * that single timing; the trace wins once several timings need testing. */
	if (__builtin_popcount((unsigned)candidates) <= 1)
		return pool_omen_activation_mask_reference(c, omenIndex,
				routeMaxAnte, candidates);
	if (!c->omenTrace) c->omenTrace = calloc(1, sizeof *c->omenTrace);
	int needDepth, maxAnte;
	if (!c->omenTrace || !pool_omen_trace_prepare(c, c->omenTrace,
			&needDepth, &maxAnte))
		return pool_omen_activation_mask_reference(c, omenIndex,
				routeMaxAnte, candidates);
	uint16_t mask = 0;
	for (int ante = 1; ante <= routeMaxAnte; ante++) {
		uint16_t bit = UINT16_C(1) << (ante - 1);
		if ((candidates & bit) && pool_omen_trace_matches(c, c->omenTrace,
				ante, needDepth, maxAnte)) mask |= bit;
	}
	pool_reset_soul_walk(c);
#ifdef BRAINSTORM_VERIFY_OMEN_TRACE
	uint16_t reference = pool_omen_activation_mask_reference(c, omenIndex,
			routeMaxAnte, candidates);
	if (mask != reference) {
		fprintf(stderr, "Omen trace mismatch seed=%s candidates=%04x trace=%04x reference=%04x\n",
				c->seed, candidates, mask, reference);
		abort();
	}
#endif
	return mask;
}

static bool pool_try_targeted_charm(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata, int baseMetadataCount, size_t baseLabelLength,
		int soulMaxAnte, int tagMaxAnte, int requireOmen) {
	const Config *g = c->g;
	const PoolPlan *p = c->p;
	int charmIndex = g->charmTagIdx;
	if (charmIndex < 0 || tagMaxAnte < 1) return false;
	if (tagMaxAnte > POOL_MAX_ANTE) tagMaxAnte = POOL_MAX_ANTE;
	int routeMaxAnte = soulMaxAnte;
	if (routeMaxAnte > POOL_MAX_VOUCHER_ANTE)
		routeMaxAnte = POOL_MAX_VOUCHER_ANTE;
	if (routeMaxAnte < p->maxVoucherAnte) routeMaxAnte = p->maxVoucherAnte;
	if (routeMaxAnte < 1) routeMaxAnte = 1;

	for (int ante = 1; ante <= tagMaxAnte; ante++) {
		for (int blind = 0; blind < 2; blind++) {
			int idx = pool_roll_tag_at(c, ante, blind);
			if (idx < 0) return false;
			uint8_t *skip = blind == 0 ? &c->skipSm[ante] : &c->skipBig[ante];
			uint8_t *reward = blind == 0 ? &c->rewardSm[ante] : &c->rewardBig[ante];
			if (idx != charmIndex || *skip) continue;
			uint8_t oldSkip = *skip, oldReward = *reward, oldCharm = c->charmRequired;
			int oldForced = c->forcedAnte;
			*skip = 1;
			*reward = 1;
			c->charmRequired = 1;
			pool_resolve_forced_ante(c);

			do {
				if (metadata) metadata->count = (uint8_t)baseMetadataCount;
				if (label) label[baseLabelLength] = 0;
				int phase = blind == 0 ? SOUL_PHASE_SMALL : SOUL_PHASE_BIG;
				if (!pool_metadata_add(metadata, (PoolOccurrence) {
						.kind = POOL_META_TAG, .keyIndex = (uint16_t)charmIndex,
						.ante = (uint8_t)ante, .phase = (uint8_t)phase,
						.flags = POOL_META_CHARM_REQUIRED,
				})) break;
				if (!requireOmen && !pool_voucher_rule_count(p)) {
					/* No voucher state can change this Charm route. The old helper
					 * validated Souls internally and the success path replayed them
					 * solely to recover metadata; perform the final walk once. */
					pool_reset_soul_walk(c);
					if (!pool_check_all_souls(c, label, labelCap, metadata)) break;
				} else {
					if (!pool_check_vouchers_mode(c, label, labelCap, metadata,
							requireOmen, 1, routeMaxAnte)) break;
					pool_reset_soul_walk(c);
					if (!pool_check_all_souls(c, label, labelCap, metadata)) break;
				}
				pool_label_add(label, labelCap, "%sCharmRequired=A%d%s",
						label && label[0] ? " " : "", ante,
						blind == 0 ? "Sm" : "Big");
				return true;
			} while (0);

			*skip = oldSkip;
			*reward = oldReward;
			c->charmRequired = oldCharm;
			c->forcedAnte = oldForced;
		}
	}
	if (metadata) metadata->count = (uint8_t)baseMetadataCount;
	if (label) label[baseLabelLength] = 0;
	return false;
}

static void pool_reset_route_state(PoolCtx *c, char *label, size_t labelCap,
		PoolMetadata *metadata) {
	memset(c->skipSm, 0, sizeof c->skipSm);
	memset(c->skipBig, 0, sizeof c->skipBig);
	memset(c->rewardSm, 0, sizeof c->rewardSm);
	memset(c->rewardBig, 0, sizeof c->rewardBig);
	memset(c->tagRollDone, 0, sizeof c->tagRollDone);
	memset(c->voucherVisits, 0, sizeof c->voucherVisits);
	memset(c->voucherPurchaseAnte, 0, sizeof c->voucherPurchaseAnte);
	memset(c->voucherPurchaseVisit, 0, sizeof c->voucherPurchaseVisit);
	c->voucherPurchased = 0;
	c->charmRequired = 0;
	if (label && labelCap) label[0] = 0;
	if (metadata) metadata->count = 0;
}

static bool pool_evaluate_pre(PoolCtx *c, const char seed[9], double hseed,
		double hfirst, char *label, size_t labelCap, PoolMetadata *metadata) {
	const PoolPlan *p = c->p;
	memcpy(c->seed, seed, 9);
	c->seedLen = (uint8_t)strlen(seed);
	c->gen++;
	c->hashSeedPrefixMask = 0;
	c->hashedSeed = hseed;
	if (p->firstKind == 1) {
		c->joker4.state = hfirst;
		c->joker4.gen = c->gen;
	} else if (p->firstKind == 2) {
		c->tag[p->firstAnte].state = hfirst;
		c->tag[p->firstAnte].gen = c->gen;
	} else {
		c->voucher[1].state = hfirst;
		c->voucher[1].gen = c->gen;
	}
	/* Specific legendary selection is independent of route streams and rejects
	 * roughly 80% of vanilla candidates, so route clearing stays deferred. */
	if (!pool_precheck_all_legendaries(c)) return false;
	pool_reset_route_state(c, label, labelCap, metadata);
	if (p->nbaseTagRules && !pool_apply_base_route(c, metadata)) return false;
	if (p->ntagRules && !pool_check_tags(c, label, labelCap, metadata)) return false;
	c->forcedAnte = 1;
	for (int ante = 1; ante <= p->maxAnte; ante++) {
		if (pool_pack_max_slots(c, ante) > 0) { c->forcedAnte = ante; break; }
	}
	int beforeVoucherMetadata = metadata ? metadata->count : 0;
	size_t beforeVoucherLabel = label ? strlen(label) : 0;
	if (pool_voucher_rule_count(p)
			&& !pool_check_vouchers(c, label, labelCap, metadata)) return false;
	pool_reset_soul_walk(c);
	if (!pool_check_all_souls(c, label, labelCap, metadata)) {
		/* Targeted alternate routes, cheapest and least-purchased first:
		 * canonical already missed, so try an actual Charm Tag with NO Omen
		 * purchase (zero extra buys, no DFS in the common case), then a
		 * voucher route that obtains Omen Globe early enough to convert
		 * Arcana cards, then a Charm branch combined with Omen. Each attempt
		 * validates the Soul result on its own final route, so a later
		 * equal-cost route cannot mask an earlier successful one. */
		if (!pool_legend_rule_count(p)) return false;
		if (metadata) metadata->count = (uint8_t)beforeVoucherMetadata;
		if (label) label[beforeVoucherLabel] = 0;
		int soulMaxAnte = 1;
		for (int i = 0; i < pool_legend_rule_count(p); i++) {
			const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
			int rngMax = r->maxAnte + (r->humanLocation
					&& r->maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
			if (rngMax > soulMaxAnte) soulMaxAnte = rngMax;
		}
		if (soulMaxAnte > POOL_MAX_VOUCHER_ANTE)
			soulMaxAnte = POOL_MAX_VOUCHER_ANTE;
		int tagMaxAnte = 1;
		for (int i = 0; i < pool_legend_rule_count(p); i++) {
			const PoolLegendaryRule *r = pool_legend_rule_at(p, i);
			if (r->maxAnte > tagMaxAnte) tagMaxAnte = r->maxAnte;
		}
		if (pool_try_targeted_charm(c, label, labelCap, metadata,
				beforeVoucherMetadata, beforeVoucherLabel,
				soulMaxAnte, tagMaxAnte, 0)) return true;
		/* Fast mode is still exact for every emitted seed: it accepts canonical
		 * and real Charm-tag routes, then deliberately omits only the automatic
		 * voucher-purchase search for an Omen-only recovery. Starting Omen and
		 * Omen obtained by an explicit voucher route remain part of the normal
		 * Soul walk above. */
		if (p->legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM)
			return false;
		if (metadata) metadata->count = (uint8_t)beforeVoucherMetadata;
		if (label) label[beforeVoucherLabel] = 0;
		bool recovered = pool_check_vouchers_mode(c, label, labelCap, metadata,
				1, 1, soulMaxAnte);
		int omenRoutePossible = c->omenRoutePossible;
		if (recovered) {
			pool_reset_soul_walk(c);
			recovered = pool_check_all_souls(c, label, labelCap, metadata);
		}
		if (!recovered) {
			if (omenRoutePossible == 0) return false;
			if (metadata) metadata->count = (uint8_t)beforeVoucherMetadata;
			if (label) label[beforeVoucherLabel] = 0;
			if (!pool_try_targeted_charm(c, label, labelCap, metadata,
					beforeVoucherMetadata, beforeVoucherLabel,
					soulMaxAnte, tagMaxAnte, 1)) return false;
		}
	}
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
	/* Shared-suffix fast path over consecutive natural ranks. */
	for (int r = 0; r < 4; r++) {
		for (int i = 0; i < ILV; i++) {
			make_seed((k + (uint64_t)i) % SEEDSPACE, seeds[i]);
		}
		if (batch_seeds_share_suffix(seeds, 8)) {
			batch_hash_seed_shared(seeds, 8, hs);
			batch_hash_key_shared(p->firstKey, seeds, 8, hf);
			for (int i = 0; i < ILV; i++) {
				if (hs[i] != pseudohash_ks("", seeds[i])) return false;
				if (hf[i] != pseudohash_ks(p->firstKey, seeds[i])) return false;
			}
		}
		k = k * 31ULL + 977ULL;
	}
	/* Variable-length spaces run at every seed length; prove each alphabet's
	 * batched hash against the serial reference too. */
	const int variableSpaces[] = { SPACE_SETTABLE, SPACE_TOTAL };
	for (size_t si = 0; si < sizeof variableSpaces / sizeof variableSpaces[0]; si++) {
		int space = variableSpaces[si];
		uint64_t alphabet = space == SPACE_SETTABLE ? CHARSET_SETTABLE_N : CHARSET_TOTAL_N;
		for (int slen = 1; slen <= 8; slen++) {
			uint64_t base = 0;
			for (int l = 1; l < slen; l++) base = (base + 1) * alphabet;
			for (int i = 0; i < ILV; i++) {
				if (make_seed_in(space, base + (uint64_t)i * 31 % alphabet, seeds[i]) != slen) return false;
			}
			batch_hash_seed_n(seeds, slen, hs);
			batch_hash_key_n(p->firstKey, seeds, slen, hf);
			for (int i = 0; i < ILV; i++) {
				if (hs[i] != pseudohash_ks("", seeds[i])) return false;
				if (hf[i] != pseudohash_ks(p->firstKey, seeds[i])) return false;
			}
			/* Shared-suffix fast path over consecutive ranks of this length. */
			for (int i = 0; i < ILV; i++) {
				if (make_seed_in(space, base + (uint64_t)i, seeds[i]) != slen) return false;
			}
			if (batch_seeds_share_suffix(seeds, slen)) {
				batch_hash_seed_shared(seeds, slen, hs);
				batch_hash_key_shared(p->firstKey, seeds, slen, hf);
				for (int i = 0; i < ILV; i++) {
					if (hs[i] != pseudohash_ks("", seeds[i])) return false;
					if (hf[i] != pseudohash_ks(p->firstKey, seeds[i])) return false;
				}
			}
		}
	}
	/* Per-candidate keyed hashes may share the exact post-seed state when key
	 * lengths match.  Exercise both cache fills and same-length cache hits over
	 * every seed encoding, including the variable-length boundaries. */
	static const char *cacheKeys[] = {
		"Tag1", "Tag2", "edisou1", "Voucher1", "shop_pack1", "omen_globe",
		"soul_Tarot1", "soul_Spectral1", "Voucher1_resample2",
	};
	PoolCtx cache;
	memset(&cache, 0, sizeof cache);
	static const int cacheSpaces[] = { SPACE_NATURAL, SPACE_SETTABLE, SPACE_TOTAL };
	for (size_t spaceIndex = 0;
			spaceIndex < sizeof cacheSpaces / sizeof cacheSpaces[0]; spaceIndex++) {
		int space = cacheSpaces[spaceIndex];
		uint64_t size = space_size(space);
		for (int i = 0; i < 256; i++) {
			uint64_t rank = (UINT64_C(104729) * (uint64_t)i
					+ UINT64_C(987654321)) % size;
			make_seed_in(space, rank, cache.seed);
			cache.seedLen = (uint8_t)strlen(cache.seed);
			cache.hashSeedPrefixMask = 0;
			for (size_t kidx = 0; kidx < sizeof cacheKeys / sizeof cacheKeys[0]; kidx++)
				if (pool_pseudohash_ks(&cache, cacheKeys[kidx])
						!= pseudohash_ks(cacheKeys[kidx], cache.seed)) return false;
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
	uint64_t *membershipDigest;
	uint64_t *metadataDigest;
} PoolScanShared;

static bool pool_read_input_batch(const PoolScanShared *s, uint64_t first,
		uint64_t count, uint64_t ranks[ILV], BspoolScratch *scratch) {
	return bspool_reader_read(&s->p->inputReader, first, count, ranks, scratch);
}

static bool pool_reader_prefix_bytes(const BspoolReader *r, int encoding,
		uint64_t records, uint64_t *bytes) {
	if (records > r->records) return false;
	if (encoding == BSPOOL_ENCODING_U64) {
		if (records > UINT64_MAX / 8u) return false;
		*bytes = records * 8u;
		return true;
	}
	if (!records) { *bytes = 0; return true; }
	uint32_t headerBytes = encoding == BSPOOL_ENCODING_DELTA_EVENTS
			? BSPOOL3_BLOCK_HEADER_SIZE : BSPOOL_BLOCK_HEADER_SIZE;
	for (uint64_t i = 0; i < r->nblocks; i++) {
		const BspoolBlockIndex *e = &r->blocks[i];
		uint64_t endRecord = e->firstRecord + e->count;
		if (endRecord == records) {
			*bytes = e->offset + headerBytes + e->payloadBytes - r->dataOff;
			return true;
		}
		if (endRecord > records) return false;
	}
	return false;
}

static bool pool_reader_metadata_digest(const BspoolReader *r, uint64_t *digest) {
	if (r->encoding != BSPOOL_ENCODING_DELTA_EVENTS) { *digest = 0; return true; }
	unsigned char buf[64 * 1024];
	uint64_t h = POOL_HASH_INIT;
	for (uint64_t i = 0; i < r->nblocks; i++) {
		const BspoolBlockIndex *e = &r->blocks[i];
		uint64_t offset = e->offset + BSPOOL3_BLOCK_HEADER_SIZE + e->rankBytes;
		uint64_t left = e->metadataBytes;
		while (left) {
			size_t n = left < sizeof buf ? (size_t)left : sizeof buf;
			if (offset > (uint64_t)INT64_MAX
					|| bs_pread(r->fd, buf, n, (int64_t)offset) != (int64_t)n) return false;
			h = pool_hash_update(h, buf, n);
			offset += n; left -= n;
		}
	}
	*digest = h;
	return true;
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

typedef struct {
	uint64_t rank;
	PoolMetadata metadata;
} PoolEventHit;

typedef struct {
	uint8_t len;
	unsigned char bytes[MAX_KEY + 8];
	uint16_t *records;
	uint32_t count, cap;
} PoolMetaDescriptor;

typedef struct {
	unsigned char *data;
	size_t size, cap;
} PoolByteBuffer;

static int pool_event_hit_compare(const void *a, const void *b) {
	const PoolEventHit *x = a, *y = b;
	return x->rank < y->rank ? -1 : x->rank > y->rank;
}

static int pool_meta_descriptor_compare(const void *a, const void *b) {
	const PoolMetaDescriptor *x = a, *y = b;
	size_t common = x->len < y->len ? x->len : y->len;
	int cmp = memcmp(x->bytes, y->bytes, common);
	return cmp ? cmp : x->len < y->len ? -1 : x->len > y->len;
}

static bool pool_byte_reserve(PoolByteBuffer *b, size_t add) {
	if (add > BSPOOL3_BLOCK_MAX_METADATA - b->size) return false;
	size_t need = b->size + add;
	if (need <= b->cap) return true;
	size_t cap = b->cap ? b->cap : 256;
	while (cap < need) {
		size_t next = cap * 2;
		if (next < cap || next > BSPOOL3_BLOCK_MAX_METADATA) {
			cap = BSPOOL3_BLOCK_MAX_METADATA; break;
		}
		cap = next;
	}
	unsigned char *p = realloc(b->data, cap);
	if (!p) return false;
	b->data = p; b->cap = cap;
	return true;
}

static bool pool_byte_append(PoolByteBuffer *b, const void *p, size_t n) {
	if (!pool_byte_reserve(b, n)) return false;
	memcpy(b->data + b->size, p, n); b->size += n;
	return true;
}

static bool pool_byte_varint(PoolByteBuffer *b, uint64_t value) {
	unsigned char raw[10];
	size_t n = 0;
	do {
		raw[n] = (unsigned char)(value & 0x7f);
		value >>= 7;
		if (value) raw[n] |= 0x80;
		n++;
	} while (value);
	return pool_byte_append(b, raw, n);
}

static bool pool_occurrence_descriptor(const Config *g, const PoolOccurrence *o,
		unsigned char out[MAX_KEY + 8], uint8_t *len) {
	const char *key = NULL;
	if (o->kind == POOL_META_TAG && o->keyIndex < (uint16_t)g->ntags)
		key = g->tagKey[o->keyIndex];
	else if (o->kind == POOL_META_LEGENDARY && o->keyIndex < (uint16_t)g->njoker[4])
		key = g->jokerKey[4][o->keyIndex];
	else if (o->kind == POOL_META_VOUCHER && o->keyIndex < (uint16_t)g->nvouch)
		key = g->vouchKey[o->keyIndex];
	if (!key) return false;
	size_t keyLen = strlen(key);
	if (!keyLen || keyLen >= MAX_KEY) return false;
	out[0] = o->kind;
	out[1] = (unsigned char)keyLen;
	memcpy(out + 2, key, keyLen);
	out[2 + keyLen] = o->ante;
	out[3 + keyLen] = o->phase;
	out[4 + keyLen] = o->source;
	out[5 + keyLen] = o->ordinal;
	out[6 + keyLen] = o->flags;
	*len = (uint8_t)(7 + keyLen);
	return true;
}

static bool pool_meta_record(PoolMetaDescriptor *d, uint16_t record) {
	if (d->count == d->cap) {
		uint32_t cap = d->cap ? d->cap * 2u : 16u;
		if (cap < d->cap || cap > POOL_EVENT_BLOCK_RECORDS) cap = POOL_EVENT_BLOCK_RECORDS;
		if (cap <= d->cap) return false;
		uint16_t *p = realloc(d->records, (size_t)cap * sizeof *p);
		if (!p) return false;
		d->records = p; d->cap = cap;
	}
	d->records[d->count++] = record;
	return true;
}

static void pool_meta_descriptors_free(PoolMetaDescriptor *d, size_t n) {
	for (size_t i = 0; i < n; i++) free(d[i].records);
	free(d);
}

static bool pool_flush_event_hits(PoolScanShared *s, PoolEventHit *hits,
		size_t *used) {
	if (!*used) return true;
	qsort(hits, *used, sizeof *hits, pool_event_hit_compare);
	unsigned char rankPayload[(POOL_EVENT_BLOCK_RECORDS - 1) * 6];
	size_t rankBytes = 0;
	uint64_t prior = hits[0].rank;
	for (size_t i = 1; i < *used; i++) {
		uint64_t delta = hits[i].rank - prior;
		if (!delta) goto fail;
		do {
			unsigned char byte = (unsigned char)(delta & 0x7f);
			delta >>= 7;
			if (delta) byte |= 0x80;
			rankPayload[rankBytes++] = byte;
		} while (delta);
		prior = hits[i].rank;
	}
	PoolMetaDescriptor *descriptors = NULL;
	size_t ndescriptors = 0, descriptorCap = 0;
	uint32_t associations = 0;
	for (uint16_t record = 0; record < (uint16_t)*used; record++) {
		const PoolMetadata *m = &hits[record].metadata;
		for (uint8_t j = 0; j < m->count; j++) {
			unsigned char bytes[MAX_KEY + 8]; uint8_t len = 0;
			if (!pool_occurrence_descriptor(s->g, &m->occurrence[j], bytes, &len)) goto fail_desc;
			size_t d = 0;
			for (; d < ndescriptors; d++)
				if (descriptors[d].len == len && !memcmp(descriptors[d].bytes, bytes, len)) break;
			if (d == ndescriptors) {
				if (ndescriptors == descriptorCap) {
					size_t cap = descriptorCap ? descriptorCap * 2 : 16;
					PoolMetaDescriptor *p = realloc(descriptors, cap * sizeof *p);
					if (!p) goto fail_desc;
					descriptors = p; descriptorCap = cap;
				}
				memset(&descriptors[d], 0, sizeof descriptors[d]);
				descriptors[d].len = len;
				memcpy(descriptors[d].bytes, bytes, len);
				ndescriptors++;
			}
			if (!pool_meta_record(&descriptors[d], record) || associations == UINT32_MAX) goto fail_desc;
			associations++;
		}
	}
	qsort(descriptors, ndescriptors, sizeof *descriptors, pool_meta_descriptor_compare);
	PoolByteBuffer metadata = { 0 };
	if (!pool_byte_varint(&metadata, ndescriptors)) goto fail_meta;
	for (size_t d = 0; d < ndescriptors; d++) {
		PoolMetaDescriptor *entry = &descriptors[d];
		if (!pool_byte_varint(&metadata, entry->len)
				|| !pool_byte_append(&metadata, entry->bytes, entry->len)
				|| !pool_byte_varint(&metadata, entry->count)
				|| !entry->count
				|| !pool_byte_varint(&metadata, entry->records[0])) goto fail_meta;
		for (uint32_t i = 1; i < entry->count; i++) {
			uint16_t delta = (uint16_t)(entry->records[i] - entry->records[i - 1]);
			if (!delta || !pool_byte_varint(&metadata, delta)) goto fail_meta;
		}
	}
	unsigned char header[BSPOOL3_BLOCK_HEADER_SIZE] = { 0 };
	memcpy(header, "BSP3", 4); header[4] = BSPOOL3_BLOCK_HEADER_SIZE;
	bspool_put_u32le(header + 8, (uint32_t)*used);
	bspool_put_u32le(header + 12, (uint32_t)rankBytes);
	bspool_put_u32le(header + 16, (uint32_t)metadata.size);
	bspool_put_u32le(header + 20, associations);
	bspool_put_u64le(header + 24, hits[0].rank);
	bspool_put_u64le(header + 32, hits[*used - 1].rank);
	uint64_t crc = bspool_crc64_update(0, header + 4, 36);
	crc = bspool_crc64_update(crc, rankPayload, rankBytes);
	crc = bspool_crc64_update(crc, metadata.data, metadata.size);
	bspool_put_u64le(header + 40, crc);
	bs_mutex_lock(&s->outMutex);
	size_t wh = fwrite(header, 1, sizeof header, s->out);
	size_t wr = rankBytes ? fwrite(rankPayload, 1, rankBytes, s->out) : 0;
	size_t wm = fwrite(metadata.data, 1, metadata.size, s->out);
	if (wh == sizeof header && wr == rankBytes && wm == metadata.size) {
		*s->membershipDigest = pool_hash_update(*s->membershipDigest, header, sizeof header);
		*s->membershipDigest = pool_hash_update(*s->membershipDigest, rankPayload, rankBytes);
		*s->membershipDigest = pool_hash_update(*s->membershipDigest, metadata.data, metadata.size);
		*s->metadataDigest = pool_hash_update(*s->metadataDigest, metadata.data, metadata.size);
	}
	bs_mutex_unlock(&s->outMutex);
	free(metadata.data);
	pool_meta_descriptors_free(descriptors, ndescriptors);
	if (wh != sizeof header || wr != rankBytes || wm != metadata.size) goto fail;
	*used = 0;
	return true;
fail_meta:
	free(metadata.data);
fail_desc:
	pool_meta_descriptors_free(descriptors, ndescriptors);
fail:
	atomic_store(&s->ioError, true);
	return false;
}

static bool pool_buffer_event_hit(PoolScanShared *s, PoolEventHit *hits,
		size_t *used, uint64_t rank, const PoolMetadata *metadata) {
	if (*used == POOL_EVENT_BLOCK_RECORDS && !pool_flush_event_hits(s, hits, used)) return false;
	hits[*used].rank = rank;
	hits[*used].metadata = *metadata;
	(*used)++;
	return true;
}

static bool pool_flush_hits(PoolScanShared *s, unsigned char *buf,
		unsigned char *encoded, size_t *used) {
	if (!*used || s->p->format == POOL_COUNT) { *used = 0; return true; }
	const unsigned char *writeBuf = buf;
	size_t writeBytes = *used;
	unsigned char header[BSPOOL3_BLOCK_HEADER_SIZE];
	size_t headerBytes = BSPOOL_BLOCK_HEADER_SIZE;
	if (s->p->format == POOL_BINARY) {
		uint32_t count = (uint32_t)(*used / 8u);
		if (s->p->outputSchema == BSPOOL_SCHEMA_EVENTS) return false;
		size_t out = pool_encode_rank_block(buf, count, encoded, header);
		writeBuf = encoded; writeBytes = out;
	}
	bs_mutex_lock(&s->outMutex);
	size_t wroteHeader = s->p->format == POOL_BINARY
			? fwrite(header, 1, headerBytes, s->out) : headerBytes;
	size_t wrote = fwrite(writeBuf, 1, writeBytes, s->out);
	if (wroteHeader == headerBytes && wrote == writeBytes && s->membershipDigest) {
		uint64_t h = *s->membershipDigest;
		if (s->p->format == POOL_BINARY) h = pool_hash_update(h, header, headerBytes);
		h = pool_hash_update(h, writeBuf, writeBytes);
		*s->membershipDigest = h;
	}
	bs_mutex_unlock(&s->outMutex);
	if (wroteHeader != headerBytes || wrote != writeBytes) {
		atomic_store(&s->ioError, true);
		return false;
	}
	*used = 0;
	return true;
}

static bool pool_buffer_hit(PoolScanShared *s, unsigned char *buf,
		unsigned char *encoded, size_t *used, uint64_t rank, const char seed[9]) {
	size_t slen = strlen(seed); /* 8 natural, 1..8 in expanded spaces */
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
	if (c) c->soulTape = calloc(1, sizeof *c->soulTape);
	unsigned char *outbuf = malloc(POOL_OUTPUT_BUFFER);
	unsigned char *encoded = malloc(POOL_OUTPUT_BUFFER);
	bool eventMode = s->p->format == POOL_BINARY
			&& s->p->outputSchema == BSPOOL_SCHEMA_EVENTS;
	bool decisionReplay = eventMode && s->p->simpleFirstSoul;
	PoolEventHit *eventHits = eventMode
			? malloc(POOL_EVENT_BLOCK_RECORDS * sizeof *eventHits) : NULL;
	BspoolScratch inputScratch = { .cachedBlock = UINT64_MAX };
	if (!c || !c->soulTape || !outbuf || !encoded || (eventMode && !eventHits)) {
		if (c) free(c->soulTape);
		free(c); free(outbuf); free(encoded); free(eventHits);
		atomic_store(&s->ioError, true);
		return NULL;
	}
	c->g = s->g;
	c->p = s->p;
	size_t outUsed = 0;
	size_t eventUsed = 0;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV], sufS[ILV], sufK[ILV];
	uint64_t ranks[ILV];
	PoolMetadata metadata;
	int firstKeyLen = (int)strlen(s->p->firstKey);
	while (!atomic_load(&s->ioError)) {
		uint64_t begin = atomic_fetch_add(&s->next, s->p->chunk);
		if (begin >= s->end) break;
		uint64_t end = begin + s->p->chunk;
		if (end > s->end) end = s->end;
		uint64_t rank = begin;
		uint64_t chunkMatched = 0;
		int space = s->p->space;
		/* Fresh scans walk contiguous ranks: the odometer advances the seed
		 * string in place and one suffix chain state survives across a whole
		 * base-N run, recomputed only on a carry past the first digit.
		 * Refilters draw arbitrary input-pool ranks and keep the per-group
		 * uniform/shared decode below. */
		SeedOdometer od;
		double sufSeed = 0.0, sufKey = 0.0;
		if (!s->p->refilter && rank + ILV <= end) {
			odometer_init(&od, space, rank);
			sufSeed = hash_shared_suffix(od.seed, od.len, 0);
			sufKey = hash_shared_suffix(od.seed, od.len, firstKeyLen);
		}
		for (; rank + ILV <= end; rank += ILV) {
			if (s->p->refilter) {
				if (!pool_read_input_batch(s, rank, ILV, ranks, &inputScratch)) {
					atomic_store(&s->ioError, true); goto done;
				}
				int l0 = 0, uniform = 1;
				for (int i = 0; i < ILV; i++) {
					int slen = make_seed_in(space, ranks[i], seeds[i]);
					if (i == 0) l0 = slen;
					else if (slen != l0) uniform = 0;
				}
				if (uniform && batch_seeds_share_suffix(seeds, l0)) {
					batch_hash_seed_shared(seeds, l0, hseed);
					batch_hash_key_shared(s->p->firstKey, seeds, l0, hfirst);
				} else if (uniform) {
					batch_hash_seed_n(seeds, l0, hseed);
					batch_hash_key_n(s->p->firstKey, seeds, l0, hfirst);
				} else {
					for (int i = 0; i < ILV; i++) {
						hseed[i] = pseudohash_ks("", seeds[i]);
						hfirst[i] = pseudohash_ks(s->p->firstKey, seeds[i]);
					}
				}
			} else {
				for (int i = 0; i < ILV; i++) {
					memcpy(seeds[i], od.seed, 9);
					ranks[i] = rank + (uint64_t)i;
					sufS[i] = sufSeed;
					sufK[i] = sufKey;
					if (odometer_next(&od)) {
						sufSeed = hash_shared_suffix(od.seed, od.len, 0);
						sufKey = hash_shared_suffix(od.seed, od.len, firstKeyLen);
					}
				}
				batch_hash_seed_pre(sufS, seeds, hseed);
				batch_hash_key_pre(s->p->firstKey, firstKeyLen, sufK, seeds, hfirst);
			}
			uint8_t gateSurvive[ILV];
			if (s->p->vectorFirstGate)
				pool_first_gate_batch(s->g, s->p, hseed, hfirst, gateSurvive);
			for (int i = 0; i < ILV; i++) {
				if (s->p->vectorFirstGate && !gateSurvive[i]) {
#ifdef BRAINSTORM_VERIFY_VECTOR_GATE
					if (pool_evaluate_pre(c, seeds[i], hseed[i], hfirst[i],
							NULL, 0, NULL)) {
						fprintf(stderr, "vector gate dropped passing seed %s\n",
								seeds[i]);
						abort();
					}
#endif
					continue;
				}
				bool passed = pool_evaluate_pre(c, seeds[i], hseed[i], hfirst[i],
						NULL, 0, eventMode && !decisionReplay ? &metadata : NULL);
				if (passed && decisionReplay) {
					passed = pool_evaluate_pre(c, seeds[i], hseed[i], hfirst[i],
							NULL, 0, &metadata);
					if (!passed) {
						fprintf(stderr, "internal decision/metadata replay mismatch for seed %s\n",
								seeds[i]);
						atomic_store(&s->ioError, true);
						goto done;
					}
				}
				if (passed) {
					chunkMatched++;
					if (eventMode) {
						if (!pool_buffer_event_hit(s, eventHits, &eventUsed,
								ranks[i], &metadata)) goto done;
					} else if (!pool_buffer_hit(s, outbuf, encoded, &outUsed,
							ranks[i], seeds[i])) goto done;
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
			bool passed = pool_evaluate_pre(c, seeds[0], hs, hf, NULL, 0,
					eventMode && !decisionReplay ? &metadata : NULL);
			if (passed && decisionReplay) {
				passed = pool_evaluate_pre(c, seeds[0], hs, hf, NULL, 0, &metadata);
				if (!passed) {
					fprintf(stderr, "internal decision/metadata replay mismatch for seed %s\n",
							seeds[0]);
					atomic_store(&s->ioError, true);
					goto done;
				}
			}
			if (passed) {
				chunkMatched++;
				if (eventMode) {
					if (!pool_buffer_event_hit(s, eventHits, &eventUsed,
							candidate, &metadata)) goto done;
				} else if (!pool_buffer_hit(s, outbuf, encoded, &outUsed,
						candidate, seeds[0])) goto done;
			}
		}
		atomic_fetch_add(&s->matched, chunkMatched);
		atomic_fetch_add(&s->scanned, end - begin);
	}
done:
	if (eventMode) (void)pool_flush_event_hits(s, eventHits, &eventUsed);
	else (void)pool_flush_hits(s, outbuf, encoded, &outUsed);
	bspool_scratch_destroy(&inputScratch);
	free(eventHits);
	free(encoded);
	free(outbuf);
	free(c->omenTrace);
	free(c->soulTape);
	free(c);
	return NULL;
}

static bool pool_write_header(FILE *f, const PoolPlan *p, uint64_t records,
		uint64_t dataBytes, uint64_t membershipDigest, uint64_t metadataDigest,
		uint64_t cursor,
		int complete, char *err, size_t errsz) {
	if (p->format != POOL_BINARY) return true;
	unsigned char buf[BSPOOL_HEADER_EVENTS_SIZE];
	if (p->headerBytes != BSPOOL_HEADER_SIZE
			&& p->headerBytes != BSPOOL_HEADER_EVENTS_SIZE) {
		snprintf(err, errsz, "unsupported output header size"); return false;
	}
	size_t headerCap = (size_t)p->headerBytes;
	memset(buf, 0, headerCap);
	/* The header embeds the criteria that built the pool so a shared .bspool
	 * is self-describing without its .manifest sidecar, and so the in-game
	 * consumer can later compose the pool's tag route with overlay filters. */
	char poolId[24];
	pool_compute_id(p, records, complete, poolId);
	uint64_t snapshotId = pool_snapshot_id(p, records, dataBytes, membershipDigest);
	int coverageComplete = complete && (!p->refilter || p->sourceCoverageComplete);
	const char *encoding = p->outputSchema == BSPOOL_SCHEMA_EVENTS
			? "delta-varint-events-v1" : "delta-varint-blocks-v1";
	int n = snprintf((char *)buf, headerCap,
			"BRAINSTORM_SEED_POOL %d\n"
			"modelver %d\nencoding %s\nheader_bytes %d\ncharset %s\nseedspace %" PRIu64 "\n"
			"space %s\n"
			"range_start %" PRIu64 "\nrange_end %" PRIu64 "\n"
			"catalog_hash %016" PRIx64 "\ncriteria_hash %016" PRIx64 "\n"
			"pool_id %s\n"
			"family_id %016" PRIx64 "\nsegment_id %016" PRIx64 "\n"
			"stage_hash %016" PRIx64 "\nlineage_id %016" PRIx64 "\n"
			"derivation_id %016" PRIx64 "\nsnapshot_id %016" PRIx64 "\n"
			"membership_digest %016" PRIx64 "\nmetadata_digest %016" PRIx64
			"\nscan_cursor %" PRIu64 "\n"
			"tag_route %s\n",
			p->outputSchema, MODELVER, encoding, p->headerBytes,
			space_charset(p->space), space_size(p->space),
			space_name(p->space),
			p->outputRangeStart, p->outputRangeEnd, p->catalogHash, p->criteriaHash,
			poolId, p->familyId, p->segmentId, p->stageHash, p->lineageId,
			p->derivationId, snapshotId, membershipDigest, metadataDigest, cursor,
			p->collectTags ? "collect" : "observe");
	if (n < 0 || (size_t)n >= headerCap) { snprintf(err, errsz, "binary header overflow"); return false; }
	int hasLegendaryRoute = p->nbaseLegendaryRules || p->nlegendary;
	if (hasLegendaryRoute
			&& (p->legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM
				|| p->inheritedFastLegendaryRoutes)) {
		int rw = snprintf((char *)buf + n, headerCap - (size_t)n,
				"legendary_routes %s\n",
				p->legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM
					? "canonical_charm" : "full");
		if (rw < 0 || (size_t)rw >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from Legendary route scope");
			return false;
		}
		n += rw;
	}
	if (p->inheritedFastLegendaryRoutes) {
		int rw = snprintf((char *)buf + n, headerCap - (size_t)n,
				"route_legendary_routes canonical_charm\n");
		if (rw < 0 || (size_t)rw >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from inherited Legendary route scope");
			return false;
		}
		n += rw;
	}
	if (p->label[0]) {
		int lw = snprintf((char *)buf + n, headerCap - (size_t)n, "label %s\n", p->label);
		if (lw < 0 || (size_t)lw >= headerCap - (size_t)n) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += lw;
	}
	for (int i = 0; i < p->nbaseTagRules; i++) {
		const PoolTagRule *r = &p->baseTagRules[i];
		int rw;
		if (r->minPhase == SOUL_PHASE_SMALL && r->maxPhase == SOUL_PHASE_BIG)
			rw = snprintf((char *)buf + n, headerCap - (size_t)n,
					"route_tag %s %s %d %d %d\n", r->collect ? "collect" : "observe",
					r->key, r->minAnte, r->maxAnte, r->minCount);
		else
			rw = snprintf((char *)buf + n, headerCap - (size_t)n,
					"route_tag %s %s %d %s %d %s %d\n",
					r->collect ? "collect" : "observe", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->minCount);
		if (rw < 0 || (size_t)rw >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from cumulative refilter route");
			return false;
		}
		n += rw;
	}
	for (int i = 0; i < p->nbaseLegendaryRules; i++) {
		const PoolLegendaryRule *r = &p->baseLegendaryRules[i];
		int rw;
		if (r->humanLocation)
			rw = snprintf((char *)buf + n, headerCap - (size_t)n,
					"route_legendary %s %d %s %d %s %d %s %d\n", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->requireNegative,
					pool_source_str(r->source), r->soulDepth);
		else
			rw = snprintf((char *)buf + n, headerCap - (size_t)n,
					"route_legendary %s %d %d %d %d\n", r->key,
					r->minAnte, r->maxAnte, r->requireNegative, r->soulDepth);
		if (rw < 0 || (size_t)rw >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from cumulative legendary route");
			return false;
		}
		n += rw;
	}
	for (int i = 0; i < p->nbaseVoucherRules; i++) {
		const PoolVoucherRule *r = &p->baseVoucherRules[i];
		int rw = snprintf((char *)buf + n, headerCap - (size_t)n,
				"route_voucher %s %d %d\n", r->key, r->minAnte, r->maxAnte);
		if (rw < 0 || (size_t)rw >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from cumulative voucher route");
			return false;
		}
		n += rw;
	}
	for (int i = 0; i < p->nbaseVoucherExclusions; i++) {
		int rw = snprintf((char *)buf + n, headerCap - (size_t)n,
				"route_voucher_exclude %s\n", p->baseVoucherExclusionKeys[i]);
		if (rw < 0 || (size_t)rw >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from voucher exclusions");
			return false;
		}
		n += rw;
	}
	for (int i = 0; i < p->ntagRules; i++) {
		const PoolTagRule *r = &p->tagRules[i];
		int w;
		if (r->minPhase == SOUL_PHASE_SMALL && r->maxPhase == SOUL_PHASE_BIG)
			w = snprintf((char *)buf + n, headerCap - (size_t)n, "tag %s %d %d %d\n",
					r->key, r->minAnte, r->maxAnte, r->minCount);
		else
			w = snprintf((char *)buf + n, headerCap - (size_t)n,
					"tag %s %d %s %d %s %d\n", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->minCount);
		if (w < 0 || (size_t)w >= headerCap - (size_t)n) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += w;
	}
	for (int i = 0; i < p->nlegendary; i++) {
		const PoolLegendaryRule *r = &p->legendary[i];
		int w;
		if (r->humanLocation)
			w = snprintf((char *)buf + n, headerCap - (size_t)n,
					"legendary %s %d %s %d %s %d %s\n", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->requireNegative,
					pool_source_str(r->source));
		else
			w = snprintf((char *)buf + n, headerCap - (size_t)n,
					"legendary %s %d %d %d\n", r->key,
					r->minAnte, r->maxAnte, r->requireNegative);
		if (w < 0 || (size_t)w >= headerCap - (size_t)n) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += w;
		if (r->soulDepth != 1) {
			w = snprintf((char *)buf + n, headerCap - (size_t)n,
					"soul_depth %s\n", pool_soul_depth_str(r->soulDepth));
			if (w < 0 || (size_t)w >= headerCap - (size_t)n) { snprintf(err, errsz, "binary header overflow"); return false; }
			n += w;
		}
	}
	for (int i = 0; i < p->nvoucherRules; i++) {
		const PoolVoucherRule *r = &p->voucherRules[i];
		int w = snprintf((char *)buf + n, headerCap - (size_t)n,
				"voucher %s %d %d\n", r->key, r->minAnte, r->maxAnte);
		if (w < 0 || (size_t)w >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from voucher criteria");
			return false;
		}
		n += w;
	}
	for (int i = 0; i < p->nvoucherExclusions; i++) {
		int w = snprintf((char *)buf + n, headerCap - (size_t)n,
				"voucher_exclude %s\n", p->voucherExclusionKeys[i]);
		if (w < 0 || (size_t)w >= headerCap - (size_t)n) {
			snprintf(err, errsz, "binary header overflow from voucher exclusions");
			return false;
		}
		n += w;
	}
	if (p->refilter) {
		int w = snprintf((char *)buf + n, headerCap - (size_t)n,
				"refilter_depth %d\nsource_criteria_hash %016" PRIx64 "\nsource_records %" PRIu64 "\nsource_pool_id %s\n"
				"source_complete %d\nsource_coverage_complete %d\n"
				"input_cursor %" PRIu64 "\ninput_record_start 0\ninput_record_end %" PRIu64 "\n"
				"parent_snapshot_id %016" PRIx64 "\nparent_segment_id %016" PRIx64 "\n"
				"parent_records %" PRIu64 "\nparent_data_bytes %" PRIu64 "\n"
				"parent_coverage_complete %d\n",
				p->refilterDepth, p->sourceCriteriaHash, p->sourceRecords,
				p->sourcePoolId[0] ? p->sourcePoolId : "-",
				p->sourceComplete, p->sourceCoverageComplete, cursor,
				p->sourceRecords, p->sourceSnapshotId, p->sourceSegmentId,
				p->sourceRecords, p->sourceDataBytes, p->sourceCoverageComplete);
		if (w < 0 || (size_t)w >= headerCap - (size_t)n) { snprintf(err, errsz, "binary header overflow"); return false; }
		n += w;
	}
	int w = snprintf((char *)buf + n, headerCap - (size_t)n,
			"records %" PRIu64 "\ndata_bytes %" PRIu64 "\ncomplete %d\ncoverage_complete %d\n"
			"end\n",
			records, dataBytes, complete, coverageComplete);
	if (w < 0 || (size_t)w >= headerCap - (size_t)n) { snprintf(err, errsz, "binary header overflow"); return false; }
	int64_t at = bs_ftello(f);
	if (bs_fseeko(f, 0, SEEK_SET) != 0 || fwrite(buf, 1, headerCap, f) != headerCap
			|| fflush(f) != 0 || bs_fsync_file(f) != 0 || bs_fseeko(f, at, SEEK_SET) != 0) {
		snprintf(err, errsz, "cannot update binary header: %s", strerror(errno));
		return false;
	}
	return true;
}

static bool pool_append_index(FILE *f, int schema, int headerBytes, int space, uint64_t records,
		uint64_t dataBytes, uint64_t membershipDigest, uint64_t metadataDigest,
		uint64_t *finalBytes, char *err, size_t errsz) {
	if (fflush(f) != 0) { snprintf(err, errsz, "cannot flush pool data before indexing"); return false; }
	BspoolHeader h;
	memset(&h, 0, sizeof h);
	h.headerBytes = headerBytes;
	h.encoding = schema == BSPOOL_SCHEMA_EVENTS
			? BSPOOL_ENCODING_DELTA_EVENTS : BSPOOL_ENCODING_DELTA_BLOCKS;
	h.space = space; h.records = records; h.dataBytes = dataBytes;
	BspoolReader r;
	if (!bspool_reader_init(&r, fileno(f), &h, (uint64_t)headerBytes + dataBytes, err, errsz)) return false;
	uint64_t indexOff = (uint64_t)headerBytes + dataBytes;
	if (bs_fseeko(f, (int64_t)indexOff, SEEK_SET) != 0) {
		snprintf(err, errsz, "cannot seek to compressed pool index"); bspool_reader_destroy(&r); return false;
	}
	uint32_t entryBytes = schema == BSPOOL_SCHEMA_EVENTS
			? BSPOOL3_INDEX_ENTRY_SIZE : BSPOOL_INDEX_ENTRY_SIZE;
	unsigned char raw[BSPOOL3_INDEX_ENTRY_SIZE * 4096];
	uint64_t done = 0;
	while (done < r.nblocks) {
		uint64_t n = r.nblocks - done;
		if (n > 4096) n = 4096;
		for (uint64_t i = 0; i < n; i++) {
			const BspoolBlockIndex *e = &r.blocks[done + i];
			unsigned char *q = raw + i * entryBytes;
			bspool_put_u64le(q, e->offset);
			bspool_put_u64le(q + 8, e->firstRecord);
			bspool_put_u32le(q + 16, e->count);
			bspool_put_u32le(q + 20, e->rankBytes);
			if (schema == BSPOOL_SCHEMA_EVENTS) {
				bspool_put_u32le(q + 24, e->metadataBytes);
				bspool_put_u32le(q + 28, e->associations);
			}
		}
		size_t bytes = (size_t)n * entryBytes;
		if (fwrite(raw, 1, bytes, f) != bytes) {
			snprintf(err, errsz, "cannot write compressed pool index"); bspool_reader_destroy(&r); return false;
		}
		done += n;
	}
	unsigned char footer[BSPOOL3_FOOTER_SIZE];
	uint32_t footerBytes = schema == BSPOOL_SCHEMA_EVENTS
			? BSPOOL3_FOOTER_SIZE : BSPOOL_FOOTER_SIZE;
	memset(footer, 0, footerBytes);
	memcpy(footer, schema == BSPOOL_SCHEMA_EVENTS ? "BSPIDX3\n" : "BSPIDX2\n", 8);
	bspool_put_u64le(footer + 8, indexOff);
	bspool_put_u64le(footer + 16, r.nblocks);
	bspool_put_u64le(footer + 24, records);
	bspool_put_u64le(footer + 32, dataBytes);
	if (schema == BSPOOL_SCHEMA_EVENTS) {
		bspool_put_u64le(footer + 40, membershipDigest);
		bspool_put_u64le(footer + 48, metadataDigest);
		bspool_put_u64le(footer + 72, bspool_crc64_update(0, footer, 72));
	}
	uint64_t blocks = r.nblocks;
	bspool_reader_destroy(&r);
	if (fwrite(footer, 1, footerBytes, f) != footerBytes || fflush(f) != 0 || bs_fsync_file(f) != 0) {
		snprintf(err, errsz, "cannot commit compressed pool index"); return false;
	}
	int64_t pos = bs_ftello(f);
	if (pos < 0 || (uint64_t)pos != indexOff + blocks * entryBytes + footerBytes) {
		snprintf(err, errsz, "compressed pool index size accounting failed"); return false;
	}
	*finalBytes = (uint64_t)pos;
	return true;
}

static bool pool_write_state(const char *path, const PoolPlan *p, const PoolState *s,
		char *err, size_t errsz) {
	char tmp[1024];
	if (snprintf(tmp, sizeof tmp, "%s.tmp.%lu", path, bs_process_id()) >= (int)sizeof tmp) {
		snprintf(err, errsz, "state path is too long"); return false;
	}
	FILE *f = fopen(tmp, "w");
	if (!f) { snprintf(err, errsz, "cannot write state: %s", strerror(errno)); return false; }
	fprintf(f, "BRAINSTORM_SEED_POOL_STATE %d\n", POOL_STATE_SCHEMA);
	fprintf(f, "catalog_hash %016" PRIx64 "\ncriteria_hash %016" PRIx64 "\n", p->catalogHash, p->criteriaHash);
	fprintf(f, "range_start %" PRIu64 "\nrange_end %" PRIu64 "\n", p->start, p->start + p->count);
	fprintf(f, "cursor %" PRIu64 "\noutput_bytes %" PRIu64 "\n", s->cursor, s->outputBytes);
	fprintf(f, "membership_digest %016" PRIx64 "\n", s->membershipDigest);
	fprintf(f, "metadata_digest %016" PRIx64 "\n", s->metadataDigest);
	fprintf(f, "matched %" PRIu64 "\nscanned %" PRIu64 "\nelapsed_seconds %.9f\ndone %d\nend\n",
			s->matched, s->scanned, s->elapsed, s->done);
	/* Always close the temporary stream, including after a flush/fsync error.
	 * Apart from leaking a descriptor, leaving it open prevents cleanup or
	 * replacement on Windows. Preserve the first useful errno for the caller. */
	int saved = 0;
	const char *phase = "write";
	if (ferror(f) || fflush(f) != 0) saved = errno ? errno : EIO;
	if (!saved && bs_fsync_file(f) != 0) {
		saved = errno ? errno : EIO;
		phase = "sync";
	}
	if (fclose(f) != 0 && !saved) {
		saved = errno ? errno : EIO;
		phase = "close";
	}
	if (saved) {
		remove(tmp);
		snprintf(err, errsz, "cannot %s state checkpoint: %s", phase, strerror(saved));
		return false;
	}
	if (bs_rename_overwrite(tmp, path) != 0) {
		saved = errno ? errno : EIO;
		remove(tmp);
		snprintf(err, errsz, "cannot replace state checkpoint: %s", strerror(saved));
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
	enum {
		SS_VERSION = 1u << 0, SS_CATALOG = 1u << 1, SS_CRITERIA = 1u << 2,
		SS_RANGE_START = 1u << 3, SS_RANGE_END = 1u << 4, SS_CURSOR = 1u << 5,
		SS_OUTPUT = 1u << 6, SS_MATCHED = 1u << 7, SS_SCANNED = 1u << 8,
		SS_ELAPSED = 1u << 9, SS_DONE = 1u << 10, SS_MEMBERSHIP = 1u << 11,
		SS_METADATA = 1u << 12
	};
	const unsigned requiredV1 = (1u << 11) - 1;
	const unsigned requiredV2 = (1u << 12) - 1;
	const unsigned requiredV3 = (1u << 13) - 1;
	unsigned seen = 0;
	uint64_t ch = 0, qh = 0, rs = 0, re = 0;
	char line[256];
	while (fgets(line, sizeof line, f)) {
		char *sp = line;
		char *d = pool_tok(&sp), *v = pool_tok(&sp);
		if (!d) continue;
		unsigned bit = 0;
		if (!strcmp(d, "BRAINSTORM_SEED_POOL_STATE")) {
			bit = SS_VERSION; if (!pool_parse_int(v, &version)) goto bad;
		} else if (!strcmp(d, "catalog_hash")) {
			bit = SS_CATALOG; if (!pool_parse_hex64(v, &ch)) goto bad;
		} else if (!strcmp(d, "criteria_hash")) {
			bit = SS_CRITERIA; if (!pool_parse_hex64(v, &qh)) goto bad;
		} else if (!strcmp(d, "range_start")) {
			bit = SS_RANGE_START; if (!pool_parse_u64(v, &rs)) goto bad;
		} else if (!strcmp(d, "range_end")) {
			bit = SS_RANGE_END; if (!pool_parse_u64(v, &re)) goto bad;
		} else if (!strcmp(d, "cursor")) {
			bit = SS_CURSOR; if (!pool_parse_u64(v, &s->cursor)) goto bad;
		} else if (!strcmp(d, "output_bytes")) {
			bit = SS_OUTPUT; if (!pool_parse_u64(v, &s->outputBytes)) goto bad;
		} else if (!strcmp(d, "membership_digest")) {
			bit = SS_MEMBERSHIP; if (!pool_parse_hex64(v, &s->membershipDigest)) goto bad;
		} else if (!strcmp(d, "metadata_digest")) {
			bit = SS_METADATA; if (!pool_parse_hex64(v, &s->metadataDigest)) goto bad;
		} else if (!strcmp(d, "matched")) {
			bit = SS_MATCHED; if (!pool_parse_u64(v, &s->matched)) goto bad;
		} else if (!strcmp(d, "scanned")) {
			bit = SS_SCANNED; if (!pool_parse_u64(v, &s->scanned)) goto bad;
		} else if (!strcmp(d, "elapsed_seconds")) {
			bit = SS_ELAPSED; if (!pool_parse_double(v, &s->elapsed)) goto bad;
		} else if (!strcmp(d, "done")) {
			bit = SS_DONE; if (!pool_parse_int(v, &s->done)) goto bad;
		} else if (!strcmp(d, "end")) {
			if (v) goto bad;
			sawEnd = 1;
			continue;
		} else {
			goto bad;
		}
		if ((seen & bit) || pool_tok(&sp)) goto bad;
		seen |= bit;
	}
	fclose(f);
	if ((version == 1 ? seen != requiredV1
				: version == 2 ? seen != requiredV2 : seen != requiredV3)
			|| (version != 1 && version != 2 && version != POOL_STATE_SCHEMA) || !sawEnd
			|| ch != p->catalogHash || qh != p->criteriaHash
			|| rs != p->start || re != p->start + p->count || s->cursor < rs || s->cursor > re
			|| s->scanned != s->cursor - rs || s->matched > s->scanned
			|| !isfinite(s->elapsed) || s->elapsed < 0.0 || (s->done != 0 && s->done != 1)
			|| (s->done && s->cursor != re)
			|| (p->format == POOL_BINARY && s->outputBytes < (uint64_t)p->headerBytes)
			|| (p->format == POOL_COUNT && s->outputBytes != 0)
			|| s->outputBytes > (uint64_t)INT64_MAX) {
		snprintf(err, errsz, "state does not match this model, criteria, or range");
		return false;
	}
	return true;
bad:
	fclose(f);
	snprintf(err, errsz, "malformed state file");
	return false;
}

static bool pool_write_manifest(const char *path, const PoolPlan *p,
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
	fprintf(f, "family_id %016" PRIx64 "\nsegment_id %016" PRIx64
			"\nstage_hash %016" PRIx64 "\nlineage_id %016" PRIx64
			"\nderivation_id %016" PRIx64 "\nmembership_digest %016" PRIx64
			"\nmetadata_digest %016" PRIx64 "\n",
			p->familyId, p->segmentId, p->stageHash, p->lineageId, p->derivationId,
			s->membershipDigest, s->metadataDigest);
	if (p->label[0]) fprintf(f, "label %s\n", p->label);
	fprintf(f, "charset %s\nseedspace %" PRIu64 "\nspace %s\n",
			space_charset(p->space), space_size(p->space), space_name(p->space));
	fprintf(f, "range_start %" PRIu64 "\nrange_end %" PRIu64 "\n", p->outputRangeStart, p->outputRangeEnd);
	int coverageComplete = s->done && (!p->refilter || p->sourceCoverageComplete);
	fprintf(f, "scanned %" PRIu64 "\nmatched %" PRIu64 "\ncomplete %d\ncoverage_complete %d\n",
			s->scanned, s->matched, s->done, coverageComplete);
	fprintf(f, "format %s\nrecord_order block-sorted\ntag_route %s\n",
			p->format == POOL_BINARY
					? (p->outputSchema == BSPOOL_SCHEMA_EVENTS
							? "delta-varint-events-v1" : "delta-varint-blocks-v1")
					: p->format == POOL_TEXT ? "seed-text" : "count-only",
			p->collectTags ? "collect-first-required" : "observe");
	int hasLegendaryRoute = p->nbaseLegendaryRules || p->nlegendary;
	if (hasLegendaryRoute
			&& (p->legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM
				|| p->inheritedFastLegendaryRoutes))
		fprintf(f, "legendary_routes %s\n",
				p->legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM
					? "canonical_charm" : "full");
	if (p->inheritedFastLegendaryRoutes)
		fprintf(f, "source_route_legendary_routes canonical_charm\n");
	if (p->refilter) fprintf(f,
			"refilter_depth %d\ninput_records %" PRIu64 "\nsource_criteria_hash %016" PRIx64 "\nsource_pool_id %s\n"
			"source_complete %d\nsource_coverage_complete %d\n"
			"parent_snapshot_id %016" PRIx64 "\nparent_segment_id %016" PRIx64
			"\nparent_records %" PRIu64 "\nparent_data_bytes %" PRIu64 "\n",
			p->refilterDepth, p->sourceRecords, p->sourceCriteriaHash,
			p->sourcePoolId[0] ? p->sourcePoolId : "-",
			p->sourceComplete, p->sourceCoverageComplete, p->sourceSnapshotId,
			p->sourceSegmentId, p->sourceRecords, p->sourceDataBytes);
	for (int i = 0; i < p->nbaseTagRules; i++) {
		const PoolTagRule *r = &p->baseTagRules[i];
		if (r->minPhase == SOUL_PHASE_SMALL && r->maxPhase == SOUL_PHASE_BIG)
			fprintf(f, "source_route_tag %s %s %d %d %d\n",
					r->collect ? "collect" : "observe", r->key,
					r->minAnte, r->maxAnte, r->minCount);
		else
			fprintf(f, "source_route_tag %s %s %d %s %d %s %d\n",
					r->collect ? "collect" : "observe", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->minCount);
	}
	for (int i = 0; i < p->nbaseLegendaryRules; i++) {
		const PoolLegendaryRule *r = &p->baseLegendaryRules[i];
		if (r->humanLocation)
			fprintf(f, "source_route_legendary %s %d %s %d %s %d %s %d\n", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->requireNegative,
					pool_source_str(r->source), r->soulDepth);
		else
			fprintf(f, "source_route_legendary %s %d %d %d %d\n", r->key,
					r->minAnte, r->maxAnte, r->requireNegative, r->soulDepth);
	}
	for (int i = 0; i < p->nbaseVoucherRules; i++) {
		const PoolVoucherRule *r = &p->baseVoucherRules[i];
		fprintf(f, "source_route_voucher %s %d %d\n",
				r->key, r->minAnte, r->maxAnte);
	}
	for (int i = 0; i < p->nbaseVoucherExclusions; i++)
		fprintf(f, "source_route_voucher_exclude %s\n",
				p->baseVoucherExclusionKeys[i]);
	for (int i = 0; i < p->ntagRules; i++) {
		const PoolTagRule *r = &p->tagRules[i];
		if (r->minPhase == SOUL_PHASE_SMALL && r->maxPhase == SOUL_PHASE_BIG)
			fprintf(f, "tag %s %d %d %d\n", r->key, r->minAnte, r->maxAnte, r->minCount);
		else
			fprintf(f, "tag %s %d %s %d %s %d\n", r->key,
					r->minAnte, pool_phase_str(r->minPhase), r->maxAnte,
					pool_phase_str(r->maxPhase), r->minCount);
	}
	for (int i = 0; i < p->nlegendary; i++) {
		const PoolLegendaryRule *r = &p->legendary[i];
		const char *depthName = r->soulDepth == 2 ? "second"
				: r->soulDepth == SOUL_DEPTH_ANY ? "either" : "first";
		if (r->humanLocation)
			fprintf(f, "%s_soul_legendary %s %d %s %d %s %d %s\n",
					depthName, r->key, r->minAnte, pool_phase_str(r->minPhase),
					r->maxAnte, pool_phase_str(r->maxPhase), r->requireNegative,
					pool_source_str(r->source));
		else
			fprintf(f, "%s_soul_legendary %s %d %d %d\n",
					depthName, r->key, r->minAnte, r->maxAnte, r->requireNegative);
		if (r->soulDepth != 1)
			fprintf(f, "soul_depth %s\n", pool_soul_depth_str(r->soulDepth));
	}
	for (int i = 0; i < p->nvoucherRules; i++) {
		const PoolVoucherRule *r = &p->voucherRules[i];
		fprintf(f, "voucher %s %d %d\n", r->key, r->minAnte, r->maxAnte);
	}
	for (int i = 0; i < p->nvoucherExclusions; i++)
		fprintf(f, "voucher_exclude %s\n", p->voucherExclusionKeys[i]);
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
				+ p->headerBytes + (p->outputSchema == BSPOOL_SCHEMA_EVENTS
						? BSPOOL3_FOOTER_SIZE : BSPOOL_FOOTER_SIZE);
		fprintf(f, "projected_full_matches %.0f\nprojected_u64_bytes %.0f\n"
				"projected_compressed_bytes %.0f\nexpected_compressed_bytes_per_record %.6f\n",
				projected, projected * 8.0, projectedCompressed, expectedDeltaBytes + 0.01);
	}
	if (p->format == POOL_BINARY && s->matched && s->outputBytes >= (uint64_t)p->headerBytes) {
		fprintf(f, "compressed_file_bytes %" PRIu64 "\nbytes_per_record %.6f\n",
				s->outputBytes, (double)(s->outputBytes - (uint64_t)p->headerBytes) / (double)s->matched);
	}
	fprintf(f, "end\n");
	if (fclose(f) != 0) { snprintf(err, errsz, "cannot close manifest: %s", strerror(errno)); return false; }
	return true;
}

static double pool_now(void) {
	return bs_monotonic_seconds();
}

static int pool_mode_scan(const Config *g, PoolPlan *p, const char *output);

static int pool_mode_scan_locked(const Config *g, PoolPlan *p, const char *output) {
	char lockPath[1024];
	if (snprintf(lockPath, sizeof lockPath, "%s.writer.lock", output)
			>= (int)sizeof lockPath) {
		fprintf(stderr, "output path is too long for its writer lock\n");
		return 1;
	}
	bool busy = false;
	bs_file_lock_t lock = bs_file_lock_acquire(lockPath, &busy);
	if (lock == BS_FILE_LOCK_INVALID) {
		if (busy)
			fprintf(stderr, "output is already being written by another scanner\n");
		else
			fprintf(stderr, "cannot acquire output writer lock: %s\n", strerror(errno));
		return 1;
	}
	int rc = pool_mode_scan(g, p, output);
	bs_file_lock_release(lock);
	return rc;
}

static int pool_mode_scan(const Config *g, PoolPlan *p, const char *output) {
	char err[256];
	if (!calibrate(g, err, sizeof err)) { fprintf(stderr, "calibration failed: %s\n", err); return 1; }
	if (!pool_batch_selftest(p)) { fprintf(stderr, "batch hash self-test failed\n"); return 1; }
	char statePath[1024], manifestPath[1024];
	if (snprintf(statePath, sizeof statePath, "%s.state", output) >= (int)sizeof statePath
			|| snprintf(manifestPath, sizeof manifestPath, "%s.manifest", output) >= (int)sizeof manifestPath) {
		fprintf(stderr, "output path is too long\n"); return 1;
	}
	PoolState state = { .cursor = p->start, .membershipDigest = POOL_HASH_INIT,
			.metadataDigest = POOL_HASH_INIT };
	FILE *out = NULL;
	bool stateExists = bs_file_exists(statePath);
	if (p->resume && !stateExists && p->format != POOL_COUNT && bs_file_exists(output)) {
		fprintf(stderr, "output exists without resumable state; set 'resume 0' to replace it\n");
		return 1;
	}
	BspoolHeader resumeHeader;
	memset(&resumeHeader, 0, sizeof resumeHeader);
	if (p->resume && stateExists && p->format == POOL_BINARY) {
		FILE *probe = fopen(output, "rb");
		if (!probe) {
			fprintf(stderr, "cannot open output header for resume: %s\n", strerror(errno));
			return 1;
		}
		bool headerOk = bspool_read_header(probe, &resumeHeader, err, sizeof err);
		fclose(probe);
		if (!headerOk || (resumeHeader.schema != BSPOOL_SCHEMA_BLOCKS
				&& resumeHeader.schema != BSPOOL_SCHEMA_EVENTS)) {
			fprintf(stderr, "%s\n", headerOk ? "resumable output uses an unsupported pool schema" : err);
			return 1;
		}
		p->outputSchema = resumeHeader.schema;
		p->headerBytes = resumeHeader.headerBytes;
	}
	if (p->resume && stateExists) {
		if (!pool_load_state(statePath, p, &state, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
		if (p->format != POOL_COUNT) {
			bool headerIdentityOk = p->format != POOL_BINARY
					|| (resumeHeader.catalogHash == p->catalogHash
						&& resumeHeader.criteriaHash == p->criteriaHash
						&& (!resumeHeader.familyId || resumeHeader.familyId == p->familyId)
						&& (!resumeHeader.segmentId || resumeHeader.segmentId == p->segmentId)
						&& (!resumeHeader.lineageId || resumeHeader.lineageId == p->lineageId)
						&& resumeHeader.rangeStart == p->outputRangeStart
						&& resumeHeader.rangeEnd == p->outputRangeEnd);
			uint64_t headerCursor = p->refilter
					? resumeHeader.inputCursor : resumeHeader.scanCursor;
			uint64_t nextCheckpoint = state.cursor + p->checkpoint;
			if (nextCheckpoint < state.cursor
					|| nextCheckpoint > p->start + p->count)
				nextCheckpoint = p->start + p->count;
			/* The binary header is flushed before the atomic .state replacement.
			 * If Windows denied that replacement, the header and its checksummed
			 * data describe one more durable checkpoint than the old state file.
			 * Accept only a forward, incomplete checkpoint from the same pool;
			 * the digest verification below proves its full payload before the
			 * state is repaired. */
			bool checkpointAhead = p->format == POOL_BINARY && headerIdentityOk
					&& !state.done && !resumeHeader.complete
					&& headerCursor == nextCheckpoint
					&& resumeHeader.records >= state.matched
					&& resumeHeader.dataBytes
						>= state.outputBytes - (uint64_t)p->headerBytes
					&& resumeHeader.dataBytes
						<= (uint64_t)INT64_MAX - (uint64_t)p->headerBytes;
			bool finalizedAhead = p->format == POOL_BINARY && !state.done
					&& resumeHeader.complete && state.cursor == p->start + p->count
					&& resumeHeader.dataBytes
							== state.outputBytes - (uint64_t)p->headerBytes;
			if (p->format == POOL_BINARY
					&& (!headerIdentityOk
						|| (!checkpointAhead && resumeHeader.records != state.matched)
						|| (state.done && !resumeHeader.complete)
						|| (!state.done && !checkpointAhead && !finalizedAhead
								&& (resumeHeader.complete
								|| resumeHeader.dataBytes
										!= state.outputBytes - (uint64_t)p->headerBytes)))) {
				fprintf(stderr, "output header does not match its resumable state\n");
				return 1;
			}
			if (checkpointAhead) {
				state.cursor = headerCursor;
				state.scanned = headerCursor - p->start;
				state.matched = resumeHeader.records;
				state.outputBytes = (uint64_t)p->headerBytes + resumeHeader.dataBytes;
				state.membershipDigest = resumeHeader.membershipDigest;
				state.metadataDigest = resumeHeader.metadataDigest;
			}
			FILE *digestFile = fopen(output, "rb");
			uint64_t committedDigest = 0;
			uint64_t digestOffset = p->format == POOL_BINARY
					? (uint64_t)p->headerBytes : 0;
			uint64_t digestBytes = p->format == POOL_BINARY
					? resumeHeader.dataBytes : state.outputBytes;
			bool digestOk = digestFile && pool_hash_fd_region(fileno(digestFile),
					digestOffset, digestBytes, &committedDigest);
			if (digestFile) fclose(digestFile);
			if (!digestOk || (state.membershipDigest
					&& state.membershipDigest != committedDigest)
					|| (checkpointAhead
						&& resumeHeader.membershipDigest != committedDigest)) {
				fprintf(stderr, "committed output digest does not match its resumable state\n");
				return 1;
			}
			state.membershipDigest = committedDigest;
			if (p->format == POOL_BINARY) {
				FILE *metadataFile = fopen(output, "rb");
				int64_t metadataFileBytes = metadataFile ? bs_file_size(metadataFile) : -1;
				BspoolReader metadataReader;
				uint64_t committedMetadata = 0;
				bool metadataOk = metadataFile && metadataFileBytes >= 0
						&& bspool_reader_init(&metadataReader, fileno(metadataFile),
								&resumeHeader, (uint64_t)metadataFileBytes, err, sizeof err)
						&& pool_reader_metadata_digest(&metadataReader, &committedMetadata);
				if (metadataOk) bspool_reader_destroy(&metadataReader);
				if (metadataFile) fclose(metadataFile);
				if (!metadataOk || (state.metadataDigest
						&& state.metadataDigest != committedMetadata)
						|| (checkpointAhead
							&& resumeHeader.metadataDigest != committedMetadata)) {
					fprintf(stderr, "committed metadata digest does not match its resumable state\n");
					return 1;
				}
				state.metadataDigest = committedMetadata;
			} else {
				state.metadataDigest = 0;
			}
			if (checkpointAhead) {
				if (!pool_write_state(statePath, p, &state, err, sizeof err)) {
					fprintf(stderr, "%s\n", err); return 1;
				}
				fprintf(stderr, "recovered committed checkpoint at %s %" PRIu64 "\n",
						p->refilter ? "input record" : "rank", state.cursor);
			}
			if (p->format == POOL_BINARY && resumeHeader.complete) {
				FILE *finished = fopen(output, "rb");
				int64_t actualBytes = finished ? bs_file_size(finished) : -1;
				BspoolReader verified;
				bool valid = finished && actualBytes >= 0
						&& bspool_reader_init(&verified, fileno(finished), &resumeHeader,
								(uint64_t)actualBytes, err, sizeof err);
				if (valid) bspool_reader_destroy(&verified);
				if (finished) fclose(finished);
				if (!valid || (state.done && (uint64_t)actualBytes != state.outputBytes)) {
					fprintf(stderr, "completed pool size does not match its state\n");
					return 1;
				}
				if (finalizedAhead) {
					state.done = 1;
					state.outputBytes = (uint64_t)actualBytes;
					if (!pool_write_state(statePath, p, &state, err, sizeof err)
							|| !pool_write_manifest(manifestPath, p, &state, err, sizeof err)) {
						fprintf(stderr, "%s\n", err); return 1;
					}
					fprintf(stderr, "recovered finalized pool (%" PRIu64 " matches)\n", state.matched);
					return 0;
				}
			}
			if (state.done) {
				if (p->format != POOL_BINARY) {
					FILE *finished = fopen(output, "rb");
					int64_t actualBytes = finished ? bs_file_size(finished) : -1;
					if (finished) fclose(finished);
					if (actualBytes < 0 || (uint64_t)actualBytes != state.outputBytes) {
						fprintf(stderr, "completed pool size does not match its state\n");
						return 1;
					}
				}
				fprintf(stderr, "pool is already complete (%" PRIu64 " matches)\n", state.matched);
				return 0;
			}
			out = fopen(output, "r+b");
			if (!out) { fprintf(stderr, "cannot open output for resume: %s\n", strerror(errno)); return 1; }
			int64_t actualBytes = bs_file_size(out);
			if (actualBytes < 0 || (uint64_t)actualBytes < state.outputBytes
					|| bs_ftruncate_file(out, (int64_t)state.outputBytes) != 0
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
				state.outputBytes = (uint64_t)p->headerBytes;
				if (bs_fseeko(out, p->headerBytes, SEEK_SET) != 0
						|| !pool_write_header(out, p, 0, 0, state.membershipDigest,
								state.metadataDigest, state.cursor, 0, err, sizeof err)) {
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
		shared.membershipDigest = &state.membershipDigest;
		shared.metadataDigest = &state.metadataDigest;
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
					state.outputBytes - (uint64_t)p->headerBytes,
					state.membershipDigest, state.metadataDigest,
					state.cursor, 0, err, sizeof err)) {
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
				? state.outputBytes - (uint64_t)p->headerBytes : state.outputBytes;
		if (p->format == POOL_BINARY && state.done
				&& !pool_append_index(out, p->outputSchema, p->headerBytes,
						p->space, state.matched, dataBytes,
						state.membershipDigest, state.metadataDigest,
						&state.outputBytes, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); fclose(out); return 1;
		}
		if (!pool_write_header(out, p, state.matched, dataBytes,
				state.membershipDigest, state.metadataDigest,
				state.cursor, state.done, err, sizeof err)) {
			fprintf(stderr, "%s\n", err); fclose(out); return 1;
		}
		if (fclose(out) != 0) { fprintf(stderr, "cannot close output: %s\n", strerror(errno)); return 1; }
	}
	if (!pool_write_state(statePath, p, &state, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
	if (!pool_write_manifest(manifestPath, p, &state, err, sizeof err)) { fprintf(stderr, "%s\n", err); return 1; }
	if (state.done) {
		fprintf(stderr, "complete: scanned=%" PRIu64 " matched=%" PRIu64 " elapsed=%.3fs\n", state.scanned, state.matched, state.elapsed);
		return 0;
	}
	fprintf(stderr, "stopped cleanly at %s %" PRIu64 "; rerun the same command to resume\n",
			p->refilter ? "input record" : "rank", state.cursor);
	return 130;
}

static bool pool_prepare_refilter(const Config *g, PoolPlan *p, const char *input,
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
	/* Schema-2 source blocks hold up to 8,192 ranks.  Keeping a refilter claim
	 * at least block-sized avoids having several workers independently decode
	 * the same compressed block after the fresh-scan default was reduced. */
	if (h.schema == BSPOOL_SCHEMA_BLOCKS
			&& p->chunk < BSPOOL_BLOCK_MAX_RECORDS)
		p->chunk = BSPOOL_BLOCK_MAX_RECORDS;
	if (h.modelver != MODELVER) {
		snprintf(err, errsz, "input pool model %d != scanner model %d", h.modelver, MODELVER);
		fclose(f); return false;
	}
	if (h.catalogHash != p->catalogHash) {
		snprintf(err, errsz, "input pool was built from a different pool/unlock snapshot");
		fclose(f); return false;
	}
	/* A top-level directive is the effective policy of the source's latest
	 * cumulative evaluation.  The route_ directive is its inherited fallback.
	 * Preferring the top level matters when a full stage was intentionally run
	 * on an earlier fast source: the next refilter must not silently switch the
	 * cumulative predicate back to fast. */
	p->baseLegendaryRoutes = h.legendaryRoutesExplicit
		? h.legendaryRoutes : h.routeLegendaryRoutes;
	/* Unlike the effective mode above, ancestry is deliberately sticky. A later
	 * full refilter can evaluate every seed that reached it, but it cannot put
	 * back Omen-only seeds omitted by an earlier fast scan. Keep that limitation
	 * visible through every descendant header/manifest. */
	p->inheritedFastLegendaryRoutes =
		h.legendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM
		|| h.routeLegendaryRoutes == BSPOOL_LEGENDARY_ROUTES_CANONICAL_CHARM;
	/* A tag/voucher-only refilter has no new Legendary policy of its own, so
	 * retain the source pool's scope. An explicitly configured new Legendary
	 * stage may independently choose full or fast coverage. */
	if (!p->legendaryRoutesExplicit && !p->nlegendary)
		p->legendaryRoutes = p->baseLegendaryRoutes;
	if (h.nrouteTagRules + p->ntagRules > POOL_MAX_TAG_RULES) {
		snprintf(err, errsz, "source + new tag routes exceed the %d-rule cumulative limit",
				POOL_MAX_TAG_RULES);
		fclose(f); return false;
	}
	for (int i = 0; i < h.nrouteTagRules; i++) {
		PoolTagRule *r = &p->baseTagRules[p->nbaseTagRules++];
		snprintf(r->key, sizeof r->key, "%s", h.routeTagRules[i].key);
		r->minAnte = h.routeTagRules[i].minAnte;
		r->minPhase = h.routeTagRules[i].minPhase;
		r->maxAnte = h.routeTagRules[i].maxAnte;
		r->maxPhase = h.routeTagRules[i].maxPhase;
		r->minCount = h.routeTagRules[i].minCount;
		r->collect = h.routeTagRules[i].collect;
		r->poolIndex = pool_find_tag(g, r->key);
		if (r->poolIndex < 0) {
			snprintf(err, errsz, "source route tag %s is not in this snapshot", r->key);
			fclose(f); return false;
		}
		if (r->maxAnte > p->maxAnte) p->maxAnte = r->maxAnte;
	}
	for (int i = 0; i < h.nrouteLegendRules; i++) {
		if (p->nbaseLegendaryRules >= MAX_POOL_LEGEND_RULES) {
			snprintf(err, errsz, "source pool has too many cumulative Soul rules");
			fclose(f); return false;
		}
		PoolLegendaryRule *r = &p->baseLegendaryRules[p->nbaseLegendaryRules++];
		snprintf(r->key, sizeof r->key, "%s", h.routeLegendRules[i].key);
		r->minAnte = h.routeLegendRules[i].minAnte;
		r->minPhase = h.routeLegendRules[i].minPhase;
		r->maxAnte = h.routeLegendRules[i].maxAnte;
		r->maxPhase = h.routeLegendRules[i].maxPhase;
		r->requireNegative = h.routeLegendRules[i].neg;
		r->source = h.routeLegendRules[i].source;
		r->humanLocation = h.routeLegendRules[i].humanLocation;
		r->soulDepth = h.routeLegendRules[i].soulDepth;
		r->used = 1;
		r->poolIndex = pool_find_legendary(g, r->key);
		if (!g->soulAllowed || r->poolIndex < 0 || !g->jokerAvail[4][r->poolIndex]) {
			snprintf(err, errsz, "source legendary %s is unavailable in this snapshot", r->key);
			fclose(f); return false;
		}
		int rngMax = r->maxAnte + (r->humanLocation
				&& r->maxPhase == SOUL_PHASE_BOSS ? 1 : 0);
		if (rngMax > p->maxAnte) p->maxAnte = rngMax;
		/* Static conflicts are only provable between exact-depth rules: an
		 * either-depth rule can settle on whichever Soul the other rule does
		 * not claim, so those combinations are left to evaluation. */
		for (int j = 0; j < p->nlegendary; j++) {
			const PoolLegendaryRule *cur = &p->legendary[j];
			if (cur->soulDepth == SOUL_DEPTH_ANY || r->soulDepth == SOUL_DEPTH_ANY) continue;
			if (cur->soulDepth != r->soulDepth) continue;
			int conflict = strcmp(cur->key, r->key) != 0;
			if (!conflict && cur->humanLocation == r->humanLocation) {
				if (r->humanLocation) {
					conflict = pool_route_position(cur->maxAnte, cur->maxPhase)
							< pool_route_position(r->minAnte, r->minPhase)
						|| pool_route_position(r->maxAnte, r->maxPhase)
							< pool_route_position(cur->minAnte, cur->minPhase)
						|| (cur->source && r->source && cur->source != r->source);
				} else {
					conflict = cur->maxAnte < r->minAnte || r->maxAnte < cur->minAnte;
				}
			}
			if (conflict) {
				snprintf(err, errsz,
						"new and source rules conflict for Soul #%d", r->soulDepth);
				fclose(f); return false;
			}
		}
	}
	if (h.nrouteVoucherRules + p->nvoucherRules > POOL_MAX_VOUCHER_RULES) {
		snprintf(err, errsz, "source + new voucher routes exceed the %d-rule cumulative limit",
				POOL_MAX_VOUCHER_RULES);
		fclose(f); return false;
	}
	if (h.nrouteVoucherRules) {
		if (g->nvouch > 64) {
			snprintf(err, errsz,
					"voucher routes support at most 64 catalog entries (snapshot has %d)",
					g->nvouch);
			fclose(f); return false;
		}
		for (int i = 0; i < g->nvouch; i++) if (!g->vouchRouteDefined[i]) {
			snprintf(err, errsz,
					"voucher route catalog is missing; refresh native_search.cfg in Balatro");
			fclose(f); return false;
		}
	}
	for (int i = 0; i < h.nrouteVoucherRules; i++) {
		PoolVoucherRule *r = &p->baseVoucherRules[p->nbaseVoucherRules++];
		snprintf(r->key, sizeof r->key, "%s", h.routeVoucherRules[i].key);
		r->minAnte = h.routeVoucherRules[i].minAnte;
		r->maxAnte = h.routeVoucherRules[i].maxAnte;
		if (r->minAnte < 1 || r->maxAnte < r->minAnte
				|| r->maxAnte > POOL_MAX_VOUCHER_ANTE) {
			snprintf(err, errsz,
					"source voucher %s exceeds the supported Ante %d route window",
					r->key, POOL_MAX_VOUCHER_ANTE);
			fclose(f); return false;
		}
		r->poolIndex = pool_find_voucher(g, r->key);
		if (r->poolIndex < 0 || !g->vouchRouteAvail[r->poolIndex]) {
			snprintf(err, errsz, "source voucher %s is unavailable in this snapshot", r->key);
			fclose(f); return false;
		}
		if (g->vouchInitiallyOwned[r->poolIndex]) {
			snprintf(err, errsz, "source voucher %s is already owned at run start", r->key);
			fclose(f); return false;
		}
		if (r->maxAnte > p->maxVoucherAnte) p->maxVoucherAnte = r->maxAnte;
	}
	for (int i = 0; i < h.nrouteVoucherExclusions; i++) {
		int index = pool_find_voucher(g, h.routeVoucherExclusions[i]);
		if (index < 0) {
			snprintf(err, errsz, "source voucher exclusion %s is unknown",
					h.routeVoucherExclusions[i]);
			fclose(f); return false;
		}
		int duplicate = 0;
		for (int j = 0; j < p->nbaseVoucherExclusions; j++)
			if (p->baseVoucherExclusions[j] == index) { duplicate = 1; break; }
		if (duplicate) continue;
		if (p->nbaseVoucherExclusions >= POOL_MAX_VOUCHER_EXCLUSIONS) {
			snprintf(err, errsz, "source pool has too many voucher exclusions");
			fclose(f); return false;
		}
		int slot = p->nbaseVoucherExclusions++;
		p->baseVoucherExclusions[slot] = index;
		snprintf(p->baseVoucherExclusionKeys[slot], MAX_KEY, "%s",
				h.routeVoucherExclusions[i]);
	}
	int combinedVoucherExclusions = p->nbaseVoucherExclusions;
	for (int i = 0; i < p->nvoucherExclusions; i++) {
		int duplicate = 0;
		for (int j = 0; j < p->nbaseVoucherExclusions; j++)
			if (p->voucherExclusions[i] == p->baseVoucherExclusions[j]) {
				duplicate = 1; break;
			}
		if (!duplicate) combinedVoucherExclusions++;
	}
	if (combinedVoucherExclusions > POOL_MAX_VOUCHER_EXCLUSIONS) {
		snprintf(err, errsz,
				"source + new voucher routes exceed the %d-exclusion cumulative limit",
				POOL_MAX_VOUCHER_EXCLUSIONS);
		fclose(f); return false;
	}
	if (p->maxVoucherAnte > p->maxAnte) p->maxAnte = p->maxVoucherAnte;
	if (pool_voucher_rule_count(p)) {
		for (int i = 0; i < p->nbaseTagRules; i++) {
			const PoolTagRule *r = &p->baseTagRules[i];
			if (r->collect && (!strcmp(r->key, "tag_voucher")
					|| !strcmp(r->key, "tag_double"))) {
				snprintf(err, errsz,
						"source route collects Voucher/Double Tags, unsupported with voucher routes");
				fclose(f); return false;
			}
		}
	}
	if (p->nbaseLegendaryRules) {
		p->firstKind = 1;
		p->firstAnte = 0;
		snprintf(p->firstKey, sizeof p->firstKey, "Joker4");
	}
	int64_t size = bs_file_size(f);
	if (size < 0 || (h.encoding == BSPOOL_ENCODING_U64
			&& (uint64_t)size < (uint64_t)h.headerBytes + h.records * 8u)
			) {
		if (!err[0]) snprintf(err, errsz, "input pool size does not match its committed record count");
		fclose(f); return false;
	}
	BspoolReader liveReader;
	if (!bspool_reader_init(&liveReader, fileno(f), &h,
					(uint64_t)(size < 0 ? 0 : size), err, errsz)) {
		if (!err[0]) snprintf(err, errsz, "input pool size does not match its committed record count");
		fclose(f); return false;
	}
	if (!h.records) {
		snprintf(err, errsz, "input pool has no seed records");
		bspool_reader_destroy(&liveReader);
		fclose(f); return false;
	}
	uint64_t sourceFamilyId = h.familyId ? h.familyId
			: pool_hash_fields("family-fallback", h.catalogHash, h.criteriaHash,
					(uint64_t)h.space, h.seedspace);
	uint64_t sourceLineageId = h.lineageId ? h.lineageId
			: pool_hash_fields("lineage-fallback", sourceFamilyId, h.criteriaHash, 0, 0);
	uint64_t sourceSegmentId = h.segmentId ? h.segmentId
			: pool_hash_fields("segment", sourceLineageId, h.rangeStart, h.rangeEnd,
					(uint64_t)h.space);
	uint64_t pinnedRecords = h.records;
	uint64_t pinnedDataBytes = h.encoding == BSPOOL_ENCODING_U64
			? h.records * 8u : h.dataBytes;
	uint64_t pinnedSnapshotId = h.snapshotId;
	int pinnedComplete = h.complete, pinnedCoverageComplete = h.coverageComplete;
	char pinnedPoolId[24];
	snprintf(pinnedPoolId, sizeof pinnedPoolId, "%s", h.poolId);
	char outputState[1024];
	bool hasResumeOutput = snprintf(outputState, sizeof outputState, "%s.state", output)
			< (int)sizeof outputState && p->resume && bs_file_exists(output)
			&& bs_file_exists(outputState);
	if (hasResumeOutput) {
		FILE *priorFile = fopen(output, "rb");
		BspoolHeader prior;
		bool priorOk = priorFile && bspool_read_header(priorFile, &prior, err, errsz);
		if (priorFile) fclose(priorFile);
		if (!priorOk) {
			bspool_reader_destroy(&liveReader); fclose(f); return false;
		}
		pinnedRecords = prior.parentRecords ? prior.parentRecords : prior.sourceRecords;
		if (!pinnedRecords || pinnedRecords > h.records) {
			snprintf(err, errsz, "resumable refilter source prefix is no longer available");
			bspool_reader_destroy(&liveReader); fclose(f); return false;
		}
		if (prior.parentSegmentId && prior.parentSegmentId != sourceSegmentId) {
			snprintf(err, errsz, "resumable refilter belongs to a different source segment");
			bspool_reader_destroy(&liveReader); fclose(f); return false;
		}
		pinnedDataBytes = prior.parentDataBytes;
		if (!pinnedDataBytes && !pool_reader_prefix_bytes(&liveReader, h.encoding,
				pinnedRecords, &pinnedDataBytes)) {
			snprintf(err, errsz, "cannot recover the resumable source block boundary");
			bspool_reader_destroy(&liveReader); fclose(f); return false;
		}
		pinnedSnapshotId = prior.parentSnapshotId;
		pinnedComplete = prior.sourceComplete;
		pinnedCoverageComplete = prior.sourceCoverageComplete;
		if (prior.sourcePoolId[0])
			snprintf(pinnedPoolId, sizeof pinnedPoolId, "%s", prior.sourcePoolId);
	}
	uint64_t liveDataBytes = h.encoding == BSPOOL_ENCODING_U64 ? h.records * 8u : h.dataBytes;
	if (pinnedDataBytes > liveDataBytes) {
		snprintf(err, errsz, "resumable refilter source data prefix was truncated");
		bspool_reader_destroy(&liveReader); fclose(f); return false;
	}
	uint64_t pinnedDigest = 0;
	if (!pool_hash_fd_region(fileno(f), (uint64_t)h.headerBytes,
			pinnedDataBytes, &pinnedDigest)) {
		snprintf(err, errsz, "cannot fingerprint the committed source prefix");
		bspool_reader_destroy(&liveReader); fclose(f); return false;
	}
	uint64_t expectedSnapshotId = pool_hash_fields("snapshot", sourceSegmentId,
			pinnedRecords, pinnedDataBytes, pinnedDigest);
	if ((pinnedSnapshotId && pinnedSnapshotId != expectedSnapshotId)
			|| (pinnedRecords == h.records && h.membershipDigest
					&& h.membershipDigest != pinnedDigest)) {
		snprintf(err, errsz, "committed source prefix digest differs from the pinned snapshot");
		bspool_reader_destroy(&liveReader); fclose(f); return false;
	}
	pinnedSnapshotId = expectedSnapshotId;
	BspoolHeader pinned = h;
	pinned.records = pinnedRecords;
	pinned.dataBytes = pinnedDataBytes;
	if (pinned.encoding != BSPOOL_ENCODING_U64) pinned.complete = 0;
	bspool_reader_destroy(&liveReader);
	if (!bspool_reader_init(&p->inputReader, fileno(f), &pinned,
			(uint64_t)size, err, errsz)) {
		fclose(f); return false;
	}
	p->refilter = 1;
	p->refilterDepth = h.refilterDepth + 1;
	p->sourceCriteriaHash = h.criteriaHash;
	p->sourceRecords = pinnedRecords;
	p->sourceRangeStart = h.rangeStart;
	p->sourceRangeEnd = h.rangeEnd;
	p->sourceDataBytes = pinnedDataBytes;
	p->sourceMembershipDigest = pinnedDigest;
	p->sourceSnapshotId = pinnedSnapshotId;
	p->sourceFamilyId = sourceFamilyId;
	p->sourceSegmentId = sourceSegmentId;
	p->sourceLineageId = sourceLineageId;
	p->sourceComplete = pinnedComplete;
	p->sourceCoverageComplete = pinnedCoverageComplete;
	snprintf(p->sourcePoolId, sizeof p->sourcePoolId, "%s", pinnedPoolId);
	p->space = h.space;
	p->start = 0;
	p->count = pinnedRecords;
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
	if (c) c->soulTape = calloc(1, sizeof *c->soulTape);
	if (!c || !c->soulTape) {
		if (c) free(c->soulTape);
		free(c);
		fclose(f);
		return 1;
	}
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
		bool ok = pool_evaluate_pre(c, seed, hs, hf, label, sizeof label, NULL);
		printf("%s %d %s\n", seed, ok ? 1 : 0, ok && label[0] ? label : "-");
	}
	free(c->omenTrace);
	free(c->soulTape);
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
	bool outputIsStdout = !strcmp(output, "-");
	FILE *out = outputIsStdout ? stdout : fopen(output, "wb");
	if (!out) { fprintf(stderr, "cannot create %s: %s\n", output, strerror(errno)); bspool_reader_destroy(&reader); fclose(in); return 1; }
	uint64_t ranks[16384];
	BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
	for (uint64_t record = 0; record < records;) {
		uint64_t n = records - record;
		if (n > 16384) n = 16384;
		if (!bspool_reader_read(&reader, record, n, ranks, &scratch)) {
			fprintf(stderr, "cannot decode pool at record %" PRIu64 "\n", record);
			bspool_scratch_destroy(&scratch); bspool_reader_destroy(&reader);
			if (!outputIsStdout) fclose(out); fclose(in); return 1;
		}
		for (uint64_t i = 0; i < n; i++) {
			char seed[9];
			make_seed_in(h.space, ranks[i], seed);
			fprintf(out, "%s\n", seed);
		}
		record += n;
	}
	bspool_scratch_destroy(&scratch); bspool_reader_destroy(&reader);
	if (!outputIsStdout && fclose(out) != 0) { fprintf(stderr, "cannot close export\n"); fclose(in); return 1; }
	fclose(in);
	fprintf(stderr, "exported %" PRIu64 " seeds%s (space %s, pool_id %s%s%s)\n",
			records, complete ? " from a complete pool" : " from an incomplete pool",
			space_name(h.space), h.poolId[0] ? h.poolId : "-",
			h.label[0] ? ", label " : "", h.label);
	return 0;
}

typedef struct {
	int overrideRange;
	uint64_t rangeStart, rangeEnd;
	const char *poolId, *label;
	int mergedParts;
	uint64_t familyId, segmentId, stageHash, lineageId, derivationId;
	uint64_t snapshotId, membershipDigest, metadataDigest, scanCursor;
} PoolHeaderRewrite;

static bool pool_write_repacked_header(FILE *f, const unsigned char *original,
		int originalHeaderBytes, int outputSchema, int outputHeaderBytes,
		uint64_t records, uint64_t dataBytes, int complete, int coverageComplete,
		const PoolHeaderRewrite *rewrite, char *err, size_t errsz) {
	if ((originalHeaderBytes != BSPOOL_HEADER_SIZE
			&& originalHeaderBytes != BSPOOL_HEADER_EVENTS_SIZE)
			|| (outputHeaderBytes != BSPOOL_HEADER_SIZE
				&& outputHeaderBytes != BSPOOL_HEADER_EVENTS_SIZE)
			|| (outputSchema != BSPOOL_SCHEMA_BLOCKS
				&& outputSchema != BSPOOL_SCHEMA_EVENTS)) {
		snprintf(err, errsz, "unsupported repacked pool format"); return false;
	}
	unsigned char out[BSPOOL_HEADER_EVENTS_SIZE];
	char work[BSPOOL_HEADER_EVENTS_SIZE + 1];
	memcpy(work, original, (size_t)originalHeaderBytes); work[originalHeaderBytes] = 0;
	memset(out, 0, (size_t)outputHeaderBytes);
	size_t used = 0;
	char *line = work;
	while (line && *line) {
		char *nl = strchr(line, '\n');
		if (nl) *nl = 0;
		char copy[BSPOOL_HEADER_EVENTS_SIZE + 1];
		snprintf(copy, sizeof copy, "%s", line);
		char *sp = copy;
		char *d = pool_tok(&sp);
		if (!d || !strcmp(d, "end")) break;
		const char *replacement = NULL;
		char generated[128];
		if (!strcmp(d, "BRAINSTORM_SEED_POOL")) {
			snprintf(generated, sizeof generated,
					"BRAINSTORM_SEED_POOL %d\nheader_bytes %d", outputSchema, outputHeaderBytes);
			replacement = generated;
		} else if (!strcmp(d, "encoding")) replacement = outputSchema == BSPOOL_SCHEMA_EVENTS
				? "encoding delta-varint-events-v1" : "encoding delta-varint-blocks-v1";
		else if (!strcmp(d, "records") || !strcmp(d, "data_bytes") || !strcmp(d, "complete")
				|| !strcmp(d, "coverage_complete") || !strcmp(d, "header_bytes")) replacement = NULL;
		else if (rewrite && rewrite->overrideRange
				&& (!strcmp(d, "range_start") || !strcmp(d, "range_end")
					|| !strcmp(d, "pool_id") || !strcmp(d, "label")
					|| !strcmp(d, "merged_parts") || !strcmp(d, "family_id")
					|| !strcmp(d, "segment_id") || !strcmp(d, "stage_hash")
					|| !strcmp(d, "lineage_id") || !strcmp(d, "derivation_id")
					|| !strcmp(d, "snapshot_id") || !strcmp(d, "membership_digest")
					|| !strcmp(d, "metadata_digest") || !strcmp(d, "scan_cursor")
					|| !strcmp(d, "input_cursor") || !strcmp(d, "parent_snapshot_id")
					|| !strcmp(d, "parent_segment_id") || !strcmp(d, "parent_records")
					|| !strcmp(d, "parent_data_bytes")
					|| !strcmp(d, "parent_coverage_complete")
					|| !strcmp(d, "input_record_start") || !strcmp(d, "input_record_end")
					|| !strcmp(d, "shard_index") || !strcmp(d, "shard_total"))) replacement = NULL;
		else replacement = line;
		if (replacement) {
			size_t n = strlen(replacement);
			if (n + 1 > (size_t)outputHeaderBytes - used) { snprintf(err, errsz, "converted pool header overflow"); return false; }
			memcpy(out + used, replacement, n); used += n; out[used++] = '\n';
		}
		line = nl ? nl + 1 : NULL;
	}
	if (rewrite && rewrite->overrideRange) {
		char merged[768];
		int m = snprintf(merged, sizeof merged,
				"range_start %" PRIu64 "\nrange_end %" PRIu64 "\npool_id %s\nlabel %s\nmerged_parts %d\n"
				"family_id %016" PRIx64 "\nsegment_id %016" PRIx64 "\nstage_hash %016" PRIx64 "\n"
				"lineage_id %016" PRIx64 "\nderivation_id %016" PRIx64 "\nsnapshot_id %016" PRIx64 "\n"
				"membership_digest %016" PRIx64 "\nmetadata_digest %016" PRIx64 "\nscan_cursor %" PRIu64 "\n",
				rewrite->rangeStart, rewrite->rangeEnd,
				rewrite->poolId ? rewrite->poolId : "-",
				rewrite->label ? rewrite->label : "merged-pool", rewrite->mergedParts,
				rewrite->familyId, rewrite->segmentId, rewrite->stageHash,
				rewrite->lineageId, rewrite->derivationId, rewrite->snapshotId,
				rewrite->membershipDigest, rewrite->metadataDigest, rewrite->scanCursor);
		if (m < 0 || (size_t)m > (size_t)outputHeaderBytes - used) { snprintf(err, errsz, "merged pool header overflow"); return false; }
		memcpy(out + used, merged, (size_t)m); used += (size_t)m;
	}
	char tail[224];
	int n = snprintf(tail, sizeof tail,
			"records %" PRIu64 "\ndata_bytes %" PRIu64 "\ncomplete %d\ncoverage_complete %d\nend\n",
			records, dataBytes, complete, coverageComplete);
	if (n < 0 || (size_t)n > (size_t)outputHeaderBytes - used) { snprintf(err, errsz, "converted pool header overflow"); return false; }
	memcpy(out + used, tail, (size_t)n);
	int64_t at = bs_ftello(f);
	if (bs_fseeko(f, 0, SEEK_SET) != 0
			|| fwrite(out, 1, (size_t)outputHeaderBytes, f) != (size_t)outputHeaderBytes
			|| fflush(f) != 0 || bs_fsync_file(f) != 0 || bs_fseeko(f, at, SEEK_SET) != 0) {
		snprintf(err, errsz, "cannot update pool header: %s", strerror(errno)); return false;
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
	if (h.encoding == BSPOOL_ENCODING_DELTA_BLOCKS
			|| h.encoding == BSPOOL_ENCODING_DELTA_EVENTS) {
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
			|| !pool_write_repacked_header(out, original, POOL_HEADER_SIZE,
					BSPOOL_SCHEMA_BLOCKS, POOL_HEADER_SIZE,
					h.records, 0, 0, 0, NULL, err, sizeof err)) {
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
		if (dataEnd < POOL_HEADER_SIZE || !pool_append_index(out, BSPOOL_SCHEMA_BLOCKS,
				POOL_HEADER_SIZE, h.space, h.records,
				(uint64_t)dataEnd - POOL_HEADER_SIZE, 0, 0,
				&finalBytes, err, sizeof err)
				|| !pool_write_repacked_header(out, original, POOL_HEADER_SIZE,
						BSPOOL_SCHEMA_BLOCKS, POOL_HEADER_SIZE, h.records,
						(uint64_t)dataEnd - POOL_HEADER_SIZE, 1, h.coverageComplete,
						NULL, err, sizeof err)) {
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

typedef struct {
	const char *path;
	FILE *file;
	BspoolHeader header;
	BspoolReader reader;
	uint64_t fileBytes;
} PoolMergePart;

static int pool_merge_part_compare(const void *a, const void *b) {
	const PoolMergePart *x = a, *y = b;
	return x->header.rangeStart < y->header.rangeStart ? -1
			: x->header.rangeStart > y->header.rangeStart;
}

static void pool_compute_merged_id(const BspoolHeader *h, uint64_t rangeStart,
		uint64_t rangeEnd, uint64_t records, char out[24]) {
	char buf[192];
	int n = snprintf(buf, sizeof buf, "%016" PRIx64 "%016" PRIx64 "%" PRIu64 "-%" PRIu64 "%s%" PRIu64 "1",
			h->catalogHash, h->criteriaHash, rangeStart, rangeEnd,
			space_name(h->space), records);
	uint64_t hash = pool_hash_update(UINT64_C(1469598103934665603), buf, (size_t)n);
	snprintf(out, 24, "%016" PRIx64, hash);
}

static void pool_output_label(const char *path, char out[136]) {
	const char *base = path;
	for (const char *p = path; *p; p++) if (*p == '/' || *p == '\\') base = p + 1;
	size_t n = strlen(base);
	if (n > 7 && !strcmp(base + n - 7, ".bspool")) n -= 7;
	if (n >= 136) n = 135;
	for (size_t i = 0; i < n; i++) {
		unsigned char c = (unsigned char)base[i];
		out[i] = (isalnum(c) || c == '.' || c == '_' || c == '+' || c == '-') ? (char)c : '-';
	}
	out[n] = 0;
	if (!out[0]) snprintf(out, 136, "merged-pool");
}

static bool pool_merge_write_part(FILE *out, PoolMergePart *part,
		uint64_t *written, uint64_t *membershipDigest, uint64_t *metadataDigest,
		char *err, size_t errsz) {
	BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
	if (part->header.encoding == BSPOOL_ENCODING_DELTA_BLOCKS
			|| part->header.encoding == BSPOOL_ENCODING_DELTA_EVENTS) {
		for (uint64_t b = 0; b < part->reader.nblocks; b++) {
			const BspoolBlockIndex *e = &part->reader.blocks[b];
			if (!bspool_decode_block(&part->reader, b, &scratch)) {
				snprintf(err, errsz, "cannot decode or verify %s block %" PRIu64, part->path, b); goto fail;
			}
			for (uint32_t i = 0; i < e->count; i++) if (scratch.ranks[i] < part->header.rangeStart
					|| scratch.ranks[i] >= part->header.rangeEnd) {
				snprintf(err, errsz, "%s contains a rank outside its declared shard range", part->path); goto fail;
			}
			unsigned char header[BSPOOL3_BLOCK_HEADER_SIZE];
			size_t headerBytes = part->header.encoding == BSPOOL_ENCODING_DELTA_EVENTS
					? BSPOOL3_BLOCK_HEADER_SIZE : BSPOOL_BLOCK_HEADER_SIZE;
			if (bs_pread(part->reader.fd, header, headerBytes, (int64_t)e->offset) != (int64_t)headerBytes
					|| fwrite(header, 1, headerBytes, out) != headerBytes
					|| (e->payloadBytes && fwrite(scratch.bytes, 1, e->payloadBytes, out) != e->payloadBytes)) {
				snprintf(err, errsz, "cannot copy verified block from %s", part->path); goto fail;
			}
			*membershipDigest = pool_hash_update(*membershipDigest, header, headerBytes);
			*membershipDigest = pool_hash_update(*membershipDigest, scratch.bytes, e->payloadBytes);
			if (part->header.encoding == BSPOOL_ENCODING_DELTA_EVENTS)
				*metadataDigest = pool_hash_update(*metadataDigest,
						scratch.bytes + e->rankBytes, e->metadataBytes);
			*written += e->count;
		}
	} else {
		uint64_t ranks[POOL_OUTPUT_BUFFER / 8];
		unsigned char encoded[POOL_OUTPUT_BUFFER], header[BSPOOL_BLOCK_HEADER_SIZE];
		for (uint64_t record = 0; record < part->header.records;) {
			uint64_t n = part->header.records - record;
			if (n > POOL_OUTPUT_BUFFER / 8) n = POOL_OUTPUT_BUFFER / 8;
			if (!bspool_reader_read(&part->reader, record, n, ranks, &scratch)) {
				snprintf(err, errsz, "cannot decode %s at record %" PRIu64, part->path, record); goto fail;
			}
			for (uint64_t i = 0; i < n; i++) if (ranks[i] < part->header.rangeStart
					|| ranks[i] >= part->header.rangeEnd) {
				snprintf(err, errsz, "%s contains a rank outside its declared shard range", part->path); goto fail;
			}
			size_t payload = pool_encode_rank_block((unsigned char *)ranks, (uint32_t)n, encoded, header);
			if (fwrite(header, 1, sizeof header, out) != sizeof header
					|| fwrite(encoded, 1, payload, out) != payload) {
				snprintf(err, errsz, "cannot write merged output"); goto fail;
			}
			*membershipDigest = pool_hash_update(*membershipDigest, header, sizeof header);
			*membershipDigest = pool_hash_update(*membershipDigest, encoded, payload);
			record += n; *written += n;
		}
	}
	bspool_scratch_destroy(&scratch);
	return true;
fail:
	bspool_scratch_destroy(&scratch);
	return false;
}

static bool pool_write_merge_manifest(const char *output, const PoolMergePart *parts,
		int ninputs, int leafParts, uint64_t rangeStart, uint64_t rangeEnd, uint64_t records,
		uint64_t fileBytes, int schema, const char *poolId, const char *label) {
	char path[1024];
	if (snprintf(path, sizeof path, "%s.manifest", output) >= (int)sizeof path) return false;
	FILE *f = fopen(path, "w");
	if (!f) return false;
	fprintf(f, "BRAINSTORM_SEED_POOL_MERGE %d\nmodelver %d\nencoding %s\n",
			schema, parts[0].header.modelver, schema == BSPOOL_SCHEMA_EVENTS
					? "delta-varint-events-v1" : "delta-varint-blocks-v1");
	fprintf(f, "catalog_hash %016" PRIx64 "\ncriteria_hash %016" PRIx64 "\npool_id %s\nlabel %s\n",
			parts[0].header.catalogHash, parts[0].header.criteriaHash, poolId, label);
	fprintf(f, "space %s\nseedspace %" PRIu64 "\nrange_start %" PRIu64
			"\nrange_end %" PRIu64 "\nrecords %" PRIu64 "\nmerged_parts %d\nfile_bytes %" PRIu64 "\n",
			space_name(parts[0].header.space), parts[0].header.seedspace,
			rangeStart, rangeEnd, records, leafParts, fileBytes);
	for (int i = 0; i < ninputs; i++) fprintf(f,
			"input %s %" PRIu64 " %" PRIu64 " %" PRIu64 " %s\n",
			parts[i].header.poolId[0] ? parts[i].header.poolId : "-",
			parts[i].header.rangeStart, parts[i].header.rangeEnd,
			parts[i].header.records, parts[i].path);
	fprintf(f, "complete 1\ncoverage_complete 1\nend\n");
	return fclose(f) == 0;
}

static int pool_mode_merge(const char *output, int ninputs, char **inputs) {
	if (ninputs < 2) { fprintf(stderr, "merge needs at least two input pools\n"); return 2; }
	if (bs_file_exists(output)) { fprintf(stderr, "output already exists; choose a new filename\n"); return 1; }
	PoolMergePart *parts = calloc((size_t)ninputs, sizeof *parts);
	if (!parts) { fprintf(stderr, "cannot allocate merge input table\n"); return 1; }
	char err[256] = "";
	int opened = 0, rc = 1;
	uint64_t totalRecords = 0;
	for (int i = 0; i < ninputs; i++) {
		PoolMergePart *p = &parts[i]; p->path = inputs[i];
		p->file = fopen(p->path, "rb");
		if (!p->file) { snprintf(err, sizeof err, "cannot open %s: %s", p->path, strerror(errno)); goto done; }
		opened++;
		if (!bspool_read_header(p->file, &p->header, err, sizeof err)) goto done;
		if (!p->header.complete) { snprintf(err, sizeof err, "%s is incomplete", p->path); goto done; }
		if (!p->header.coverageComplete) {
			snprintf(err, sizeof err,
					"%s is a provisional derivative; lineage-aware provisional merging is not available yet",
					p->path);
			goto done;
		}
		if (p->header.rangeStart >= p->header.rangeEnd || p->header.rangeEnd > p->header.seedspace) {
			snprintf(err, sizeof err, "%s has an invalid shard range", p->path); goto done;
		}
		int64_t bytes = bs_file_size(p->file);
		if (bytes < 0 || (p->header.encoding == BSPOOL_ENCODING_U64
				&& (uint64_t)bytes != (uint64_t)p->header.headerBytes + p->header.records * 8u)
				|| !bspool_reader_init(&p->reader, fileno(p->file), &p->header,
						(uint64_t)(bytes < 0 ? 0 : bytes), err, sizeof err)) goto done;
		p->fileBytes = (uint64_t)bytes;
		if (UINT64_MAX - totalRecords < p->header.records) { snprintf(err, sizeof err, "merged record count overflows"); goto done; }
		totalRecords += p->header.records;
	}
	qsort(parts, (size_t)ninputs, sizeof *parts, pool_merge_part_compare);
	int outputSchema = parts[0].header.encoding == BSPOOL_ENCODING_DELTA_EVENTS
			? BSPOOL_SCHEMA_EVENTS : BSPOOL_SCHEMA_BLOCKS;
	int outputHeaderBytes = outputSchema == BSPOOL_SCHEMA_EVENTS
			? BSPOOL_HEADER_EVENTS_SIZE : BSPOOL_HEADER_SIZE;
	for (int i = 1; i < ninputs; i++) {
		const BspoolHeader *a = &parts[0].header, *b = &parts[i].header;
		if (b->modelver != a->modelver || b->catalogHash != a->catalogHash
				|| b->criteriaHash != a->criteriaHash || b->space != a->space
				|| b->seedspace != a->seedspace || b->route != a->route
				|| (a->familyId && b->familyId && a->familyId != b->familyId)
				|| (a->stageHash && b->stageHash && a->stageHash != b->stageHash)
				|| (a->lineageId && b->lineageId && a->lineageId != b->lineageId)) {
			snprintf(err, sizeof err, "%s is not compatible with %s (model, profile, space, or criteria differ)",
					parts[i].path, parts[0].path); goto done;
		}
		if (parts[i - 1].header.rangeEnd != b->rangeStart) {
			snprintf(err, sizeof err, "shard ranges are not contiguous: %s ends at %" PRIu64
					" but %s starts at %" PRIu64,
					parts[i - 1].path, parts[i - 1].header.rangeEnd, parts[i].path, b->rangeStart);
			goto done;
		}
		if ((b->encoding == BSPOOL_ENCODING_DELTA_EVENTS)
				!= (outputSchema == BSPOOL_SCHEMA_EVENTS)) {
			snprintf(err, sizeof err,
					"%s cannot be merged with %s because their metadata schemas differ",
					parts[i].path, parts[0].path);
			goto done;
		}
	}
	unsigned char original[BSPOOL_HEADER_EVENTS_SIZE];
	if (parts[0].header.headerBytes != outputHeaderBytes
			|| bs_pread(fileno(parts[0].file), original, (size_t)outputHeaderBytes, 0)
				!= (int64_t)outputHeaderBytes) {
		snprintf(err, sizeof err, "cannot preserve source pool header"); goto done;
	}
	uint64_t rangeStart = parts[0].header.rangeStart;
	uint64_t rangeEnd = parts[ninputs - 1].header.rangeEnd;
	char poolId[24], label[136];
	pool_compute_merged_id(&parts[0].header, rangeStart, rangeEnd, totalRecords, poolId);
	pool_output_label(output, label);
	int mergedParts = 0;
	for (int i = 0; i < ninputs; i++) {
		int leaves = parts[i].header.mergedParts > 0 ? parts[i].header.mergedParts : 1;
		if (leaves > INT_MAX - mergedParts) {
			snprintf(err, sizeof err, "merged leaf-part count overflows"); goto done;
		}
		mergedParts += leaves;
	}
	FILE *out = fopen(output, "w+b");
	if (!out) { snprintf(err, sizeof err, "cannot create %s: %s", output, strerror(errno)); goto done; }
	uint64_t familyId = parts[0].header.familyId ? parts[0].header.familyId
			: pool_hash_fields("family-fallback", parts[0].header.catalogHash,
					parts[0].header.criteriaHash, (uint64_t)parts[0].header.space,
					parts[0].header.seedspace);
	uint64_t stageHash = parts[0].header.stageHash ? parts[0].header.stageHash
			: parts[0].header.criteriaHash;
	uint64_t lineageId = parts[0].header.lineageId ? parts[0].header.lineageId
			: pool_hash_fields("lineage-fallback", familyId,
					parts[0].header.criteriaHash, 0, 0);
	uint64_t segmentId = pool_hash_fields("segment", lineageId, rangeStart,
			rangeEnd, (uint64_t)parts[0].header.space);
	PoolHeaderRewrite rewrite = {
		.overrideRange = 1, .rangeStart = rangeStart, .rangeEnd = rangeEnd,
		.poolId = poolId, .label = label, .mergedParts = mergedParts,
		.familyId = familyId, .segmentId = segmentId, .stageHash = stageHash,
		.lineageId = lineageId,
		.derivationId = pool_hash_fields("derive-merge", lineageId, segmentId,
				(uint64_t)mergedParts, totalRecords),
		.membershipDigest = POOL_HASH_INIT,
		.metadataDigest = outputSchema == BSPOOL_SCHEMA_EVENTS ? POOL_HASH_INIT : 0,
		.scanCursor = rangeEnd,
	};
	rewrite.snapshotId = pool_hash_fields("snapshot", segmentId, totalRecords, 0,
			rewrite.membershipDigest);
	if (bs_fseeko(out, outputHeaderBytes, SEEK_SET) != 0
			|| !pool_write_repacked_header(out, original, outputHeaderBytes,
					outputSchema, outputHeaderBytes, totalRecords, 0, 0, 0,
					&rewrite, err, sizeof err)) { fclose(out); remove(output); goto done; }
	uint64_t written = 0;
	uint64_t membershipDigest = POOL_HASH_INIT;
	uint64_t metadataDigest = outputSchema == BSPOOL_SCHEMA_EVENTS ? POOL_HASH_INIT : 0;
	for (int i = 0; i < ninputs; i++) {
		if (!pool_merge_write_part(out, &parts[i], &written,
				&membershipDigest, &metadataDigest, err, sizeof err)) {
			fclose(out); remove(output); goto done;
		}
		fprintf(stderr, "merged=%d/%d records=%" PRIu64 "/%" PRIu64 "\n",
				i + 1, ninputs, written, totalRecords);
	}
	int64_t dataEnd = bs_ftello(out);
	uint64_t finalBytes = 0;
	uint64_t mergedDataBytes = dataEnd >= outputHeaderBytes
			? (uint64_t)dataEnd - (uint64_t)outputHeaderBytes : 0;
	rewrite.membershipDigest = membershipDigest;
	rewrite.metadataDigest = metadataDigest;
	rewrite.snapshotId = pool_hash_fields("snapshot", segmentId, totalRecords,
			mergedDataBytes, membershipDigest);
	/* Do not allow short-circuiting to skip fclose: Windows cannot remove a
	 * failed output while it is open. */
	bool finalOk = written == totalRecords && dataEnd >= outputHeaderBytes;
	if (finalOk) finalOk = pool_append_index(out, outputSchema,
			outputHeaderBytes, parts[0].header.space, totalRecords,
			mergedDataBytes, membershipDigest, metadataDigest,
			&finalBytes, err, sizeof err);
	if (finalOk) finalOk = pool_write_repacked_header(out, original, outputHeaderBytes,
			outputSchema, outputHeaderBytes, totalRecords,
			(uint64_t)dataEnd - (uint64_t)outputHeaderBytes, 1, 1,
			&rewrite, err, sizeof err);
	int closeRc = fclose(out);
	if (!finalOk || closeRc != 0) {
		if (!err[0]) snprintf(err, sizeof err, "cannot finalize merged pool");
		remove(output); goto done;
	}
	if (!pool_write_merge_manifest(output, parts, ninputs, mergedParts, rangeStart, rangeEnd,
			totalRecords, finalBytes, outputSchema, poolId, label))
		fprintf(stderr, "warning: merged pool is complete but its optional manifest could not be written\n");
	fprintf(stderr, "merged %d compatible shards: range=%" PRIu64 "-%" PRIu64
			" records=%" PRIu64 " size=%.3f GB pool_id=%s\n",
			ninputs, rangeStart, rangeEnd, totalRecords, (double)finalBytes / 1e9, poolId);
	rc = 0;
done:
	if (rc && err[0]) fprintf(stderr, "merge error: %s\n", err);
	for (int i = 0; i < opened; i++) {
		bspool_reader_destroy(&parts[i].reader);
		if (parts[i].file) fclose(parts[i].file);
	}
	free(parts);
	return rc;
}

static void pool_usage(const char *prog) {
	fprintf(stderr,
			"usage:\n"
			"  %s scan <native-snapshot.cfg> <pool-criteria.cfg> <output>\n"
			"  %s refilter <native-snapshot.cfg> <pool-criteria.cfg> <input.bspool> <output.bspool>\n"
			"  %s fixture <native-snapshot.cfg> <pool-criteria.cfg> <seed-file>\n"
			"  %s export <input.bspool> <output.txt|->\n"
			"  %s convert <legacy-input.bspool> <compressed-output.bspool>\n"
			"  %s merge <output.bspool> <part1.bspool> <part2.bspool> [more parts...]\n",
			prog, prog, prog, prog, prog, prog);
}

int main(int argc, char **argv) {
	bs_platform_init();
	init_key_tables();
	if (argc == 4 && !strcmp(argv[1], "export")) return pool_mode_export(argv[2], argv[3]);
	if (argc == 4 && !strcmp(argv[1], "convert")) return pool_mode_convert(argv[2], argv[3]);
	if (argc >= 5 && !strcmp(argv[1], "merge")) return pool_mode_merge(argv[2], argc - 3, argv + 3);
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
	if (refilter && !pool_prepare_refilter(&catalog, &plan, argv[4], argv[5], &inputFile, err, sizeof err)) {
		fprintf(stderr, "input pool error: %s\n", err);
		return 1;
	}
	pool_finalize_hot_plan(&catalog, &plan);
	plan.criteriaHash = pool_hash_plan(&plan);
	pool_set_identity(&plan);
	if (!strcmp(argv[1], "scan")) return pool_mode_scan_locked(&catalog, &plan, argv[4]);
	if (refilter) {
		int rc = pool_mode_scan_locked(&catalog, &plan, argv[5]);
		bspool_reader_destroy(&plan.inputReader);
		fclose(inputFile);
		return rc;
	}
	return pool_mode_fixture(&catalog, &plan, argv[4]);
}
