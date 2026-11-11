"""API 层测试（通过 FastAPI TestClient）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_and_root() -> None:
    assert client.get("/health").json() == {"status": "ok"}
    root = client.get("/").json()
    assert root["docs"] == "/docs"


def test_openapi_contains_examples_and_discriminated_response() -> None:
    schema = client.get("/openapi.json").json()
    endpoint = schema["paths"]["/api/v1/trips/match"]["post"]
    examples = endpoint["requestBody"]["content"]["application/json"]["examples"]
    assert set(examples) == {"overlap", "no_path"}
    # 响应为带 discriminator 的联合模型
    resp_ref = endpoint["responses"]["200"]["content"]["application/json"]["schema"]
    assert "oneOf" in resp_ref
    assert resp_ref.get("discriminator", {}).get("propertyName") == "status"


def test_api_match_success_overlap_example() -> None:
    body = {
        "polyline": [
            {"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 100, "y": 100},
            {"x": 0, "y": 100}, {"x": 0, "y": 0}, {"x": 0, "y": -50},
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
    }
    resp = client.post("/api/v1/trips/match", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "matched"
    assert len(data["points"]) == 10
    assert [p["segment_index"] for p in data["points"]] == [
        0, 0, 1, 1, 1, 2, 2, 3, 3, 4
    ]
    mileages = [p["mileage"] for p in data["points"]]
    assert all(b >= a for a, b in zip(mileages, mileages[1:]))
    assert data["points"][0]["index"] == 0
    # 折线总长 450 米，末点位于最后一段 30 米处 -> 430
    assert data["points"][-1]["mileage"] == 430.0


def test_api_no_path_does_not_return_partial_result() -> None:
    body = {
        "polyline": [
            {"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 200, "y": 0},
        ],
        "points": [
            {"x": 0, "y": 0, "timestamp": 0},
            {"x": 150, "y": 0, "timestamp": 10},
            {"x": 200, "y": 0, "timestamp": 20},
        ],
        "max_deviation": 5.0,
        "max_speed": 5.0,
    }
    resp = client.post("/api/v1/trips/match", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "no_path"
    assert data["failed_index"] == 1
    assert data["reason_code"] == "speed_exceeded"
    assert "points" not in data  # 不输出部分成功结果
    assert len(data["candidates"]) >= 1


def test_api_no_candidate_failure() -> None:
    body = {
        "polyline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}],
        "points": [{"x": 0, "y": 99, "timestamp": 0}],
        "max_deviation": 5.0,
        "max_speed": 10.0,
    }
    data = client.post("/api/v1/trips/match", json=body).json()
    assert data["status"] == "no_path"
    assert data["failed_index"] == 0
    assert data["reason_code"] == "no_candidate"
    assert data["candidates"] == []


def test_api_validation_error_returns_422() -> None:
    body = {
        "polyline": [{"x": 0, "y": 0}],  # 少于 2 个顶点
        "points": [{"x": 0, "y": 0, "timestamp": 0}],
        "max_deviation": 5.0,
        "max_speed": 10.0,
    }
    assert client.post("/api/v1/trips/match", json=body).status_code == 422

    body_bad_speed = {
        "polyline": [{"x": 0, "y": 0}, {"x": 10, "y": 0}],
        "points": [{"x": 0, "y": 0, "timestamp": 0}],
        "max_deviation": 5.0,
        "max_speed": -1.0,
    }
    assert client.post(
        "/api/v1/trips/match", json=body_bad_speed
    ).status_code == 422
