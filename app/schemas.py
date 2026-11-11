"""API 请求 / 响应数据模型。

坐标统一为米制平面坐标；时间戳既支持 ISO 8601 字符串，也支持数值型
Unix 秒（内部统一换算成秒参与速度约束计算）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class Vertex(BaseModel):
    """折线顶点（米制平面坐标）。"""

    x: float
    y: float


class TrackPoint(BaseModel):
    """按时间递增的车载定位点。"""

    x: float
    y: float
    # 接受 ISO 8601 字符串或数值型 Unix 秒
    timestamp: Union[datetime, float]


class MatchRequest(BaseModel):
    """单次行程的里程匹配请求。"""

    polyline: list[Vertex] = Field(
        ..., min_length=2, description="有序线路折线顶点，至少包含 2 个点"
    )
    points: list[TrackPoint] = Field(
        ..., min_length=1, description="按时间递增的定位点，至少包含 1 个点"
    )
    max_deviation: float = Field(
        ..., gt=0, description="最大偏离距离（米），投影距离超过该值的候选被丢弃"
    )
    max_speed: float = Field(
        ..., ge=0, description="速度上限（米/秒），相邻里程差不得超过 max_speed × 时间差"
    )

    @model_validator(mode="after")
    def _validate_request(self) -> "MatchRequest":
        # 折线中不允许零长度线段，否则里程与投影定义不明确
        for i in range(len(self.polyline) - 1):
            a = self.polyline[i]
            b = self.polyline[i + 1]
            if a.x == b.x and a.y == b.y:
                raise ValueError(
                    f"polyline 第 {i} 与第 {i + 1} 个顶点重合，零长度线段不允许"
                )

        # 时间戳归一化：ISO 字符串与数值秒可混用，但带时区 ISO 与数值/无时区
        # 混用会产生基准歧义，直接拒绝。
        seconds: list[float] = []
        saw_aware = False
        saw_naive_or_number = False
        for p in self.points:
            ts = p.timestamp
            if isinstance(ts, datetime):
                if ts.tzinfo is not None:
                    saw_aware = True
                    seconds.append(ts.timestamp())
                else:
                    saw_naive_or_number = True
                    seconds.append(ts.replace(tzinfo=timezone.utc).timestamp())
            else:
                saw_naive_or_number = True
                seconds.append(float(ts))
        if saw_aware and saw_naive_or_number:
            raise ValueError(
                "timestamp 不允许混用带时区的 ISO 时间与无时区时间/数值秒，请统一时间基准"
            )

        for i in range(1, len(seconds)):
            if seconds[i] <= seconds[i - 1]:
                raise ValueError(
                    f"points 时间戳必须严格递增，第 {i - 1} 与第 {i} 个点不满足"
                )
        object.__setattr__(self, "_time_seconds", seconds)
        return self

    @property
    def time_seconds(self) -> list[float]:
        """归一化后的各定位点时间（秒）。"""
        return getattr(self, "_time_seconds")


class MatchedPoint(BaseModel):
    """单个定位点的匹配结果。"""

    index: int = Field(..., description="定位点序号（从 0 开始）")
    segment_index: int = Field(
        ..., description="命中的线段编号，即 polyline[i] -> polyline[i+1] 的 i"
    )
    mileage: float = Field(..., description="累计里程（米，沿折线起点累计）")
    deviation: float = Field(..., description="定位点到投影位置的垂直偏离距离（米）")


class CandidateInfo(BaseModel):
    """无完整路径时的候选诊断信息。"""

    segment_index: int
    mileage: float
    deviation: float


class MatchSuccess(BaseModel):
    """存在完整可行路径时的响应。"""

    status: Literal["matched"] = "matched"
    points: list[MatchedPoint]


class MatchFailure(BaseModel):
    """不存在完整可行路径时的响应（不输出部分成功结果）。"""

    status: Literal["no_path"] = "no_path"
    failed_index: int = Field(..., description="首个无法延续的定位点序号（从 0 开始）")
    reason_code: Literal["no_candidate", "mileage_regression", "speed_exceeded"]
    reason: str = Field(..., description="人类可读的中文原因说明")
    candidates: list[CandidateInfo] = Field(
        default_factory=list,
        description="该点在距离阈值内的全部候选（无候选时为空，最多返回 50 条）",
    )


MatchResponse = Annotated[
    Union[MatchSuccess, MatchFailure],
    Field(discriminator="status"),
]
