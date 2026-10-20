"""OS-level locking for Paper runtime process and SQLite journal ownership.

The launcher must acquire these locks before constructing SQLitePaperRuntimeLedger.
This module does not register OS tasks, open exchange connections, or touch the journal.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO, cast


class RuntimeLockError(RuntimeError):
    """Base error for fail-closed Paper process/journal locking."""


class RuntimeAlreadyRunning(RuntimeLockError):
    """Another process owns the application's Paper instance or journal lock."""


class RuntimeLockAcquisitionError(RuntimeLockError):
    """A lock could not be acquired or its lock file failed integrity checks."""


class _FileLock:
    """Non-blocking OS byte/file lock backed by a restrictive sidecar file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: BinaryIO | None = None
        self._locked = False

    def acquire(self) -> None:
        if self._stream is not None:
            raise RuntimeLockAcquisitionError("Lock object is already open.")
        flags = os.O_CREAT | os.O_RDWR
        if os.name != "nt":
            flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeLockAcquisitionError("Lock path is not a regular file.")
            if os.name != "nt":
                if info.st_uid != os.geteuid():
                    raise RuntimeLockAcquisitionError("Lock file is not owned by this user.")
                if stat.S_IMODE(info.st_mode) & 0o077:
                    raise RuntimeLockAcquisitionError(
                        "Lock file permissions are broader than owner-only."
                    )
            self._stream = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
            self._stream.seek(0, os.SEEK_END)
            if self._stream.tell() == 0:
                self._stream.write(b"\0")
                self._stream.flush()
            self._stream.seek(0)
            if os.name == "nt":
                locking = importlib.import_module("msvcrt").locking
                lock_mode = importlib.import_module("msvcrt").LK_NBLCK
                try:
                    locking(self._stream.fileno(), lock_mode, 1)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise RuntimeAlreadyRunning(
                            f"Paper lock is already held: {self.path.name}"
                        ) from exc
                    raise
            else:
                fcntl = importlib.import_module("fcntl")
                try:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise RuntimeAlreadyRunning(
                            f"Paper lock is already held: {self.path.name}"
                        ) from exc
                    raise
            self._locked = True
        except RuntimeLockError:
            self.release()
            raise
        except OSError as exc:
            self.release()
            raise RuntimeLockAcquisitionError(
                f"Cannot acquire Paper lock: {self.path.name} ({type(exc).__name__})."
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            if self._locked:
                stream.seek(0)
                if os.name == "nt":
                    importlib.import_module("msvcrt").locking(
                        stream.fileno(),
                        importlib.import_module("msvcrt").LK_UNLCK,
                        1,
                    )
                else:
                    importlib.import_module("fcntl").flock(
                        stream.fileno(), importlib.import_module("fcntl").LOCK_UN
                    )
        finally:
            self._locked = False
            stream.close()
            self._stream = None


class PaperRuntimeLocks:
    """Acquire an app-instance lock followed by an exclusive journal lock.

    The journal's parent directory must already exist and be access-controlled.
    On Windows the first lock is a named Global mutex; on other platforms it is an
    exclusive file lock. A duplicate exits before opening the journal lock or journal.
    """

    _ERROR_ALREADY_EXISTS = 183

    def __init__(self, journal_path: str | Path) -> None:
        raw_path = Path(journal_path).expanduser()
        if not raw_path.parent.is_dir():
            raise RuntimeLockAcquisitionError(
                "Journal parent directory must exist before locks are acquired."
            )
        if raw_path.exists() and raw_path.is_symlink():
            raise RuntimeLockAcquisitionError("Journal path must not be a symbolic link.")
        if raw_path.exists() and not raw_path.is_file():
            raise RuntimeLockAcquisitionError("Journal path is not a regular file.")
        self.journal_path = raw_path.parent.resolve() / raw_path.name
        identity = hashlib.sha256(
            os.path.normcase(str(self.journal_path)).encode("utf-8")
        ).hexdigest()[:24]
        self.journal_lock_path = self.journal_path.with_name(
            f".{self.journal_path.name}.journal.lock"
        )
        self.instance_lock_path = self.journal_path.with_name(
            f".{self.journal_path.name}.instance.lock"
        )
        self._mutex_name = f"Global\\EVORYN.PaperRuntime.{identity}"
        self._process_file_lock: _FileLock | None = None
        self._journal_file_lock: _FileLock | None = None
        self._kernel32: Any | None = None
        self._mutex_handle: int | None = None
        self._mutex_owned = False
        self._held = False

    @property
    def held(self) -> bool:
        """Whether both required locks are currently owned."""
        return self._held

    def __enter__(self) -> PaperRuntimeLocks:
        if self._held:
            raise RuntimeLockAcquisitionError("Lock set is already held.")
        try:
            if os.name == "nt":
                self._acquire_windows_mutex()
            else:
                process_lock = _FileLock(self.instance_lock_path)
                process_lock.acquire()
                self._process_file_lock = process_lock

            journal_lock = _FileLock(self.journal_lock_path)
            journal_lock.acquire()
            self._journal_file_lock = journal_lock
            self._held = True
            return self
        except Exception:
            self._release_all()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        release_error = self._release_all()
        if release_error is not None and exc is None:
            raise RuntimeLockAcquisitionError(
                "Failed to release Paper runtime locks."
            ) from release_error

    def _acquire_windows_mutex(self) -> None:
        win_dll = getattr(ctypes, "WinDLL")  # noqa: B009
        kernel32 = win_dll("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        raw_handle = create_mutex(None, 1, self._mutex_name)
        error_code = getattr(ctypes, "get_last_error")()  # noqa: B009
        if not raw_handle:
            raise RuntimeLockAcquisitionError(
                f"Cannot create Paper runtime mutex (WinError {error_code})."
            )
        handle = cast(int, raw_handle)
        if error_code == self._ERROR_ALREADY_EXISTS:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(handle)
            raise RuntimeAlreadyRunning("EVORYN Paper runtime mutex is already held.")
        self._kernel32 = kernel32
        self._mutex_handle = handle
        self._mutex_owned = True

    def _release_all(self) -> BaseException | None:
        """Attempt every cleanup action, returning the first release error."""
        errors: list[BaseException] = []
        if self._journal_file_lock is not None:
            try:
                self._journal_file_lock.release()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self._journal_file_lock = None
        if self._process_file_lock is not None:
            try:
                self._process_file_lock.release()
            except BaseException as exc:
                errors.append(exc)
            finally:
                self._process_file_lock = None
        if self._mutex_handle is not None and self._kernel32 is not None:
            handle = self._mutex_handle
            kernel32 = self._kernel32
            if self._mutex_owned:
                try:
                    release_mutex = kernel32.ReleaseMutex
                    release_mutex.argtypes = [ctypes.c_void_p]
                    release_mutex.restype = ctypes.c_int
                    if not release_mutex(handle):
                        errors.append(
                            RuntimeLockAcquisitionError(
                                "Windows refused to release the Paper runtime mutex."
                            )
                        )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    self._mutex_owned = False
            try:
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = [ctypes.c_void_p]
                close_handle.restype = ctypes.c_int
                if not close_handle(handle):
                    errors.append(
                        RuntimeLockAcquisitionError(
                            "Windows refused to close the Paper runtime mutex handle."
                        )
                    )
            except BaseException as exc:
                errors.append(exc)
            finally:
                self._mutex_handle = None
                self._kernel32 = None
        self._held = False
        return errors[0] if errors else None
