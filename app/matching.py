"""核心里程匹配算法。

流程：
1. 预处理折线：计算每条线段的长度、起点累计里程。
2. 每个定位点向所有线段做投影，保留垂直距离 <= max_deviation 的候选，
   候选记录线段编号、沿折线的累计里程、偏离距离。
3. 动态规划求完整路径：
   - 里程不倒退（mileage 非递减）；
   - 相邻里程差 <= max_speed × 相邻时间差；
   - 在所有完整路径中取总平方偏离最小者（平方偏离相同时按确定规则平局）。
4. 找不到完整路径时抛出 :class:`NoPathError`，携带首个无法延续的位置与原因，
   调用方不输出任何部分结果。

环线重叠（共线）路段会在同一点产生多个不同里程的候选，由 DP 的里程与
速度约束在全局范围内消歧。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .schemas import MatchRequest

# 浮点比较的相对容差
_EPS = 1e-9


@dataclass(frozen=True)
class Segment:
    index: int
    ax: float
    ay: float
    bx: float
    by: float
    length: float          # 线段长度（米）
    start_mileage: float   # 线段起点的累计里程（米）


@dataclass(frozen=True)
class Candidate:
    segment_index: int
    mileage: float
    deviation: float


@dataclass
class _State:
    """DP 状态：某定位点选择某个候选时的最优结果。"""

    candidate: Candidate
    total_sq: float
    predecessor: Optional["_State"] = None


class NoPathError(Exception):
    """不存在满足全部约束的完整路径。"""

    def __init__(
        self,
        failed_index: int,
        reason_code: str,
        reason: str,
        candidates: list[Candidate],
    ) -> None:
        super().__init__(reason)
        self.failed_index = failed_index
        self.reason_code = reason_code
        self.reason = reason
        self.candidates = candidates


def build_segments(polyline: list) -> list[Segment]:
    """由折线顶点构造带累计里程的线段集合。"""
    segments: list[Segment] = []
    cumulative = 0.0
    for i in range(len(polyline) - 1):
        a = polyline[i]
        b = polyline[i + 1]
        length = math.hypot(b.x - a.x, b.y - a.y)
        segments.append(
            Segment(
                index=i,
                ax=a.x,
                ay=a.y,
                bx=b.x,
                by=b.y,
                length=length,
                start_mileage=cumulative,
            )
        )
        cumulative += length
    return segments


def project_point(seg: Segment, x: float, y: float) -> Candidate:
    """点向线段投影，返回候选（含累计里程与偏离距离）。

    投影参数 t 被截断到 [0, 1]，即投影点始终落在线段（含端点）上。
    """
    dx = seg.bx - seg.ax
    dy = seg.by - seg.ay
    t = ((x - seg.ax) * dx + (y - seg.ay) * dy) / (seg.length * seg.length)
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    proj_x = seg.ax + t * dx
    proj_y = seg.ay + t * dy
    deviation = math.hypot(x - proj_x, y - proj_y)
    mileage = seg.start_mileage + t * seg.length
    return Candidate(
        segment_index=seg.index,
        mileage=mileage,
        deviation=deviation,
    )


def _candidate_candidates(
    segments: list[Segment], x: float, y: float, max_deviation: float
) -> list[Candidate]:
    """生成一个定位点在距离阈值内的全部候选，按线段编号排序。"""
    out: list[Candidate] = []
    tol = max_deviation * (1.0 + _EPS)
    for seg in segments:
        cand = project_point(seg, x, y)
        if cand.deviation <= tol:
            out.append(cand)
    out.sort(key=lambda c: c.segment_index)
    return out


def _ge(a: float, b: float) -> bool:
    """带容差的 a >= b。"""
    return a >= b - _EPS * max(1.0, abs(a), abs(b))


def _le(a: float, b: float) -> bool:
    """带容差的 a <= b。"""
    return a <= b + _EPS * max(1.0, abs(a), abs(b))


def match_trip(request: MatchRequest) -> list[Candidate]:
    """对单次行程执行里程匹配，返回与定位点等长、同序的候选序列。

    找不到完整路径时抛出 :class:`NoPathError`。
    """
    segments = build_segments(request.polyline)
    times = request.time_seconds
    points = request.points

    prev_states: list[_State] = []

    for i, point in enumerate(points):
        candidates = _candidate_candidates(
            segments, point.x, point.y, request.max_deviation
        )

        if not candidates:
            raise NoPathError(
                failed_index=i,
                reason_code="no_candidate",
                reason=(
                    f"第 {i} 个定位点在最大偏离距离 {request.max_deviation} 米内"
                    "没有任何线段候选"
                ),
                candidates=[],
            )

        if i == 0:
            # 首点没有前驱，阈值内候选全部可行；每个候选自身即一个状态
            prev_states = [
                _State(candidate=c, total_sq=c.deviation * c.deviation)
                for c in candidates
            ]
            continue

        dt = times[i] - times[i - 1]
        max_delta = request.max_speed * dt
        current_states: list[_State] = []

        for cand in candidates:
            best: Optional[_State] = None
            # 平局判定键：总平方偏离 -> 前驱线段 -> 前驱里程，保证结果确定
            best_key: tuple[float, int, float] | None = None

            for prev in prev_states:
                prev_cand = prev.candidate
                # 约束 1：累计里程不倒退
                if not _ge(cand.mileage, prev_cand.mileage):
                    continue
                # 约束 2：相邻里程差不超过速度上限 × 时间差
                if not _le(cand.mileage - prev_cand.mileage, max_delta):
                    continue
                total_sq = prev.total_sq + cand.deviation * cand.deviation
                key = (
                    round(total_sq, 12),
                    prev_cand.segment_index,
                    round(prev_cand.mileage, 12),
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best = _State(
                        candidate=cand,
                        total_sq=total_sq,
                        predecessor=prev,
                    )

            # 单个候选接不上时跳过，让同点的其他重叠段候选继续尝试
            if best is not None:
                current_states.append(best)

        if not current_states:
            # 该点的全部候选都无法延续：依据是否存在「里程不倒退」的前驱
            # 区分原因——存在则说明被速度约束拦截，否则是里程倒退。
            any_monotonic = any(
                _ge(cand.mileage, s.candidate.mileage)
                for cand in candidates
                for s in prev_states
            )
            if not any_monotonic:
                reason_code = "mileage_regression"
                reason = (
                    f"第 {i} 个定位点在阈值内的全部候选相对上一点均发生里程倒退，"
                    "累计里程不允许倒退"
                )
            else:
                reason_code = "speed_exceeded"
                reason = (
                    f"第 {i} 个定位点与上一点时间差 {dt:g} 秒，"
                    f"按速度上限 {request.max_speed:g} 米/秒允许的最大里程增量为 "
                    f"{max_delta:g} 米，没有候选同时满足里程不倒退与该速度约束"
                )
            raise NoPathError(
                failed_index=i,
                reason_code=reason_code,
                reason=reason,
                candidates=candidates,
            )

        prev_states = current_states

    # 全部定位点均有可行状态：取总平方偏离最小的完整路径
    final = min(
        prev_states,
        key=lambda s: (
            round(s.total_sq, 12),
            s.candidate.segment_index,
            round(s.candidate.mileage, 12),
        ),
    )

    # 回溯
    result: list[Candidate] = []
    state: Optional[_State] = final
    while state is not None:
        result.append(state.candidate)
        state = state.predecessor
    result.reverse()
    return result
