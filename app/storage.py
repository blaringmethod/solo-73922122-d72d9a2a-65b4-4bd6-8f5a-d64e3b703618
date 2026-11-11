"""分区存储层：不可变 JSONL 文件 + 按日 flock + 压实/读取/回收。

并发模型（两个 uvicorn worker 进程）
------------------------------------
* ``locks/<day>.lock`` 上的 flock：
  - 读取快照持有**当日共享锁（LOCK_SH）**，多个读取可并行，请求结束才释放；
  - 压实与回收持有**当日排他锁（LOCK_EX）**，与读取互斥、与同日其他压实互斥；
  - 锁是按日的，处理 A 日完全不阻塞 B 日；追加不获取日锁，只走 SQLite
    毫秒级短事务，因此压实期间其他日期（乃至同日）的追加照常提交。
* 可见性 = JSONL 原子 rename 完成 **且** SQLite 索引事务 commit。
* 读取租约（db.read_leases）与 flock 构成双保险：回收既要查无活跃租约，
  又要拿到排他锁，保证正在进行的读取不会遇到文件被删。

崩溃安全
--------
* 写入：临时文件 ``tmp-*.jsonl`` -> fsync -> rename -> 索引事务；任何一步
  崩溃都不会留下可见残片，重启时孤儿 tmp 文件被清理。
* 压实：先把入选文件在事务中标记 compacting（崩溃后启动恢复为 active），
  合并写临时文件，最后单事务切换（旧文件 sealed + 新文件 active）。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from . import config, db
from .schemas import PositionRecord

# JSONL 文件名前缀，便于与临时文件区分
ACTIVE_PREFIX = "batch-"
TMP_PREFIX = "tmp-"
SUFFIX = ".jsonl"


# ---------------------------------------------------------------------------
# 按日文件锁
# ---------------------------------------------------------------------------

class DayLock:
    """对 locks/<day>.lock 的 flock 封装。"""

    def __init__(self, day: str) -> None:
        self.day = day
        self._fd: Optional[int] = None

    def acquire_shared(self) -> "DayLock":
        config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOCK_DIR / f"{self.day}.lock"
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_SH)
        return self

    def acquire_exclusive(self, blocking: bool = True) -> bool:
        config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        path = config.LOCK_DIR / f"{self.day}.lock"
        self._fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self._fd, flags)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(self._fd)
                self._fd = None
                return False
            raise
        return True

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "DayLock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@contextlib.contextmanager
def shared_file_lock(path: Path) -> Iterator[None]:
    """对单个数据文件加共享锁（读取期间防止被回收删除）。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# 路径与原子写入
# ---------------------------------------------------------------------------

def day_dir(day: str) -> Path:
    path = config.PARTITION_ROOT / day
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fsync_dir(path: Path) -> None:
    """目录 fsync，保证 rename 在掉电后也落盘。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_jsonl(
    directory: Path, final_name: str, lines: list[str]
) -> tuple[int, str]:
    """写临时文件 -> fsync -> 原子 rename。返回 (字节数, sha256)。"""
    tmp_name = f"{TMP_PREFIX}{uuid.uuid4().hex}-{final_name}"
    tmp_path = directory / tmp_name
    final_path = directory / final_name
    payload = "".join(lines).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise
    else:
        os.close(fd)
    os.rename(tmp_path, final_path)  # 同目录原子替换
    _fsync_dir(directory)
    return len(payload), digest


def _record_line(rec: PositionRecord) -> str:
    return json.dumps(
        {
            "vehicle_id": rec.vehicle_id,
            "utc_ts": rec.utc_ts,
            "x": rec.x,
            "y": rec.y,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


# ---------------------------------------------------------------------------
# 追加
# ---------------------------------------------------------------------------

@dataclass
class AppendResult:
    file_id: int
    file_name: str
    sequence: int
    record_count: int
    byte_size: int
    checksum: str


class SequenceConflict(RuntimeError):
    """序号冲突（理论上仅在极端并发下出现），调用方可重试。"""


def append_batch(day: str, records: list[PositionRecord]) -> AppendResult:
    """追加一批记录为不可变 JSONL，并登记索引。

    流程：先原子写好 UUID 命名的 JSONL（此时无索引行，对读取不可见；
    即使崩溃也由启动孤儿清理删除），再在**单个 SQLite 写事务**内取当日
    序号并登记索引。事务串行化决定批次提交顺序，并发追加只会在唯一
    约束上短暂冲突并重试，不会丢批次。
    """
    directory = day_dir(day)
    lines = [_record_line(r) for r in records]

    # 文件名先不含序号，避免序号竞争；序号在索引事务内确定
    final_name = f"{ACTIVE_PREFIX}{uuid.uuid4().hex}{SUFFIX}"
    byte_size, checksum = _atomic_write_jsonl(directory, final_name, lines)

    for attempt in range(5):
        try:
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                sequence = db.next_sequence(conn, day)
                file_id = db.insert_file(
                    conn,
                    day=day,
                    file_name=final_name,
                    sequence=sequence,
                    record_count=len(records),
                    byte_size=byte_size,
                    checksum=checksum,
                )
                conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:  # 极端并发下的序号竞争
            if attempt == 4:
                with contextlib.suppress(FileNotFoundError):
                    (directory / final_name).unlink()
                raise SequenceConflict("序号冲突重试耗尽") from exc
            continue
        return AppendResult(
            file_id=file_id,
            file_name=final_name,
            sequence=sequence,
            record_count=len(records),
            byte_size=byte_size,
            checksum=checksum,
        )
    raise SequenceConflict("不可达")  # pragma: no cover


# ---------------------------------------------------------------------------
# 压实
# ---------------------------------------------------------------------------

class CompactionBusy(RuntimeError):
    """该日已有压实/回收在进行。"""


class CompactionNothingToDo(RuntimeError):
    """当日 active 文件不足 2 个，无需压实。"""


class CompactionFailed(RuntimeError):
    """合并过程出错，旧分区状态保持不变。"""


def compact_day(day: str, *, blocking: bool = False) -> dict:
    """压实当日碎文件，返回压实结果描述。

    默认非阻塞：同日已有压实/回收/读取时抛 :class:`CompactionBusy`。
    传入 ``blocking=True`` 则排队等待排他日锁（读取释放后自动获得）。

    顺序保证：被选中的 active 文件按 (sequence, file_id) 排序，逐文件
    逐行原样拷贝，因此「批次提交顺序 + 批内原序」完整保留，不去重。
    压实期间新追加的批次不在快照内、不会丢失，仍是独立 active 文件。
    """
    lock = DayLock(day)
    if not lock.acquire_exclusive(blocking=blocking):
        raise CompactionBusy(f"运营日 {day} 正在压实或回收")
    try:
        return _compact_locked(day)
    finally:
        lock.release()


def _compact_locked(day: str) -> dict:
    """调用方必须已持有当日排他日锁。"""
    directory = day_dir(day)
    # 1. 短事务选定文件并标记 compacting（不把文件 IO 包在事务里）
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = db.list_active_files(conn, day)
        if len(active) < 2:
            conn.execute("COMMIT")
            raise CompactionNothingToDo(
                f"运营日 {day} 仅有 {len(active)} 个文件，无需压实"
            )
        source_ids = [int(r["file_id"]) for r in active]
        source_names = [r["file_name"] for r in active]
        expected_records = sum(int(r["record_count"]) for r in active)
        expected_bytes = sum(int(r["byte_size"]) for r in active)
        merged_sequence = min(int(r["sequence"]) for r in active)
        db.mark_compacting(conn, day, source_ids)
        conn.execute("COMMIT")

    # 2. 流式合并到临时文件（事务外，不占 SQLite 写锁）
    merged_name = (
        f"{ACTIVE_PREFIX}merged-{merged_sequence:012d}-"
        f"{uuid.uuid4().hex}{SUFFIX}"
    )
    tmp_path = directory / f"{TMP_PREFIX}{uuid.uuid4().hex}-{merged_name}"
    merged_records = 0
    hasher = hashlib.sha256()
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            for name in source_names:
                src = directory / name
                with shared_file_lock(src):  # 与回收互斥（压实持 EX 时冗余保护）
                    with open(src, "rb") as sf:
                        for line in sf:
                            # 逐行校验是合法 JSON 且字段齐全
                            obj = json.loads(line.decode("utf-8"))
                            for key in ("vehicle_id", "utc_ts", "x", "y"):
                                if key not in obj:
                                    raise CompactionFailed(
                                        f"源文件 {name} 存在缺字段记录"
                                    )
                            os.write(fd, line)
                            hasher.update(line)
                            merged_records += 1
            os.fsync(fd)
        finally:
            os.close(fd)

        if merged_records != expected_records:
            raise CompactionFailed(
                f"合并记录数 {merged_records} 与索引 {expected_records} 不一致"
            )

        # 3. 原子 rename 合并文件
        final_path = directory / merged_name
        os.rename(tmp_path, final_path)
        _fsync_dir(directory)
        merged_bytes = final_path.stat().st_size
        checksum = hasher.hexdigest()
        if merged_bytes != expected_bytes:
            # 字节数不一致说明行内容被改动过，拒绝切换
            raise CompactionFailed(
                f"合并字节数 {merged_bytes} 与索引 {expected_bytes} 不一致"
            )

        # 4. 单事务索引切换：旧文件 sealed + 新文件 active
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            merged_id = db.switch_compacted(
                conn,
                day,
                source_ids,
                merged_file_name=merged_name,
                merged_sequence=merged_sequence,
                merged_records=merged_records,
                merged_bytes=merged_bytes,
                merged_checksum=checksum,
            )
            conn.execute("COMMIT")
    except BaseException:
        # 任何失败：回滚 compacting 状态、清理临时/已改名的合并文件，
        # 旧分区原样可用（合并文件此时尚无 active 索引行，对读取不可见）
        with contextlib.suppress(Exception):
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                db.rollback_compacting(conn, day)
                conn.execute("COMMIT")
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            (directory / merged_name).unlink()
        _fsync_dir(directory)
        raise

    # 5. 旧文件回收（无活跃读取时立即回收，否则留待后续 reap）
    reaped = _reap_locked(day)

    return {
        "merged_file_id": merged_id,
        "merged_file_name": merged_name,
        "merged_records": merged_records,
        "merged_source_files": len(source_ids),
        "sealed_files": len(source_ids),
        "reaped_files": reaped,
    }


# ---------------------------------------------------------------------------
# 旧文件回收
# ---------------------------------------------------------------------------

def _reap_locked(day: str) -> int:
    """调用方已持有该日排他锁。删除满足条件的 sealed 文件及其索引行。

    条件：sealed 超过宽限期，且无 TTL 内活跃读取租约。flock 排他锁
    保证没有任何读取正打开这些文件。
    """
    directory = day_dir(day)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        db.purge_expired_leases(conn, db.lease_cutoff_iso())
        # 宽限只取 REAP_GRACE_SECONDS：活跃租约由上面的 NOT EXISTS 子查询
        # 单独拦截，TTL 仅用于清理持有者已崩溃的过期租约
        cutoff = db.grace_cutoff_iso(config.REAP_GRACE_SECONDS)
        candidates = db.list_sealed_candidates(conn, day, cutoff)
        if not candidates:
            conn.execute("COMMIT")
            return 0
        removed = 0
        for row in candidates:
            path = directory / row["file_name"]
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            removed += 1
        db.delete_file_rows(conn, [int(r["file_id"]) for r in candidates])
        conn.execute("COMMIT")
        _fsync_dir(directory)
        return removed


def reap_day(day: str, blocking: bool = False) -> int:
    """回收接口。

    ``blocking=False`` 时拿不到排他日锁（有读取/压实进行中）抛出
    :class:`ReapBusy`；拿到锁但没有满足条件的 sealed 文件则返回 0。
    """
    lock = DayLock(day)
    if not lock.acquire_exclusive(blocking=blocking):
        raise ReapBusy(f"运营日 {day} 有读取或压实进行中，跳过回收")
    try:
        return _reap_locked(day)
    finally:
        lock.release()


class ReapBusy(RuntimeError):
    """该日有读取或压实进行中，回收暂时无法执行。"""


# ---------------------------------------------------------------------------
# 流式读取（请求开始即固定快照）
# ---------------------------------------------------------------------------

@dataclass
class SnapshotFile:
    file_id: int
    file_name: str
    sequence: int
    record_count: int


@dataclass
class ReadSnapshot:
    day: str
    lease_id: str
    files: list[SnapshotFile]
    total_records: int
    # 当日锁的共享版本：请求开始即持有，直到流结束释放，与压实/回收的
    # 排他日锁互斥（不同日期互不影响），保证快照文件在读取期间不被删除
    _day_lock: "DayLock"

    def stream_lines(self) -> Iterator[bytes]:
        """按快照顺序逐块产出 JSONL 字节。

        共享日锁在 open_snapshot 时已持有：压实与回收的排他日锁必须
        等待本读取结束，因此本次内容不受压实与追加影响。
        """
        directory = config.PARTITION_ROOT / self.day
        try:
            for sf in self.files:
                path = directory / sf.file_name
                if not path.exists():
                    # 双保险下不应发生（排他日锁拿不到就不能删）
                    raise FileNotFoundError(
                        f"快照文件 {sf.file_name} 已被回收"
                    )
                db.heartbeat(self.lease_id)  # 每个文件开始前置续租约
                with open(path, "rb") as sf_handle:
                    while True:
                        chunk = sf_handle.read(config.STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        yield chunk
        finally:
            self.release()

    def release(self) -> None:
        """释放共享日锁并删除读取租约（幂等）。"""
        self._day_lock.release()
        db.drop_lease(self.lease_id)


def open_snapshot(day: str) -> ReadSnapshot:
    """请求开始时固定本次读取的文件集合，并登记读取租约。

    顺序：先在事务内取快照 + 建租约，事务提交后立即获取当日共享锁
    （与压实/回收排他锁互斥、与其他读取共享），再复查快照文件仍在；
    若期间发生了索引切换（极端时序），释放锁重试整个流程。
    """
    directory = day_dir(day)  # 确保目录存在
    for _attempt in range(5):
        day_lock = DayLock(day).acquire_shared()
        try:
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                db.purge_expired_leases(conn, db.lease_cutoff_iso())
                rows = db.list_active_files(conn, day)
                lease_id = db.create_lease(conn, day)
                conn.execute("COMMIT")

            files = [
                SnapshotFile(
                    file_id=int(r["file_id"]),
                    file_name=r["file_name"],
                    sequence=int(r["sequence"]),
                    record_count=int(r["record_count"]),
                )
                for r in rows
            ]

            # 持锁后复查：任何快照文件缺失说明与压实/回收发生了交错
            if any(not (directory / sf.file_name).exists() for sf in files):
                day_lock.release()
                db.drop_lease(lease_id)
                continue

            return ReadSnapshot(
                day=day,
                lease_id=lease_id,
                files=files,
                total_records=sum(f.record_count for f in files),
                _day_lock=day_lock,
            )
        except BaseException:
            day_lock.release()
            raise
    raise CompactionFailed("获取读取快照重试耗尽")  # pragma: no cover


# ---------------------------------------------------------------------------
# 启动恢复
# ---------------------------------------------------------------------------

def recover_on_startup() -> None:
    """进程重启后的清理（每个 worker 启动执行一次，幂等）。

    1. compacting 状态恢复为 active（压实事务未完成）；
    2. 清理各分区目录中的 tmp- 孤儿临时文件；
    3. 清理磁盘上没有索引行的孤儿 JSONL（写了文件没来得及登记即崩溃，
       该批次按未提交处理）。
    """
    db.recover_compacting_on_startup()
    root = config.PARTITION_ROOT
    if not root.exists():
        return
    for day_path in sorted(p for p in root.iterdir() if p.is_dir()):
        with contextlib.suppress(Exception):
            _recover_partition_dir(day_path)


def _recover_partition_dir(directory: Path) -> None:
    day = directory.name
    indexed: set[str] = set()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT file_name FROM files WHERE day=?", (day,)
        ).fetchall()
        indexed = {r["file_name"] for r in rows}

    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        if entry.name.startswith(TMP_PREFIX):
            with contextlib.suppress(OSError):
                entry.unlink()
        elif entry.name.endswith(SUFFIX) and entry.name not in indexed:
            # 无索引的孤儿文件：写入未完成提交，删除避免残片可见
            with contextlib.suppress(OSError):
                entry.unlink()
    _fsync_dir(directory)
