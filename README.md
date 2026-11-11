# 公交定位分区服务（Bus Position Partition API）

按运营日接收车载定位**小批次**（1～1000 条/批），每批落为一个**不可变
JSONL 文件**，用 **SQLite** 记录文件索引；提供**按日压实**（合并碎文件，
降低下游轨迹清洗的读取开销）与**流式读取**接口。

- Web 框架：FastAPI + Uvicorn（默认 **2 个工作进程**）
- 存储：`data/partitions/<day>/*.jsonl` 数据文件 + `data/index.sqlite3` 索引
- 交互式文档：启动后访问 **`/docs`**（含追加、压实、读取的可执行示例）

---

## 1. 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000
# 文档：http://localhost:8000/docs
```

数据保存在命名卷 `bus-position-data`（容器内 `/data`），容器重启后已提交
数据保留。停止并清理：

```bash
docker compose down          # 停止
docker compose down -v       # 停止并删除数据卷
```

### 不使用 Docker（本地）

```bash
pip install -r requirements.txt
export BUS_DATA_DIR=./data
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

### 一键演示脚本

服务启动后：

```bash
./scripts/demo.sh http://localhost:8000
```

脚本依次演示：追加两批 → 查看状态 → 流式读取 → 压实 → 再读取 → 非法请求被拒。

---

## 2. 数据模型

每条定位记录（JSONL 中的一行）：

```json
{"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0}
```

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `vehicle_id` | string(1..64) | 车辆 ID |
| `utc_ts` | integer | UTC 整数秒（Unix epoch seconds，严格整数） |
| `x` | number | 米制平面坐标 x（米） |
| `y` | number | 米制平面坐标 y（米） |

记录允许重复，压实**不去重**；多余字段一律拒绝（`extra="forbid"`）。

---

## 3. API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/partitions/{day}/batches` | 追加一批（1～1000 条），落不可变 JSONL |
| POST | `/api/v1/partitions/{day}/compact` | 按日压实碎文件 |
| GET  | `/api/v1/partitions/{day}/records` | 流式读取当日记录（NDJSON，请求开始即快照） |
| GET  | `/api/v1/partitions/{day}/status` | 分区文件索引状态 |
| POST | `/api/v1/partitions/{day}/reap` | 手动回收 sealed 旧文件 |
| GET  | `/health`、`/` | 运维入口 |

`{day}` 为运营日，固定 `YYYY-MM-DD`。

### 3.1 追加

```bash
curl -X POST http://localhost:8000/api/v1/partitions/2026-11-11/batches \
  -H 'Content-Type: application/json' \
  -d '{"records": [
    {"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0},
    {"vehicle_id": "BUS-2042", "utc_ts": 1762848018, "x": 9980.1,  "y": 7745.6}
  ]}'
```

返回 `201`，含 `file_id`、`file_name`、当日单调 `sequence`、字节数与
SHA-256 校验和。

### 3.2 压实

```bash
curl -X POST http://localhost:8000/api/v1/partitions/2026-11-11/compact
```

合并开始时选中的全部 active 文件，**保留全部记录、批次提交顺序与批内
原序，不去重**；合并期间新追加的批次不在本次快照内、不会丢失。旧文件
标记为 `sealed`，在所有读取结束后回收。

### 3.3 流式读取

```bash
curl -i http://localhost:8000/api/v1/partitions/2026-11-11/records
```

响应为 `application/x-ndjson`（每行一条记录），响应头：

- `X-Snapshot-Files`：本次读取固定的文件集合（请求开始时确定）
- `X-Snapshot-Records`：本次快照的记录总数
- `X-Lease-Id`：读取租约 ID

压实与追加都不会改变本次读取的内容。

### 3.4 状态与回收

```bash
curl http://localhost:8000/api/v1/partitions/2026-11-11/status
curl -X POST http://localhost:8000/api/v1/partitions/2026-11-11/reap
```

有读取/压实进行中时，回收返回 `409`，不影响其他运营日。

---

## 4. 可靠性与并发设计

### 4.1 不可变文件与原子可见性

- 每批先写 `tmp-<uuid>.jsonl`，`fsync` 后 **原子 rename** 为正式文件，
  再在 SQLite 短事务内登记索引；只有「文件完整 + 索引提交」后才可见。
- 写入失败或进程中断不会暴露残片：重启时清理 `tmp-` 临时文件与无索引
  的孤儿 JSONL（该批次按未提交处理），已提交数据不受影响。
- SQLite 启用 **WAL + synchronous=NORMAL**，`compacting` 等中间状态在
  重启时自动回滚为 `active`。

### 4.2 压实流程（索引切换原子化）

1. 短事务：选中当日全部 active 文件并标记 `compacting`；
2. 事务外流式合并到临时文件（逐行校验、校验和、记录数/字节数核对）；
3. 原子 rename 合并文件；
4. **单事务**切换：旧文件 `sealed` + 合并文件 `active`，失败整体回滚；
5. 在锁内尝试回收满足条件的旧文件。

任何一步失败都会回滚 `compacting` 状态并清理临时文件，旧分区原样可用。

### 4.3 读取快照与旧文件回收

- 读取请求开始时：事务内固定 active 文件集合、创建读取租约，并获取
  **当日共享锁（flock LOCK_SH）**，直到流结束才释放。
- 压实/回收持有**当日排他锁（flock LOCK_EX）**，必须等待读取结束；
  回收同时要求无 TTL 内的活跃租约、且超过宽限期（双保险）。
- 因此旧文件只在「已有读取全部结束」后才被删除。

### 4.4 两个工作进程、按日不互斥

- 锁粒度是**运营日**（`data/locks/<day>.lock`）：处理 A 日的压实/回收
  不会阻塞 B 日的任何操作。
- 追加不使用日锁，仅依赖 SQLite 毫秒级写事务决定提交顺序；不同日期、
  同日的追加都不会被压实阻塞。
- 不存在覆盖整个文件读写过程的全局锁。

### 4.5 配置项（环境变量）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `BUS_DATA_DIR` | `/data` | 数据根目录 |
| `BUS_MAX_BATCH_RECORDS` | `1000` | 单批记录数上限 |
| `BUS_SQLITE_BUSY_TIMEOUT_MS` | `30000` | SQLite 写锁等待 |
| `BUS_LEASE_TTL_SECONDS` | `120` | 读取租约 TTL（流中自动续租） |
| `BUS_REAP_GRACE_SECONDS` | `30` | sealed 文件回收宽限 |
| `BUS_STREAM_CHUNK_BYTES` | `65536` | 流式读取块大小 |

---

## 5. 目录结构

```
app/
  config.py    # 环境变量配置
  schemas.py   # Pydantic 请求/响应模型与 OpenAPI 示例
  db.py        # SQLite 索引、状态机、读取租约
  storage.py   # 按日 flock、原子写入、压实、快照读取、回收、恢复
  main.py      # FastAPI 路由与 lifespan 恢复
scripts/demo.sh
tests/         # pytest 端到端用例（含跨进程 flock、并发追加、崩溃恢复）
```

## 6. 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

覆盖：参数校验、不可变落盘、压实保序/不去重、并发追加不丢、读取快照
隔离、读取结束前禁止回收（跨进程 flock）、按日锁不阻塞其他日期、重启
孤儿清理与 `compacting` 回滚、重复压实顺序稳定。
