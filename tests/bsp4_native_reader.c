#define BRAINSTORM_NATIVE_CORE_ONLY
#include "../native/brainstorm_native_search.c"

#include <inttypes.h>

static int open_reader(const char *path, FILE **fileOut, BspoolReader *reader) {
	char error[256];
	FILE *file = fopen(path, "rb");
	if (!file) {
		fprintf(stderr, "open: %s\n", strerror(errno));
		return 1;
	}
	BspoolHeader header;
	if (!bspool_read_header(file, &header, error, sizeof error)) {
		fprintf(stderr, "header: %s\n", error);
		fclose(file);
		return 1;
	}
	int64_t bytes = bs_file_size(file);
	if (bytes < 0 || !bspool_reader_init(reader, fileno(file), &header,
			(uint64_t)bytes, error, sizeof error)) {
		fprintf(stderr, "reader: %s\n", bytes < 0 ? "cannot stat pool" : error);
		fclose(file);
		return 1;
	}
	*fileOut = file;
	return 0;
}

int main(int argc, char **argv) {
	if (argc < 3) {
		fprintf(stderr,
				"usage: %s open|read|exhaust|digests POOL [LIMIT [METADATA]]\n",
				argv[0]);
		return 2;
	}
	FILE *file = NULL;
	BspoolReader reader;
	if (open_reader(argv[2], &file, &reader)) return 1;
	int status = 0;
	if (!strcmp(argv[1], "open")) {
		/* Complete BSP4 opens validate footer/index structure without walking
		 * the rank data region. Physical headers and rank CRCs are checked
		 * lazily when their block is accessed. */
	} else if (!strcmp(argv[1], "read")) {
		uint64_t ranks[257];
		BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
		for (uint64_t first = 0; first < reader.records; first += 257) {
			uint64_t count = reader.records - first;
			if (count > 257) count = 257;
			if (!bspool_reader_read(&reader, first, count, ranks, &scratch)) {
				fprintf(stderr, "rank decode failed at record %" PRIu64 "\n", first);
				status = 1;
				break;
			}
			for (uint64_t i = 0; i < count; i++)
				printf("%" PRIu64 "\n", ranks[i]);
		}
		bspool_scratch_destroy(&scratch);
	} else if (!strcmp(argv[1], "exhaust")) {
		/* Exercise the same rotated random-access pattern used by a restricted
		 * native search and prove every committed record is dealt once. */
		uint64_t rotation = reader.records ? reader.records / 3 : 0;
		uint64_t claim = 4096;
		if (argc >= 4) {
			char *end = NULL;
			errno = 0;
			claim = strtoull(argv[3], &end, 10);
			if (errno || !end || *end || !claim || claim > 65536)
				status = 2;
		}
		uint64_t *ranks = status ? NULL : malloc((size_t)claim * sizeof *ranks);
		uint64_t digest = UINT64_C(1469598103934665603);
		BspoolScratch scratch = { .cachedBlock = UINT64_MAX };
		if (!status && !ranks) status = 1;
		for (uint64_t dealt = 0; !status && dealt < reader.records;) {
			uint64_t count = reader.records - dealt;
			if (count > claim) count = claim;
			uint64_t record = rotation + dealt;
			if (record >= reader.records) record -= reader.records;
			uint64_t first = count;
			if (first > reader.records - record)
				first = reader.records - record;
			if (!bspool_reader_read(
						&reader, record, first, ranks, &scratch)
					|| (first < count && !bspool_reader_read(
						&reader, 0, count - first, ranks + first, &scratch))) {
				fprintf(stderr,
						"restricted decode failed at record %" PRIu64 "\n",
						record);
				status = 1; break;
			}
			for (uint64_t i = 0; i < count; i++) {
				unsigned char raw[8];
				bspool_put_u64le(raw, ranks[i]);
				digest = pool_hash_update(digest, raw, sizeof raw);
			}
			dealt += count;
		}
		if (!status)
			printf("%" PRIu64 " %016" PRIx64 "\n", reader.records, digest);
		free(ranks);
		bspool_scratch_destroy(&scratch);
	} else if (!strcmp(argv[1], "digests")) {
		uint64_t limit = reader.records;
		int metadata = 1;
		if (argc >= 4) {
			char *end = NULL;
			errno = 0;
			limit = strtoull(argv[3], &end, 10);
			if (errno || !end || *end) status = 2;
		}
		if (argc >= 5) metadata = atoi(argv[4]) != 0;
		uint64_t membership = 0, metadataDigest = 0;
		if (!status && !bspool_reader_recompute_digests(&reader, limit,
				metadata, &membership, metadata ? &metadataDigest : NULL)) {
			fprintf(stderr, "logical digest recomputation failed\n");
			status = 1;
		}
		if (!status) {
			printf("%016" PRIx64, membership);
			if (metadata) printf(" %016" PRIx64, metadataDigest);
			putchar('\n');
		}
	} else {
		fprintf(stderr, "unknown mode\n");
		status = 2;
	}
	bspool_reader_destroy(&reader);
	fclose(file);
	return status;
}
