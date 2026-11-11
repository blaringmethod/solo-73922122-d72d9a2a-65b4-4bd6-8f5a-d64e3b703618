"""运行配置：全部可通过环境变量覆盖，便于容器部署。"""

from __future__ import annotations

import os
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


# 数据根目录：data/partitions/<day>/ 放 JSONL，data/index.sqlite3 为索引库
DATA_DIR = Path(os.environ.get("BUS_DATA_DIR", "/data")).resolve()
PARTITION_ROOT = DATA_DIR / "partitions"
LOCK_DIR = DATA_DIR / "locks"
DB_PATH = DATA_DIR / "index.sqlite3"

# 单批记录数上限（需求：1～1000）
MAX_BATCH_RECORDS = _env_int("BUS_MAX_BATCH_RECORDS", 1000)

# SQLite 写锁等待（毫秒），两个 worker 并发写时短暂排队，不报错
SQLITE_BUSY_TIMEOUT_MS = _env_int("BUS_SQLITE_BUSY_TIMEOUT_MS", 30_000)

# 读取租约 TTL（秒）：流式读取每块数据续租；超过 TTL 视为持有者已崩溃
LEASE_TTL_SECONDS = _env_int("BUS_LEASE_TTL_SECONDS", 120)

# 压实后旧文件回收：除租约外再保留的宽限秒数，给崩溃租约留清理窗口
REAP_GRACE_SECONDS = _env_int("BUS_REAP_GRACE_SECONDS", 30)

# 流式读取每批 yield 的字节数
STREAM_CHUNK_BYTES = _env_int("BUS_STREAM_CHUNK_BYTES", 64 * 1024)
