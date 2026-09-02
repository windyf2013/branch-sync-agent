import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockTimeoutError(TimeoutError):
    """获取全局锁超时。"""


def branch_lock_path(log_dir: str | Path, branch: str) -> Path:
    """分分支锁路径：``{log_dir}/locks/branch/<branch>.lock``（决策 5.3）。

    分支名含斜杠时替换为下划线，防止嵌套路径越权。分分支锁让手动同步与
    cron 可并行处理不同分支，仅同一分支互斥。
    """
    safe = branch.replace("/", "_")
    return Path(log_dir) / "locks" / "branch" / f"{safe}.lock"


@contextmanager
def flock_acquire(path: str | Path, timeout: float = 1800.0) -> Iterator[None]:
    """阻塞+超时获取全局文件锁；进程退出自动释放，不会死锁。"""
    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(f"获取全局锁超时（{timeout:.0f}s）：{lock}") from None
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
