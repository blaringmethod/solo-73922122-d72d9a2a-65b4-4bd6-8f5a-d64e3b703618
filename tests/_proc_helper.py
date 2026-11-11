"""测试专用子进程入口：以独立进程身份执行压实/回收，验证跨进程 flock。

用法：
  python -m tests._proc_helper compact         <data_dir> <day>   # 阻塞等锁
  python -m tests._proc_helper reap            <data_dir> <day> [grace_seconds]
  python -m tests._proc_helper try_lock_ex     <data_dir> <day>   # 非阻塞
"""

from __future__ import annotations

import sys
from pathlib import Path

from app import config, db, storage


def _bind(data_dir: str) -> None:
    config.DATA_DIR = Path(data_dir).resolve()
    config.PARTITION_ROOT = config.DATA_DIR / "partitions"
    config.LOCK_DIR = config.DATA_DIR / "locks"
    config.DB_PATH = config.DATA_DIR / "index.sqlite3"
    db.init_db()


def main() -> int:
    action, data_dir, day = sys.argv[1], sys.argv[2], sys.argv[3]
    _bind(data_dir)

    if action in ("compact", "compact_blocking"):
        try:
            storage.compact_day(day, blocking=True)
        except storage.CompactionNothingToDo:
            return 0
        return 0

    if action == "reap":
        if len(sys.argv) > 4:
            config.REAP_GRACE_SECONDS = int(sys.argv[4])
        try:
            storage.reap_day(day, blocking=False)
        except storage.ReapBusy:
            return 3
        return 0

    if action == "try_lock_ex":
        lock = storage.DayLock(day)
        ok = lock.acquire_exclusive(blocking=False)
        if ok:
            lock.release()
            return 0
        return 1

    print(f"unknown action: {action}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
