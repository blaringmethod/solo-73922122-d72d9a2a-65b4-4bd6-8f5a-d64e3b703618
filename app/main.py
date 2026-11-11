"""FastAPI 应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
文档：/docs（Swagger UI）、/redoc、/openapi.json
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from .matching import NoPathError, match_trip
from .schemas import (
    CandidateInfo,
    MatchFailure,
    MatchRequest,
    MatchResponse,
    MatchedPoint,
    MatchSuccess,
)

app = FastAPI(
    title="公交行程里程匹配 API",
    version="1.0.0",
    description=(
        "对单次行程的车载定位点沿有序线路折线做投影匹配，在「里程不倒退、"
        "相邻里程差不超过速度上限 × 时间差」的约束下，返回总平方偏离最小的"
        "完整路径；无完整路径时返回首个无法延续的位置及原因。坐标均为米制平面坐标。"
    ),
)

# ---------------------------------------------------------------------------
# OpenAPI 示例：包含环线重叠（共线）路段
# 折线几何（单位米）：
#   (0,0)->(100,0)->(100,100)->(0,100)->(0,0)->(0,-50)
# 其中 0->1 段（(0,0)->(100,0)）与 4->3 段（(0,0)->(0,100) 反向即
# (0,100)->(0,0)）在 (0,0) 周边形成环线重叠；闭合后第 4 段再次经过起点。
# 定位点 (50, 2) 同时落在线段 0（里程 50）和闭合环线回来的共线位置上，
# 由 DP 的里程单调性 + 速度约束完成消歧。
# ---------------------------------------------------------------------------
_OVERLAP_EXAMPLE: dict[str, Any] = {
    "summary": "含共线重叠段的环线行程，DP 全局消歧",
    "value": {
        "polyline": [
            {"x": 0, "y": 0},
            {"x": 100, "y": 0},
            {"x": 100, "y": 100},
            {"x": 0, "y": 100},
            {"x": 0, "y": 0},
            {"x": 0, "y": -50},
        ],
        "points": [
            {"x": 2, "y": 1, "timestamp": "2026-11-11T08:00:00"},
            {"x": 50, "y": 2, "timestamp": "2026-11-11T08:00:50"},
            {"x": 98, "y": 3, "timestamp": "2026-11-11T08:01:40"},
            {"x": 102, "y": 50, "timestamp": "2026-11-11T08:02:40"},
            {"x": 101, "y": 99, "timestamp": "2026-11-11T08:03:30"},
            {"x": 50, "y": 102, "timestamp": "2026-11-11T08:04:30"},
            {"x": 1, "y": 101, "timestamp": "2026-11-11T08:05:20"},
            {"x": -2, "y": 50, "timestamp": "2026-11-11T08:06:20"},
            {"x": -1, "y": 1, "timestamp": "2026-11-11T08:07:10"},
            {"x": 0, "y": -30, "timestamp": "2026-11-11T08:08:00"},
        ],
        "max_deviation": 5.0,
        "max_speed": 15.0,
    },
}

_NO_PATH_EXAMPLE: dict[str, Any] = {
    "summary": "相邻定位点里程增量超过速度上限 × 时间差",
    "value": {
        "polyline": [
            {"x": 0, "y": 0},
            {"x": 100, "y": 0},
            {"x": 200, "y": 0},
        ],
        "points": [
            {"x": 0, "y": 0, "timestamp": 0},
            # 10 秒内沿线路前进 150 米，需要 15 m/s，超过上限 5 m/s（允许 50 米）
            {"x": 150, "y": 0, "timestamp": 10},
            {"x": 200, "y": 0, "timestamp": 20},
        ],
        "max_deviation": 5.0,
        "max_speed": 5.0,
    },
}


@app.get("/health", tags=["运维"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["运维"])
def root() -> dict[str, Any]:
    return {
        "service": "bus-trip-mileage-matching",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }


@app.post(
    "/api/v1/trips/match",
    response_model=MatchResponse,
    tags=["里程匹配"],
    summary="单次行程里程匹配",
    responses={
        200: {
            "description": "匹配成功返回 matched；无完整路径返回 no_path（均为 HTTP 200，"
            "用 status 字段区分；请求参数本身非法时返回 422）。",
        }
    },
)
def match(
    request: MatchRequest = Body(
        ...,
        openapi_examples={
            "overlap": _OVERLAP_EXAMPLE,
            "no_path": _NO_PATH_EXAMPLE,
        },
    )
) -> JSONResponse:
    try:
        matched = match_trip(request)
    except NoPathError as exc:
        payload = MatchFailure(
            failed_index=exc.failed_index,
            reason_code=exc.reason_code,  # type: ignore[arg-type]
            reason=exc.reason,
            candidates=[
                CandidateInfo(
                    segment_index=c.segment_index,
                    mileage=round(c.mileage, 6),
                    deviation=round(c.deviation, 6),
                )
                for c in exc.candidates[:50]
            ],
        )
        return JSONResponse(content=payload.model_dump())

    payload = MatchSuccess(
        points=[
            MatchedPoint(
                index=i,
                segment_index=c.segment_index,
                mileage=round(c.mileage, 6),
                deviation=round(c.deviation, 6),
            )
            for i, c in enumerate(matched)
        ]
    )
    return JSONResponse(content=payload.model_dump())
