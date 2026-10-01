# -*- coding: utf-8 -*-
"""测试夹具：每个用例独立临时数据库。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from chessroom.catalog import seed_catalog
from chessroom.db import Database
from chessroom.services import Actor, ChessRoomService

import pytest


@pytest.fixture()
def svc(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    seed_catalog(db, reset=True)
    return ChessRoomService(db, photo_dir=str(tmp_path / "photos"))


@pytest.fixture()
def admin():
    return Actor("管理员老郑", "admin")


@pytest.fixture()
def director():
    return Actor("社区负责人陈主任", "director")


@pytest.fixture()
def staff():
    return Actor("前台小周", "staff")
