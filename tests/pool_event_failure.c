/* Prove that a compact-event overflow allocation failure aborts the ordered
 * scan instead of silently abandoning a chunk and deadlocking writeNext. */
#define BRAINSTORM_TEST_FAIL_EVENT_OVERFLOW_ALLOC 1
#define main brainstorm_seed_pool_program_main
#include "../native/brainstorm_seed_pool.c"
#undef main

int main(void) {
	bs_platform_init();
	PoolPlan plan;
	PoolScanShared shared;
	PoolMetadata metadata;
	memset(&plan, 0, sizeof plan);
	memset(&shared, 0, sizeof shared);
	memset(&metadata, 0, sizeof metadata);
	plan.outputSchema = BSPOOL_SCHEMA_ADAPTIVE;
	shared.p = &plan;
	atomic_init(&shared.ioError, false);
	bs_mutex_init(&shared.outMutex);
	for (size_t i = 0; i < 64; i++)
		bs_cond_init(&shared.outReady[i]);
	bs_cond_init(&shared.depositRoom);
	bs_cond_init(&shared.eventEncodeReady);
	bs_cond_init(&shared.eventPipelineRoom);

	PoolChunkWriter writer = {
		.shared = &shared,
		.begin = 0,
		.end = 1,
		.ownsTurn = false,
	};
	PoolEventRun *run = pool_event_run_create(16);
	if (!run) return 2;
	metadata.count = POOL_EVENT_INLINE_OCCURRENCES + 1;
	for (uint8_t i = 0; i < metadata.count; i++) {
		metadata.occurrence[i].keyIndex = i;
		metadata.occurrence[i].kind = POOL_META_TAG;
		metadata.occurrence[i].ante = 1;
	}

	bool accepted = pool_buffer_event_hit(&writer, run, 7, &metadata);
	bool failed = atomic_load(&shared.ioError);
	pool_event_run_destroy(run);
	for (size_t i = 0; i < 64; i++)
		bs_cond_destroy(&shared.outReady[i]);
	bs_cond_destroy(&shared.depositRoom);
	bs_cond_destroy(&shared.eventEncodeReady);
	bs_cond_destroy(&shared.eventPipelineRoom);
	bs_mutex_destroy(&shared.outMutex);
	if (accepted || !failed || writer.ownsTurn) {
		fprintf(stderr,
				"event allocation failure did not abort ordered publication\n");
		return 1;
	}
	puts("PASS: event overflow allocation failure aborts ordered publication");
	return 0;
}
