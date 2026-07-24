/* Exercise the production BSP2/BSP3 index finalizer and reader across both
 * signed-32-bit and unsigned-32-bit file boundaries without consuming
 * gigabytes of physical disk.  The payload ranges are sparse holes; this
 * regression is deliberately about 64-bit positioning and preserving every
 * committed block header while the final index is appended. */
#define main brainstorm_seed_pool_program_main
#include "../native/brainstorm_seed_pool.c"
#undef main

#ifdef _WIN32
#include <winioctl.h>
#endif

#define LARGE_OFFSET_BLOCKS 258u

static bool make_sparse(FILE *f) {
#ifdef _WIN32
	HANDLE h = (HANDLE)_get_osfhandle(_fileno(f));
	DWORD ignored = 0;
	if (h == INVALID_HANDLE_VALUE) return false;
	return DeviceIoControl(h, FSCTL_SET_SPARSE, NULL, 0, NULL, 0,
			&ignored, NULL) != 0;
#else
	(void)f;
	return true;
#endif
}

static uint64_t fixture_block_bytes(void) {
	return BSPOOL3_BLOCK_HEADER_SIZE
			+ (uint64_t)BSPOOL3_BLOCK_MAX_METADATA;
}

static uint64_t fixture_block_offset(uint64_t block) {
	return BSPOOL_HEADER_EVENTS_SIZE + block * fixture_block_bytes();
}

static bool write_fixture_block(FILE *f, uint64_t block) {
	unsigned char header[BSPOOL3_BLOCK_HEADER_SIZE] = { 0 };
	memcpy(header, "BSP3", 4);
	header[4] = BSPOOL3_BLOCK_HEADER_SIZE;
	bspool_put_u32le(header + 8, 1);
	bspool_put_u32le(header + 12, 0);
	bspool_put_u32le(
			header + 16, BSPOOL3_BLOCK_MAX_METADATA);
	bspool_put_u32le(header + 20, 0);
	bspool_put_u64le(header + 24, block * 2u);
	bspool_put_u64le(header + 32, block * 2u);
	bspool_put_u64le(header + 40, 0);
	uint64_t offset = fixture_block_offset(block);
	return offset <= (uint64_t)INT64_MAX
			&& bs_fseeko(f, (int64_t)offset, SEEK_SET) == 0
			&& fwrite(header, 1, sizeof header, f) == sizeof header;
}

static bool fixture_header_is_intact(FILE *f, uint64_t block) {
	unsigned char header[BSPOOL3_BLOCK_HEADER_SIZE];
	BspoolBlockInfo info;
	uint64_t offset = fixture_block_offset(block);
	return offset <= (uint64_t)INT64_MAX
			&& bs_fseeko(f, (int64_t)offset, SEEK_SET) == 0
			&& fread(header, 1, sizeof header, f) == sizeof header
			&& bspool_legacy_block_header_raw(
				header, sizeof header,
				BSPOOL_ENCODING_DELTA_EVENTS, &info)
			&& info.count == 1
			&& info.rankBytes == 0
			&& info.metadataBytes
				== BSPOOL3_BLOCK_MAX_METADATA
			&& info.associations == 0
			&& info.first == block * 2u
			&& info.last == block * 2u;
}

int main(int argc, char **argv) {
	if (argc != 2) {
		fprintf(stderr, "usage: %s <temporary-pool-path>\n", argv[0]);
		return 2;
	}
	bs_platform_init();
	FILE *f = fopen(argv[1], "w+b");
	if (!f) {
		fprintf(stderr, "cannot create sparse fixture: %s\n",
				strerror(errno));
		return 1;
	}
	int rc = 1;
	char err[256] = "";
	if (!make_sparse(f)) {
		fprintf(stderr, "cannot mark fixture sparse\n");
		goto done;
	}
	for (uint64_t block = 0; block < LARGE_OFFSET_BLOCKS; block++)
		if (!write_fixture_block(f, block)) {
			fprintf(stderr, "cannot write fixture block %" PRIu64 "\n",
					block);
			goto done;
		}
	uint64_t dataBytes =
			(uint64_t)LARGE_OFFSET_BLOCKS * fixture_block_bytes();
	uint64_t indexOff = BSPOOL_HEADER_EVENTS_SIZE + dataBytes;
	if (indexOff <= UINT32_MAX) {
		fprintf(stderr, "fixture does not cross the 4 GiB boundary\n");
		goto done;
	}
	if (bs_ftruncate_file(f, (int64_t)indexOff) != 0
			|| bs_fseeko(f, (int64_t)indexOff, SEEK_SET) != 0) {
		fprintf(stderr, "cannot extend sparse fixture: %s\n",
				strerror(errno));
		goto done;
	}
	uint64_t finalBytes = 0;
	if (!pool_append_index(f, BSPOOL_SCHEMA_EVENTS,
				BSPOOL_HEADER_EVENTS_SIZE, SPACE_NATURAL,
				LARGE_OFFSET_BLOCKS, dataBytes,
				UINT64_C(0x1111222233334444),
				UINT64_C(0x5555666677778888),
				&finalBytes, err, sizeof err)) {
		fprintf(stderr, "large-offset finalization failed: %s\n", err);
		goto done;
	}
	uint64_t expectedBytes = indexOff
			+ (uint64_t)LARGE_OFFSET_BLOCKS
				* BSPOOL3_INDEX_ENTRY_SIZE
			+ BSPOOL3_FOOTER_SIZE;
	if (finalBytes != expectedBytes
			|| bs_file_size(f) != (int64_t)expectedBytes) {
		fprintf(stderr, "large-offset final size differs\n");
		goto done;
	}
	/* 128 crosses INT32_MAX, 256 crosses UINT32_MAX, and 222 is close to
	 * the 3.725 GB field report that motivated this regression. */
	static const uint64_t probes[] = { 0, 128, 222, 256, 257 };
	for (size_t i = 0; i < sizeof probes / sizeof probes[0]; i++)
		if (!fixture_header_is_intact(f, probes[i])) {
			fprintf(stderr,
					"index finalization changed block %" PRIu64
					" at byte %" PRIu64 "\n",
					probes[i], fixture_block_offset(probes[i]));
			goto done;
		}
	unsigned char footer[BSPOOL3_FOOTER_SIZE];
	if (bs_fseeko(f,
				(int64_t)(finalBytes - BSPOOL3_FOOTER_SIZE),
				SEEK_SET) != 0
			|| fread(footer, 1, sizeof footer, f) != sizeof footer
			|| memcmp(footer, "BSPIDX3\n", 8)
			|| bspool_get_u64le(footer + 8) != indexOff
			|| bspool_get_u64le(footer + 16)
				!= LARGE_OFFSET_BLOCKS
			|| bspool_get_u64le(footer + 24)
				!= LARGE_OFFSET_BLOCKS
			|| bspool_get_u64le(footer + 32) != dataBytes
			|| bspool_crc64_update(0, footer, 72)
				!= bspool_get_u64le(footer + 72)) {
		fprintf(stderr, "large-offset footer differs\n");
		goto done;
	}
	BspoolHeader header;
	memset(&header, 0, sizeof header);
	header.schema = BSPOOL_SCHEMA_EVENTS;
	header.headerBytes = BSPOOL_HEADER_EVENTS_SIZE;
	header.encoding = BSPOOL_ENCODING_DELTA_EVENTS;
	header.space = SPACE_NATURAL;
	header.records = LARGE_OFFSET_BLOCKS;
	header.dataBytes = dataBytes;
	header.rangeStart = 0;
	header.rangeEnd = LARGE_OFFSET_BLOCKS * 2u + 1u;
	header.membershipDigest = UINT64_C(0x1111222233334444);
	header.metadataDigest = UINT64_C(0x5555666677778888);
	header.complete = 1;
	BspoolReader reader;
	if (!bspool_reader_init(&reader, fileno(f), &header,
				finalBytes, err, sizeof err)) {
		fprintf(stderr, "large-offset reader failed: %s\n", err);
		goto done;
	}
	PoolMergePart part;
	memset(&part, 0, sizeof part);
	part.path = argv[1];
	part.header = header;
	part.reader = reader;
	PoolBlockOrder order =
			pool_reader_block_order(&part, false, err, sizeof err);
	bspool_reader_destroy(&reader);
	if (order != POOL_BLOCK_ORDER_ASCENDING) {
		fprintf(stderr, "large-offset block verification failed: %s\n",
				err);
		goto done;
	}
	rc = 0;
	puts("PASS: BSP3 finalization preserves sparse headers beyond 2/4 GiB");
done:
	if (fclose(f) != 0 && rc == 0) rc = 1;
	remove(argv[1]);
	return rc;
}
