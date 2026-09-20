"""设置 → 外观: 图标/壁纸持久化接口（/api/icon, /api/bg）。

规格钉子: 用户上传件必须落在 APPEARANCE_DIR（~/.x-code/appearance/,
重启存活）, 绝不写进 STATIC_DIR——PyInstaller onefile 下那是 _MEIxxxx
临时解包目录, 退出即焚（头像/壁纸"重启就丢"的根因）。读取走 GET 端点
带出厂兜底, 前端不再引用 /static 下的用户资产。
"""
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

PNG_BYTES = base64.b64decode(PNG.split(",", 1)[1])


@pytest.fixture()
def iso(monkeypatch, tmp_path):
    """外观资产目录整体隔离到 tmp_path。"""
    app_dir = tmp_path / "appearance"
    icon_default = tmp_path / "icon-default.png"
    icon_default.write_bytes(PNG_BYTES)          # 出厂副本用最小 PNG 顶替
    monkeypatch.setattr(server, "APPEARANCE_DIR", app_dir)
    monkeypatch.setattr(server, "_ICON_LIVE", app_dir / "icon.png")
    monkeypatch.setattr(server, "_BG_LIVE", app_dir / "bg-user.png")
    monkeypatch.setattr(server, "_ICON_DEFAULT", icon_default)
    return tmp_path


@pytest.fixture()
def client(iso):
    return TestClient(server.app)


# ------------------------------------------------------------
# 背景 /api/bg
# ------------------------------------------------------------

def test_bg_upload_and_ver(client, iso):
    r = client.post("/api/bg", json={"data": PNG})
    assert r.status_code == 200
    ver = r.json()["ver"]
    assert ver > 0
    assert (iso / "appearance" / "bg-user.png").read_bytes().startswith(b"\x89PNG")
    # settings 能看到版本号
    assert client.get("/api/settings").json()["bg_ver"] == ver


def test_bg_clear(client, iso):
    client.post("/api/bg", json={"data": PNG})
    r = client.post("/api/bg", json={"data": None})
    assert r.status_code == 200
    assert r.json()["ver"] == 0
    assert not (iso / "appearance" / "bg-user.png").exists()


def test_bg_reject_bad_data(client):
    assert client.post("/api/bg", json={"data": "not-a-dataurl"}).status_code == 400


def test_bg_too_large(client):
    # 上限与 server._BG_MAX 对齐: 解码后 20MB（前端 file.size 同为 20MB 口径）
    big = "data:image/png;base64," + base64.b64encode(b"x" * (20 * 1024 * 1024 + 1)).decode()
    assert client.post("/api/bg", json={"data": big}).status_code == 400


def test_bg_get_serves_upload_and_404_when_unset(client, iso):
    # 未设置: 404（前端以 bg_ver=0 为"无壁纸"口径, 不盲拉）
    assert client.get("/api/bg").status_code == 404
    client.post("/api/bg", json={"data": PNG})
    r = client.get("/api/bg")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")


# ------------------------------------------------------------
# 应用图标 /api/icon（消息头像同源）
# ------------------------------------------------------------

def test_icon_upload_and_get(client, iso):
    r = client.post("/api/icon", json={"data": PNG})
    assert r.status_code == 200
    assert r.json()["ver"] > 0
    assert (iso / "appearance" / "icon.png").read_bytes() == PNG_BYTES
    got = client.get("/api/icon")
    assert got.status_code == 200
    assert got.content == PNG_BYTES


def test_icon_reset_deletes_user_copy_and_falls_back(client, iso):
    client.post("/api/icon", json={"data": PNG})
    r = client.post("/api/icon", json={"data": None})
    assert r.status_code == 200
    assert r.json()["ver"] == 0                       # 回落到出厂: ver 归零
    assert not (iso / "appearance" / "icon.png").exists()
    got = client.get("/api/icon")
    assert got.status_code == 200
    assert got.content == PNG_BYTES                   # 出厂兜底仍可读


def test_icon_reject_bad_data(client):
    assert client.post("/api/icon", json={"data": "not-a-dataurl"}).status_code == 400


def test_favicon_serves_user_icon_then_factory(client, iso):
    assert client.get("/favicon.ico", follow_redirects=False).status_code == 200
    client.post("/api/icon", json={"data": PNG})
    assert client.get("/favicon.ico").content == PNG_BYTES


# ------------------------------------------------------------
# 升级迁移: 旧版写在 STATIC_DIR 的用户资产一次性搬进 appearance 目录
# ------------------------------------------------------------

def test_migrate_moves_legacy_user_assets(monkeypatch, tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "icon.png").write_bytes(b"user-icon")          # 与出厂不同
    (static_dir / "icon-default.png").write_bytes(PNG_BYTES)
    (static_dir / "bg-user.png").write_bytes(b"user-bg")
    app_dir = tmp_path / "appearance"
    monkeypatch.setattr(server, "STATIC_DIR", static_dir)
    monkeypatch.setattr(server, "APPEARANCE_DIR", app_dir)
    monkeypatch.setattr(server, "_ICON_LIVE", app_dir / "icon.png")
    monkeypatch.setattr(server, "_BG_LIVE", app_dir / "bg-user.png")
    monkeypatch.setattr(server, "_ICON_DEFAULT", static_dir / "icon-default.png")

    server._migrate_legacy_appearance()

    assert (app_dir / "icon.png").read_bytes() == b"user-icon"
    assert (app_dir / "bg-user.png").read_bytes() == b"user-bg"
    assert not (static_dir / "bg-user.png").exists()


def test_migrate_skips_factory_identical_icon(monkeypatch, tmp_path):
    # icon 与出厂副本逐字节一致 = 用户没改过, 不迁移（出厂件不需要跟随）
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "icon.png").write_bytes(PNG_BYTES)
    (static_dir / "icon-default.png").write_bytes(PNG_BYTES)
    app_dir = tmp_path / "appearance"
    monkeypatch.setattr(server, "STATIC_DIR", static_dir)
    monkeypatch.setattr(server, "APPEARANCE_DIR", app_dir)
    monkeypatch.setattr(server, "_ICON_LIVE", app_dir / "icon.png")
    monkeypatch.setattr(server, "_BG_LIVE", app_dir / "bg-user.png")
    monkeypatch.setattr(server, "_ICON_DEFAULT", static_dir / "icon-default.png")

    server._migrate_legacy_appearance()

    assert not (app_dir / "icon.png").exists()
