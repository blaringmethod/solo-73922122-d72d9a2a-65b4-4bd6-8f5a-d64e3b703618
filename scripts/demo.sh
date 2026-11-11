#!/usr/bin/env bash
# 公交定位分区服务：可演示的 API 入口脚本。
# 用法：scripts/demo.sh [BASE_URL]，默认 http://127.0.0.1:8000
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
DAY="2026-11-11"
BASE="${BASE_URL}/api/v1/partitions/${DAY}"

line() { printf '\n========== %s ==========\n' "$1"; }

line "1. 健康检查"
curl -sS "${BASE_URL}/health"; echo

line "2. 追加第一批（3 条）"
curl -sS -X POST "${BASE}/batches" -H 'Content-Type: application/json' -d '{
  "records": [
    {"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0},
    {"vehicle_id": "BUS-1001", "utc_ts": 1762848015, "x": 12260.2, "y": 8321.7},
    {"vehicle_id": "BUS-2042", "utc_ts": 1762848018, "x": 9980.1,  "y": 7745.6}
  ]
}'; echo

line "3. 追加第二批（含重复记录，压实不去重）"
curl -sS -X POST "${BASE}/batches" -H 'Content-Type: application/json' -d '{
  "records": [
    {"vehicle_id": "BUS-1001", "utc_ts": 1762848000, "x": 12150.5, "y": 8320.0},
    {"vehicle_id": "BUS-3308", "utc_ts": 1762848030, "x": 15020.0, "y": 9010.3}
  ]
}'; echo

line "4. 查看分区状态（压实前：2 个 active 碎文件）"
curl -sS "${BASE}/status" | python3 -m json.tool

line "5. 流式读取（NDJSON，响应头携带本次固定快照）"
curl -sS -D - "${BASE}/records" -o /tmp/records-before.jsonl
echo "--- 记录内容 ---"
cat /tmp/records-before.jsonl

line "6. 按日压实"
curl -sS -X POST "${BASE}/compact" | python3 -m json.tool

line "7. 再次查看状态（压实后：1 个 active 合并文件，记录不丢）"
curl -sS "${BASE}/status" | python3 -m json.tool

line "8. 压实后流式读取（顺序、重复记录原样保留）"
curl -sS "${BASE}/records"; echo

line "9. 非法批次被拒（1001 条超过上限）"
BASE="${BASE}" python3 - <<'PY'
import json, os, urllib.request
base = os.environ["BASE"]
body = json.dumps({"records": [
    {"vehicle_id": "B", "utc_ts": i, "x": 0.0, "y": 0.0} for i in range(1001)
]}).encode()
req = urllib.request.Request(
    base + "/batches", data=body,
    headers={"Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, "->", e.read().decode()[:160])
PY

line "演示完成，文档地址：${BASE_URL}/docs"
