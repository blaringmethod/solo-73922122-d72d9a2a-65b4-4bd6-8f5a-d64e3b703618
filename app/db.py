"""SQLite 文件索引与读取租约。

设计要点
--------
1. WAL + synchronous=NORMAL：崩溃/掉电后已 commit 的事务不丢，未 commit 的
   残片事务由 SQLite 自动回滚，索引不会出现半条状态。
2. 每个操作开短连接（``with connect()``），配合 busy_timeout，两个 worker
   进程可并发读、串行写；写事务只包住索引更新（毫秒级），不包住文件 IO。
3. ``files`` 表是唯一可见性来源：JSONL 只有完成原子 rename 且索引行
   commit 后才对读取可见。
4. ``read_leases`` 记录流式读取开始时持有的快照；回收旧文件前必须确认
   不存在 TTL 内的活跃租约（再叠加按日 flock 双保险）。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    day           TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    sequence      INTEGER NOT NULL,           -- 当日全局单调序号（提交顺序）
    record_count  INTEGER NOT NULL,
    byte_size     INTEGER NOT NULL,
    checksum      TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'active',
                  -- active: 可见；compacting: 压实合并中；sealed: 待回收
    created_at    TEXT NOT NULL,
    sealed_at     TEXT
);

-- 同一天内序号唯一；sealed 行已退出可见集合，合并文件可继承被合并文件
-- 的最小序号，因此唯一约束只覆盖 active/compacting 行
CREATE UNIQUE INDEX IF NOT EXISTS ux_files_day_sequence
    ON files(day, sequence) WHERE state != 'sealed';
CREATE INDEX IF NOT EXISTS ix_files_day_state ON files(day, state);

CREATE TABLE IF NOT EXISTS read_leases (
    lease_id     TEXT PRIMARY KEY,
    day          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_beat_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leases_day_beat ON read_leases(day, last_beat_at);
"""


def _utcnow_iso() -> str:
    # 统一偏移量格式 +00:00（不用后缀 Z），保证字符串词法顺序与时间顺序
    # 一致，sealed_at / last_beat_at 的字符串比较才正确
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def init_db() -> None:
    """建库建表（幂等），每个 worker 启动时各执行一次。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PARTITION_ROOT.mkdir(parents=True, exist_ok=True)
    config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个短连接，自动 commit/rollback/关闭。"""
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=config.SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # 显式 BEGIN，事务边界完全自控
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=%d" % config.SQLITE_BUSY_TIMEOUT_MS)
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 文件索引
# ---------------------------------------------------------------------------

def insert_file(
    conn: sqlite3.Connection,
    *,
    day: str,
    file_name: str,
    sequence: int,
    record_count: int,
    byte_size: int,
    checksum: str,
) -> int:
    """在已有事务内插入一条 active 文件索引，返回 file_id。"""
    cur = conn.execute(
        """
        INSERT INTO files (day, file_name, sequence, record_count, byte_size,
                           checksum, state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (day, file_name, sequence, record_count, byte_size, checksum,
         _utcnow_iso()),
    )
    return int(cur.lastrowid)


def next_sequence(conn: sqlite3.Connection, day: str) -> int:
    """当日下一个序号 = 现存文件最大序号 + 1（sealed 文件也占位，永不复用）。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_seq FROM files WHERE day = ?",
        (day,),
    ).fetchone()
    return int(row["next_seq"])


def list_active_files(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    """读取快照：请求开始时刻所有 active 文件，按提交顺序 + 批内顺序排列。"""
    return list(
        conn.execute(
            """
            SELECT file_id, file_name, sequence, record_count, byte_size,
                   checksum, state, created_at
            FROM files
            WHERE day = ? AND state = 'active'
            ORDER BY sequence ASC, file_id ASC
            """,
            (day,),
        )
    )


def mark_compacting(
    conn: sqlite3.Connection, day: str, file_ids: list[int]
) -> None:
    conn.executemany(
        "UPDATE files SET state='compacting' WHERE file_id = ? AND day = ?",
        [(fid, day) for fid in file_ids],
    )


def switch_compacted(
    conn: sqlite3.Connection,
    day: str,
    source_ids: list[int],
    *,
    merged_file_name: str,
    merged_sequence: int,
    merged_records: int,
    merged_bytes: int,
    merged_checksum: str,
) -> int:
    """一个事务内完成索引切换：旧文件 sealed + 新文件 active。

    任何一步失败整个事务回滚，外部 JSONL 临时文件由调用方清理，旧分区
    状态保持不变。
    """
    now = _utcnow_iso()
    conn.executemany(
        "UPDATE files SET state='sealed', sealed_at=? "
        "WHERE file_id=? AND day=? AND state='compacting'",
        [(now, fid, day) for fid in source_ids],
    )
    cur = conn.execute(
        """
        INSERT INTO files (day, file_name, sequence, record_count, byte_size,
                           checksum, state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (day, merged_file_name, merged_sequence, merged_records, merged_bytes,
         merged_checksum, now),
    )
    return int(cur.lastrowid)


def rollback_compacting(conn: sqlite3.Connection, day: str) -> int:
    """压实失败：compacting 全部恢复为 active。返回恢复行数。"""
    cur = conn.execute(
        "UPDATE files SET state='active' WHERE day=? AND state='compacting'",
        (day,),
    )
    return cur.rowcount


def recover_compacting_on_startup() -> None:
    """进程重启：上次崩溃时停在 compacting 的文件恢复为 active。

    对应的临时合并文件（tmp- 前缀）由分区层在启动时按孤儿文件清理。
    """
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE files SET state='active' WHERE state='compacting'")
        conn.execute("COMMIT")


def list_sealed_candidates(
    conn: sqlite3.Connection, day: str, grace_before: str
) -> list[sqlite3.Row]:
    """超过宽限期的 sealed 文件，且当前没有该日的活跃读取租约。"""
    return list(
        conn.execute(
            """
            SELECT f.* FROM files f
            WHERE f.day = ? AND f.state = 'sealed'
              AND f.sealed_at IS NOT NULL AND f.sealed_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM read_leases r
                  WHERE r.day = ? AND r.last_beat_at >= ?
              )
            ORDER BY f.sequence
            """,
            (day, grace_before, day, grace_before),
        )
    )


def delete_file_rows(conn: sqlite3.Connection, file_ids: list[int]) -> None:
    conn.executemany(
        "DELETE FROM files WHERE file_id = ? AND state = 'sealed'",
        [(fid,) for fid in file_ids],
    )


def partition_status(conn: sqlite3.Connection, day: str) -> dict:
    rows = list(
        conn.execute(
            "SELECT * FROM files WHERE day = ? ORDER BY sequence, file_id",
            (day,),
        )
    )
    active = [r for r in rows if r["state"] == "active"]
    return {
        "day": day,
        "active_files": len(active),
        "compacting_files": sum(1 for r in rows if r["state"] == "compacting"),
        "sealed_files": sum(1 for r in rows if r["state"] == "sealed"),
        "total_records": sum(r["record_count"] for r in active),
        "total_active_bytes": sum(r["byte_size"] for r in active),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# 读取租约
# ---------------------------------------------------------------------------

def create_lease(conn: sqlite3.Connection, day: str) -> str:
    lease_id = uuid.uuid4().hex
    now = _utcnow_iso()
    conn.execute(
        "INSERT INTO read_leases (lease_id, day, created_at, last_beat_at) "
        "VALUES (?, ?, ?, ?)",
        (lease_id, day, now, now),
    )
    return lease_id


def heartbeat(lease_id: str) -> None:
    """流式读取过程中续租，避免长读取超过 TTL 被误判为崩溃。"""
    with connect() as conn:
        conn.execute(
            "UPDATE read_leases SET last_beat_at=? WHERE lease_id=?",
            (_utcnow_iso(), lease_id),
        )


def drop_lease(lease_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM read_leases WHERE lease_id=?", (lease_id,))


def purge_expired_leases(conn: sqlite3.Connection, cutoff: str) -> None:
    """清理超过 TTL 的租约（持有者进程已崩溃的残留）。"""
    conn.execute(
        "DELETE FROM read_leases WHERE last_beat_at < ?", (cutoff,)
    )


def grace_cutoff_iso(grace_seconds: int) -> str:
    """reap 判定用的 ISO 时间阈值（UTC，词法与时间顺序一致）。"""
    dt = datetime.now(timezone.utc).timestamp() - grace_seconds
    return datetime.fromtimestamp(dt, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f+00:00"
    )


def lease_cutoff_iso() -> str:
    return grace_cutoff_iso(config.LEASE_TTL_SECONDS)
