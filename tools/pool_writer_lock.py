#!/usr/bin/env python3
"""Cross-platform lock shared by every process that publishes a seed pool."""

import os
from contextlib import contextmanager


@contextmanager
def pool_writer_guard(path):
    """Take the native scanner's non-blocking ``<pool>.writer.lock``.

    POSIX uses ``flock`` on one persistent inode; Windows uses the native
    scanner's exclusive, no-sharing ``CreateFile`` contract.  The lock file is
    intentionally retained after release so unlinking cannot split POSIX lock
    ownership across old and newly created inodes.
    """
    lock_path = os.path.abspath(path) + ".writer.lock"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(lock_path, 0xC0000000, 0, None, 4, 0x80, None)
        invalid = ctypes.c_void_p(-1).value
        if handle == invalid:
            error = ctypes.get_last_error()
            if error in (32, 33):  # sharing / lock violation
                raise ValueError(
                    "That pool is currently being written by another process; "
                    "pause or finish it first.")
            raise OSError(error, "Could not acquire the pool writer lock", lock_path)
        try:
            yield
        finally:
            close_handle(handle)
        return

    import errno
    import fcntl

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ValueError(
                    "That pool is currently being written by another process; "
                    "pause or finish it first.") from None
            raise
        yield
    finally:
        os.close(fd)
