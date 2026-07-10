/* Compare the generalized pool engine with the established Model-3 filter
 * path wherever their semantics overlap: one collected tag anywhere A1-A8
 * plus the first Soul yielding a legendary anywhere A1-A8. */
#define main brainstorm_seed_pool_cli_main
#include "../native/brainstorm_seed_pool.c"
#undef main

int main(int argc, char **argv) {
	if (argc != 4) {
		fprintf(stderr, "usage: %s <native-snapshot.cfg> <criteria.cfg> <seed-count>\n", argv[0]);
		return 2;
	}
	Config catalog, legacy;
	PoolPlan plan;
	char err[256] = "";
	if (!load_config(argv[1], &catalog, err, sizeof err)) {
		fprintf(stderr, "snapshot: %s\n", err); return 1;
	}
	if (!pool_load_plan(argv[2], &catalog, &plan, err, sizeof err)) {
		fprintf(stderr, "criteria: %s\n", err); return 1;
	}
	if (!calibrate(&catalog, err, sizeof err)) {
		fprintf(stderr, "calibration: %s\n", err); return 1;
	}
	if (plan.ntagRules != 1 || plan.tagRules[0].minAnte != 1
			|| plan.tagRules[0].maxAnte != 8 || plan.tagRules[0].minCount != 1
			|| !plan.collectTags || !plan.legendary.used
			|| plan.legendary.minAnte != 1 || plan.legendary.maxAnte != 8) {
		fprintf(stderr, "compat test requires one collected tag and one first-Soul legendary, both A1-A8\n");
		return 2;
	}

	legacy = catalog;
	legacy.soulCount = 0;
	snprintf(legacy.legendary, sizeof legacy.legendary, "%s", plan.legendary.key);
	legacy.negLegendary = plan.legendary.requireNegative;
	legacy.legAnywhere = 1;
	snprintf(legacy.tag, sizeof legacy.tag, "%s", plan.tagRules[0].key);
	legacy.tagAnywhere = 1;
	legacy.voucher[0] = 0;
	legacy.npack = 0;
	legacy.ntargets = 0;
	legacy.anyAnteActive = 0;

	Ctx oldctx = { 0 };
	oldctx.g = &legacy;
	PoolCtx newctx = { 0 };
	newctx.g = &catalog;
	newctx.p = &plan;
	uint64_t count = strtoull(argv[3], NULL, 10);
	for (uint64_t rank = 0; rank < count; rank++) {
		char seed[9], label[POOL_LABEL];
		make_seed(rank, seed);
		memcpy(oldctx.seed, seed, sizeof seed);
		bool oldok = passes(&oldctx);
		bool newok = pool_evaluate_pre(&newctx, seed,
				pseudohash_ks("", seed), pseudohash_ks(plan.firstKey, seed),
				label, sizeof label);
		if (oldok != newok) {
			fprintf(stderr, "mismatch rank=%" PRIu64 " seed=%s legacy=%d pool=%d label=%s\n",
					rank, seed, oldok, newok, label);
			return 1;
		}
	}
	printf("PASS: pool/Model-3 compatibility across %" PRIu64 " seeds\n", count);
	return 0;
}
