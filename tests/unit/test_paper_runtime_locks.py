"""Cross-process checks for fail-closed Paper runtime/journal locks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from evoryn.runtime.single_instance import PaperRuntimeLocks, RuntimeLockAcquisitionError

_CHILD_PROBE = """
from pathlib import Path
import sys
from evoryn.runtime.single_instance import PaperRuntimeLocks, RuntimeAlreadyRunning
try:
    with PaperRuntimeLocks(Path(sys.argv[1])):
        print("UNEXPECTEDLY_ACQUIRED", flush=True)
except RuntimeAlreadyRunning:
    print("ALREADY_RUNNING", flush=True)
    raise SystemExit(17)
"""


def test_lock_set_requires_existing_parent_without_creating_it(tmp_path: Path) -> None:
    journal = tmp_path / "not-created" / "runtime.sqlite3"

    with pytest.raises(RuntimeLockAcquisitionError, match="parent directory"):
        PaperRuntimeLocks(journal)

    assert not journal.parent.exists()
    assert not journal.exists()


def test_lock_set_acquires_process_and_journal_locks_without_creating_journal(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "runtime.sqlite3"

    with PaperRuntimeLocks(journal) as locks:
        assert locks.held
        assert not journal.exists()
        assert locks.journal_lock_path.is_file()
        if sys.platform != "win32":
            assert locks.instance_lock_path.is_file()

    assert not locks.held


def test_duplicate_process_is_rejected_before_journal_mutation(tmp_path: Path) -> None:
    journal = tmp_path / "runtime.sqlite3"
    journal.write_bytes(b"immutable sentinel: not a SQLite journal")
    before = journal.read_bytes()

    with PaperRuntimeLocks(journal):
        child = subprocess.run(
            [sys.executable, "-c", _CHILD_PROBE, str(journal)],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )

        assert child.returncode == 17, child.stderr
        assert child.stdout.strip() == "ALREADY_RUNNING"
        assert journal.read_bytes() == before


def test_lock_set_releases_all_locks_after_exception(tmp_path: Path) -> None:
    journal = tmp_path / "runtime.sqlite3"

    with pytest.raises(RuntimeError, match="injected"), PaperRuntimeLocks(journal):
        raise RuntimeError("injected")

    with PaperRuntimeLocks(journal) as locks:
        assert locks.held


@pytest.mark.skipif(sys.platform == "win32", reason="O_NOFOLLOW is POSIX-specific.")
def test_lock_sidecar_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    journal = tmp_path / "runtime.sqlite3"
    target = tmp_path / "do-not-touch.txt"
    target.write_text("sentinel", encoding="utf-8")
    sidecar = journal.with_name(f".{journal.name}.instance.lock")
    sidecar.symlink_to(target)

    with (
        pytest.raises(RuntimeLockAcquisitionError, match="Cannot acquire Paper lock"),
        PaperRuntimeLocks(journal),
    ):
        pass

    assert target.read_text(encoding="utf-8") == "sentinel"
