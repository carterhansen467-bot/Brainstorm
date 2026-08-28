#!/usr/bin/env python3
"""Single owner for Python-side seed-pool filesystem mutation.

The native scanner owns its live streaming files. Every Python workflow uses
this module for the shared mutation contract: persistent writer locking,
durable atomic replacement, no-overwrite publication, identity-safe rollback,
and main-file-last artifact deletion.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from contextlib import contextmanager
from typing import Sequence


class SeedPoolMutationOwner:
    """Own the filesystem rules shared by Builder and Organizer mutations."""

    @contextmanager
    def guard(self, path):
        """Take the native writer's persistent non-blocking pool lock."""
        lock_path = os.path.abspath(path) + ".writer.lock"
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                wintypes.HANDLE,
            )
            create_file.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            handle = create_file(
                lock_path, 0xC0000000, 0, None, 4, 0x80, None)
            invalid = ctypes.c_void_p(-1).value
            if handle == invalid:
                error = ctypes.get_last_error()
                if error in (32, 33):
                    raise ValueError(
                        "That pool is currently being written by another "
                        "process; pause or finish it first.")
                raise OSError(
                    error, "Could not acquire the pool writer lock",
                    lock_path)
            try:
                yield
            finally:
                close_handle(handle)
            return

        import fcntl

        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ValueError(
                        "That pool is currently being written by another "
                        "process; pause or finish it first.") from None
                raise
            yield
        finally:
            os.close(descriptor)

    def fsync_directory(self, path):
        """Durably record directory-entry mutation where supported."""
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = None
        try:
            descriptor = os.open(os.path.abspath(path), flags)
            os.fsync(descriptor)
        except OSError:
            # Network and virtual filesystems may reject directory fsync while
            # still supporting file fsync plus atomic link/replace.
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _atomic_replace(self, path, write, prefix, suffix):
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        descriptor, staged = tempfile.mkstemp(
            prefix=prefix, suffix=suffix, dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                write(handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, path)
            staged = ""
            self.fsync_directory(directory)
        finally:
            if staged:
                try:
                    os.unlink(staged)
                except OSError:
                    pass

    def atomic_text(self, path, text, prefix=".pool-text-", suffix=".tmp",
                    encoding="utf-8"):
        """Durably replace one text sidecar or report."""
        if not isinstance(text, str):
            raise TypeError("atomic text value must be a string")

        def write(handle):
            handle.write(text.encode(encoding))

        self._atomic_replace(path, write, prefix, suffix)

    def atomic_json(self, path, value):
        """Durably replace one deterministic JSON report."""
        encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode(
            "utf-8")
        self._atomic_replace(
            path, lambda handle: handle.write(encoded),
            ".organizer-report-", ".tmp")

    def link_no_overwrite(self, staged_path, final_path,
                          consume_staged=False):
        """Publish by hard link, refusing overwrite and preserving identity."""
        self.link_many_no_overwrite(
            ((staged_path, final_path),), consume_staged=consume_staged)

    def link_many_no_overwrite(self, publications,
                               consume_staged=False):
        """Publish several hard links with one durability sync per directory.

        A link failure rolls back links made by this call. Later workflow
        failures can use ``rollback_link`` while the stages still exist.
        """
        pairs = tuple((os.fspath(staged), os.fspath(final))
                      for staged, final in publications)
        if consume_staged and len(pairs) != 1:
            raise ValueError(
                "staged consumption is only safe for one publication")
        linked = []
        directories = set()
        try:
            for staged_path, final_path in pairs:
                os.link(staged_path, final_path)
                linked.append((staged_path, final_path))
                directories.add(os.path.dirname(os.path.abspath(final_path)))
                if consume_staged:
                    os.unlink(staged_path)
            for directory in sorted(directories):
                self.fsync_directory(directory)
            return tuple(final_path for _staged_path, final_path in linked)
        except BaseException:
            for staged_path, final_path in reversed(linked):
                self.rollback_link(staged_path, final_path)
            for directory in sorted(directories):
                self.fsync_directory(directory)
            raise

    def publish_no_overwrite(self, staged_path, final_path):
        """Publish on ordinary Windows/POSIX filesystems without overwrite.

        Windows rename consumes the stage and supports FAT/exFAT. POSIX uses a
        hard link because rename would overwrite an existing destination.
        Callers must treat the staged pathname as opaque after success.
        """
        if os.name == "nt":
            os.rename(staged_path, final_path)
        else:
            os.link(staged_path, final_path, follow_symlinks=False)
        self.fsync_directory(os.path.dirname(os.path.abspath(final_path)))

    def remove(self, path, missing_ok=False):
        """Remove one exact artifact and durably record its disappearance."""
        try:
            os.unlink(path)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        self.fsync_directory(os.path.dirname(os.path.abspath(path)))
        return True

    @staticmethod
    def _same_artifact_entry(first_path, second_path):
        """Compare exact directory entries without following symlinks."""
        first = os.lstat(first_path)
        second = os.lstat(second_path)
        return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)

    def rollback_link(self, staged_path, final_path):
        """Remove only this publication, preserving any raced replacement.

        Comparing ``final_path`` and then unlinking it leaves a check/unlink
        race.  Move the current directory entry into a private same-filesystem
        quarantine first; its identity can then be checked without another
        process replacing the pathname under that check.  A foreign artifact
        is restored without overwrite.  POSIX retains the private quarantine
        link as a recovery copy because removing it after a hard-link restore
        would introduce the same race again.
        """
        staged_path = os.fspath(staged_path)
        final_path = os.path.abspath(os.fspath(final_path))
        directory = os.path.dirname(final_path)
        try:
            quarantine_dir = tempfile.mkdtemp(
                prefix=".pool-rollback-", dir=directory)
        except OSError:
            return False
        quarantine_path = os.path.join(quarantine_dir, "artifact")
        retain_quarantine = False
        try:
            try:
                os.rename(final_path, quarantine_path)
            except OSError:
                return False
            self.fsync_directory(directory)
            self.fsync_directory(quarantine_dir)

            try:
                staged_publication = self._same_artifact_entry(
                    staged_path, quarantine_path)
            except OSError:
                staged_publication = False
            if staged_publication:
                try:
                    self.remove(quarantine_path)
                except OSError:
                    retain_quarantine = True
                    return False
                return True

            try:
                self.publish_no_overwrite(quarantine_path, final_path)
            except OSError:
                retain_quarantine = True
                return False
            if os.name != "nt":
                retain_quarantine = True
            return False
        finally:
            if retain_quarantine:
                self.fsync_directory(quarantine_dir)
            else:
                try:
                    os.rmdir(quarantine_dir)
                except OSError:
                    pass
                else:
                    self.fsync_directory(directory)

    def delete_artifacts(self, paths: Sequence[str], main_path: str):
        """Delete one reviewed artifact family with its main pool last."""
        ordered = [path for path in paths if path != main_path]
        if main_path in paths:
            ordered.append(main_path)
        removed = []
        directories = set()
        try:
            for path in ordered:
                os.unlink(path)
                removed.append(os.path.basename(path))
                directories.add(os.path.dirname(os.path.abspath(path)))
        finally:
            for directory in sorted(directories):
                self.fsync_directory(directory)
        return removed


seed_pool_mutations = SeedPoolMutationOwner()
pool_writer_guard = seed_pool_mutations.guard
