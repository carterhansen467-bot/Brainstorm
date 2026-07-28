/* ========================================================================
 * platform.h -- the ONLY file allowed to know what OS this is.
 * ------------------------------------------------------------------------
 * Both binaries (brainstorm_native_search, brainstorm_seed_pool) are pure
 * computation plus a thin layer of OS plumbing: threads, positional reads,
 * durable writes, atomic renames, sleeps, and a stop signal. This header
 * maps that plumbing to Win32 or POSIX behind bs_* names so the RNG/filter
 * code stays byte-identical across platforms. No RNG, filter, or file-format
 * logic belongs here.
 *
 * Windows notes that shaped this file:
 *   - rename() on Windows refuses to overwrite; status/checkpoint commits
 *     need MoveFileEx(MOVEFILE_REPLACE_EXISTING) or the atomic-replace
 *     pattern silently breaks (bs_rename_overwrite).
 *   - The pool reader needs thread-safe positional reads; ReadFile with an
 *     explicit OVERLAPPED offset is the Win32 pread (bs_pread).
 *   - Ctrl+C / Ctrl+Break must keep the checkpointed-pause contract, so the
 *     stop hook goes through SetConsoleCtrlHandler, which also catches the
 *     console window being closed (bs_install_stop_handler).
 * ======================================================================== */
#ifndef BRAINSTORM_PLATFORM_H
#define BRAINSTORM_PLATFORM_H

#include <stdint.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
/* ----------------------------------------------------------------- Win32 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#include <fcntl.h>
#include <process.h>
#include <sys/stat.h>

/* Protocol/fixture output must be byte-identical across platforms (the mod
 * and the equivalence harnesses parse it as exact bytes), so keep stdout and
 * stderr out of the CRT's \n -> \r\n translation. */
static inline void bs_platform_init(void) {
	_setmode(_fileno(stdout), _O_BINARY);
	_setmode(_fileno(stderr), _O_BINARY);
}

typedef HANDLE bs_thread_t;

typedef struct {
	void *(*fn)(void *);
	void *arg;
} BsThreadTramp;

static inline unsigned __stdcall bs_thread_tramp(void *vp) {
	BsThreadTramp t = *(BsThreadTramp *)vp;
	free(vp);
	t.fn(t.arg);
	return 0;
}

/* Worker return values are never inspected, so joins discard them. */
static inline int bs_thread_create(bs_thread_t *th, void *(*fn)(void *), void *arg) {
	BsThreadTramp *t = malloc(sizeof *t);
	if (!t) return -1;
	t->fn = fn;
	t->arg = arg;
	uintptr_t h = _beginthreadex(NULL, 0, bs_thread_tramp, t, 0, NULL);
	if (!h) { free(t); return -1; }
	*th = (HANDLE)h;
	return 0;
}

static inline void bs_thread_join(bs_thread_t th) {
	WaitForSingleObject(th, INFINITE);
	CloseHandle(th);
}

/* SRWLOCK: static-initializable (matches PTHREAD_MUTEX_INITIALIZER use)
 * and needs no destroy. */
typedef SRWLOCK bs_mutex_t;
#define BS_MUTEX_INIT SRWLOCK_INIT
static inline void bs_mutex_init(bs_mutex_t *m) { InitializeSRWLock(m); }
static inline void bs_mutex_destroy(bs_mutex_t *m) { (void)m; }
static inline void bs_mutex_lock(bs_mutex_t *m) { AcquireSRWLockExclusive(m); }
static inline void bs_mutex_unlock(bs_mutex_t *m) { ReleaseSRWLockExclusive(m); }

static inline void bs_set_errno_from_win32(DWORD code);

typedef CONDITION_VARIABLE bs_cond_t;
#define BS_COND_INIT CONDITION_VARIABLE_INIT
static inline void bs_cond_init(bs_cond_t *c) { InitializeConditionVariable(c); }
static inline void bs_cond_destroy(bs_cond_t *c) { (void)c; }
static inline void bs_cond_wait(bs_cond_t *c, bs_mutex_t *m) {
	SleepConditionVariableSRW(c, m, INFINITE, 0);
}
/* Return 0 when signalled, ETIMEDOUT when the deadline expires, or the
 * platform error mapped to errno otherwise.  Keeping this beside the ordinary
 * wait lets supervisor loops sleep without adding fixed completion latency. */
static inline int bs_cond_wait_ms(bs_cond_t *c, bs_mutex_t *m, unsigned ms) {
	if (SleepConditionVariableSRW(c, m, (DWORD)ms, 0)) return 0;
	DWORD code = GetLastError();
	if (code == ERROR_TIMEOUT) return ETIMEDOUT;
	bs_set_errno_from_win32(code);
	return errno;
}
static inline void bs_cond_signal(bs_cond_t *c) { WakeConditionVariable(c); }
static inline void bs_cond_broadcast(bs_cond_t *c) { WakeAllConditionVariable(c); }

static inline int64_t bs_pread(int fd, void *buf, size_t count, int64_t offset) {
	HANDLE h = (HANDLE)_get_osfhandle(fd);
	if (h == INVALID_HANDLE_VALUE) return -1;
	OVERLAPPED ov;
	memset(&ov, 0, sizeof ov);
	ov.Offset = (DWORD)(offset & 0xFFFFFFFFu);
	ov.OffsetHigh = (DWORD)((uint64_t)offset >> 32);
	DWORD got = 0;
	if (!ReadFile(h, buf, (DWORD)count, &got, &ov)) return -1;
	return (int64_t)got;
}

static inline int bs_dup(int fd) { return _dup(fd); }
static inline int bs_close(int fd) { return _close(fd); }
static inline unsigned long bs_process_id(void) { return (unsigned long)GetCurrentProcessId(); }

typedef HANDLE bs_file_lock_t;
#define BS_FILE_LOCK_INVALID INVALID_HANDLE_VALUE

static inline bs_file_lock_t bs_file_lock_acquire(const char *path, bool *busy) {
	if (busy) *busy = false;
	HANDLE h = CreateFileA(path, GENERIC_READ | GENERIC_WRITE, 0, NULL,
			OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
	if (h != INVALID_HANDLE_VALUE) return h;
	DWORD saved = GetLastError();
	if (busy) *busy = saved == ERROR_SHARING_VIOLATION
			|| saved == ERROR_LOCK_VIOLATION;
	bs_set_errno_from_win32(saved);
	return INVALID_HANDLE_VALUE;
}

static inline void bs_file_lock_release(bs_file_lock_t lock) {
	if (lock != INVALID_HANDLE_VALUE) CloseHandle(lock);
}

static inline void bs_set_errno_from_win32(DWORD code) {
	switch (code) {
	case ERROR_FILE_NOT_FOUND:
	case ERROR_PATH_NOT_FOUND:
	case ERROR_INVALID_DRIVE:
	case ERROR_BAD_NETPATH:
		errno = ENOENT; break;
	case ERROR_ACCESS_DENIED:
	case ERROR_SHARING_VIOLATION:
	case ERROR_LOCK_VIOLATION:
		errno = EACCES; break;
	case ERROR_FILE_EXISTS:
	case ERROR_ALREADY_EXISTS:
		errno = EEXIST; break;
	case ERROR_DISK_FULL:
	case ERROR_HANDLE_DISK_FULL:
		errno = ENOSPC; break;
	case ERROR_TOO_MANY_OPEN_FILES:
		errno = EMFILE; break;
	case ERROR_INVALID_HANDLE:
		errno = EBADF; break;
	case ERROR_NOT_ENOUGH_MEMORY:
	case ERROR_OUTOFMEMORY:
		errno = ENOMEM; break;
	case ERROR_WRITE_PROTECT:
		errno = EROFS; break;
	case ERROR_INVALID_PARAMETER:
		errno = EINVAL; break;
	default:
		errno = EIO; break;
	}
}

/* Create a brand-new binary update stream without ever opening an existing
 * path for truncation.  The cooperative writer lock serializes Brainstorm
 * processes; CREATE_NEW also protects the publication name from unrelated
 * programs that create it after the lock is acquired. */
#ifndef BS_PLATFORM_OPEN_OSFHANDLE
#define BS_PLATFORM_OPEN_OSFHANDLE _open_osfhandle
#endif
#ifndef BS_PLATFORM_FDOPEN
#define BS_PLATFORM_FDOPEN _fdopen
#endif
static inline FILE *bs_fopen_exclusive_binary_update(const char *path) {
	HANDLE h = CreateFileA(path, GENERIC_READ | GENERIC_WRITE | DELETE,
			FILE_SHARE_READ, NULL, CREATE_NEW,
			FILE_ATTRIBUTE_NORMAL, NULL);
	if (h == INVALID_HANDLE_VALUE) {
		bs_set_errno_from_win32(GetLastError());
		return NULL;
	}
	int fd = BS_PLATFORM_OPEN_OSFHANDLE((intptr_t)h, _O_RDWR | _O_BINARY);
	if (fd < 0) {
		int saved = errno ? errno : EIO;
		CloseHandle(h);
		/* Keep the uniquely created placeholder.  Once our handle is closed,
		 * path-based deletion cannot prove that the name still denotes our
		 * file and could remove another process's replacement instead. */
		errno = saved;
		return NULL;
	}
	FILE *f = BS_PLATFORM_FDOPEN(fd, "r+b");
	if (!f) {
		int saved = errno ? errno : EIO;
		_close(fd);
		/* As above, deliberately leave the empty placeholder rather than
		 * performing racy cleanup by name after releasing the handle. */
		errno = saved;
		return NULL;
	}
	return f;
}

/* Discard a failed exclusive output while its owned identity is still pinned.
 * Windows deletes by handle, so a pathname replacement can never be removed.
 * Failure to mark the owned handle leaves the partial file in place instead
 * of falling back to an unsafe path-based delete. */
static inline int bs_fclose_discard_owned(FILE *f, const char *path) {
	(void)path;
	DWORD markError = ERROR_SUCCESS;
	HANDLE h = (HANDLE)_get_osfhandle(_fileno(f));
	FILE_DISPOSITION_INFO disposition;
	disposition.DeleteFile = TRUE;
	if (h == INVALID_HANDLE_VALUE
			|| !SetFileInformationByHandle(
					h, FileDispositionInfo,
					&disposition, sizeof disposition))
		markError = h == INVALID_HANDLE_VALUE
				? ERROR_INVALID_HANDLE : GetLastError();
	int closeRc = fclose(f);
	if (markError != ERROR_SUCCESS) {
		bs_set_errno_from_win32(markError);
		return -1;
	}
	return closeRc;
}

static inline int bs_fsync_file(FILE *f) {
	HANDLE h = (HANDLE)_get_osfhandle(_fileno(f));
	if (h == INVALID_HANDLE_VALUE) { errno = EBADF; return -1; }
	DWORD saved = ERROR_SUCCESS;
	unsigned waited = 0;
	for (;;) {
		if (FlushFileBuffers(h)) return 0;
		saved = GetLastError();
		bool transient = saved == ERROR_SHARING_VIOLATION
				|| saved == ERROR_LOCK_VIOLATION
				|| saved == ERROR_BUSY
				|| saved == ERROR_NOT_READY
				|| saved == ERROR_RETRY;
		if (!transient || waited >= 5000u) break;
		Sleep(50u);
		waited += 50u;
	}
	bs_set_errno_from_win32(saved);
	return -1;
}

static inline int bs_fseeko(FILE *f, int64_t off, int whence) { return _fseeki64(f, off, whence); }
static inline int64_t bs_ftello(FILE *f) { return _ftelli64(f); }

static inline int bs_ftruncate_file(FILE *f, int64_t size) {
	if (fflush(f) != 0) return -1;
	return _chsize_s(_fileno(f), size) == 0 ? 0 : -1;
}

static inline int bs_rename_overwrite(const char *from, const char *to) {
	/* Python's ordinary open() and some antivirus/indexing tools omit
	 * FILE_SHARE_DELETE.  A reader that catches the tiny replacement window
	 * then makes MoveFileEx fail with a sharing/access error even though both
	 * files and the disk are healthy.  Checkpoints run for hours or days, so a
	 * single collision must not terminate the scan.  Retry only errors that
	 * can be caused by a temporary handle/lock; permanent path and disk errors
	 * still fail promptly. */
	DWORD saved = ERROR_SUCCESS;
	unsigned waited = 0;
	for (;;) {
		if (MoveFileExA(from, to,
				MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) return 0;
		saved = GetLastError();
		bool transient = saved == ERROR_ACCESS_DENIED
				|| saved == ERROR_SHARING_VIOLATION
				|| saved == ERROR_LOCK_VIOLATION
				|| saved == ERROR_BUSY
				|| saved == ERROR_DELETE_PENDING;
		if (!transient || waited >= 30000u) break;
		unsigned delay = waited < 100u ? 10u : waited < 1000u ? 50u : 100u;
		Sleep(delay);
		waited += delay;
	}
	bs_set_errno_from_win32(saved);
	return -1;
}

static inline int bs_rename_noreplace(const char *from, const char *to) {
	if (MoveFileExA(from, to, MOVEFILE_WRITE_THROUGH)) return 0;
	bs_set_errno_from_win32(GetLastError());
	return -1;
}

static inline bool bs_file_exists(const char *path) { return _access(path, 0) == 0; }

static inline int64_t bs_file_size(FILE *f) {
	struct _stat64 st;
	if (_fstat64(_fileno(f), &st) != 0) return -1;
	return (int64_t)st.st_size;
}

static inline bool bs_same_file(FILE *a, FILE *b) {
	BY_HANDLE_FILE_INFORMATION ai, bi;
	HANDLE ah = (HANDLE)_get_osfhandle(_fileno(a));
	HANDLE bh = (HANDLE)_get_osfhandle(_fileno(b));
	if (ah == INVALID_HANDLE_VALUE || bh == INVALID_HANDLE_VALUE
			|| !GetFileInformationByHandle(ah, &ai)
			|| !GetFileInformationByHandle(bh, &bi)) return false;
	return ai.dwVolumeSerialNumber == bi.dwVolumeSerialNumber
		&& ai.nFileIndexHigh == bi.nFileIndexHigh
		&& ai.nFileIndexLow == bi.nFileIndexLow;
}

static inline double bs_file_age_seconds(const char *path) {
	struct _stat64 st;
	if (_stat64(path, &st) != 0) return 1e9;
	return difftime(time(NULL), st.st_mtime);
}

static inline int bs_cpu_count(void) {
	SYSTEM_INFO si;
	GetSystemInfo(&si);
	return si.dwNumberOfProcessors > 0 ? (int)si.dwNumberOfProcessors : 1;
}

static inline void bs_sleep_ms(unsigned ms) { Sleep(ms); }

static inline double bs_monotonic_seconds(void) {
	static LARGE_INTEGER freq;
	LARGE_INTEGER now;
	if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
	QueryPerformanceCounter(&now);
	return (double)now.QuadPart / (double)freq.QuadPart;
}

static void (*bs_stop_callback)(void);

static inline BOOL WINAPI bs_console_ctrl(DWORD type) {
	if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT || type == CTRL_CLOSE_EVENT) {
		if (bs_stop_callback) bs_stop_callback();
		/* TRUE = handled: the process keeps running so the scan loop can
		 * finish its epoch and commit a checkpoint (close still hard-kills
		 * after ~5s; the resume path tolerates that). */
		return TRUE;
	}
	return FALSE;
}

static inline void bs_install_stop_handler(void (*fn)(void)) {
	bs_stop_callback = fn;
	SetConsoleCtrlHandler(bs_console_ctrl, TRUE);
}

/* BSD/glibc strsep (config tokenizer) is missing from the Windows CRT.
 * Same contract: empty fields between adjacent delimiters are returned. */
static inline char *strsep(char **stringp, const char *delim) {
	char *start = *stringp;
	if (!start) return NULL;
	char *p = start + strcspn(start, delim);
	if (*p) {
		*p = 0;
		*stringp = p + 1;
	} else {
		*stringp = NULL;
	}
	return start;
}

#else
/* ----------------------------------------------------------------- POSIX */
#include <pthread.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/file.h>

static inline void bs_platform_init(void) {}

typedef pthread_t bs_thread_t;

static inline int bs_thread_create(bs_thread_t *th, void *(*fn)(void *), void *arg) {
	return pthread_create(th, NULL, fn, arg);
}

static inline void bs_thread_join(bs_thread_t th) { pthread_join(th, NULL); }

typedef pthread_mutex_t bs_mutex_t;
#define BS_MUTEX_INIT PTHREAD_MUTEX_INITIALIZER
static inline void bs_mutex_init(bs_mutex_t *m) { pthread_mutex_init(m, NULL); }
static inline void bs_mutex_destroy(bs_mutex_t *m) { pthread_mutex_destroy(m); }
static inline void bs_mutex_lock(bs_mutex_t *m) { pthread_mutex_lock(m); }
static inline void bs_mutex_unlock(bs_mutex_t *m) { pthread_mutex_unlock(m); }

typedef pthread_cond_t bs_cond_t;
#define BS_COND_INIT PTHREAD_COND_INITIALIZER
static inline void bs_cond_init(bs_cond_t *c) { pthread_cond_init(c, NULL); }
static inline void bs_cond_destroy(bs_cond_t *c) { pthread_cond_destroy(c); }
static inline void bs_cond_wait(bs_cond_t *c, bs_mutex_t *m) { pthread_cond_wait(c, m); }
static inline int bs_cond_wait_ms(bs_cond_t *c, bs_mutex_t *m, unsigned ms) {
	struct timespec deadline;
	if (clock_gettime(CLOCK_REALTIME, &deadline) != 0) return errno;
	deadline.tv_sec += (time_t)(ms / 1000u);
	deadline.tv_nsec += (long)(ms % 1000u) * 1000000L;
	if (deadline.tv_nsec >= 1000000000L) {
		deadline.tv_sec++;
		deadline.tv_nsec -= 1000000000L;
	}
	return pthread_cond_timedwait(c, m, &deadline);
}
static inline void bs_cond_signal(bs_cond_t *c) { pthread_cond_signal(c); }
static inline void bs_cond_broadcast(bs_cond_t *c) { pthread_cond_broadcast(c); }

static inline int64_t bs_pread(int fd, void *buf, size_t count, int64_t offset) {
	return (int64_t)pread(fd, buf, count, (off_t)offset);
}

static inline int bs_dup(int fd) { return dup(fd); }
static inline int bs_close(int fd) { return close(fd); }
static inline unsigned long bs_process_id(void) { return (unsigned long)getpid(); }

typedef int bs_file_lock_t;
#define BS_FILE_LOCK_INVALID (-1)

static inline bs_file_lock_t bs_file_lock_acquire(const char *path, bool *busy) {
	if (busy) *busy = false;
	int fd = open(path, O_CREAT | O_RDWR, 0600);
	if (fd < 0) return -1;
	if (flock(fd, LOCK_EX | LOCK_NB) == 0) return fd;
	int saved = errno;
	if (busy) *busy = saved == EWOULDBLOCK || saved == EAGAIN;
	close(fd);
	errno = saved;
	return -1;
}

static inline void bs_file_lock_release(bs_file_lock_t lock) {
	if (lock >= 0) close(lock);
}

/* O_EXCL is the POSIX half of the same publication contract as CREATE_NEW:
 * fdopen only wraps the already-exclusive descriptor and cannot truncate a
 * target that appeared after an advisory existence check. */
static inline FILE *bs_fopen_exclusive_binary_update(const char *path) {
	int fd = open(path, O_CREAT | O_EXCL | O_RDWR, 0666);
	if (fd < 0) return NULL;
	FILE *f = fdopen(fd, "r+b");
	if (!f) {
		int saved = errno ? errno : EIO;
		close(fd);
		/* Retain the uniquely created placeholder. POSIX has no portable
		 * unlink-by-handle operation, and unlink(path) after a rename race
		 * could remove another process's replacement. */
		errno = saved;
		return NULL;
	}
	return f;
}

static inline int bs_fclose_discard_owned(FILE *f, const char *path) {
	/* There is no portable POSIX unlink-by-open-file identity. Transform
	 * writers use a private partial pathname, so retain it on failure rather
	 * than risk unlinking a replacement installed by another process. */
	(void)path;
	return fclose(f);
}

static inline int bs_fsync_file(FILE *f) { return fsync(fileno(f)); }

static inline int bs_fseeko(FILE *f, int64_t off, int whence) { return fseeko(f, (off_t)off, whence); }
static inline int64_t bs_ftello(FILE *f) { return (int64_t)ftello(f); }

static inline int bs_ftruncate_file(FILE *f, int64_t size) {
	if (fflush(f) != 0) return -1;
	return ftruncate(fileno(f), (off_t)size);
}

static inline int bs_rename_overwrite(const char *from, const char *to) { return rename(from, to); }

static inline int bs_rename_noreplace(const char *from, const char *to) {
#if defined(__APPLE__)
	/* Darwin provides an atomic exclusive rename, including on filesystems
	 * where hard links are unavailable. */
	return renamex_np(from, to, RENAME_EXCL);
#else
	if (link(from, to) != 0) return -1;
	/* The output hard link is already a complete no-overwrite publication.
	 * Failure to remove the private staging name only leaves harmless debris. */
	(void)unlink(from);
	return 0;
#endif
}

static inline bool bs_file_exists(const char *path) { return access(path, F_OK) == 0; }

static inline int64_t bs_file_size(FILE *f) {
	struct stat st;
	if (fstat(fileno(f), &st) != 0) return -1;
	return (int64_t)st.st_size;
}

static inline bool bs_same_file(FILE *a, FILE *b) {
	struct stat as, bs;
	if (fstat(fileno(a), &as) != 0 || fstat(fileno(b), &bs) != 0) return false;
	return as.st_dev == bs.st_dev && as.st_ino == bs.st_ino;
}

static inline double bs_file_age_seconds(const char *path) {
	struct stat st;
	if (stat(path, &st) != 0) return 1e9;
	return difftime(time(NULL), st.st_mtime);
}

static inline int bs_cpu_count(void) {
	long n = sysconf(_SC_NPROCESSORS_ONLN);
	return n > 0 ? (int)n : 1;
}

static inline void bs_sleep_ms(unsigned ms) {
	struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000000L };
	nanosleep(&ts, NULL);
}

static inline double bs_monotonic_seconds(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static void (*bs_stop_callback)(void);

static inline void bs_posix_stop_tramp(int sig) {
	(void)sig;
	if (bs_stop_callback) bs_stop_callback();
}

static inline void bs_install_stop_handler(void (*fn)(void)) {
	bs_stop_callback = fn;
	signal(SIGINT, bs_posix_stop_tramp);
	signal(SIGTERM, bs_posix_stop_tramp);
}

#endif /* _WIN32 */
#endif /* BRAINSTORM_PLATFORM_H */
