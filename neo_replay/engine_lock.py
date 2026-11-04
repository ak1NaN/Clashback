"""Shared lock between the player and updater."""
from contextlib import contextmanager
import fcntl

from .paths import CACHE, ENGINE_LOCK, RUNTIME


@contextmanager
def engine_access(*, updating=False):
    """Prevent changing engine/assets while a replay is using them."""
    ENGINE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with ENGINE_LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, (fcntl.LOCK_EX if updating else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("请先关闭回放窗口再更新；如果正在更新，请等更新完成后打开回放。") from None
        try:
            if updating:
                # Also recognize windows started before this lock was introduced.
                for sessions in (CACHE / "sessions", RUNTIME / "sessions"):
                    for lease in sessions.glob("*/.tick-cache.lock"):
                        with lease.open("a") as active:
                            try:
                                fcntl.flock(active, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                raise RuntimeError("仍有回放窗口使用游戏资源，请关闭后重新更新。") from None
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
