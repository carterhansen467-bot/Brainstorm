/* ===========================================================================
 * Brainstorm native helpers: platform compatibility layer
 * ---------------------------------------------------------------------------
 * The searcher and the pool builder are written against a small POSIX
 * surface; this header maps it onto Win32 so ONE codebase builds on macOS,
 * Linux, and Windows (MinGW/clang via `zig cc -target x86_64-windows-gnu`,
 * see build_windows.sh; MSVC should work too but is not the tested path).
 *
 * Nothing here may affect arithmetic: the bit-exactness contract
 * (-ffp-contract=off + runtime fma calibration) is compiler-flag and
 * check-line territory, and this file is only threads, files, and signals.
 *
 * Win32 notes, the non-obvious ones:
 *  - rename() does NOT overwrite on Windows; the status/state commit protocol
 *    (write tmp, atomically replace) requires MoveFileEx(REPLACE_EXISTING).
 *  - struct stat st_size is 32-bit under MSVC/MinGW defaults; pools exceed
 *    2 GiB, so bs_stat/bs_fstat use _stat64.
 *  - pread has no direct equivalent; ReadFile with an OVERLAPPED offset is
 *    positional and thread-safe on the same handle (the handle's file
 *    pointer becomes unspecified, which is fine: every reader passes its
 *    own offset).
 *  - Paths go through the ANSI code page (fopen/MoveFileExA). Non-ASCII
 *    install paths are a known limitation, documented in the README.
 * =========================================================================== */
#ifndef BRAINSTORM_PLATFORM_H
#define BRAINSTORM_PLATFORM_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <time.h>

#ifdef _WIN32

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <io.h>
#include <process.h>

#ifndef F_OK
#define F_OK 0
#endif
#ifdef _MSC_VER
#define access _access
#define dup _dup
#define fileno _fileno
typedef intptr_t ssize_t;
#endif

/* 64-bit stream offsets (MinGW/MSVC ftello is 32-bit by default). */
#undef fseeko
#undef ftello
#define fseeko _fseeki64
#define ftello _ftelli64

typedef struct _stat64 bs_stat_t;
#define bs_stat _stat64
#define bs_fstat _fstat64

/* --------------------------------------------------------------- threads --
 * The helpers use the plain pthread create/join/mutex quartet; map it onto
 * Win32 primitives instead of depending on a winpthreads runtime DLL. */
typedef HANDLE pthread_t;
typedef SRWLOCK pthread_mutex_t;
#define PTHREAD_MUTEX_INITIALIZER SRWLOCK_INIT

typedef struct { void *(*fn)(void *); void *arg; } bs_thread_boot;

static unsigned __stdcall bs_thread_tramp(void *p) {
	bs_thread_boot b = *(bs_thread_boot *)p;
	free(p);
	b.fn(b.arg);
	return 0;
}

static inline int pthread_create(pthread_t *t, const void *attr,
		void *(*fn)(void *), void *arg) {
	(void)attr;
	bs_thread_boot *b = malloc(sizeof *b);
	if (!b) return -1;
	b->fn = fn;
	b->arg = arg;
	uintptr_t h = _beginthreadex(NULL, 0, bs_thread_tramp, b, 0, NULL);
	if (!h) { free(b); return -1; }
	*t = (HANDLE)h;
	return 0;
}

static inline int pthread_join(pthread_t t, void **ret) {
	(void)ret;
	WaitForSingleObject(t, INFINITE);
	CloseHandle(t);
	return 0;
}

static inline int pthread_mutex_init(pthread_mutex_t *m, const void *attr) {
	(void)attr;
	InitializeSRWLock(m);
	return 0;
}
static inline int pthread_mutex_destroy(pthread_mutex_t *m) { (void)m; return 0; }
static inline int pthread_mutex_lock(pthread_mutex_t *m) { AcquireSRWLockExclusive(m); return 0; }
static inline int pthread_mutex_unlock(pthread_mutex_t *m) { ReleaseSRWLockExclusive(m); return 0; }

/* ------------------------------------------------------------------ files */
static inline int64_t bs_pread(int fd, void *buf, uint64_t n, uint64_t off) {
	HANDLE h = (HANDLE)_get_osfhandle(fd);
	if (h == INVALID_HANDLE_VALUE) return -1;
	uint64_t done = 0;
	while (done < n) {
		uint64_t want64 = n - done;
		DWORD want = want64 > (1u << 30) ? (1u << 30) : (DWORD)want64;
		DWORD got = 0;
		OVERLAPPED ov;
		memset(&ov, 0, sizeof ov);
		ov.Offset = (DWORD)((off + done) & 0xffffffffu);
		ov.OffsetHigh = (DWORD)((off + done) >> 32);
		if (!ReadFile(h, (char *)buf + done, want, &got, &ov)) {
			if (GetLastError() == ERROR_HANDLE_EOF) break;
			return -1;
		}
		if (got == 0) break;
		done += got;
	}
	return (int64_t)done;
}

static inline int bs_fsync(int fd) {
	HANDLE h = (HANDLE)_get_osfhandle(fd);
	if (h == INVALID_HANDLE_VALUE) return -1;
	return FlushFileBuffers(h) ? 0 : -1;
}

static inline int bs_ftruncate(int fd, int64_t size) {
	return _chsize_s(fd, size) == 0 ? 0 : -1;
}

static inline int bs_rename(const char *from, const char *to) {
	return MoveFileExA(from, to, MOVEFILE_REPLACE_EXISTING) ? 0 : -1;
}

/* strsep is BSD; not guaranteed by the Windows CRTs. */
static inline char *bs_strsep_impl(char **sp, const char *delim) {
	char *s = *sp;
	if (!s) return NULL;
	char *tok = s;
	s += strcspn(s, delim);
	if (*s) { *s = 0; *sp = s + 1; } else { *sp = NULL; }
	return tok;
}
#define strsep bs_strsep_impl

/* ------------------------------------------------------- time and signals */
static inline void bs_sleep_ms(unsigned ms) { Sleep(ms); }

static inline double bs_monotonic_seconds(void) {
	static LARGE_INTEGER freq; /* zero until first call; QPC freq is constant */
	LARGE_INTEGER now;
	if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
	QueryPerformanceCounter(&now);
	return (double)now.QuadPart / (double)freq.QuadPart;
}

static inline int bs_ncpu(void) {
	SYSTEM_INFO si;
	GetSystemInfo(&si);
	return si.dwNumberOfProcessors > 0 ? (int)si.dwNumberOfProcessors : 1;
}

/* Ctrl+C / Ctrl+Break / console-close all request the same checkpointed stop
 * the POSIX build gets from SIGINT/SIGTERM. Returning TRUE claims the event
 * so the process isn't killed before the current epoch commits (close still
 * enforces the system's ~5s grace limit). */
typedef void (*bs_stop_fn)(int);
static bs_stop_fn bs_stop_cb;

static inline BOOL WINAPI bs_console_ctrl(DWORD type) {
	switch (type) {
	case CTRL_C_EVENT:
	case CTRL_BREAK_EVENT:
	case CTRL_CLOSE_EVENT:
		if (bs_stop_cb) bs_stop_cb(0);
		return TRUE;
	}
	return FALSE;
}

static inline void bs_install_stop_handler(bs_stop_fn fn) {
	bs_stop_cb = fn;
	SetConsoleCtrlHandler(bs_console_ctrl, TRUE);
}

#else /* ------------------------------------------------------------ POSIX */

#include <pthread.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>

typedef struct stat bs_stat_t;
#define bs_stat stat
#define bs_fstat fstat

static inline int64_t bs_pread(int fd, void *buf, uint64_t n, uint64_t off) {
	return (int64_t)pread(fd, buf, (size_t)n, (off_t)off);
}

static inline int bs_fsync(int fd) { return fsync(fd); }
static inline int bs_ftruncate(int fd, int64_t size) { return ftruncate(fd, (off_t)size); }
static inline int bs_rename(const char *from, const char *to) { return rename(from, to); }

static inline void bs_sleep_ms(unsigned ms) {
	struct timespec ts = { ms / 1000, (long)(ms % 1000) * 1000 * 1000 };
	nanosleep(&ts, NULL);
}

static inline double bs_monotonic_seconds(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static inline int bs_ncpu(void) {
	long n = sysconf(_SC_NPROCESSORS_ONLN);
	return n > 0 ? (int)n : 1;
}

typedef void (*bs_stop_fn)(int);
static inline void bs_install_stop_handler(bs_stop_fn fn) {
	signal(SIGINT, fn);
	signal(SIGTERM, fn);
}

#endif /* _WIN32 */

#endif /* BRAINSTORM_PLATFORM_H */
