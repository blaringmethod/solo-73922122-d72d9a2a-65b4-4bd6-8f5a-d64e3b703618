# 公交行程里程匹配 API

清洗车载定位数据时，用于在**环线重叠路段**上区分行程位置、修正入库轨迹的
FastAPI 服务。坐标统一为米制平面坐标。

## 匹配语义

对单次行程，输入：

| 字段 | 含义 |
| --- | --- |
| `polyline` | 有序线路折线顶点（至少 2 个，不允许相邻重合顶点） |
| `points` | 按时间**严格递增**的定位点，时间戳支持 ISO 8601 字符串或数值型 Unix 秒 |
| `max_deviation` | 最大偏离距离（米，> 0） |
| `max_speed` | 速度上限（米/秒，≥ 0） |

处理流程：

1. **投影候选**：每个定位点向折线的每条线段做正交投影（投影点截断在线段
   `[0,1]` 参数区间内），仅保留垂直距离 `<= max_deviation` 的候选，候选携带
   线段编号、沿折线起点累计的里程、偏离距离。
2. **动态规划选完整路径**，路径必须同时满足：
   - 累计里程**不倒退**（非递减，含等号，支持停车等时同里程场景）；
   - 相邻里程差 `<= max_speed × 相邻时间差`（边界等号允许）；
   - 在所有完整可行路径中取**总平方偏离最小**者；平方偏离相同时按
     「总平方偏离 → 前驱线段编号 → 前驱里程」确定唯一结果，保证可复现。
3. **环线重叠消歧**：共线/折返重叠段会让同一个定位点产生多个不同里程的候选，
   由 DP 在全局范围内用里程单调性与速度约束选择可延续的候选；某个候选接不上时
   会继续尝试同点的其他候选，只有该点**全部**候选都无法延续才判定失败。
4. **无完整路径即整体失败**：返回首个无法延续的定位点序号与原因，
   **不输出任何部分成功结果**。失败原因分三类：
   - `no_candidate`：该点在偏离阈值内没有任何线段候选；
   - `mileage_regression`：全部候选相对上一点都发生里程倒退；
   - `speed_exceeded`：存在满足里程不倒退的候选，但里程增量均超过
     `max_speed × 时间差`。

成功与失败都返回 HTTP 200，用响应体的 `status` 字段（`matched` /
`no_path`，OpenAPI discriminator）区分；请求参数本身非法返回 422。

## 响应示例

成功：

```json
{
  "status": "matched",
  "points": [
    {"index": 0, "segment_index": 0, "mileage": 2.0, "deviation": 1.0}
  ]
}
```

失败（含该点全部阈值内候选，最多 50 条，便于排查）：

```json
{
  "status": "no_path",
  "failed_index": 1,
  "reason_code": "speed_exceeded",
  "reason": "第 1 个定位点与上一点时间差 10 秒，按速度上限 5 米/秒允许的最大里程增量为 50 米……",
  "candidates": [
    {"segment_index": 1, "mileage": 150.0, "deviation": 0.0}
  ]
}
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：<http://localhost:8000>
- Swagger UI（含「环线重叠路段匹配」「无完整路径（超速）」两个可直接运行的
  请求示例）：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- OpenAPI JSON：<http://localhost:8000/openapi.json>
- 健康检查：`GET /health`

本地裸机调试：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求示例

```bash
curl -X POST http://localhost:8000/api/v1/trips/match \
  -H 'Content-Type: application/json' \
  -d '{
    "polyline": [
      {"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100},
      {"x": 0, "y": 100}, {"x": 0, "y": 0}, {"x": 0, "y": -50}
    ],
    "points": [
      {"x": 2, "y": 1, "timestamp": "2026-11-11T08:00:00"},
      {"x": 50, "y": 2, "timestamp": "2026-11-11T08:00:50"}
    ],
    "max_deviation": 5.0,
    "max_speed": 15.0
  }'
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：投影截断与累计里程、多段衔接、环线共线重叠的全局消歧、总平方偏离最优、
无候选/超速/里程倒退三类失败、零速度停车、速度边界包含、时间戳与折线校验、
OpenAPI discriminator 与示例、422 校验。

## 目录结构

```
app/
  __init__.py
  schemas.py     # Pydantic 请求/响应模型与输入校验
  matching.py    # 投影、候选生成、DP 匹配、NoPathError
  main.py        # FastAPI 应用、OpenAPI 示例
tests/           # pytest 单元与 API 测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

## 复杂度

设定位点数为 n、线段数为 m、每点平均候选数为 k：时间 O(n·m + n·k²)，
空间 O(n·k)。城市公交线路 m 通常为数百、k 在重叠很少时接近 1，可直接在线服务；
超长折线可在前置步骤按空间分块减少投影线段数。
