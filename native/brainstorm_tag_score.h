#ifndef BRAINSTORM_TAG_SCORE_H
#define BRAINSTORM_TAG_SCORE_H

/* Persistent, numeric-only protocol for tagcalc-1-exact. Build without
 * fast-math or floating-point contraction, like the containing pool helper.
 * No pool, snapshot, or output file is opened by this command. */
#include <stdint.h>
#include <inttypes.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include <limits.h>

#define BS_TAG_SCORE_TAGS 76
#define BS_TAG_SCORE_PAIRS (38 * 38)
#define BS_TAG_SCORE_SHOPS 120
#define BS_TAG_SCORE_NONE SIZE_MAX

typedef struct { int kind, ante, blind; } BsTagScoreTag;
typedef struct { int a, b, first, second, shop; } BsTagScorePair;
typedef struct {
	double T, r, BP, BS, M;
	size_t previous;
	int pair, shop, k, g;
} BsTagScoreState;
typedef struct { size_t *items, count, capacity; } BsTagScoreIndexes;
typedef struct {
	BsTagScoreState *items;
	size_t count, capacity;
} BsTagScoreStates;
typedef struct {
	BsTagScoreTag firstTag, secondTag;
	int first, second, shop, pair, previousPair, final, k, g, F;
	double score, T, r, M, drop;
} BsTagScoreChoice;
typedef struct {
	double score;
	int fillers[2], count;
	BsTagScoreChoice route[38];
} BsTagScoreResult;

static int bs_tag_score_reserve(void **buffer, size_t *capacity,
		size_t needed, size_t itemSize) {
	if (needed <= *capacity) return 1;
	if (needed > SIZE_MAX / itemSize) return 0;
	size_t next = *capacity ? *capacity : 16;
	while (next < needed) {
		if (next > SIZE_MAX / 2) { next = needed; break; }
		next *= 2;
	}
	if (next > SIZE_MAX / itemSize) next = needed;
	void *grown = realloc(*buffer, next * itemSize);
	if (!grown) return 0;
	*buffer = grown; *capacity = next;
	return 1;
}

static int bs_tag_score_index_add(BsTagScoreIndexes *list, size_t value) {
	if (list->count == SIZE_MAX || !bs_tag_score_reserve((void **)&list->items,
			&list->capacity, list->count + 1, sizeof *list->items)) return 0;
	list->items[list->count++] = value;
	return 1;
}

static int bs_tag_score_state_add(BsTagScoreStates *list, BsTagScoreState value,
		size_t *index) {
	if (list->count == SIZE_MAX || !bs_tag_score_reserve((void **)&list->items,
			&list->capacity, list->count + 1, sizeof *list->items)) return 0;
	*index = list->count;
	list->items[list->count++] = value;
	return 1;
}

static int bs_tag_score_index_compare(const void *left, const void *right) {
	size_t a = *(const size_t *)left, b = *(const size_t *)right;
	return a < b ? -1 : a > b;
}

static double bs_tag_score_cost(int normal) {
	double pBP = 1.0 / 9.0, pBS = 1.0 / 9.0;
	double target = normal ? pBP + pBS * (1.0 - 0.30) : pBP + pBS;
	double usable = target * (1.0 - 0.037);
	double rare = 1.0 / usable;
	double consumed = 1.0 - 0.037 - 0.003;
	double negative = consumed / usable;
	return rare + negative;
}

static double bs_tag_score_multiplier(double r, int k, int g, double cost) {
	return (1.0 - r) + (k + r + g * (1.0 - r)) / cost;
}

static BsTagScoreChoice bs_tag_score_choice(const BsTagScoreTag *tags,
		const BsTagScorePair *pair, int pairIndex, const BsTagScoreState *source,
		int final, double score, double T, double r, int k, int g, double M, int F) {
	BsTagScoreChoice choice;
	memset(&choice, 0, sizeof choice);
	choice.firstTag = tags[pair->a]; choice.secondTag = tags[pair->b];
	choice.first = pair->first; choice.second = pair->second; choice.shop = pair->shop;
	choice.pair = pairIndex; choice.previousPair = source->pair;
	choice.final = final; choice.k = k; choice.g = g; choice.F = F;
	choice.score = score; choice.T = T; choice.r = r; choice.M = M;
	choice.drop = 1.0 - source->r;
	return choice;
}

static int bs_tag_score_fixed(const BsTagScoreTag *tags, int tagCount,
		int baseline, int fillerA, int fillerB, BsTagScoreResult *result) {
	int coordinates[BS_TAG_SCORE_TAGS];
	BsTagScorePair pairs[BS_TAG_SCORE_PAIRS];
	int pairCount = 0;
	for (int i = 0; i < tagCount; i++)
		coordinates[i] = 3 * (tags[i].ante - 1 + (fillerA <= tags[i].ante)
				+ (fillerB <= tags[i].ante)) + tags[i].blind;
	for (int a = 0; a < tagCount; a++) for (int b = a + 1; b < tagCount; b++) {
		if (tags[a].kind == tags[b].kind) continue;
		if (pairCount >= BS_TAG_SCORE_PAIRS) return 0;
		pairs[pairCount++] = (BsTagScorePair){a, b, coordinates[a], coordinates[b], coordinates[b] + 1};
	}
	BsTagScoreStates states = {0};
	BsTagScoreIndexes fronts[BS_TAG_SCORE_SHOPS] = {{0}}, sources = {0};
	BsTagScoreState initial = {0};
	initial.T = 1.0; initial.r = 7.0 / 17.0;
	initial.BP = initial.T * (1.0 - initial.r); initial.BS = initial.T * initial.r;
	initial.previous = BS_TAG_SCORE_NONE; initial.pair = -1; initial.shop = baseline;
	size_t initialIndex;
	int ok = 0;
	if (!bs_tag_score_state_add(&states, initial, &initialIndex)) goto cleanup;
	double normalCost = bs_tag_score_cost(1), finalCost = bs_tag_score_cost(0);
	double best = -1.0, finalT = 0, finalM = 0;
	int priorFirst = -1, finalPair = -1, finalK = 0, finalG = 0, finalF = 0;
	size_t finalSource = BS_TAG_SCORE_NONE;
	for (int pi = 0; pi < pairCount; pi++) {
		const BsTagScorePair *pair = &pairs[pi];
		int first = pair->first, shop = pair->shop, g = pair->second - first - 1;
		int F = BS_TAG_SCORE_SHOPS - shop;
		if (shop < 0 || shop >= BS_TAG_SCORE_SHOPS || g < 0) goto cleanup;
		if (first != priorFirst) {
			sources.count = 0;
			if (initial.shop < first && !bs_tag_score_index_add(&sources, initialIndex)) goto cleanup;
			for (int end = 0; end < first && end < BS_TAG_SCORE_SHOPS; end++)
				for (size_t j = 0; j < fronts[end].count; j++)
					if (!bs_tag_score_index_add(&sources, fronts[end].items[j])) goto cleanup;
			/* Arena order is pair order, then stable creation order. This is
			 * Python's stable sort by pair_index, including ties. */
			if (sources.count > 1) qsort(sources.items, sources.count, sizeof *sources.items,
					bs_tag_score_index_compare);
			priorFirst = first;
		}
		BsTagScoreIndexes *front = &fronts[shop];
		for (size_t si = 0; si < sources.count; si++) {
			size_t sourceIndex = sources.items[si];
			/* Copy: appending a state can reallocate the arena. No pointer
			 * into that arena survives an append. Routes use stable indexes. */
			BsTagScoreState source = states.items[sourceIndex];
			int k = first - source.shop;
			double Mfinal = bs_tag_score_multiplier(source.r, k, g, finalCost);
			double Tfinal = source.T * Mfinal;
			double score = Tfinal * F;
			if (!isfinite(score)) goto cleanup;
			if (Mfinal > 0 && score > best) {
				best = score; finalSource = sourceIndex; finalPair = pi;
				finalT = Tfinal; finalM = Mfinal; finalK = k; finalG = g; finalF = F;
			}
			double M = bs_tag_score_multiplier(source.r, k, g, normalCost);
			if (M <= 0) continue;
			double T = source.T * M;
			double jOverT = (k + source.r + g * (1.0 - source.r)) / normalCost;
			double r = ((7.0 / 17.0) * jOverT) / M;
			double BP = T * (1.0 - r), BS = T * r;
			if (!isfinite(T) || !isfinite(r)) goto cleanup;
			int dominated = 0;
			for (size_t j = 0; j < front->count; j++) {
				const BsTagScoreState *old = &states.items[front->items[j]];
				if (old->BP >= BP && old->BS >= BS) { dominated = 1; break; }
			}
			if (dominated) continue;
			size_t kept = 0;
			for (size_t j = 0; j < front->count; j++) {
				const BsTagScoreState *old = &states.items[front->items[j]];
				if (old->BP > BP || old->BS > BS) front->items[kept++] = front->items[j];
			}
			front->count = kept;
			BsTagScoreState next = {T, r, BP, BS, M, sourceIndex, pi, shop, k, g};
			size_t nextIndex;
			if (!bs_tag_score_state_add(&states, next, &nextIndex)
					|| !bs_tag_score_index_add(front, nextIndex)) goto cleanup;
		}
	}
	if (best > result->score && finalSource != BS_TAG_SCORE_NONE) {
		size_t chain[38], at = finalSource;
		int count = 0;
		while (states.items[at].previous != BS_TAG_SCORE_NONE) {
			if (count >= 37) goto cleanup; /* each route consumes two distinct tags */
			chain[count++] = at; at = states.items[at].previous;
		}
		result->count = 0;
		for (int i = count - 1; i >= 0; i--) {
			const BsTagScoreState *node = &states.items[chain[i]];
			result->route[result->count++] = bs_tag_score_choice(tags, &pairs[node->pair], node->pair,
					&states.items[node->previous], 0, node->T, node->T, node->r,
					node->k, node->g, node->M, 0);
		}
		result->route[result->count++] = bs_tag_score_choice(tags, &pairs[finalPair], finalPair,
				&states.items[finalSource], 1, best, finalT, 0.5, finalK, finalG, finalM, finalF);
		result->score = best; result->fillers[0] = fillerA; result->fillers[1] = fillerB;
	}
	ok = 1;
cleanup:
	free(states.items); free(sources.items);
	for (int i = 0; i < BS_TAG_SCORE_SHOPS; i++) free(fronts[i].items);
	return ok;
}

static int bs_tag_score_optimize(const BsTagScoreTag *tags, int tagCount,
		int baselineAnte, int baselineBlind, BsTagScoreResult *result) {
	memset(result, 0, sizeof *result); result->score = -1.0;
	int first = baselineAnte + 1;
	if (first > 39) first = 39;
	int available[40] = {0}, points[39], count = 0;
	available[first] = 1;
	for (int i = 0; i < tagCount; i++)
		if (tags[i].ante >= first) available[tags[i].ante + 1] = 1;
	for (int i = first; i <= 39; i++) if (available[i]) points[count++] = i;
	result->fillers[0] = result->fillers[1] = first;
	int baseline = 3 * (baselineAnte - 1) + baselineBlind;
	for (int a = 0; a < count; a++) for (int b = a; b < count; b++)
		if (!bs_tag_score_fixed(tags, tagCount, baseline, points[a], points[b], result)) return 0;
	return 1;
}

static void bs_tag_score_print(uint64_t request, const BsTagScoreResult *result, int details) {
	static const char *kind[] = {"NEG", "RARE"};
	static const char *shortBlind[] = {"SB", "BB", "Boss"};
	static const char *longBlind[] = {"Small", "Big", "Boss"};
	printf("{\"id\":%" PRIu64 ",\"score\":%.17g,\"fillers\":[%d,%d],\"route\":[",
			request, result->score, result->fillers[0], result->fillers[1]);
	for (int i = 0; details && i < result->count; i++) {
		const BsTagScoreChoice *c = &result->route[i];
		const BsTagScoreTag *negative = c->firstTag.kind == 0 ? &c->firstTag : &c->secondTag;
		const BsTagScoreTag *rare = c->firstTag.kind == 1 ? &c->firstTag : &c->secondTag;
		printf("%s{\"prev_pair_index\":%d,\"pair_index\":%d,\"final\":%s,"
				"\"score\":%.17g,\"T_after\":%.17g,\"r_after\":%.17g,\"F\":%d,"
				"\"k\":%d,\"g\":%d,\"M\":%.17g,\"temp_drop\":%.17g,"
				"\"neg_label\":\"NEG A%d%s\",\"rare_label\":\"RARE A%d%s\","
				"\"first_desc\":\"%s A%d %s (real A%d)\","
				"\"second_desc\":\"%s A%d %s (real A%d)\","
				"\"redeem_shop_desc\":\"A%d %s shop exit\"}",
				i ? "," : "", c->previousPair, c->pair, c->final ? "true" : "false",
				c->score, c->T, c->r, c->F, c->k, c->g, c->M, c->drop,
				negative->ante, shortBlind[negative->blind], rare->ante, shortBlind[rare->blind],
				kind[c->firstTag.kind], c->first / 3 + 1, longBlind[c->first % 3], c->firstTag.ante,
				kind[c->secondTag.kind], c->second / 3 + 1, longBlind[c->second % 3], c->secondTag.ante,
				c->shop / 3 + 1, longBlind[c->shop % 3]);
	}
	fputs("]}\n", stdout);
}

static int bs_tag_score_number(const char **cursor, uint64_t *value) {
	const char *p = *cursor;
	while (*p == ' ' || *p == '\t') p++;
	if (*p < '0' || *p > '9') return 0;
	uint64_t n = 0;
	do {
		unsigned digit = (unsigned)(*p++ - '0');
		if (n > (UINT64_MAX - digit) / 10) return 0;
		n = n * 10 + digit;
	} while (*p >= '0' && *p <= '9');
	if (*p && *p != ' ' && *p != '\t' && *p != '\r' && *p != '\n') return 0;
	*cursor = p; *value = n;
	return 1;
}

static int bs_tag_score_main(void) {
	char line[4096];
	uint64_t previousRequest = 0;
	fputs("BRAINSTORM_TAG_SCORE 1\n", stdout);
	if (fflush(stdout) || ferror(stdout)) return 1;
	while (fgets(line, sizeof line, stdin)) {
		if (!strchr(line, '\n')) { fputs("score-tags: oversized or incomplete request\n", stderr); return 1; }
		const char *p = line;
		uint64_t request, ante, blind, details, count;
		if (!bs_tag_score_number(&p, &request) || request <= previousRequest
				|| !bs_tag_score_number(&p, &ante) || ante < 1 || ante > 40
				|| !bs_tag_score_number(&p, &blind) || blind > 2
				|| !bs_tag_score_number(&p, &details) || details > 1
				|| !bs_tag_score_number(&p, &count) || count > BS_TAG_SCORE_TAGS) goto malformed;
		int locations[BS_TAG_SCORE_TAGS];
		for (int i = 0; i < BS_TAG_SCORE_TAGS; i++) locations[i] = -1;
		for (uint64_t i = 0; i < count; i++) {
			uint64_t kind, tagAnte, tagBlind;
			if (!bs_tag_score_number(&p, &kind) || kind > 1
					|| !bs_tag_score_number(&p, &tagAnte) || tagAnte < 1 || tagAnte > 38
					|| !bs_tag_score_number(&p, &tagBlind) || tagBlind > 1) goto malformed;
			int slot = 2 * ((int)tagAnte - 1) + (int)tagBlind;
			if (locations[slot] >= 0 && locations[slot] != (int)kind) goto malformed;
			locations[slot] = (int)kind;
		}
		while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
		if (*p) goto malformed;
		BsTagScoreTag tags[BS_TAG_SCORE_TAGS];
		int used = 0;
		for (int i = 0; i < BS_TAG_SCORE_TAGS; i++) if (locations[i] >= 0)
			tags[used++] = (BsTagScoreTag){locations[i], i / 2 + 1, i % 2};
		BsTagScoreResult result;
		if (!bs_tag_score_optimize(tags, used, (int)ante, (int)blind, &result)) {
			fputs("score-tags: cannot compute an exact finite result\n", stderr); return 1;
		}
		bs_tag_score_print(request, &result, (int)details);
		if (fflush(stdout) || ferror(stdout)) return 1;
		previousRequest = request;
		continue;
malformed:
		fputs("score-tags: malformed numeric request\n", stderr); return 1;
	}
	return ferror(stdin) ? 1 : 0;
}

#endif
