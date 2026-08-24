import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockTimeoutError(TimeoutError):
    """获取全局锁超时。"""


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
