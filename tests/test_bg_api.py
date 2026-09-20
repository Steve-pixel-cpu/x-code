"""设置 → 外观: 背景图片上传/清除接口（/api/bg）。"""
import base64

import pytest
from fastapi.testclient import TestClient

import server

# 1x1 PNG（最小合法图片）
PNG = ("data:image/png;base64,"
       + base64.b64encode(bytes.fromhex(
           "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
           "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
       )).decode())


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "STATIC_DIR", tmp_path)
    return TestClient(server.app)


def test_bg_upload_and_ver(client, tmp_path):
    r = client.post("/api/bg", json={"data": PNG})
    assert r.status_code == 200
    ver = r.json()["ver"]
    assert ver > 0
    assert (tmp_path / "bg-user.png").read_bytes().startswith(b"\x89PNG")
    # settings 能看到版本号
    assert client.get("/api/settings").json()["bg_ver"] == ver


def test_bg_clear(client, tmp_path):
    client.post("/api/bg", json={"data": PNG})
    r = client.post("/api/bg", json={"data": None})
    assert r.status_code == 200
    assert r.json()["ver"] == 0
    assert not (tmp_path / "bg-user.png").exists()


def test_bg_reject_bad_data(client):
    assert client.post("/api/bg", json={"data": "not-a-dataurl"}).status_code == 400


def test_bg_too_large(client):
    big = "data:image/png;base64," + base64.b64encode(b"x" * (8 * 1024 * 1024 + 1)).decode()
    assert client.post("/api/bg", json={"data": big}).status_code == 400
