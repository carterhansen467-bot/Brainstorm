#include <inttypes.h>

#define BRAINSTORM_NATIVE_CORE_ONLY
#include "../native/brainstorm_native_search.c"

static int reference_pick(const Config *g, double poll) {
	double cumulative = 0.0;
	for (int i = 0; i < g->nboost; i++) {
		if (!g->boostAvail[i]) continue;
		cumulative += g->boostW[i];
		if (cumulative >= poll && cumulative - g->boostW[i] <= poll) return i;
	}
	return -1;
}

static void check_poll(const Config *g, double poll, uint64_t *checked) {
	int reference = reference_pick(g, poll);
	int fast = boost_pick_index(g, poll);
	if (fast != reference) {
		fprintf(stderr, "booster picker mismatch poll=%.17g fast=%d reference=%d\n",
				poll, fast, reference);
		exit(1);
	}
	(*checked)++;
}

static void check_fraction(const Config *g, uint64_t fraction,
		uint64_t *checked) {
	fraction &= UINT64_C(0x000fffffffffffff);
	double poll = boost_poll_from_fraction(g, fraction);
	int reference = reference_pick(g, poll);
	uint16_t want = reference >= 0 && g->boostSoul[reference]
			? (uint16_t)((g->boostSoul[reference] << 8)
					| g->boostCards[reference]) : 0;
	uint16_t got = boost_pick_soul_fraction(g, fraction);
	if (got != want) {
		fprintf(stderr,
				"booster Soul LUT mismatch fraction=%" PRIu64
				" poll=%.17g got=%u want=%u\n",
				fraction, poll, got, want);
		exit(1);
	}
	check_poll(g, poll, checked);
}

int main(int argc, char **argv) {
	const char *path = argc > 1 ? argv[1] : "native_search.cfg";
	uint64_t samples = argc > 2 ? strtoull(argv[2], NULL, 10)
			: UINT64_C(5000000);
	Config g;
	char err[256];
	if (!load_config(path, &g, err, sizeof err)) {
		fprintf(stderr, "%s\n", err);
		return 2;
	}
	uint64_t checked = 0;
	for (int i = 0; i < g.nboostAvailable; i++) {
		double values[6] = {
			g.boostAvailableLower[i],
			nextafter(g.boostAvailableLower[i], -INFINITY),
			nextafter(g.boostAvailableLower[i], INFINITY),
			g.boostAvailableCume[i],
			nextafter(g.boostAvailableCume[i], -INFINITY),
			nextafter(g.boostAvailableCume[i], INFINITY),
		};
		for (int j = 0; j < 6; j++) check_poll(&g, values[j], &checked);
	}
	const uint64_t limit = UINT64_C(1) << 52;
	for (int run = 0; run < g.nboostSoulRuns; run++) {
		uint64_t edge = g.boostSoulRunEnd[run];
		uint64_t lo = edge > 3 ? edge - 3 : 0;
		uint64_t hi = edge + 3 < limit ? edge + 3 : limit - 1;
		for (uint64_t fraction = lo; fraction <= hi; fraction++)
			check_fraction(&g, fraction, &checked);
	}
	for (unsigned bucket = 0; bucket < BOOST_SOUL_LUT_SIZE; bucket++) {
		uint64_t lo = (uint64_t)bucket << (52 - BOOST_SOUL_LUT_BITS);
		uint64_t hi = (((uint64_t)bucket + 1u)
				<< (52 - BOOST_SOUL_LUT_BITS)) - 1u;
		check_fraction(&g, lo, &checked);
		check_fraction(&g, lo + ((hi - lo) >> 1), &checked);
		check_fraction(&g, hi, &checked);
	}
	/* LuaJIT constructs random fractions from 52 mantissa bits. Sample that
	 * exact lattice densely, including both endpoints reachable by the draw. */
	uint64_t state = UINT64_C(0x6a09e667f3bcc909);
	for (uint64_t i = 0; i < samples; i++) {
		state ^= state >> 12;
		state ^= state << 25;
		state ^= state >> 27;
		uint64_t mantissa = (state * UINT64_C(2685821657736338717))
				& UINT64_C(0x000fffffffffffff);
		check_fraction(&g, mantissa, &checked);
	}
	printf("PASS: exact booster picker matches source loop across %" PRIu64
			" boundaries/lattice samples\n", checked);
	return 0;
}
