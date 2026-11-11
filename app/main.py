"""公交定位分区 API 入口。

路由：
* POST   /api/v1/partitions/{day}/batches           追加一批（1～1000 条）
* POST   /api/v1/partitions/{day}/compact           按日压实碎文件
* GET    /api/v1/partitions/{day}/records           流式读取（开始即快照）
* GET    /api/v1/partitions/{day}/status            分区文件索引状态
* POST   /api/v1/partitions/{day}/reap              手动回收 sealed 旧文件
* GET    /health、/                                  运维入口

交互式文档：/docs（Swagger UI，含追加/压实/读取示例）、/redoc。
启动：uvicorn app.main:app --workers 2 --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Path
from fastapi.responses import StreamingResponse

from . import db, storage
from .schemas import (
    AppendBatchRequest,
    AppendBatchResponse,
    CompactResponse,
    PartitionFileInfo,
    PartitionStatusResponse,
    ReapResponse,
    append_openapi_examples,
    validate_day,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    # 每个 worker 启动：建库 + 崩溃恢复（compacting 回滚、孤儿文件清理）
    db.init_db()
    storage.recover_on_startup()
    yield


app = FastAPI(
    title="公交定位分区服务",
    version="1.0.0",
    description=(
        "按运营日接收车载定位小批次（1～1000 条/批），每批保存为不可变 "
        "JSONL 文件，SQLite 记录文件索引；提供按日压实与流式读取。\n\n"
        "关键语义：\n"
        "- 追加：临时文件 fsync 后原子 rename + 索引事务，残片永不可见；\n"
        "- 压实：保留全部记录、批次提交顺序与批内原序，不去重，并发追加"
        "不丢失；\n"
        "- 读取：请求开始即固定文件快照，压实/追加不改变本次内容；\n"
        "- 回收：旧文件在所有读取结束（租约 + 按日文件锁双保险）后才删除；\n"
        "- 并发：按运营日 flock，两个 worker 下不同日期互不阻塞。"
    ),
    lifespan=lifespan,
)


def _check_day(day: str) -> str:
    try:
        return validate_day(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 运维
# ---------------------------------------------------------------------------

@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["运维"])
def root() -> dict[str, Any]:
    return {
        "service": "bus-position-partition",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "endpoints": {
            "append": "POST /api/v1/partitions/{day}/batches",
            "compact": "POST /api/v1/partitions/{day}/compact",
            "read": "GET /api/v1/partitions/{day}/records",
            "status": "GET /api/v1/partitions/{day}/status",
            "reap": "POST /api/v1/partitions/{day}/reap",
        },
    }


# ---------------------------------------------------------------------------
# 追加
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/partitions/{day}/batches",
    response_model=AppendBatchResponse,
    status_code=201,
    tags=["定位分区"],
    summary="追加一批定位记录（每批 1～1000 条，落为不可变 JSONL）",
)
def append_batch(
    day: str = Path(..., description="运营日 YYYY-MM-DD"),
    request: AppendBatchRequest = Body(
        ..., openapi_examples=append_openapi_examples()
    ),
) -> AppendBatchResponse:
    day = _check_day(day)
    try:
        result = storage.append_batch(day, request.records)
    except storage.SequenceConflict as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return AppendBatchResponse(
        day=day,
        file_id=result.file_id,
        file_name=result.file_name,
        record_count=result.record_count,
        byte_size=result.byte_size,
        sequence=result.sequence,
        checksum_sha256=result.checksum,
    )


# ---------------------------------------------------------------------------
# 压实
# ---------------------------------------------------------------------------


@app.post(
    "/api/v1/partitions/{day}/compact",
    response_model=CompactResponse,
    tags=["定位分区"],
    summary="按日压实：合并碎文件，保留全部记录与顺序，不去重",
    responses={
        409: {"description": "该日已有压实/回收或有读取进行中（按日锁，不影响其他日期）"},
        422: {"description": "运营日格式非法"},
    },
)
def compact_day(
    day: str = Path(..., description="运营日 YYYY-MM-DD"),
) -> CompactResponse:
    day = _check_day(day)
    try:
        result = storage.compact_day(day)
    except storage.CompactionBusy as exc:
        # 同日已有压实进行：非全局锁，其他日期不受影响
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except storage.CompactionNothingToDo as exc:
        return CompactResponse(
            day=day,
            merged_file_id=0,
            merged_file_name="",
            merged_records=0,
            merged_source_files=0,
            sealed_files=0,
            reaped_files=0,
            note=str(exc),
        )
    except storage.CompactionFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return CompactResponse(day=day, note="ok", **result)


# ---------------------------------------------------------------------------
# 流式读取
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/partitions/{day}/records",
    tags=["定位分区"],
    summary="流式读取当日全部记录（请求开始即固定快照，NDJSON）",
    responses={
        200: {
            "description": (
                "application/x-ndjson：每行一条定位记录 JSON，按批次提交"
                "顺序、批内原序输出；响应头 X-Snapshot-Files 给出本次快照"
                "固定的文件集合，X-Snapshot-Records 给出记录总数。"
            ),
            "content": {
                "application/x-ndjson": {
                    "example": (
                        '{"vehicle_id":"BUS-1001","utc_ts":1762848000,'
                        '"x":12150.5,"y":8320.0}\n'
                        '{"vehicle_id":"BUS-1001","utc_ts":1762848015,'
                        '"x":12260.2,"y":8321.7}\n'
                    )
                }
            },
        }
    },
)
def read_records(day: str = Path(..., description="运营日 YYYY-MM-DD")) -> Any:
    day = _check_day(day)
    snapshot = storage.open_snapshot(day)
    headers = {
        "X-Snapshot-Files": ",".join(sf.file_name for sf in snapshot.files),
        "X-Snapshot-Records": str(snapshot.total_records),
        "X-Lease-Id": snapshot.lease_id,
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        snapshot.stream_lines(),
        media_type="application/x-ndjson",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 状态与回收
# ---------------------------------------------------------------------------

@app.get(
    "/api/v1/partitions/{day}/status",
    response_model=PartitionStatusResponse,
    tags=["定位分区"],
    summary="查看当日分区文件索引状态",
)
def partition_status(day: str = Path(..., description="运营日 YYYY-MM-DD")) -> Any:
    day = _check_day(day)
    with db.connect() as conn:
        status = db.partition_status(conn, day)
    return PartitionStatusResponse(
        day=status["day"],
        active_files=status["active_files"],
        compacting_files=status["compacting_files"],
        sealed_files=status["sealed_files"],
        total_records=status["total_records"],
        total_active_bytes=status["total_active_bytes"],
        files=[
            PartitionFileInfo(
                file_id=int(r["file_id"]),
                file_name=r["file_name"],
                sequence=int(r["sequence"]),
                record_count=int(r["record_count"]),
                byte_size=int(r["byte_size"]),
                state=r["state"],
                checksum_sha256=r["checksum"],
                created_at=r["created_at"],
            )
            for r in status["rows"]
        ],
    )


@app.post(
    "/api/v1/partitions/{day}/reap",
    response_model=ReapResponse,
    tags=["定位分区"],
    summary="手动回收 sealed 旧文件（无活跃读取时才真正删除）",
)
def reap_day(day: str = Path(..., description="运营日 YYYY-MM-DD")) -> ReapResponse:
    day = _check_day(day)
    try:
        count = storage.reap_day(day, blocking=False)
    except storage.ReapBusy as exc:
        # 有读取/压实进行中：非阻塞跳过，不影响其他日期
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ReapResponse(day=day, reaped_files=count, reaped_bytes=0)
