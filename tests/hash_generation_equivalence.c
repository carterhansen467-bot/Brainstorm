/* Equivalence proof for the contiguous-scan fast paths: the seed odometer
 * must reproduce make_seed_in rank-for-rank, and the shared-suffix /
 * per-lane-suffix batched hashes must reproduce serial pseudohash_ks
 * bit-for-bit -- across radix carries at every digit, length transitions
 * 1..8 in the expanded spaces, and full-space rotation wraparound. */
#define BRAINSTORM_NATIVE_CORE_ONLY
#include "../native/brainstorm_native_search.c"

#include <inttypes.h>

static const char *KEYS[] = { "Tag1", "shop_pack1", "soul_Tarot1" };
#define NKEYS (sizeof KEYS / sizeof KEYS[0])

/* Walk `count` consecutive ranks from startRank exactly like the scan
 * workers do (odometer + per-lane suffix + ILV batches), comparing every
 * candidate against make_seed_in and serial pseudohash_ks. */
static int check_walk(int space, uint64_t startRank, uint64_t count,
		const char *key) {
	uint64_t size = space_size(space);
	uint64_t rank = startRank % size;
	SeedOdometer od;
	odometer_init(&od, space, rank);
	int kl = (int)strlen(key);
	double sufSeed = hash_shared_suffix(od.seed, od.len, 0);
	double sufKey = hash_shared_suffix(od.seed, od.len, kl);
	char seeds[ILV][9];
	double sufS[ILV], sufK[ILV], hs[ILV], hf[ILV];
	for (uint64_t done = 0; done < count; done += ILV) {
		for (int i = 0; i < ILV; i++) {
			char ref[9];
			int rlen = make_seed_in(space, rank, ref);
			if (rlen != od.len || strcmp(ref, od.seed)) {
				fprintf(stderr,
						"FAIL: odometer diverged space=%d rank=%" PRIu64
						" ref=%s(len %d) odometer=%s(len %d)\n",
						space, rank, ref, rlen, od.seed, od.len);
				return 0;
			}
			memcpy(seeds[i], od.seed, 9);
			sufS[i] = sufSeed;
			sufK[i] = sufKey;
			rank = (rank + 1) % size;
			if (odometer_next(&od)) {
				sufSeed = hash_shared_suffix(od.seed, od.len, 0);
				sufKey = hash_shared_suffix(od.seed, od.len, kl);
			}
		}
		batch_hash_seed_pre(sufS, seeds, hs);
		batch_hash_key_pre(key, kl, sufK, seeds, hf);
		for (int i = 0; i < ILV; i++) {
			if (hs[i] != pseudohash_ks("", seeds[i])) {
				fprintf(stderr, "FAIL: seed-hash pre mismatch space=%d seed=%s\n",
						space, seeds[i]);
				return 0;
			}
			if (hf[i] != pseudohash_ks(key, seeds[i])) {
				fprintf(stderr, "FAIL: key-hash pre mismatch space=%d key=%s seed=%s\n",
						space, key, seeds[i]);
				return 0;
			}
		}
	}
	return 1;
}

/* The pool-record path detects sharing per group instead of carrying state:
 * verify batch_hash_seed_shared / batch_hash_key_shared over the same
 * boundary ranks, and count exercised groups so the test fails loudly if the
 * fast path silently stops triggering. */
static int check_shared_groups(int space, uint64_t startRank, uint64_t count,
		const char *key, uint64_t *exercised) {
	uint64_t size = space_size(space);
	char seeds[ILV][9];
	double hs[ILV], hf[ILV];
	for (uint64_t g = 0; g + ILV <= count; g += ILV) {
		int l0 = 0, uniform = 1;
		for (int i = 0; i < ILV; i++) {
			int slen = make_seed_in(space, (startRank + g + (uint64_t)i) % size, seeds[i]);
			if (i == 0) l0 = slen;
			else if (slen != l0) uniform = 0;
		}
		if (!uniform || !batch_seeds_share_suffix(seeds, l0)) continue;
		(*exercised)++;
		batch_hash_seed_shared(seeds, l0, hs);
		batch_hash_key_shared(key, seeds, l0, hf);
		for (int i = 0; i < ILV; i++) {
			if (hs[i] != pseudohash_ks("", seeds[i])
					|| hf[i] != pseudohash_ks(key, seeds[i])) {
				fprintf(stderr, "FAIL: shared-group hash mismatch space=%d seed=%s\n",
						space, seeds[i]);
				return 0;
			}
		}
	}
	return 1;
}

static uint64_t alphabet_of(int space) {
	if (space == SPACE_TOTAL) return CHARSET_TOTAL_N;
	if (space == SPACE_SETTABLE) return CHARSET_SETTABLE_N;
	return CHARSET_N;
}

int main(void) {
	const int spaces[] = { SPACE_NATURAL, SPACE_SETTABLE, SPACE_TOTAL };
	uint64_t sharedGroups = 0;
	for (size_t si = 0; si < sizeof spaces / sizeof spaces[0]; si++) {
		int space = spaces[si];
		uint64_t size = space_size(space);
		uint64_t alphabet = alphabet_of(space);
		/* rank 0, every power-of-base carry, every length-block boundary,
		 * the full-space wrap, and arbitrary mid-space bases */
		uint64_t bases[40];
		int nbases = 0;
		bases[nbases++] = 0;
		uint64_t power = 1;
		for (int k = 1; k <= 7; k++) {
			power *= alphabet;
			if (power >= 8 && power < size) bases[nbases++] = power - 8;
		}
		if (space != SPACE_NATURAL) {
			uint64_t block = 0, blockSize = 1;
			for (int len = 1; len <= 7; len++) {
				blockSize *= alphabet;
				block += blockSize;
				if (block >= 8 && block < size) bases[nbases++] = block - 8;
			}
		}
		bases[nbases++] = size - 24; /* wraps back to rank 0 mid-walk */
		bases[nbases++] = size / 2;
		bases[nbases++] = 987654321ULL % size;
		for (int b = 0; b < nbases; b++) {
			for (size_t ki = 0; ki < NKEYS; ki++) {
				if (!check_walk(space, bases[b], 64, KEYS[ki])) return 1;
				if (!check_shared_groups(space, bases[b], 64, KEYS[ki],
						&sharedGroups)) return 1;
			}
		}
		/* long soak: hundreds of thousands of consecutive carries */
		if (!check_walk(space, 424242ULL % size, 300000, KEYS[0])) return 1;
	}
	if (sharedGroups == 0) {
		fprintf(stderr, "FAIL: shared-suffix fast path never triggered\n");
		return 1;
	}
	printf("PASS: odometer + shared/pre-suffix hashing match serial reference "
			"(all spaces, carries, lengths, wraparound; %" PRIu64
			" shared groups)\n", sharedGroups);
	return 0;
}
