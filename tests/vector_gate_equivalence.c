/* Bounded differential for the lane-parallel first gates.  Exercise the
 * production batch gate over natural ranks, then run the unchanged scalar
 * evaluator for every lane.  A rejected scalar-passing seed is a correctness
 * failure; requiring both rejects and passes keeps each case non-vacuous. */
#define BRAINSTORM_NATIVE_CORE_ONLY
#include "../native/brainstorm_native_search.c"

#include <inttypes.h>

static int check_bucket_intervals(void) {
	for (int n = 1; n <= 256; n++) {
		int undecided = 0;
		for (int hi = 0; hi < 256; hi++) {
			uint64_t first = (uint64_t)hi << 44;
			uint64_t last = (((uint64_t)hi + 1u) << 44) - 1u;
			int lo = (int)floor(ldexp((double)first, -52) * (double)n);
			int high = (int)floor(ldexp((double)last, -52) * (double)n);
			int expected = lo == high ? lo : -1;
			int got = gate_decided_bucket((uint8_t)hi, n);
			if (got != expected) {
				fprintf(stderr,
						"FAIL bucket interval n=%d hi=%d got=%d expected=%d\n",
						n, hi, got, expected);
				return 0;
			}
			if (got < 0) undecided++;
		}
		if (n == 32 && undecided != 0) {
			fprintf(stderr, "FAIL n=32 left %d high-byte intervals undecided\n",
					undecided);
			return 0;
		}
	}
	return 1;
}

static int check_config(const char *path, uint64_t count) {
	Config g;
	char err[256];
	if (!load_config(path, &g, err, sizeof err)) {
		fprintf(stderr, "FAIL load %s: %s\n", path, err);
		return 0;
	}
	if (!calibrate(&g, err, sizeof err)) {
		fprintf(stderr, "FAIL calibrate %s: %s\n", path, err);
		return 0;
	}
	if (!g.vgKind || !g.fsKey) {
		fprintf(stderr, "FAIL %s did not enable a first gate\n", path);
		return 0;
	}
	Ctx *c = calloc(1, sizeof *c);
	if (!c) return 0;
	c->g = &g;
	uint64_t accepted = 0, rejected = 0;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	uint8_t survive[ILV];
	count -= count % ILV;
	for (uint64_t rank = 0; rank < count; rank += ILV) {
		for (int lane = 0; lane < ILV; lane++)
			make_seed_in(SPACE_NATURAL, rank + (uint64_t)lane, seeds[lane]);
		batch_hash_seed_n((const char (*)[9])seeds, 8, hseed);
		batch_hash_key_n(g.fsKey, (const char (*)[9])seeds, 8, hfirst);
		first_gate_batch(&g, (const char (*)[9])seeds, 8,
				hseed, hfirst, survive);
		for (int lane = 0; lane < ILV; lane++) {
			bool scalar = passes_pre(c, seeds[lane], hseed[lane], hfirst[lane]);
			accepted += scalar;
			if (!survive[lane]) {
				rejected++;
				if (scalar) {
					fprintf(stderr,
							"FAIL gate kind %d dropped passing seed %s from %s\n",
							g.vgKind, seeds[lane], path);
					free(c->omenTrace);
					free(c);
					return 0;
				}
			}
		}
	}
	free(c->omenTrace);
	free(c);
	if (!accepted || !rejected) {
		fprintf(stderr,
				"FAIL gate kind %d was vacuous: accepted=%" PRIu64
				" rejected=%" PRIu64 "\n", g.vgKind, accepted, rejected);
		return 0;
	}
	printf("PASS gate kind %d: candidates=%" PRIu64 " accepted=%" PRIu64
			" decided-rejected=%" PRIu64 "\n",
			g.vgKind, count, accepted, rejected);
	return 1;
}

static int check_duplicate_target_disables_gate(const char *path,
		int expectedFs, const char *label) {
	Config g;
	char err[256];
	if (!load_config(path, &g, err, sizeof err)) {
		fprintf(stderr, "FAIL load duplicate-%s fixture: %s\n", label, err);
		return 0;
	}
	if (g.fsId != expectedFs || g.vgKind != 0) {
		fprintf(stderr,
				"FAIL duplicate target %s retained index-specific gate (fs=%d gate=%d)\n",
				label, g.fsId, g.vgKind);
		return 0;
	}
	printf("PASS duplicate target %s conservatively disables its index gate\n",
			label);
	return 1;
}

int main(int argc, char **argv) {
	if (argc != 8) {
		fprintf(stderr,
				"usage: %s <count> <soul.cfg> <legend.cfg> <tag.cfg> <voucher.cfg> <duplicate-tag.cfg> <duplicate-voucher.cfg>\n",
				argv[0]);
		return 2;
	}
	char *end = NULL;
	uint64_t count = strtoull(argv[1], &end, 10);
	if (!end || *end || count < ILV) return 2;
	if (!check_bucket_intervals()) return 1;
	for (int i = 2; i <= 5; i++) if (!check_config(argv[i], count)) return 1;
	if (!check_duplicate_target_disables_gate(argv[6], FS_TAG, "tag")) return 1;
	if (!check_duplicate_target_disables_gate(argv[7], FS_VOUCH, "voucher")) return 1;
	puts("PASS vector first-gate bounded differential");
	return 0;
}
