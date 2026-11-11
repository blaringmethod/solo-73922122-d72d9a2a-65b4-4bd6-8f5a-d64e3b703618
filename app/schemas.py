"""API 请求 / 响应数据模型。

记录字段：vehicle_id（车辆 ID）、utc_ts（UTC 整数秒）、x/y（米制坐标）。
记录允许重复（压实不去重），因此除记录数外不做任何唯一性约束。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 运营日固定为 YYYY-MM-DD（与数据分区目录一一对应）
DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

DayPath = Annotated[
    str,
    Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="运营日，格式 YYYY-MM-DD，例如 2026-11-11",
    ),
]


class PositionRecord(BaseModel):
    """一条车载定位记录。"""

    # 禁止多余字段，避免脏数据静默落盘
    model_config = ConfigDict(extra="forbid")

    vehicle_id: str = Field(
        ..., min_length=1, max_length=64, description="车辆 ID（非空字符串）"
    )
    utc_ts: int = Field(
        ...,
        strict=True,
        description="定位时刻，UTC 整数秒（Unix epoch seconds）",
    )
    x: float = Field(..., strict=True, description="米制平面坐标 x（米）")
    y: float = Field(..., strict=True, description="米制平面坐标 y（米）")


class AppendBatchRequest(BaseModel):
    """追加一批定位记录（1～1000 条）。"""

    model_config = ConfigDict(extra="forbid")

    records: list[PositionRecord] = Field(
        ..., min_length=1, max_length=1000, description="本批记录，1～1000 条"
    )


class AppendBatchResponse(BaseModel):
    day: str
    file_id: int = Field(..., description="本批不可变 JSONL 文件的索引 ID")
    file_name: str = Field(..., description="落盘文件名（相对分区目录）")
    record_count: int
    byte_size: int
    sequence: int = Field(
        ..., description="当日全局单调序号，决定批次提交顺序"
    )
    checksum_sha256: str


class CompactResponse(BaseModel):
    day: str
    merged_file_id: int
    merged_file_name: str
    merged_records: int
    merged_source_files: int = Field(
        ..., description="本次压实合并的碎文件数"
    )
    sealed_files: int = Field(..., description="已标记待回收的旧文件数")
    reaped_files: int = Field(..., description="本次同步回收掉的旧文件数")
    note: str


class PartitionFileInfo(BaseModel):
    file_id: int
    file_name: str
    sequence: int
    record_count: int
    byte_size: int
    state: Literal["active", "compacting", "sealed"]
    checksum_sha256: str
    created_at: str


class PartitionStatusResponse(BaseModel):
    day: str
    active_files: int
    compacting_files: int
    sealed_files: int
    total_records: int
    total_active_bytes: int
    files: list[PartitionFileInfo]


class ReapResponse(BaseModel):
    day: str
    reaped_files: int
    reaped_bytes: int


_APPEND_EXAMPLE: dict[str, Any] = {
    "summary": "追加 3 条定位记录（小批次）",
    "value": {
        "records": [
            {"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0},
            {"vehicle_id": "BUS-1001", "utc_ts": 1762848015, "x": 12260.2, "y": 8321.7},
            {"vehicle_id": "BUS-2042", "utc_ts": 1762848018, "x": 9980.1, "y": 7745.6},
        ]
    },
}

_APPEND_DUP_EXAMPLE: dict[str, Any] = {
    "summary": "允许重复记录（压实不去重，按提交顺序原样保留）",
    "value": {
        "records": [
            {"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0},
            {"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0},
        ]
    },
}


def append_openapi_examples() -> dict[str, dict[str, Any]]:
    """供路由层 Body(openapi_examples=...) 使用。"""
    return {"normal": _APPEND_EXAMPLE, "with_duplicates": _APPEND_DUP_EXAMPLE}


def validate_day(day: str) -> str:
    """路径参数外的二次校验，并验证是真实日历日期。"""
    if not DAY_PATTERN.match(day):
        raise ValueError("day 必须是 YYYY-MM-DD 格式")
    # 校验真实日期（拒绝 2026-13-40 之类）
    from datetime import date

    year, month, dom = (int(p) for p in day.split("-"))
    date(year, month, dom)
    return day
