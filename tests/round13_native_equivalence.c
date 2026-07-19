/* Differential proof for the native string-free round13 fast path. */
#define BRAINSTORM_NATIVE_CORE_ONLY
#include "../native/brainstorm_native_search.c"

#include <inttypes.h>

static uint64_t fuzz_state = UINT64_C(0x4d595df4d0f33173);

static uint64_t fuzz64(void) {
	uint64_t x = fuzz_state;
	x ^= x >> 12;
	x ^= x << 25;
	x ^= x >> 27;
	fuzz_state = x;
	return x * UINT64_C(2685821657736338717);
}

static double reference_round13(double x) {
	char buf[48];
	snprintf(buf, sizeof buf, "%.13f", x);
	return fabs(strtod(buf, NULL));
}

static int check_one(double x, const char *kind, uint64_t at) {
	double got = round13(x), want = reference_round13(x);
	uint64_t gb, wb;
	memcpy(&gb, &got, sizeof gb);
	memcpy(&wb, &want, sizeof wb);
	if (gb == wb) return 1;
	fprintf(stderr,
			"round13 mismatch kind=%s at=%" PRIu64 " x=%.17g got=%.17g want=%.17g\n",
			kind, at, x, got, want);
	return 0;
}

int main(int argc, char **argv) {
	uint64_t count = argc > 1 ? strtoull(argv[1], NULL, 10) : UINT64_C(5000000);
	for (uint64_t i = 0; i < count; i++) {
		/* Uniform binary64 values in [0,1), including very small exponents. */
		uint64_t bits = fuzz64() & UINT64_C(0x3fffffffffffffff);
		double x;
		memcpy(&x, &bits, sizeof x);
		if (isfinite(x) && x < 1.0 && !check_one(x, "binary", i)) return 1;

		/* Values produced by the real pseudoseed recurrence. */
		x = lua_mod1(2.134453429141
				+ (double)(fuzz64() >> 11) * 0x1p-53 * 1.72431234);
		if (!check_one(x, "recurrence", i)) return 1;

		/* Decimal half-way constructions and every adjacent binary64 value. */
		uint64_t n = fuzz64() % UINT64_C(10000000000000);
		x = ((double)n + 0.5) / 1e13;
		for (int ulp = -3; ulp <= 3; ulp++) {
			double nearbyValue = x;
			for (int step = 0; step < (ulp < 0 ? -ulp : ulp); step++)
				nearbyValue = nextafter(nearbyValue,
						ulp < 0 ? -INFINITY : INFINITY);
			if (!check_one(nearbyValue, "half",
					i * 7u + (uint64_t)(ulp + 3))) return 1;
		}
	}

	/* Chained values catch a one-bit error that redirects every later draw. */
	double got = 0.12345678901234567, want = got;
	for (uint64_t i = 0; i < count; i++) {
		got = round13(lua_mod1(2.134453429141 + got * 1.72431234));
		want = reference_round13(lua_mod1(2.134453429141 + want * 1.72431234));
		if (got != want) {
			fprintf(stderr, "round13 chain mismatch at=%" PRIu64 "\n", i);
			return 1;
		}
	}
	fprintf(stderr, "round13 native equivalence: ok (%" PRIu64 " cases per family)\n", count);
	return 0;
}
