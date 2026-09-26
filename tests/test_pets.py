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
    """仓库必须带内置宠物(feibi/kunge 两只, 不再多带), 否则开箱无宠物可用。"""
    root = Path(server.__file__).resolve().parent
    for pid, rows in (("feibi", 11), ("kunge", 9)):
        folder = root / "pets" / pid
        assert (folder / "pet.json").exists(), pid
        assert server._image_size(folder / "spritesheet.webp") == (1536, rows * 208), pid


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


def test_bundled_pets_pass_contract():
    """仓库 pets/ 里的内置宠物(随安装包分发)必须全部过扫描契约:
    图集 1536 宽、行数 9(v1)/11(v2)、manifest 合规。内置默认宠物
    就 feibi/kunge 两只——精确集合断言, 多带少带都拦。新增或替换
    内置宠物时这里兜底——坏图集在测试期就被拦下, 而不是装到用户
    机器上表现为桌宠白屏。"""
    repo_pets = Path(server.__file__).resolve().parent / "pets"
    bundled = sorted(d for d in repo_pets.iterdir() if d.is_dir())
    assert {d.name for d in bundled} == {"feibi", "kunge"}
    for folder in bundled:
        found = server._scan_pet_folder(folder, "install")
        assert found is not None, f"内置宠物 {folder.name} 没过契约校验"
        info, sheet = found
        assert info["id"] == folder.name      # id 即文件夹名(扫描层的约定)
        assert sheet.is_file()


# ------------------------------------------------------------
# 人设(pet.json persona)清洗与透传
# ------------------------------------------------------------

def test_persona_sanitized(tmp_path):
    """persona 白名单清洗: 只留 name/style/lines, 超长截断, 空段丢弃。"""
    d = _make_pet(tmp_path, "persona-pet", manifest={
        "displayName": "人设猫",
        "spritesheetPath": "spritesheet.png",
        "persona": {
            "name": "  昆哥  ",
            "style": "毒舌老哥 " * 60,           # 超长 → 截到 300
            "lines": {
                "done": ["收工", "  ", 42, "溜了"],   # 脏元素被滤掉
                "junk-group": "not-a-list",           # 非数组整组丢弃
                "ack": [],                            # 空组丢弃
            },
            "evil": {"deep": True},                   # 未知键丢弃
        },
    })
    info, _ = server._scan_pet_folder(d, "install")
    persona = info["persona"]
    assert persona["name"] == "昆哥"
    assert len(persona["style"]) <= 300 and "毒舌老哥" in persona["style"]
    assert persona["lines"] == {"done": ["收工", "溜了"]}
    assert "evil" not in persona


def test_persona_absent_or_invalid_omitted(tmp_path):
    """没有 persona / 不是 dict: 信息里不带该键, 老清单零影响。"""
    d = _make_pet(tmp_path, "plain")
    info, _ = server._scan_pet_folder(d, "install")
    assert "persona" not in info

    d2 = _make_pet(tmp_path, "weird", manifest={
        "spritesheetPath": "spritesheet.png", "persona": "毒舌",
    })
    info2, _ = server._scan_pet_folder(d2, "install")
    assert "persona" not in info2


# ------------------------------------------------------------
# REST /api/pet/chat（桌宠周期点评）
# ------------------------------------------------------------

def _stub_generate(monkeypatch, reply):
    """替换全局单例的 generate_text, 记录入参供断言。"""
    calls = {}

    def fake(system, user, max_tokens=512):
        calls["system"] = system
        calls["user"] = user
        calls["max_tokens"] = max_tokens
        return reply

    monkeypatch.setattr(server.api_client, "generate_text", fake)
    return calls


def test_pet_chat_quip_ok(client, monkeypatch):
    calls = _stub_generate(monkeypatch, '{"say": "键盘又热了"}')
    r = client.post("/api/pet/chat", json={
        "persona": {"name": "昆哥", "style": "毒舌老哥"},
        "state": {"event": "Bash 连着用了 6 次", "base": "running",
                  "run_minutes": 18, "top_tools": "Bash×6 Read×2",
                  "errors": 1, "hour": 23, "busy_sessions": 2},
    })
    assert r.status_code == 200
    assert r.json() == {"say": "键盘又热了"}
    assert calls["max_tokens"] == 64
    joined = "\n".join(calls["system"])
    assert "昆哥" in joined and "毒舌老哥" in joined          # 人设进 system
    user = calls["user"]
    assert "刚刚发生: Bash 连着用了 6 次" in user             # 事件上下文
    assert "18 分钟" in user and "Bash×6" in user             # 现场统计
    assert "出错 1 次" in user and "23 点" in user
    assert "针对刚发生的这件事" in user                       # 事件措辞
    assert "打工" in user


def test_pet_chat_quip_without_event(client, monkeypatch):
    """无事件 = 兜底随机点评: 通用措辞, 无"刚刚发生"行。"""
    calls = _stub_generate(monkeypatch, '{"say": "稳得很"}')
    r = client.post("/api/pet/chat", json={
        "state": {"base": "idle"},
    })
    assert r.json() == {"say": "稳得很"}
    assert "刚刚发生" not in calls["user"]
    assert "结合人设和现场" in calls["user"]


def test_pet_chat_parse_fallback(client, monkeypatch):
    """认不出 JSON: 整段当 say; 缺 say 键兜成空。"""
    _stub_generate(monkeypatch, "就一句大实话")
    assert client.post("/api/pet/chat", json={}).json() == {
        "say": "就一句大实话"}

    _stub_generate(monkeypatch, '{"msg": "没有 say 键"}')
    r = client.post("/api/pet/chat", json={})
    assert r.json()["say"] == ""                              # 空回退, 不抛错


def test_pet_chat_generate_failure_returns_empty(client, monkeypatch):
    def boom(system, user, max_tokens=512):
        raise RuntimeError("网络炸了")

    monkeypatch.setattr(server.api_client, "generate_text", boom)
    r = client.post("/api/pet/chat", json={})
    assert r.status_code == 200
    assert r.json() == {"say": ""}


def test_pet_chat_unknown_provider_falls_back_to_global(client, monkeypatch):
    """provider 不存在/被禁用: 回落全局单例, 也不留桌宠专属缓存。"""
    monkeypatch.setattr(server, "_provider_cfg",
                        {"active": {}, "providers": []})
    monkeypatch.setattr(server, "_pet_client", None)
    calls = _stub_generate(monkeypatch, '{"say": "在"}')
    r = client.post("/api/pet/chat", json={
        "provider_id": "ghost", "model_id": "x"})
    assert r.json() == {"say": "在"}
    assert calls                                              # 走的就是全局单例
    assert server._pet_client is None


def test_pet_chat_dedicated_client_cached(client, monkeypatch):
    """指定 provider: 按其配置构建专属 client, 连接要素不变命中缓存,
    换模型即重建。"""
    monkeypatch.setattr(server, "_provider_cfg", {
        "active": {"provider": "global-p"},
        "providers": [{"id": "cheap", "enabled": True, "api_key": "k-1",
                       "base_url": "https://api.cheap.example/v1",
                       "protocol": "openai", "models": [{"id": "mini"}]},
                      {"id": "global-p", "enabled": True, "api_key": "k-2",
                       "base_url": "", "protocol": "anthropic",
                       "models": [{"id": "big"}]}],
    })
    monkeypatch.setattr(server, "_pet_client", None)
    built = []

    def fake_make(protocol, *, api_key, model, base_url, **kw):
        class _Stub:
            pass
        stub = _Stub()
        built.append((protocol, api_key, model, base_url))
        return stub

    monkeypatch.setattr(server, "make_api_client", fake_make)
    cli1 = server._pet_api_client("cheap", "mini")
    cli2 = server._pet_api_client("cheap", "mini")
    assert cli1 is cli2 and len(built) == 1                   # 缓存命中
    assert built[0] == ("openai", "k-1", "mini", "https://api.cheap.example/v1")
    server._pet_api_client("cheap", "other")                  # 换模型 → 重建
    assert len(built) == 2 and built[1][2] == "other"
    # 指向全局 active 的 provider: 直接用全局单例, 不建专属
    assert server._pet_api_client("global-p", None) is server.api_client
