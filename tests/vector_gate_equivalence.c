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

static int check_loaded_config(Config *g, const char *label, uint64_t count) {
	if (!g->vgKind || !g->fsKey) {
		fprintf(stderr, "FAIL %s did not enable a first gate\n", label);
		return 0;
	}
	Ctx *c = calloc(1, sizeof *c);
	if (!c) return 0;
	c->g = g;
	uint64_t accepted = 0, rejected = 0;
	char seeds[ILV][9];
	double hseed[ILV], hfirst[ILV];
	uint8_t survive[ILV];
	int legendFirst[ILV];
	double legendState[ILV];
	count -= count % ILV;
	for (uint64_t rank = 0; rank < count; rank += ILV) {
		for (int lane = 0; lane < ILV; lane++)
			make_seed_in(SPACE_NATURAL, rank + (uint64_t)lane, seeds[lane]);
		batch_hash_seed_n((const char (*)[9])seeds, 8, hseed);
		batch_hash_key_n(g->fsKey, (const char (*)[9])seeds, 8, hfirst);
		first_gate_batch(g, (const char (*)[9])seeds, 8,
				hseed, hfirst, survive, legendFirst, legendState);
		for (int lane = 0; lane < ILV; lane++) {
			bool scalar = passes_pre(c, seeds[lane], hseed[lane], hfirst[lane]);
			if (g->vgKind == 2 && legendFirst[lane] >= 0) {
				bool handed = passes_pre_handoff(c, seeds[lane], hseed[lane],
						hfirst[lane], legendFirst[lane], legendState[lane]);
				if (handed != scalar) {
					fprintf(stderr,
							"FAIL legendary handoff diverged for seed %s from %s\n",
							seeds[lane], label);
					free(c->omenTrace);
					free(c);
					return 0;
				}
			}
			accepted += scalar;
			if (!survive[lane]) {
				rejected++;
				if (scalar) {
					fprintf(stderr,
							"FAIL gate kind %d dropped passing seed %s from %s\n",
							g->vgKind, seeds[lane], label);
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
				" rejected=%" PRIu64 "\n", g->vgKind, accepted, rejected);
		return 0;
	}
	printf("PASS gate kind %d: candidates=%" PRIu64 " accepted=%" PRIu64
			" decided-rejected=%" PRIu64 "\n",
			g->vgKind, count, accepted, rejected);
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
	return check_loaded_config(&g, path, count);
}

static int pack_key_is_active(const Config *g, int at) {
	for (int i = 0; i < g->npack; i++)
		if (!strcmp(g->boostKey[at], g->packKeys[i])) return 1;
	return 0;
}

static int check_pack_weight_variants(const char *path, uint64_t count) {
	Config base;
	char err[256];
	if (!load_config(path, &base, err, sizeof err)
			|| !calibrate(&base, err, sizeof err)) {
		fprintf(stderr, "FAIL prepare weighted-pack variants: %s\n", err);
		return 0;
	}
	if (count < 100000) count = 100000;
	for (int variant = 0; variant < 3; variant++) {
		Config g = base;
		g.nboostAvailable = 0;
		g.boostCume = 0.0;
		g.forceBuffoon = 0;
		for (int i = 0; i < g.nboost; i++) {
			double weight;
			int available;
			if (variant == 0) {
				weight = (double)((i * 7) % 19 + 1) / 37.0;
				available = 1;
			} else if (variant == 1) {
				weight = pack_key_is_active(&g, i) ? 50.0
						: (i % 5 == 0 ? 1000.0 : 0.000001 * (double)(i + 1));
				available = 1;
			} else {
				weight = (double)((i * 11) % 23 + 1) / 16.0;
				available = pack_key_is_active(&g, i) || i % 3 != 0;
			}
			g.boostW[i] = weight;
			g.boostAvail[i] = (uint8_t)available;
			if (available) {
				if (!strcmp(g.boostKey[i], "p_buffoon_normal_1"))
					g.forceBuffoon = 1;
				g.boostCume += weight;
				int at = g.nboostAvailable++;
				g.boostAvailableIndex[at] = i;
				g.boostAvailableLower[at] = g.boostCume - weight;
				g.boostAvailableCume[at] = g.boostCume;
			}
		}
		memset(g.vgPackMayHit, 0, sizeof g.vgPackMayHit);
		if (!config_build_pack_gate(&g)) {
			fprintf(stderr, "FAIL weighted-pack variant %d did not enable gate\n",
					variant + 1);
			return 0;
		}
		g.vgKind = 5;
		char label[64];
		snprintf(label, sizeof label, "modded pack-weight variant %d", variant + 1);
		if (!check_loaded_config(&g, label, count)) return 0;
	}
	puts("PASS modded weighted-pack catalogs preserve scalar membership");
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

static int check_forced_pack_disables_gate(const char *path) {
	Config g;
	char err[256];
	if (!load_config(path, &g, err, sizeof err)) {
		fprintf(stderr, "FAIL load forced-pack fixture: %s\n", err);
		return 0;
	}
	if (g.fsId != FS_PACK || g.vgKind != 0 || !g.forceBuffoon) {
		fprintf(stderr,
				"FAIL forced Buffoon pack retained a useless gate (fs=%d gate=%d forced=%d)\n",
				g.fsId, g.vgKind, g.forceBuffoon);
		return 0;
	}
	puts("PASS forced first-shop Buffoon match disables the pack gate");
	return 1;
}

static int check_missing_pack_rejects_all(const char *path) {
	Config g;
	char err[256];
	if (!load_config(path, &g, err, sizeof err)) {
		fprintf(stderr, "FAIL load missing-pack fixture: %s\n", err);
		return 0;
	}
	if (g.fsId != FS_PACK || g.vgKind != 5) {
		fprintf(stderr, "FAIL missing pack did not enable certain-reject gate\n");
		return 0;
	}
	for (int hi = 0; hi < 256; hi++) if (g.vgPackMayHit[hi]) {
		fprintf(stderr, "FAIL missing pack marked high byte %d as a possible hit\n", hi);
		return 0;
	}
	puts("PASS absent pack target is a certain gate rejection");
	return 1;
}

static int check_poolfile_pack_disables_gate(const char *path) {
	Config g;
	char err[256];
	if (!load_config(path, &g, err, sizeof err)) {
		fprintf(stderr, "FAIL load poolfile-pack fixture: %s\n", err);
		return 0;
	}
	if (g.fsId != FS_PACK || g.vgKind != 0 || !g.poolFile[0]) {
		fprintf(stderr,
				"FAIL route-dependent pool pack retained gate (fs=%d gate=%d pool=%d)\n",
				g.fsId, g.vgKind, !!g.poolFile[0]);
		return 0;
	}
	puts("PASS route-dependent poolfile pack remains on the scalar path");
	return 1;
}

int main(int argc, char **argv) {
	if (argc != 13) {
		fprintf(stderr,
				"usage: %s <count> <soul.cfg> <legend.cfg> <tag.cfg> <voucher.cfg> <multi-pack.cfg> <single-pack.cfg> <duplicate-tag.cfg> <duplicate-voucher.cfg> <forced-pack.cfg> <missing-pack.cfg> <poolfile-pack.cfg>\n",
				argv[0]);
		return 2;
	}
	char *end = NULL;
	uint64_t count = strtoull(argv[1], &end, 10);
	if (!end || *end || count < ILV) return 2;
	if (!check_bucket_intervals()) return 1;
	for (int i = 2; i <= 7; i++) if (!check_config(argv[i], count)) return 1;
	if (!check_pack_weight_variants(argv[6], count / 4)) return 1;
	if (!check_duplicate_target_disables_gate(argv[8], FS_TAG, "tag")) return 1;
	if (!check_duplicate_target_disables_gate(argv[9], FS_VOUCH, "voucher")) return 1;
	if (!check_forced_pack_disables_gate(argv[10])) return 1;
	if (!check_missing_pack_rejects_all(argv[11])) return 1;
	if (!check_poolfile_pack_disables_gate(argv[12])) return 1;
	puts("PASS vector first-gate bounded differential");
	return 0;
}
