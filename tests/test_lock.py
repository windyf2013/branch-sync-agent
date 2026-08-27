import threading
import time

import pytest

from bsa.executor.lock import LockTimeoutError, branch_lock_path, flock_acquire


def test_branch_lock_path_nests_under_locks_branch(tmp_path):
    path = branch_lock_path(tmp_path, "br_v4.34_develop_20260130")
    assert path == tmp_path / "locks" / "branch" / "br_v4.34_develop_20260130.lock"


def test_branch_lock_path_sanitizes_slashes(tmp_path):
    # 分支名理论上无斜杠，但防御性处理：斜杠 → 下划线，避免嵌套越权。
    path = branch_lock_path(tmp_path, "feature/x")
    assert path == tmp_path / "locks" / "branch" / "feature_x.lock"


def test_branch_lock_mutually_excludes_same_branch(tmp_path):
    lock = branch_lock_path(tmp_path, "br_main")
    with flock_acquire(lock):
        with pytest.raises(LockTimeoutError):
            with flock_acquire(lock, timeout=0.1):
                pass


def test_branch_lock_allows_different_branches_parallel(tmp_path):
    lock_a = branch_lock_path(tmp_path, "br_a")
    lock_b = branch_lock_path(tmp_path, "br_b")
    with flock_acquire(lock_a):
        # 不同分支锁互不阻塞：立即获取 br_b（不抛超时）
        with flock_acquire(lock_b, timeout=1.0):
            pass


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
