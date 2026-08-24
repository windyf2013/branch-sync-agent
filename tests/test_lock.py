import threading
import time

import pytest

from bsa.executor.lock import LockTimeoutError, flock_acquire


def test_acquires_and_releases(tmp_path):
    lock = tmp_path / "l.lock"
    with flock_acquire(lock):
        pass
    assert lock.exists()


def test_mutex_between_threads(tmp_path):
    lock = tmp_path / "l.lock"
    entered: list[bool] = []
    with flock_acquire(lock):
        other = threading.Thread(
            target=lambda: [entered.append(flock_acquire(lock).__enter__() is None)],
            daemon=True,
        )
        other.start()
        other.join(timeout=0.5)
        assert entered == [], "second holder should block while first holds"
    other.join(timeout=2.0)
    assert entered == [True], "second holder should acquire only after release"


def test_released_after_exception(tmp_path):
    lock = tmp_path / "l.lock"
    with pytest.raises(RuntimeError):
        with flock_acquire(lock):
            raise RuntimeError("boom")
    with flock_acquire(lock, timeout=1.0):
        pass


def test_timeout_raises(tmp_path):
    lock = tmp_path / "l.lock"
    with flock_acquire(lock):
        with pytest.raises(LockTimeoutError):
            with flock_acquire(lock, timeout=0.1):
                pass
