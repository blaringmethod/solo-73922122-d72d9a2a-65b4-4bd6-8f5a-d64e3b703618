"""核心匹配算法单元测试。"""

from __future__ import annotations

import math

import pytest

from app.matching import (
    NoPathError,
    _candidate_candidates,
    build_segments,
    match_trip,
    project_point,
)
from app.schemas import MatchRequest, TrackPoint, Vertex


def _req(polyline, points, max_deviation=5.0, max_speed=10.0) -> MatchRequest:
    return MatchRequest(
        polyline=[Vertex(x=x, y=y) for x, y in polyline],
        points=[TrackPoint(x=x, y=y, timestamp=t) for x, y, t in points],
        max_deviation=max_deviation,
        max_speed=max_speed,
    )


# --------------------------------------------------------------------------
# 几何预处理与投影
# --------------------------------------------------------------------------
def test_build_segments_cumulative_mileage() -> None:
    segs = build_segments([Vertex(x=0, y=0), Vertex(x=3, y=4), Vertex(x=3, y=14)])
    assert len(segs) == 2
    assert segs[0].length == pytest.approx(5.0)
    assert segs[0].start_mileage == 0.0
    assert segs[1].length == pytest.approx(10.0)
    assert segs[1].start_mileage == pytest.approx(5.0)


def test_project_clamps_to_endpoints() -> None:
    segs = build_segments([Vertex(x=0, y=0), Vertex(x=10, y=0)])
    # 投影落在起点之前 -> 截断到起点
    behind = project_point(segs[0], -5, 3)
    assert behind.mileage == pytest.approx(0.0)
    assert behind.deviation == pytest.approx(math.hypot(5, 3))
    # 投影落在终点之后 -> 截断到终点
    ahead = project_point(segs[0], 15, -4)
    assert ahead.mileage == pytest.approx(10.0)
    assert ahead.deviation == pytest.approx(math.hypot(5, 4))


def test_project_midpoint() -> None:
    segs = build_segments([Vertex(x=0, y=0), Vertex(x=10, y=0)])
    c = project_point(segs[0], 4, 3)
    assert c.segment_index == 0
    assert c.mileage == pytest.approx(4.0)
    assert c.deviation == pytest.approx(3.0)


# --------------------------------------------------------------------------
# 基础匹配
# --------------------------------------------------------------------------
def test_basic_match_along_straight_line() -> None:
    req = _req(
        polyline=[(0, 0), (100, 0)],
        points=[(0, 0, 0), (20, 1, 10), (60, -2, 20), (100, 0, 30)],
        max_deviation=5.0,
        max_speed=10.0,
    )
    result = match_trip(req)
    assert [c.segment_index for c in result] == [0, 0, 0, 0]
    assert [c.mileage for c in result] == pytest.approx([0, 20, 60, 100])
    assert [c.deviation for c in result] == pytest.approx([0, 1, 2, 0])


def test_match_across_multiple_segments() -> None:
    req = _req(
        polyline=[(0, 0), (100, 0), (100, 100)],
        points=[(50, 0, 0), (100, 0, 10), (100, 50, 20)],
        max_deviation=1.0,
        max_speed=10.0,
    )
    result = match_trip(req)
    assert [c.segment_index for c in result] == [0, 0, 1]
    assert [c.mileage for c in result] == pytest.approx([50, 100, 150])


# --------------------------------------------------------------------------
# 环线重叠（共线）路段：同一点有多个候选，DP 全局消歧
# --------------------------------------------------------------------------
def test_loop_overlap_disambiguated_by_mileage_monotonicity() -> None:
    # 矩形环线闭合后沿 x=0 反向再走一段：(0,100)->(0,0) 与首段在 (0,0) 衔接，
    # 点 (1, 50) 同时靠近线段 1（x=100，不满足阈值）——这里用 x=0 上的点，
    # 它只匹配线段 3（正向里程 250）与重叠共线位置，验证顺序消歧。
    req = _req(
        polyline=[
            (0, 0), (100, 0), (100, 100), (0, 100), (0, 0), (0, -50),
        ],
        points=[
            (2, 1, 0),
            (50, 2, 50),
            (98, 3, 100),
            (102, 50, 160),
            (101, 99, 210),
            (50, 102, 270),
            (1, 101, 320),
            (-2, 50, 380),   # 线段 3：里程 250
            (-1, 1, 430),    # 线段 4：里程 300
            (0, -30, 480),   # 线段 5：里程 320
        ],
        max_deviation=5.0,
        max_speed=15.0,
    )
    result = match_trip(req)
    # 6 个顶点 -> 5 条线段（编号 0..4）；点 2/4/6/8 存在多候选，
    # DP 选择满足约束且总平方偏离更小的线段
    assert [c.segment_index for c in result] == [0, 0, 1, 1, 1, 2, 2, 3, 3, 4]
    assert [round(c.mileage, 1) for c in result] == [
        2.0, 50.0, 103.0, 150.0, 199.0, 250.0, 299.0, 350.0, 399.0, 430.0
    ]
    # 里程严格非递减
    mileages = [c.mileage for c in result]
    assert all(b >= a for a, b in zip(mileages, mileages[1:]))
    assert mileages[0] == pytest.approx(2.0)
    assert mileages[-1] == pytest.approx(430.0)


def test_overlap_rejects_backward_candidate_and_continues_loop() -> None:
    """环线回到起点附近时，同一点同时命中低里程重叠段与高里程当前段，
    低里程候选因倒退被拒，路径沿高里程候选继续，且后续点仍可匹配。"""
    req = _req(
        polyline=[
            (0, 0), (100, 0), (100, 100), (0, 100), (0, 0), (0, -50),
        ],
        points=[(-1, 1, 0), (0, -30, 60)],
        max_deviation=5.0,
        max_speed=15.0,
    )
    segs = build_segments(req.polyline)
    # 点 0 同时是线段 0（里程 0）、线段 3（里程 ~399）、线段 4（里程 400）的候选
    cands0 = _candidate_candidates(segs, -1, 1, 5.0)
    assert {c.segment_index for c in cands0} == {0, 3, 4}

    result = match_trip(req)
    # 必须选高里程候选（399/400）而非线段 0 的里程 0，否则无法延续到点 1
    assert result[0].segment_index in (3, 4)
    assert result[0].mileage >= 399.0
    assert result[1].segment_index == 4
    assert result[1].mileage == pytest.approx(430.0)


def test_overlap_prefers_path_with_min_total_squared_deviation() -> None:
    # 两条共线重叠线段（同方向不同里程），两个候选到点的偏离不同，
    # 在都可行的前提下应选平方偏离更小的完整路径。
    req = _req(
        polyline=[(0, 0), (100, 0), (200, 0)],
        points=[(10, 0, 0), (10, 3, 10)],
        max_deviation=5.0,
        max_speed=100.0,
    )
    result = match_trip(req)
    # 第二个点里程 10、偏离 3，只能落在线段 0；验证偏离被记录
    assert result[1].segment_index == 0
    assert result[1].deviation == pytest.approx(3.0)
    assert result[1].mileage == pytest.approx(10.0)


# --------------------------------------------------------------------------
# 约束失败
# --------------------------------------------------------------------------
def test_no_candidate_raises_with_index_zero() -> None:
    req = _req(
        polyline=[(0, 0), (100, 0)],
        points=[(0, 50, 0)],  # 偏离 50，阈值 5
        max_deviation=5.0,
    )
    with pytest.raises(NoPathError) as exc:
        match_trip(req)
    assert exc.value.failed_index == 0
    assert exc.value.reason_code == "no_candidate"
    assert exc.value.candidates == []


def test_speed_exceeded_reports_first_failed_position() -> None:
    req = _req(
        polyline=[(0, 0), (100, 0), (200, 0)],
        points=[(0, 0, 0), (150, 0, 10), (200, 0, 20)],
        max_deviation=5.0,
        max_speed=5.0,  # 10 秒最多 50 米，实际需要 150 米
    )
    with pytest.raises(NoPathError) as exc:
        match_trip(req)
    assert exc.value.failed_index == 1
    assert exc.value.reason_code == "speed_exceeded"
    # 失败点的候选仍要带回，便于排查
    assert len(exc.value.candidates) >= 1
    assert all(c.deviation <= 5.0 + 1e-9 for c in exc.value.candidates)


def test_mileage_regression_classified() -> None:
    # 点 1 在时间上晚于点 0，但几何上只能投影到更小里程（折线折返重叠），
    # 且速度上限不足以解释 -> 里程倒退。
    req = _req(
        polyline=[(0, 0), (100, 0), (100, -50)],
        points=[(90, 0, 0), (10, 0, 10)],  # 里程 90 -> 10，倒退
        max_deviation=5.0,
        max_speed=1.0,
    )
    with pytest.raises(NoPathError) as exc:
        match_trip(req)
    assert exc.value.failed_index == 1
    assert exc.value.reason_code == "mileage_regression"


def test_zero_speed_allows_stationary_points() -> None:
    req = _req(
        polyline=[(0, 0), (100, 0)],
        points=[(20, 1, 0), (20, -1, 30)],
        max_deviation=5.0,
        max_speed=0.0,
    )
    result = match_trip(req)
    assert all(c.mileage == pytest.approx(20.0) for c in result)


def test_speed_boundary_is_inclusive() -> None:
    # 10 秒走 100 米，速度恰好 10 m/s，等于上限，应通过
    req = _req(
        polyline=[(0, 0), (100, 0)],
        points=[(0, 0, 0), (100, 0, 10)],
        max_deviation=1.0,
        max_speed=10.0,
    )
    result = match_trip(req)
    assert result[1].mileage == pytest.approx(100.0)


# --------------------------------------------------------------------------
# 请求校验
# --------------------------------------------------------------------------
def test_rejects_non_increasing_timestamps() -> None:
    with pytest.raises(ValueError):
        _req(
            polyline=[(0, 0), (10, 0)],
            points=[(0, 0, 10), (5, 0, 10)],
        )


def test_rejects_zero_length_segment() -> None:
    with pytest.raises(ValueError):
        _req(
            polyline=[(0, 0), (0, 0), (10, 0)],
            points=[(0, 0, 0)],
        )


def test_rejects_mixed_aware_and_naive_timestamps() -> None:
    with pytest.raises(ValueError):
        MatchRequest(
            polyline=[Vertex(x=0, y=0), Vertex(x=10, y=0)],
            points=[
                TrackPoint(x=0, y=0, timestamp="2026-11-11T08:00:00+08:00"),
                TrackPoint(x=5, y=0, timestamp=1000),
            ],
            max_deviation=5.0,
            max_speed=10.0,
        )


def test_numeric_and_naive_iso_timestamps_accepted() -> None:
    from datetime import datetime, timezone

    base = (
        datetime(2026, 11, 11, 8, 0, 0)
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    req = MatchRequest(
        polyline=[Vertex(x=0, y=0), Vertex(x=10, y=0)],
        points=[
            TrackPoint(x=0, y=0, timestamp="2026-11-11T08:00:00"),
            TrackPoint(x=5, y=0, timestamp=base + 1000),
        ],
        max_deviation=5.0,
        max_speed=10.0,
    )
    assert req.time_seconds[1] - req.time_seconds[0] == 1000.0
