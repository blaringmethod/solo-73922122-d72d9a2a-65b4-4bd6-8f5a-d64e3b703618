"""API 端到端测试：追加、压实、流式读取、快照隔离、回收与恢复。"""

from __future__ import annotations

import json
import os
import threading
import time

from app import config, storage
from app.main import app
from fastapi.testclient import TestClient


def _rec(vid: str, ts: int, x: float = 0.0, y: float = 0.0) -> dict:
    return {"vehicle_id": vid, "utc_ts": ts, "x": x, "y": y}


def _append(client: TestClient, day: str, records: list[dict]) -> dict:
    resp = client.post(f"/api/v1/partitions/{day}/batches", json={"records": records})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _read_all(client: TestClient, day: str) -> tuple[list[dict], dict]:
    resp = client.get(f"/api/v1/partitions/{day}/records")
    assert resp.status_code == 200
    lines = [json.loads(line) for line in resp.content.splitlines() if line.strip()]
    return lines, dict(resp.headers)


# ---------------------------------------------------------------------------
# 基础功能
# ---------------------------------------------------------------------------

def test_health_root_and_openapi_examples(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    root = client.get("/").json()
    assert root["service"] == "bus-position-partition"

    schema = client.get("/openapi.json").json()
    append_ep = schema["paths"]["/api/v1/partitions/{day}/batches"]["post"]
    examples = append_ep["requestBody"]["content"]["application/json"]["examples"]
    assert set(examples) == {"normal", "with_duplicates"}
    # 压实与读取示例也在文档中
    compact_ep = schema["paths"]["/api/v1/partitions/{day}/compact"]["post"]
    assert compact_ep["summary"]
    read_ep = schema["paths"]["/api/v1/partitions/{day}/records"]["get"]
    assert "application/x-ndjson" in read_ep["responses"]["200"]["content"]


def test_day_path_validation(client: TestClient) -> None:
    assert client.post(
        "/api/v1/partitions/2026-13-40/batches", json={"records": [_rec("B1", 1)]}
    ).status_code == 422
    assert client.post(
        "/api/v1/partitions/20261111/batches", json={"records": [_rec("B1", 1)]}
    ).status_code == 422


def test_batch_size_bounds(client: TestClient, day: str) -> None:
    # 空批次 422
    assert client.post(
        f"/api/v1/partitions/{day}/batches", json={"records": []}
    ).status_code == 422
    # 1001 条 422
    too_many = {"records": [_rec("B1", i) for i in range(1001)]}
    assert client.post(
        f"/api/v1/partitions/{day}/batches", json=too_many
    ).status_code == 422
    # 1000 条通过
    ok = {"records": [_rec("B1", i) for i in range(1000)]}
    assert client.post(
        f"/api/v1/partitions/{day}/batches", json=ok
    ).status_code == 201


def test_strict_field_validation(client: TestClient, day: str) -> None:
    # utc_ts 必须是整数秒（不接受字符串）
    bad = {"records": [{"vehicle_id": "B1", "utc_ts": "1762848000", "x": 1, "y": 2}]}
    assert client.post(
        f"/api/v1/partitions/{day}/batches", json=bad
    ).status_code == 422
    # 多余字段拒绝
    extra = {"records": [{**_rec("B1", 1), "speed": 12.5}]}
    assert client.post(
        f"/api/v1/partitions/{day}/batches", json=extra
    ).status_code == 422
    # vehicle_id 不可为空
    empty_vid = {"records": [_rec("", 1)]}
    assert client.post(
        f"/api/v1/partitions/{day}/batches", json=empty_vid
    ).status_code == 422


def test_append_creates_immutable_jsonl(client: TestClient, day: str) -> None:
    res = _append(client, day, [_rec("BUS-1", 100, 1.5, 2.5), _rec("BUS-2", 101)])
    path = config.PARTITION_ROOT / day / res["file_name"]
    assert path.exists()
    content = path.read_text(encoding="utf-8").splitlines()
    assert len(content) == 2
    assert json.loads(content[0]) == {
        "vehicle_id": "BUS-1", "utc_ts": 100, "x": 1.5, "y": 2.5
    }
    # 没有临时文件残留
    assert not list((config.PARTITION_ROOT / day).glob("tmp-*"))


# ---------------------------------------------------------------------------
# 压实：保序、不去重、并发追加不丢
# ---------------------------------------------------------------------------

def test_compact_preserves_all_records_order_and_duplicates(
    client: TestClient, day: str
) -> None:
    b1 = [_rec("A", 1), _rec("A", 2)]
    b2 = [_rec("B", 3), _rec("A", 1), _rec("A", 2)]  # 与 b1 完全重复
    b3 = [_rec("C", 9)]
    _append(client, day, b1)
    _append(client, day, b2)
    _append(client, day, b3)

    before, _ = _read_all(client, day)
    expected = b1 + b2 + b3
    assert before == expected

    status = client.get(f"/api/v1/partitions/{day}/status").json()
    assert status["active_files"] == 3
    assert status["total_records"] == 6

    resp = client.post(f"/api/v1/partitions/{day}/compact")
    assert resp.status_code == 200
    compact = resp.json()
    assert compact["merged_source_files"] == 3
    assert compact["merged_records"] == 6  # 重复记录保留，不去重

    after, headers = _read_all(client, day)
    assert after == expected  # 顺序与内容完全一致
    assert len(headers["x-snapshot-files"].split(",")) == 1

    status = client.get(f"/api/v1/partitions/{day}/status").json()
    assert status["active_files"] == 1
    assert status["total_records"] == 6


def test_compact_nothing_to_do(client: TestClient) -> None:
    day = "2026-11-12"
    _append(client, day, [_rec("A", 1)])
    resp = client.post(f"/api/v1/partitions/{day}/compact").json()
    assert resp["merged_source_files"] == 0
    assert "无需压实" in resp["note"]


def test_append_during_compaction_is_not_lost(client: TestClient, day: str) -> None:
    for i in range(4):
        _append(client, day, [_rec(f"OLD-{i}", i)])

    compact_result: dict = {}
    barrier = threading.Event()

    def do_compact() -> None:
        # 等待读取快照建立后再压实，模拟压实与读取并发
        barrier.wait(timeout=5)
        compact_result.update(
            client.post(f"/api/v1/partitions/{day}/compact").json()
        )

    t = threading.Thread(target=do_compact)
    t.start()

    # 先建立读取快照（4 条），再放行压实，同时追加新批次
    resp = client.get(f"/api/v1/partitions/{day}/records")
    snapshot_records = [json.loads(x) for x in resp.content.splitlines()]
    assert len(snapshot_records) == 4
    barrier.set()

    new_batch = [_rec("NEW-1", 100), _rec("NEW-2", 101)]
    appended = _append(client, day, new_batch)
    t.join(timeout=10)

    assert compact_result["merged_records"] == 4
    # 新追加批次未被压实吞掉，独立 active 存在
    final_records, _ = _read_all(client, day)
    assert len(final_records) == 6
    assert final_records[-2:] == new_batch
    status = client.get(f"/api/v1/partitions/{day}/status").json()
    assert status["active_files"] == 2
    assert any(f["file_id"] == appended["file_id"] for f in status["files"])


def test_snapshot_is_stable_under_compact_and_append(
    client: TestClient, day: str
) -> None:
    import subprocess
    import sys

    for i in range(3):
        _append(client, day, [_rec(f"V{i}", i)])

    # 用底层 storage 拿到固定快照（此刻已持有当日共享锁）
    snapshot = storage.open_snapshot(day)
    snap_names = [sf.file_name for sf in snapshot.files]
    snap_total = snapshot.total_records

    # 压实放到独立子进程（模拟另一个 worker）：共享锁会让它阻塞，
    # 直到本读取释放；先启动它，再验证本次读取内容不受影响
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests._proc_helper", "compact",
         str(config.DATA_DIR), day]
    )
    # 给子进程一点时间进入等锁状态
    time.sleep(0.3)
    assert proc.poll() is None  # 被共享锁挡住，尚未完成

    _append(client, day, [_rec("LATE", 999)])  # 追加不走日锁，照常提交

    payload = b"".join(snapshot.stream_lines())  # 读取结束，释放共享锁
    streamed = [json.loads(line) for line in payload.splitlines() if line.strip()]
    assert len(streamed) == snap_total == 3
    assert len(snap_names) == 3
    assert all(r["vehicle_id"] != "LATE" for r in streamed)

    assert proc.wait(timeout=15) == 0  # 读取释放后子进程压实完成
    status = client.get(f"/api/v1/partitions/{day}/status").json()
    # 子进程在 LATE 追加之后才拿到锁，因此 4 个文件被一起合并为 1 个，
    # 关键是所有记录（含 LATE）一条不丢、顺序不变
    assert status["active_files"] == 1
    assert status["total_records"] == 4
    final_records, _ = _read_all(client, day)
    assert [r["vehicle_id"] for r in final_records] == ["V0", "V1", "V2", "LATE"]


# ---------------------------------------------------------------------------
# 回收：读取结束后才删旧文件
# ---------------------------------------------------------------------------

def test_sealed_files_reaped_after_readers_finish(client: TestClient) -> None:
    import subprocess
    import sys

    day = "2026-11-13"
    for i in range(3):
        _append(client, day, [_rec(f"R{i}", i)])

    client.post(f"/api/v1/partitions/{day}/compact")
    partition = config.PARTITION_ROOT / day

    def run_helper(action: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tests._proc_helper", action,
             str(config.DATA_DIR), day, *extra],
            capture_output=True, text=True, timeout=20,
        )

    # 持有读取快照（共享日锁）期间，独立进程的回收/排他锁都必须等待
    snapshot = storage.open_snapshot(day)
    try:
        # 非阻塞尝试拿排他日锁：应失败（退出码 1）
        assert run_helper("try_lock_ex").returncode == 1
        # 非阻塞回收同样拿不到锁（退出码 3），旧文件都还在
        assert run_helper("reap", "0").returncode == 3
        assert len(list(partition.glob("*.jsonl"))) >= 2
    finally:
        list(snapshot.stream_lines())  # 读完释放共享锁与租约

    # 读取结束后，独立进程以 0 宽限回收，成功删除旧文件
    time.sleep(0.05)
    assert run_helper("reap", "0").returncode == 0
    remaining = list(partition.glob("*.jsonl"))
    assert len(remaining) == 1  # 只剩合并后的文件
    records, _ = _read_all(client, day)
    assert [r["vehicle_id"] for r in records] == ["R0", "R1", "R2"]


# ---------------------------------------------------------------------------
# 并发与多日期隔离
# ---------------------------------------------------------------------------

def test_concurrent_appends_keep_all_records(client: TestClient) -> None:
    day = "2026-11-14"
    n_threads, per_thread = 8, 20

    def worker(tid: int) -> None:
        c = TestClient(app)
        for i in range(per_thread):
            _append(c, day, [_rec(f"T{tid}", i)])

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    records, headers = _read_all(client, day)
    assert len(records) == n_threads * per_thread
    # 序号严格单调、无重复 file_id
    status = client.get(f"/api/v1/partitions/{day}/status").json()
    sequences = [f["sequence"] for f in status["files"]]
    assert len(sequences) == len(set(sequences))
    assert headers["x-snapshot-records"] == str(n_threads * per_thread)


def test_locks_are_per_day_and_do_not_block_other_days(
    client: TestClient,
) -> None:
    day_a, day_b = "2026-11-15", "2026-11-16"
    for i in range(3):
        _append(client, day_a, [_rec(f"A{i}", i)])
        _append(client, day_b, [_rec(f"B{i}", i)])

    held = threading.Event()
    release = threading.Event()

    def hold_day_a_lock() -> None:
        lock = storage.DayLock(day_a)
        lock.acquire_exclusive()
        held.set()
        release.wait(timeout=10)
        lock.release()

    t = threading.Thread(target=hold_day_a_lock)
    t.start()
    held.wait(timeout=5)
    try:
        # A 日压实拿不到锁 -> 409
        resp = client.post(f"/api/v1/partitions/{day_a}/compact")
        assert resp.status_code == 409
        # B 日完全不受影响，照常压实
        ok = client.post(f"/api/v1/partitions/{day_b}/compact")
        assert ok.status_code == 200
        assert ok.json()["merged_records"] == 3
        # 追加不使用日锁，A 日追加也正常
        _append(client, day_a, [_rec("A-NEW", 1)])
    finally:
        release.set()
        t.join(timeout=10)


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------

def test_recovery_cleans_orphans_and_restores_compacting(
    client: TestClient,
) -> None:
    day = "2026-11-17"
    _append(client, day, [_rec("OK", 1), _rec("OK", 2)])
    partition = config.PARTITION_ROOT / day

    # 模拟崩溃残片：tmp 临时文件 + 无索引的孤儿 JSONL
    (partition / "tmp-orphan-1234.jsonl").write_text('{"x":1}\n', encoding="utf-8")
    (partition / "batch-orphan-9999.jsonl").write_text(
        '{"vehicle_id":"X","utc_ts":0,"x":0,"y":0}\n', encoding="utf-8"
    )

    # 模拟压实中途崩溃：索引里有 compacting 行
    from app import db

    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = db.list_active_files(conn, day)
        db.mark_compacting(conn, day, [int(r["file_id"]) for r in rows])
        conn.execute("COMMIT")

    storage.recover_on_startup()

    assert not list(partition.glob("tmp-*"))
    assert not list(partition.glob("batch-orphan-*"))
    status = client.get(f"/api/v1/partitions/{day}/status").json()
    assert status["compacting_files"] == 0
    assert status["active_files"] == 1
    records, _ = _read_all(client, day)
    assert len(records) == 2  # 已提交数据完整保留


def test_repeated_compaction_keeps_order(client: TestClient) -> None:
    day = "2026-11-18"
    _append(client, day, [_rec("P", 1)])
    _append(client, day, [_rec("Q", 2)])
    client.post(f"/api/v1/partitions/{day}/compact")
    _append(client, day, [_rec("R", 3)])
    _append(client, day, [_rec("S", 4)])
    client.post(f"/api/v1/partitions/{day}/compact")

    records, _ = _read_all(client, day)
    assert [r["vehicle_id"] for r in records] == ["P", "Q", "R", "S"]
    status = client.get(f"/api/v1/partitions/{day}/status").json()
    assert status["active_files"] == 1
    assert status["total_records"] == 4
