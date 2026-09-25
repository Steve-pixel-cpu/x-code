"""桌宠(Codex 宠物格式兼容层)测试。

目录解析/图集校验/扫描走纯函数; REST 走 TestClient。
图集资产只需一个合法文件头——_image_size 只读前 32 字节,
不必真的生成 1536x1872 图片。

运行方式(在 x-code 目录下):
    .venv/Scripts/python.exe -m pytest tests/test_pets.py -v
"""

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server


@pytest.fixture()
def client():
    return TestClient(server.app)


def _png_head(w: int, h: int) -> bytes:
    """最小 PNG 头: 签名 + IHDR 长度/类型 + 宽高(足够 _image_size 用)。"""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", w, h))


def _gif_head(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h)


def _make_pet(folder: Path, pid: str = None, *, w=1536, rows=9,
              manifest=None, sheet_name="spritesheet.png"):
    """在 folder/pid 下放一份宠物资产(manifest=None 则不写 pet.json)。"""
    d = folder / (pid or "pet-a")
    d.mkdir(parents=True)
    (d / sheet_name).write_bytes(_png_head(w, rows * 208))
    if manifest is not False:
        (d / "pet.json").write_text(json.dumps(manifest if manifest is not None else {
            "id": pid or "pet-a", "displayName": "宠物甲",
            "description": "测试", "spritesheetPath": sheet_name,
        }), encoding="utf-8")
    return d


# ------------------------------------------------------------
# 文件头解析
# ------------------------------------------------------------

def test_image_size_png_gif():
    assert server._image_size(Path("x")) is None          # 不存在
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.png"
        p.write_bytes(_png_head(1536, 1872))
        assert server._image_size(p) == (1536, 1872)
        g = Path(td) / "b.gif"
        g.write_bytes(_gif_head(640, 480))
        assert server._image_size(g) == (640, 480)
        j = Path(td) / "c.bin"
        j.write_bytes(b"\x00" * 32)
        assert server._image_size(j) is None              # 认不出的格式


def test_image_size_webp_vp8x():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s.webp"
        # RIFF + WEBP + VP8X 扩展头: 宽高以 1 偏移存 3 字节
        head = bytearray(32)
        head[0:4] = b"RIFF"
        head[8:12] = b"WEBP"
        head[12:16] = b"VP8X"
        head[24:27] = (1536 - 1).to_bytes(3, "little")
        head[27:30] = (2288 - 1).to_bytes(3, "little")
        p.write_bytes(bytes(head))
        assert server._image_size(p) == (1536, 2288)      # v2 图集


# ------------------------------------------------------------
# 目录解析(源码态 / 冻结态双布局 / CODEX_HOME)
# ------------------------------------------------------------

def test_pets_dirs_source_mode(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    dirs = server._pets_dirs()
    # 首候选=用户目录(可写, 打开目录指向它), 其后是仓库开发样例与 Codex
    assert dirs[0] == (Path.home() / ".x-code" / "pets", "user")
    assert dirs[1] == (Path(server.__file__).resolve().parent / "pets", "install")
    assert dirs[-1][1] == "codex" and dirs[-1][0].name == "pets"


def test_pets_dirs_codex_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "mycodex"))
    dirs = server._pets_dirs()
    assert dirs[-1][0] == tmp_path / "mycodex" / "pets"


def test_pets_dirs_frozen_layouts(monkeypatch, tmp_path):
    """冻结态: 后端在 <安装>/resources/server/ 下, 候选必须覆盖
    Tauri(<安装>/pets)与 Electron(<安装>/resources/pets)两种布局。"""
    exe = tmp_path / "install" / "resources" / "server" / "x-code-server.exe"
    exe.parent.mkdir(parents=True)
    monkeypatch.setattr(server.sys, "frozen", True, raising=False)
    monkeypatch.setattr(server.sys, "executable", str(exe))
    dirs = server._pets_dirs()
    assert dirs[0] == (Path.home() / ".x-code" / "pets", "user")
    assert dirs[1][0] == tmp_path / "install" / "pets"
    assert dirs[2][0] == tmp_path / "install" / "resources" / "pets"


# ------------------------------------------------------------
# 文件夹扫描与图集契约
# ------------------------------------------------------------

def test_scan_pet_folder_full_manifest(tmp_path):
    d = _make_pet(tmp_path)
    found = server._scan_pet_folder(d, "install")
    assert found
    info, sheet = found
    assert info == {"id": "pet-a", "displayName": "宠物甲",
                    "description": "测试", "source": "install", "rows": 9}
    assert sheet == d / "spritesheet.png"


def test_scan_pet_folder_fallbacks(tmp_path):
    """没有 pet.json: 文件夹名做显示名; manifest 指向的图不在文件夹内则回退同名。"""
    d = _make_pet(tmp_path, "lonely-cat", manifest=False)
    info, sheet = server._scan_pet_folder(d, "codex")
    assert info["displayName"] == "lonely-cat" and info["source"] == "codex"
    assert info["description"] == ""

    d2 = _make_pet(tmp_path, "evil", manifest={"spritesheetPath": "../steal.png"})
    found = server._scan_pet_folder(d2, "install")       # 穿越路径被忽略, 回退命中
    assert found and found[1] == d2 / "spritesheet.png"


@pytest.mark.parametrize("w,rows", [(1024, 9), (1536, 4), (1536, 10)])
def test_scan_pet_folder_rejects_bad_sheet(tmp_path, w, rows):
    d = _make_pet(tmp_path, f"bad-{w}-{rows}", w=w, rows=rows)
    assert server._scan_pet_folder(d, "install") is None


def test_scan_pet_folder_without_sheet(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "pet.json").write_text("{}", encoding="utf-8")
    assert server._scan_pet_folder(d, "install") is None


def test_scan_pet_folder_tolerates_broken_manifest(tmp_path):
    d = tmp_path / "broken"
    d.mkdir()
    (d / "pet.json").write_text("{ 不是 json", encoding="utf-8")
    (d / "spritesheet.webp").write_bytes(b"\x00" * 10)   # 认不出的图 → 整体跳过
    assert server._scan_pet_folder(d, "install") is None


def test_list_pets_priority_and_cache(tmp_path, monkeypatch):
    install, codex = tmp_path / "pets", tmp_path / "codex-pets"
    _make_pet(install, "shared")                 # 两边同 id: 安装目录赢
    _make_pet(codex, "shared", manifest={"displayName": "Codex 侧"})
    _make_pet(codex, "codex-only")
    monkeypatch.setattr(server, "_pets_dirs",
                        lambda: [(install, "install"), (codex, "codex")])
    out = server._list_pets()
    ids = [p["id"] for p in out["pets"]]
    assert ids == ["shared", "codex-only"]       # sorted 且安装目录优先
    assert out["pets"][0]["source"] == "install"
    assert out["pets"][1]["source"] == "codex"
    assert out["petsDir"] == str(install)
    assert server._pet_sheets["shared"] == install / "shared" / "spritesheet.png"


# ------------------------------------------------------------
# REST 路由
# ------------------------------------------------------------

def test_builtin_pet_ships():
    """仓库必须带内置宠物(xcode-cat), 否则开箱无宠物可用。"""
    root = Path(server.__file__).resolve().parent
    sheet = root / "pets" / "xcode-cat" / "spritesheet.png"
    assert (root / "pets" / "xcode-cat" / "pet.json").exists()
    assert server._image_size(sheet) == (1536, 1872)


def _isolated_pets(tmp_path, monkeypatch):
    install = tmp_path / "pets"
    _make_pet(install, "pet-a")
    monkeypatch.setattr(server, "_pets_dirs", lambda: [(install, "install")])
    return install


def test_api_pets(client, tmp_path, monkeypatch):
    _isolated_pets(tmp_path, monkeypatch)
    r = client.get("/api/pets")
    assert r.status_code == 200
    data = r.json()
    assert data["petsDir"].endswith("pets")
    assert data["pets"] == [{"id": "pet-a", "displayName": "宠物甲",
                             "description": "测试", "source": "install", "rows": 9}]


def test_api_pet_sheet(client, tmp_path, monkeypatch):
    _isolated_pets(tmp_path, monkeypatch)
    r = client.get("/api/pets/pet-a/sheet")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")


def test_api_pet_sheet_unknown_and_traversal(client, tmp_path, monkeypatch):
    _isolated_pets(tmp_path, monkeypatch)
    assert client.get("/api/pets/none/sheet").status_code == 404
    # 白名单外的 id(路径穿越/特殊字符)一律 404, 不许触到文件系统
    for bad in ("..%2Fx", "a%2Fb", ".%2E", "x y", "con"):
        resp = client.get(f"/api/pets/{bad}/sheet")
        assert resp.status_code == 404, bad


def test_api_pet_sheet_picked_up_after_start(client, tmp_path, monkeypatch):
    """进程启动后才放进目录的宠物: 重扫一次即可用, 不必重启。"""
    install = _isolated_pets(tmp_path, monkeypatch)
    assert client.get("/api/pets/late/sheet").status_code == 404
    _make_pet(install, "late", manifest={"displayName": "迟到"})
    r = client.get("/api/pets/late/sheet")
    assert r.status_code == 200


def test_pet_html_page_and_token_gate(client, monkeypatch):
    monkeypatch.setattr(server, "API_TOKEN", "t")
    assert client.get("/pet.html").status_code == 403          # 门禁先拦
    ok = client.get("/pet.html", params={"token": "t"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/html")
    assert "pet-float" in ok.text
