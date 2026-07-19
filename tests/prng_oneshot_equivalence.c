/* Differential proof for the synthesized first-draw TW223 transform. */
#include <inttypes.h>

#define BRAINSTORM_NATIVE_CORE_ONLY
#include "../native/brainstorm_native_search.c"

static uint64_t fuzz_state = UINT64_C(0x13198a2e03707344);

static uint64_t fuzz64(void) {
	uint64_t x = fuzz_state;
	x ^= x >> 12;
	x ^= x << 25;
	x ^= x >> 27;
	fuzz_state = x;
	return x * UINT64_C(2685821657736338717);
}

int main(int argc, char **argv) {
	uint64_t count = argc > 1 ? strtoull(argv[1], NULL, 10)
			: UINT64_C(5000000);
	for (int fmaMode = 0; fmaMode <= 1; fmaMode++) {
		g_seed_fma = fmaMode;
		for (uint64_t i = 0; i < count; i++) {
			double seed = (double)(fuzz64() >> 11) * 0x1p-53;
			PRNG reference;
			lj_random_seed(&reference, seed);
			double want = lj_random(&reference);
			double got = lj_random_seed_one(seed);
			if (got != want) {
				fprintf(stderr,
						"PRNG one-shot mismatch mode=%d at=%" PRIu64
						" seed=%.17g got=%.17g want=%.17g\n",
						fmaMode, i, seed, got, want);
				return 1;
			}
			int n = (int)(fuzz64() % 96u) + 1;
			int gotN = lj_random_seed_one_n(seed, n);
			int wantN = (int)(floor(want * (double)n) + 1.0);
			if (gotN != wantN) {
				fprintf(stderr,
						"PRNG one-shot n mismatch mode=%d at=%" PRIu64
						" n=%d got=%d want=%d\n",
						fmaMode, i, n, gotN, wantN);
				return 1;
			}
			int got32 = lj_random_seed_one_n(seed, 32);
			int want32 = (int)(floor(want * 32.0) + 1.0);
			if (got32 != want32) {
				fprintf(stderr,
						"PRNG 32-way shortcut mismatch mode=%d at=%" PRIu64
						" got=%d want=%d\n",
						fmaMode, i, got32, want32);
				return 1;
			}
			if (lj_random_seed_gt_997(seed) != (want > 0.997)
					|| lj_random_seed_gt_08(seed) != (want > 0.8)) {
				fprintf(stderr,
						"PRNG threshold mismatch mode=%d at=%" PRIu64
						" value=%.17g\n", fmaMode, i, want);
				return 1;
			}
		}
		for (uint64_t group = 0; group < count / PRNG_BATCH_MAX; group++) {
			double seed[PRNG_BATCH_MAX];
			double gotDouble[PRNG_BATCH_MAX];
			int got[PRNG_BATCH_MAX], got32[PRNG_BATCH_MAX];
			int n = (int)(fuzz64() % 96u) + 1;
			for (int lane = 0; lane < PRNG_BATCH_MAX; lane++)
				seed[lane] = (double)(fuzz64() >> 11) * 0x1p-53;
			lj_random_seed_one_batch(seed, PRNG_BATCH_MAX, gotDouble);
			lj_random_seed_one_n_batch(seed, PRNG_BATCH_MAX, n, got);
			lj_random_seed_one_n_batch(seed, PRNG_BATCH_MAX, 32, got32);
			for (int lane = 0; lane < PRNG_BATCH_MAX; lane++) {
				double wantDouble = lj_random_seed_one(seed[lane]);
				int want = lj_random_seed_one_n(seed[lane], n);
				int want32 = lj_random_seed_one_n(seed[lane], 32);
				if (gotDouble[lane] != wantDouble || got[lane] != want
						|| got32[lane] != want32) {
					fprintf(stderr,
							"PRNG batch mismatch mode=%d group=%" PRIu64
							" lane=%d n=%d double=%.17g/%.17g n=%d/%d 32=%d/%d\n",
							fmaMode, group, lane, n, gotDouble[lane],
							wantDouble, got[lane], want, got32[lane], want32);
					return 1;
				}
			}
		}
	}
	fprintf(stderr, "PRNG synthesized first-draw equivalence: ok (%" PRIu64
			" seeds per FMA mode)\n", count);
	return 0;
}
