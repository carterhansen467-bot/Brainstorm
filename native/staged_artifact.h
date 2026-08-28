/* Owned lifecycle for one native no-overwrite staged publication.
 *
 * Callers receive only the writable stream. This module owns the private
 * pathname, durability barrier, close, exclusive publication, and the safe
 * platform-specific failure policy until publish or abort completes.
 */
#ifndef BRAINSTORM_STAGED_ARTIFACT_H
#define BRAINSTORM_STAGED_ARTIFACT_H

#include "platform.h"

typedef struct {
	FILE *file;
	char *stagedPath;
	char *destination;
} BsStagedArtifact;

#define BS_STAGED_ARTIFACT_INIT { NULL, NULL, NULL }

static inline void bs_staged_artifact_release_names(BsStagedArtifact *artifact) {
	free(artifact->stagedPath);
	free(artifact->destination);
	artifact->stagedPath = NULL;
	artifact->destination = NULL;
}

static inline bool bs_staged_artifact_open(BsStagedArtifact *artifact,
		const char *destination, char *err, size_t errsz) {
	if (!artifact || !destination || artifact->file || artifact->stagedPath) {
		snprintf(err, errsz, "staged output lifecycle is already active");
		return false;
	}
	size_t outputBytes = strlen(destination);
	if (outputBytes > SIZE_MAX - 80u) {
		snprintf(err, errsz, "output path is too long");
		return false;
	}
	artifact->stagedPath = malloc(outputBytes + 80u);
	artifact->destination = malloc(outputBytes + 1u);
	if (!artifact->stagedPath || !artifact->destination) {
		snprintf(err, errsz, "cannot allocate temporary output path");
		bs_staged_artifact_release_names(artifact);
		return false;
	}
	memcpy(artifact->destination, destination, outputBytes + 1u);
	for (unsigned attempt = 0; attempt < 10000u; attempt++) {
		snprintf(artifact->stagedPath, outputBytes + 80u,
				"%s.partial.%lu.%u", destination,
				bs_process_id(), attempt);
		artifact->file = bs_fopen_exclusive_binary_update(
				artifact->stagedPath);
		if (artifact->file) return true;
		if (errno != EEXIST) {
			snprintf(err, errsz, "cannot create temporary output: %s",
					strerror(errno));
			bs_staged_artifact_release_names(artifact);
			return false;
		}
	}
	snprintf(err, errsz, "cannot reserve a unique temporary output");
	bs_staged_artifact_release_names(artifact);
	return false;
}

static inline FILE *bs_staged_artifact_file(BsStagedArtifact *artifact) {
	return artifact ? artifact->file : NULL;
}

static inline bool bs_staged_artifact_publish(BsStagedArtifact *artifact,
		char *err, size_t errsz) {
	if (!artifact || !artifact->file || !artifact->stagedPath
			|| !artifact->destination) {
		snprintf(err, errsz, "temporary output identity is missing");
		return false;
	}
	if (ferror(artifact->file) || fflush(artifact->file) != 0
			|| bs_fsync_file(artifact->file) != 0) {
		snprintf(err, errsz, "cannot finalize completed output: %s",
				strerror(errno));
		return false;
	}
	if (fclose(artifact->file) != 0) {
		artifact->file = NULL;
		snprintf(err, errsz,
				"cannot close completed output; failed output was retained");
		return false;
	}
	artifact->file = NULL;
	if (bs_rename_noreplace(
			artifact->stagedPath, artifact->destination) != 0) {
		snprintf(err, errsz, "%s",
				errno == EEXIST
					? "output already exists; choose a new filename"
					: "cannot publish completed output");
		return false;
	}
	bs_staged_artifact_release_names(artifact);
	return true;
}

static inline void bs_staged_artifact_abort(BsStagedArtifact *artifact) {
	if (!artifact) return;
	if (artifact->file) {
		(void)bs_fclose_discard_owned(
				artifact->file, artifact->stagedPath);
		artifact->file = NULL;
	}
	if (artifact->stagedPath && bs_file_exists(artifact->stagedPath))
		fprintf(stderr, "incomplete temporary output retained at %s\n",
				artifact->stagedPath);
	bs_staged_artifact_release_names(artifact);
}

#endif /* BRAINSTORM_STAGED_ARTIFACT_H */
