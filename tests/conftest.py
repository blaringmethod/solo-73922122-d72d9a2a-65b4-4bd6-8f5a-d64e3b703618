"""测试夹具：每个用例使用独立临时数据目录，并调小回收相关 TTL。"""

from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app.* 之前设置配置环境变量
os.environ.setdefault("BUS_LEASE_TTL_SECONDS", "5")
# 宽限给足：压实结尾的同步回收因宽限未到而保留 sealed 旧文件，
# 由回收用例显式等待窗口后再验证删除
os.environ.setdefault("BUS_REAP_GRACE_SECONDS", "120")
os.environ.setdefault("BUS_SQLITE_BUSY_TIMEOUT_MS", "10000")

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, storage  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: str) -> None:
    """每个用例独立数据目录：清空目录后重新建库，杜绝跨用例污染。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # config 在 import 时基于环境变量解析，这里直接重绑定运行时路径
    config.DATA_DIR = data_dir.resolve()
    config.PARTITION_ROOT = config.DATA_DIR / "partitions"
    config.LOCK_DIR = config.DATA_DIR / "locks"
    config.DB_PATH = config.DATA_DIR / "index.sqlite3"
    db.init_db()
    storage.recover_on_startup()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def day() -> str:
    return "2026-11-11"
