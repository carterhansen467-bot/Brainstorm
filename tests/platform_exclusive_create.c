/* Focused contract test for no-overwrite pool publication.
 *
 * Build on the host:
 *   cc -O2 -Wall -Wextra -pthread -o /tmp/platform-exclusive \
 *      tests/platform_exclusive_create.c
 * Run with a path in a disposable directory:
 *   /tmp/platform-exclusive /tmp/existing-target.bspool
 */
#ifdef _WIN32
#include <stdint.h>
#include <stdio.h>

static int bs_test_open_osfhandle(intptr_t handle, int flags);
static FILE *bs_test_fdopen(int fd, const char *mode);
#define BS_PLATFORM_OPEN_OSFHANDLE bs_test_open_osfhandle
#define BS_PLATFORM_FDOPEN bs_test_fdopen
#endif

#include "../native/staged_artifact.h"

#ifdef _WIN32
enum {
	BS_TEST_FAIL_NONE,
	BS_TEST_FAIL_OPEN_OSFHANDLE,
	BS_TEST_FAIL_FDOPEN,
};

static int bs_test_failure;

static int bs_test_open_osfhandle(intptr_t handle, int flags) {
	if (bs_test_failure == BS_TEST_FAIL_OPEN_OSFHANDLE) {
		errno = EMFILE;
		return -1;
	}
	return _open_osfhandle(handle, flags);
}

static FILE *bs_test_fdopen(int fd, const char *mode) {
	if (bs_test_failure == BS_TEST_FAIL_FDOPEN) {
		errno = EMFILE;
		return NULL;
	}
	return _fdopen(fd, mode);
}

static int test_conversion_failure(const char *base, const char *suffix,
		int failure) {
	size_t pathBytes = strlen(base) + strlen(suffix) + 1;
	char *path = malloc(pathBytes);
	if (!path) return 0;
	snprintf(path, pathBytes, "%s%s", base, suffix);
	remove(path);

	bs_test_failure = failure;
	errno = 0;
	FILE *created = bs_fopen_exclusive_binary_update(path);
	int saved = errno;
	bs_test_failure = BS_TEST_FAIL_NONE;
	if (created) fclose(created);

	/* A conversion failure must close its HANDLE/fd but retain the exact
	 * CREATE_NEW file.  Opening it for update proves no exclusive handle
	 * leaked; seeing EOF first proves the placeholder was not replaced. */
	FILE *probe = fopen(path, "r+b");
	int empty = probe && fgetc(probe) == EOF;
	unsigned char marker = 0xa5;
	int writable = empty && fwrite(&marker, 1, 1, probe) == 1;
	if (probe && fclose(probe) != 0) writable = 0;
	int ok = !created && saved == EMFILE && empty && writable;
	if (!ok) {
		fprintf(stderr,
				"exclusive create did not safely retain a conversion-failure placeholder\n");
	}
	remove(path);
	free(path);
	return ok;
}
#endif

typedef struct {
	const char *path;
	unsigned char marker;
	int result;
	int error;
} ExclusiveRace;

static int read_exact(const char *path, const unsigned char *want, size_t n) {
	unsigned char got[64];
	if (n > sizeof got) return 0;
	FILE *f = fopen(path, "rb");
	if (!f) return 0;
	size_t read = fread(got, 1, n, f);
	int tail = fgetc(f);
	int closed = fclose(f);
	return read == n && tail == EOF && closed == 0
			&& memcmp(got, want, n) == 0;
}

static int test_noreplace_publish(const char *base) {
	static const unsigned char targetBytes[] = "existing publication";
	static const unsigned char stageBytes[] = "completed private stage";
	size_t targetSize = strlen(base) + sizeof ".publish";
	size_t stageSize = strlen(base) + sizeof ".publish-stage";
	char *target = malloc(targetSize);
	char *stage = malloc(stageSize);
	if (!target || !stage) {
		free(target);
		free(stage);
		return 0;
	}
	snprintf(target, targetSize, "%s.publish", base);
	snprintf(stage, stageSize, "%s.publish-stage", base);
	remove(target);
	remove(stage);
	FILE *targetFile = fopen(target, "wb");
	FILE *stageFile = fopen(stage, "wb");
	int prepared = targetFile && stageFile
			&& fwrite(targetBytes, 1, sizeof targetBytes, targetFile)
				== sizeof targetBytes
			&& fwrite(stageBytes, 1, sizeof stageBytes, stageFile)
				== sizeof stageBytes;
	if (targetFile && fclose(targetFile) != 0) prepared = 0;
	if (stageFile && fclose(stageFile) != 0) prepared = 0;

	errno = 0;
	int rejected = prepared ? bs_rename_noreplace(stage, target) : 0;
	int safe = prepared && rejected != 0 && errno == EEXIST
			&& read_exact(target, targetBytes, sizeof targetBytes)
			&& read_exact(stage, stageBytes, sizeof stageBytes);
	if (safe && remove(target) == 0)
		safe = bs_rename_noreplace(stage, target) == 0
				&& !bs_file_exists(stage)
				&& read_exact(target, stageBytes, sizeof stageBytes);
	remove(stage);
	remove(target);
	free(stage);
	free(target);
	if (!safe)
		fprintf(stderr, "no-replace staged publication changed an existing target\n");
	return safe;
}

static int test_staged_artifact_lifecycle(const char *base) {
	static const unsigned char bytes[] = "owned staged artifact";
	size_t targetSize = strlen(base) + sizeof ".artifact";
	char *target = malloc(targetSize);
	if (!target) return 0;
	snprintf(target, targetSize, "%s.artifact", base);
	remove(target);
	char err[256] = "";
	BsStagedArtifact artifact = BS_STAGED_ARTIFACT_INIT;
	int ok = bs_staged_artifact_open(
			&artifact, target, err, sizeof err);
	FILE *file = bs_staged_artifact_file(&artifact);
	ok = ok && file && fwrite(bytes, 1, sizeof bytes, file) == sizeof bytes
			&& bs_staged_artifact_publish(&artifact, err, sizeof err)
			&& !artifact.file && !artifact.stagedPath && !artifact.destination
			&& read_exact(target, bytes, sizeof bytes);

	/* The lifecycle's final publication check, not a prior existence check,
	 * protects a destination that appears while the private file is written. */
	BsStagedArtifact collision = BS_STAGED_ARTIFACT_INIT;
	ok = ok && bs_staged_artifact_open(
			&collision, target, err, sizeof err);
	file = bs_staged_artifact_file(&collision);
	errno = 0;
	ok = ok && file && fwrite(bytes, 1, sizeof bytes, file) == sizeof bytes
			&& !bs_staged_artifact_publish(&collision, err, sizeof err)
			&& errno == EEXIST
			&& read_exact(target, bytes, sizeof bytes);
	char *retained = NULL;
	if (collision.stagedPath) {
		size_t n = strlen(collision.stagedPath) + 1u;
		retained = malloc(n);
		if (retained) memcpy(retained, collision.stagedPath, n);
	}
	bs_staged_artifact_abort(&collision);
	if (retained) {
		remove(retained);
		free(retained);
	}
	remove(target);
	free(target);
	if (!ok) fprintf(stderr, "owned staged-artifact lifecycle failed: %s\n", err);
	return ok;
}

static void *race_create(void *opaque) {
	ExclusiveRace *race = (ExclusiveRace *)opaque;
	errno = 0;
	FILE *f = bs_fopen_exclusive_binary_update(race->path);
	if (!f) {
		race->result = 0;
		race->error = errno;
		return NULL;
	}
	bool ok = fwrite(&race->marker, 1, 1, f) == 1
			&& fflush(f) == 0
			&& bs_fsync_file(f) == 0;
	if (fclose(f) != 0) ok = false;
	race->result = ok ? 1 : -1;
	race->error = ok ? 0 : errno;
	return NULL;
}

int main(int argc, char **argv) {
	static const unsigned char sentinel[] = {
		0x42, 0x53, 0x50, 0x2d, 0x45, 0x58, 0x49, 0x53,
		0x54, 0x49, 0x4e, 0x47, 0x00, 0x0d, 0x0a, 0xff,
	};
	static const unsigned char created[] = {
		0x42, 0x53, 0x50, 0x2d, 0x43, 0x52, 0x45, 0x41,
		0x54, 0x45, 0x44, 0x00, 0x0d, 0x0a, 0xfe,
	};
	if (argc != 2) {
		fprintf(stderr, "usage: %s <disposable-target-path>\n", argv[0]);
		return 2;
	}
#ifdef _WIN32
	if (!test_conversion_failure(argv[1], ".fail-open",
				BS_TEST_FAIL_OPEN_OSFHANDLE)
			|| !test_conversion_failure(argv[1], ".fail-fdopen",
				BS_TEST_FAIL_FDOPEN)) {
		return 1;
	}
#endif
	size_t freshBytes = strlen(argv[1]) + sizeof ".fresh";
	char *freshPath = malloc(freshBytes);
	size_t raceBytes = strlen(argv[1]) + sizeof ".race";
	char *racePath = malloc(raceBytes);
	size_t discardBytes = strlen(argv[1]) + sizeof ".discard-owned";
	char *discardPath = malloc(discardBytes);
	if (!freshPath || !racePath || !discardPath) {
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	snprintf(freshPath, freshBytes, "%s.fresh", argv[1]);
	snprintf(racePath, raceBytes, "%s.race", argv[1]);
	snprintf(discardPath, discardBytes, "%s.discard-owned", argv[1]);

	FILE *existing = fopen(argv[1], "wb");
	bool existingOk = existing
			&& fwrite(sentinel, 1, sizeof sentinel, existing)
				== sizeof sentinel;
	if (existing && fclose(existing) != 0) existingOk = false;
	if (!existingOk) {
		fprintf(stderr, "cannot prepare pre-existing target\n");
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}

	errno = 0;
	FILE *rejected = bs_fopen_exclusive_binary_update(argv[1]);
	if (rejected || errno != EEXIST) {
		if (rejected) fclose(rejected);
		fprintf(stderr, "exclusive create accepted a pre-existing target\n");
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	if (!read_exact(argv[1], sentinel, sizeof sentinel)) {
		fprintf(stderr, "exclusive create changed a pre-existing target\n");
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	if (!test_noreplace_publish(argv[1])) {
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	if (!test_staged_artifact_lifecycle(argv[1])) {
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}

	remove(discardPath);
	FILE *discard = bs_fopen_exclusive_binary_update(discardPath);
	bool discardOk = discard
			&& fwrite(created, 1, sizeof created, discard) == sizeof created;
	if (discard) {
		if (bs_fclose_discard_owned(discard, discardPath) != 0)
			discardOk = false;
	}
#ifdef _WIN32
	discardOk = discardOk && !bs_file_exists(discardPath);
#else
	discardOk = discardOk && read_exact(
			discardPath, created, sizeof created);
#endif
	if (!discardOk) {
		remove(discardPath);
		fprintf(stderr, "owned failure cleanup violated its safe platform policy\n");
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	remove(discardPath);

	FILE *fresh = bs_fopen_exclusive_binary_update(freshPath);
	bool freshOk = fresh
			&& fwrite(created, 1, sizeof created, fresh) == sizeof created
			&& fflush(fresh) == 0
			&& bs_fsync_file(fresh) == 0;
	if (fresh && fclose(fresh) != 0) freshOk = false;
	if (!freshOk) {
		fprintf(stderr, "exclusive create could not publish a fresh target\n");
		remove(freshPath);
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	errno = 0;
	rejected = bs_fopen_exclusive_binary_update(freshPath);
	if (rejected || errno != EEXIST
			|| !read_exact(freshPath, created, sizeof created)) {
		if (rejected) fclose(rejected);
		fprintf(stderr, "second exclusive create changed its target\n");
		remove(freshPath);
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}

	ExclusiveRace races[2] = {
		{ .path = racePath, .marker = 0x31 },
		{ .path = racePath, .marker = 0x32 },
	};
	bs_thread_t threads[2];
	int createdThreads = 0;
	if (bs_thread_create(&threads[0], race_create, &races[0]) == 0) {
		createdThreads = 1;
		if (bs_thread_create(&threads[1], race_create, &races[1]) == 0)
			createdThreads = 2;
	}
	if (createdThreads != 2) {
		fprintf(stderr, "cannot create exclusive-create race workers\n");
		if (createdThreads == 1) bs_thread_join(threads[0]);
		remove(racePath);
		remove(freshPath);
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}
	bs_thread_join(threads[0]);
	bs_thread_join(threads[1]);
	int winner = races[0].result == 1 ? 0
			: races[1].result == 1 ? 1 : -1;
	int loser = winner == 0 ? 1 : 0;
	if (winner < 0 || races[loser].result != 0
			|| races[loser].error != EEXIST
			|| !read_exact(racePath, &races[winner].marker, 1)) {
		fprintf(stderr, "concurrent exclusive creates did not choose one winner\n");
		remove(racePath);
		remove(freshPath);
		remove(argv[1]);
		free(freshPath);
		free(racePath);
		free(discardPath);
		return 1;
	}

	remove(racePath);
	remove(freshPath);
	remove(argv[1]);
	free(freshPath);
	free(racePath);
	free(discardPath);
	puts("platform exclusive-create regression: ok");
	return 0;
}
