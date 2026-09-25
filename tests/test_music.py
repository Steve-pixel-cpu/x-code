"""摸鱼电台（网易云代理层 + 本地曲库）测试。

music.py 的网络调用全部 monkeypatch 掉, 不出真网;
本地曲库 monkeypatch 到 tmp_path, 不碰真实 ~/.x-code;
server 路由走 TestClient 验证响应结构与错误映射。

运行方式（在 x-code 目录下）:
    .venv/Scripts/python.exe -m pytest tests/test_music.py -v
"""

import json

import pytest
from fastapi.testclient import TestClient

import music as _music_mod
import server


@pytest.fixture()
def client():
    return TestClient(server.app)


@pytest.fixture()
def lib_file(tmp_path, monkeypatch):
    """本地曲库重定向到 tmp, 每个用例独立文件。"""
    p = tmp_path / "music-library.json"
    monkeypatch.setattr(_music_mod, "LIB_FILE", p)
    return p


def _song(sid=42, name="测试歌", **over):
    """一份前端形态的歌曲简报（_song_brief 的字段）。"""
    s = {
        "id": sid, "name": name, "artist": "张三 / 李四",
        "album": "测试专辑", "duration": 213.0, "fee": 8,
        "pic": "https://p1.music.126.net/x.jpg", "source": "netease",
    }
    s.update(over)
    return s


# ------------------------------------------------------------
# music.py 纯逻辑（_song_brief / _norm_song / 曲库读写）
# ------------------------------------------------------------

def test_song_brief_maps_fields_and_https():
    raw = {
        "id": 42, "name": "测试歌",
        "artists": [{"name": "张三"}, {"name": "李四"}],
        "album": {"name": "测试专辑", "picUrl": "http://p1.music.126.net/x.jpg"},
        "duration": 213000, "fee": 8,
    }
    assert _music_mod._song_brief(raw) == _song()


def test_song_brief_accepts_v3_shape():
    """搜索 v3 接口形态: ar / al / dt。"""
    b = _music_mod._song_brief({
        "id": 7, "name": "V3", "ar": [{"name": "歌手"}],
        "al": {"name": "专辑", "picUrl": "http://x/y.png"}, "dt": 300500, "fee": 1,
    })
    assert b["artist"] == "歌手" and b["duration"] == 300.5 and b["fee"] == 1


def test_norm_song_whitelist_and_reject():
    """只留白名单字段; id 不齐/歌名空 → 拒收 None。
    id 保持原样返回（BV 号是字符串; 与库内对齐在 add 时做）;
    没有 source 时按 netease 补。"""
    ok = _music_mod._norm_song({"id": "9", "name": " 歌 ", "hacker": "x",
                                "artist": "A", "extra": 1})
    assert ok == {"id": "9", "name": "歌", "artist": "A", "album": None,
                  "duration": None, "fee": None, "pic": None, "source": "netease"}
    # id 保留原样; 拒收只针对「无 id / 空 id」和「空名」
    assert _music_mod._norm_song({"id": "abc", "name": "x"})["id"] == "abc"
    assert _music_mod._norm_song({"id": "", "name": "x"}) is None
    assert _music_mod._norm_song({"id": None, "name": "x"}) is None
    assert _music_mod._norm_song({"id": 1, "name": "  "}) is None
    assert _music_mod._norm_song({"name": "没有id"}) is None
    assert _music_mod._norm_song("不是字典") is None


def test_norm_song_accepts_bili_source():
    """B 站视频: id 是 BV 号字符串, source=bili 保留。"""
    ok = _music_mod._norm_song({"id": "BV1FHeE66Ew5", "name": "视频",
                                "source": "bili", "fee": 0})
    assert ok["source"] == "bili" and ok["id"] == "BV1FHeE66Ew5"
    # 非白名单 source 一律归一为 netease
    assert _music_mod._norm_song({"id": 1, "name": "x", "source": "other"}) \
        ["source"] == "netease"


def test_norm_songs_dedupes_by_id():
    out = _music_mod._norm_songs([_song(1), _song(1, "重复"), "垃圾", _song(2)])
    assert [s["id"] for s in out] == [1, 2]


# ------------------------------------------------------------
# music.py 本地曲库: 收藏 / 歌单（文件读写走 tmp）
# ------------------------------------------------------------

def test_library_empty_state(lib_file):
    assert _music_mod.list_library() == {"favorites": [], "playlists": []}


def test_favorites_add_dedupe_and_persist(lib_file):
    r1 = _music_mod.add_favorites([_song(1, "A"), _song(2, "B")])
    assert r1["added"] == 2
    r2 = _music_mod.add_favorites([_song(1, "A改名也不算新歌")])
    assert r2["added"] == 0                            # 按 id 去重
    assert _music_mod.list_library()["favorites"][0]["name"] == "A"
    assert json.loads(lib_file.read_text(encoding="utf-8"))["favorites"]  # 真落盘


def test_favorites_remove_missing_raises(lib_file):
    _music_mod.add_favorites([_song(1)])
    _music_mod.remove_favorite(1)
    assert _music_mod.list_library()["favorites"] == []
    with pytest.raises(KeyError):
        _music_mod.remove_favorite(1)


def test_favorites_cap(lib_file, monkeypatch):
    monkeypatch.setattr(_music_mod, "FAVORITES_MAX", 3)
    _music_mod.add_favorites([_song(i, f"S{i}") for i in range(1, 6)])
    favs = _music_mod.list_library()["favorites"]
    assert len(favs) == 3 and favs[0]["id"] == 1        # 到上限截断


def test_playlist_crud_roundtrip(lib_file):
    pl = _music_mod.create_playlist("加班BGM", [_song(1), _song(1), "垃圾"])
    assert pl["id"] == 1 and pl["name"] == "加班BGM" and len(pl["songs"]) == 1

    _music_mod.add_to_playlist(1, [_song(2), _song(1)])
    assert [s["id"] for s in _music_mod.add_to_playlist(1, [])["songs"]] == [1, 2]

    assert _music_mod.rename_playlist(1, "摸鱼FM")["name"] == "摸鱼FM"
    assert _music_mod.list_library()["playlists"][0]["name"] == "摸鱼FM"

    _music_mod.remove_from_playlist(1, [1])
    assert [s["id"] for s in _music_mod.list_library()["playlists"][0]["songs"]] == [2]

    _music_mod.delete_playlist(1)
    assert _music_mod.list_library()["playlists"] == []


def test_playlist_errors(lib_file):
    with pytest.raises(ValueError):
        _music_mod.create_playlist("   ")               # 空名
    _music_mod.create_playlist("有歌")
    with pytest.raises(KeyError):
        _music_mod.add_to_playlist(999, [_song(1)])     # 歌单不存在
    with pytest.raises(KeyError):
        _music_mod.rename_playlist(999, "x")
    with pytest.raises(KeyError):
        _music_mod.delete_playlist(999)
    with pytest.raises(ValueError):
        _music_mod.rename_playlist(1, "")               # 改成空名


def test_playlist_name_unique(lib_file):
    """歌单名去重: 新建重名/重命名撞别人都不行, 改回自己的名字放行。"""
    _music_mod.create_playlist("摸鱼FM")
    _music_mod.create_playlist("加班BGM")
    with pytest.raises(ValueError):
        _music_mod.create_playlist("摸鱼FM")            # 新建重名
    with pytest.raises(ValueError):
        _music_mod.rename_playlist(2, "摸鱼FM")         # 改名撞别人
    out = _music_mod.rename_playlist(1, "摸鱼FM")       # 改回自己的名字: 放行
    assert out["name"] == "摸鱼FM"
    assert [p["name"] for p in _music_mod.list_library()["playlists"]] \
        == ["摸鱼FM", "加班BGM"]


def test_playlist_cap_clamps(lib_file, monkeypatch):
    """文件里塞超量歌单/歌曲时读取阶段收敛到上限（自愈, 不崩）。"""
    pl = _music_mod.create_playlist("大歌单", [_song(i) for i in range(3)])
    monkeypatch.setattr(_music_mod, "PLAYLIST_SONGS_MAX", 2)
    monkeypatch.setattr(_music_mod, "PLAYLISTS_MAX", 1)
    lib = _music_mod.list_library()
    assert len(lib["playlists"]) == 1 and len(lib["playlists"][0]["songs"]) == 2


def test_library_corrupt_file_is_empty(lib_file):
    lib_file.write_text("{不是JSON", encoding="utf-8")
    assert _music_mod.list_library() == {"favorites": [], "playlists": []}


# ------------------------------------------------------------
# 复合键（source+id）: B 站视频与网易云歌可混库, 互不串键
# ------------------------------------------------------------

def _bili_video(bvid="BV1FHeE66Ew5", name="测试视频", **over):
    return {"id": bvid, "name": name, "artist": "B站视频", "album": "",
            "duration": 200.0, "fee": 0,
            "pic": "https://i0.hdslb.com/bfs/archive/x.jpg", "source": "bili",
            **over}


def test_favorites_composite_key_mix_and_dedupe(lib_file):
    """网易云歌与 B 站视频可同时收藏; 同 id 不同来源互不覆盖, 同来源去重。"""
    _music_mod.add_favorites([_song(1, "网易A"), _bili_video()])
    assert len(_music_mod.list_library()["favorites"]) == 2

    # 同 BV 加重复 → 去重; 不同来源同名字 → 不冲突
    _music_mod.add_favorites([_bili_video("BV1FHeE66Ew5", "重复视频"),
                              _song(1, "同名歌")])
    lib = _music_mod.list_library()
    assert len(lib["favorites"]) == 2
    assert {s["source"] for s in lib["favorites"]} == {"netease", "bili"}
    assert lib["favorites"][0]["id"] == 1

    # 取消收藏按来源删: BV 删掉不影响网易云 id 1
    _music_mod.remove_favorite("BV1FHeE66Ew5", source="bili")
    lib = _music_mod.list_library()
    assert len(lib["favorites"]) == 1 and lib["favorites"][0]["source"] == "netease"
    with pytest.raises(KeyError):
        _music_mod.remove_favorite("BV1FHeE66Ew5", source="bili")


def test_playlist_composite_key_mix(lib_file):
    """歌单里 B 站视频与网易云歌并存, 移除按来源。"""
    pl = _music_mod.create_playlist("混排", [_song(1), _bili_video()])
    assert len(pl["songs"]) == 2

    _music_mod.add_to_playlist(pl["id"], [_bili_video("BV1FHeE66Ew5", "再来"),
                                          _song(1, "同名")])
    pl = _music_mod.list_library()["playlists"][0]
    assert len(pl["songs"]) == 2                       # 混排去重不误伤

    _music_mod.remove_from_playlist(pl["id"], ["BV1FHeE66Ew5"], source="bili")
    pl = _music_mod.list_library()["playlists"][0]
    assert len(pl["songs"]) == 1 and pl["songs"][0]["id"] == 1


# ------------------------------------------------------------
# server 路由: 搜索/直链/歌词（网络调用 monkeypatch 掉）
# ------------------------------------------------------------

def test_api_music_search(client, monkeypatch):
    seen = {}

    def fake_search(kw, limit=30):
        seen.update(kw=kw, limit=limit)
        return {"kw": kw, "songs": [{"id": 1, "name": kw}]}

    monkeypatch.setattr(_music_mod, "search_songs", fake_search)
    r = client.get("/api/music/search", params={"kw": "晴天", "limit": 10})
    assert r.status_code == 200
    assert r.json()["songs"][0]["name"] == "晴天"
    assert seen == {"kw": "晴天", "limit": 10}


def test_api_music_url_free_vs_vip(client, monkeypatch):
    monkeypatch.setattr(_music_mod, "song_url", lambda sid, br=128000: {
        "id": sid, "url": "https://m/x.mp3", "br": br, "size": 1, "type": "mp3"})
    ok = client.get("/api/music/url", params={"id": 5, "br": 320000}).json()
    assert ok["url"].startswith("https://") and ok["br"] == 320000

    monkeypatch.setattr(_music_mod, "song_url", lambda sid, br=128000: {
        "id": sid, "url": None, "br": 0, "size": 0, "type": "mp3"})
    vip = client.get("/api/music/url", params={"id": 6}).json()
    assert vip["url"] is None                 # 前端据此跳过


def test_api_music_upstream_failure_is_502(client, monkeypatch):
    def boom(sid, br=128000):
        raise _music_mod.MusicApiError("网易接口请求失败: timeout")

    monkeypatch.setattr(_music_mod, "song_url", boom)
    r = client.get("/api/music/url", params={"id": 1})
    assert r.status_code == 502
    assert "timeout" in r.json()["detail"]


def test_api_music_lyric(client, monkeypatch):
    monkeypatch.setattr(_music_mod, "song_lyric",
                        lambda sid: {"id": sid, "lrc": "[00:01.00]你好"})
    r = client.get("/api/music/lyric", params={"id": 9})
    assert r.status_code == 200
    assert r.json() == {"id": 9, "lrc": "[00:01.00]你好"}


# ------------------------------------------------------------
# server 路由: 本地曲库
# ------------------------------------------------------------

def test_api_library_empty(client, lib_file):
    r = client.get("/api/music/library")
    assert r.status_code == 200
    assert r.json() == {"favorites": [], "playlists": []}


def test_api_favorites_flow(client, lib_file):
    r = client.post("/api/music/favorites", json={"songs": [_song(5, "五")]})
    assert r.status_code == 200 and r.json()["added"] == 1

    # 重复收藏: 去重
    r = client.post("/api/music/favorites", json={"songs": [_song(5, "五")]})
    assert r.json()["added"] == 0

    # 坏 body
    assert client.post("/api/music/favorites", json={}).status_code == 400
    assert client.post("/api/music/favorites", json={"songs": "x"}).status_code == 400
    assert client.post("/api/music/favorites",
                       json={"songs": [{"id": "abc"}]}).status_code == 200  # 清洗后全丢

    assert client.delete("/api/music/favorites/5").status_code == 200
    assert client.delete("/api/music/favorites/5").status_code == 404   # 已不在


def test_api_playlists_flow(client, lib_file):
    r = client.post("/api/music/playlists", json={"name": "码农之歌", "songs": [_song(1)]})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert r.json()["songs"][0]["id"] == 1

    # 追加 / 去重
    r = client.post(f"/api/music/playlists/{pid}/songs",
                    json={"songs": [_song(2), _song(1)]})
    assert [s["id"] for s in r.json()["songs"]] == [1, 2]

    # 重命名
    r = client.patch(f"/api/music/playlists/{pid}", json={"name": "敲码BGM"})
    assert r.json()["name"] == "敲码BGM"

    # 删歌 / 删歌单
    assert client.delete(f"/api/music/playlists/{pid}/songs/1").status_code == 200
    assert client.delete(f"/api/music/playlists/{pid}").status_code == 200
    assert client.get("/api/music/library").json()["playlists"] == []


def test_api_playlists_error_mapping(client, lib_file):
    assert client.post("/api/music/playlists", json={}).status_code == 400
    assert client.post("/api/music/playlists", json={"name": " "}).status_code == 400
    assert client.patch("/api/music/playlists/42", json={"name": "x"}).status_code == 404
    assert client.delete("/api/music/playlists/42").status_code == 404
    assert client.post("/api/music/playlists/42/songs",
                       json={"songs": [_song(1)]}).status_code == 404
    assert client.delete("/api/music/playlists/42/songs/1").status_code == 404


# ------------------------------------------------------------
# music_play 工具: 聊天点播（搜索 + 选歌 + _meta 交给前端电台）
# ------------------------------------------------------------

def test_music_play_defaults_to_first_free(monkeypatch):
    """默认播第一个非 VIP 候选; meta 带 song/queue/qname 三件套。"""
    songs = [_song(1, "VIP版", fee=1), _song(2, "免费版"), _song(3, "也是VIP", fee=1)]
    monkeypatch.setattr(_music_mod, "search_songs", lambda kw, limit: {"kw": kw, "songs": songs})
    out = _music_mod.music_play_tool({"kw": "测试"})
    assert out._meta["music"]["song"]["id"] == 2    # 跳过 VIP 挑免费
    assert [s["id"] for s in out._meta["music"]["queue"]] == [1, 2, 3]
    assert out._meta["music"]["qname"] == "聊天点播"
    payload = json.loads(str(out))
    assert payload["picked"] == 2
    assert payload["playing"] == "免费版 - 张三 / 李四"
    assert len(payload["candidates"]) == 3


def test_music_play_explicit_index_clamped(monkeypatch):
    """指定 index 播那一版; 越界钳到最后一首, 非法值回落默认选歌。"""
    songs = [_song(1), _song(2), _song(3)]
    monkeypatch.setattr(_music_mod, "search_songs", lambda kw, limit: {"kw": kw, "songs": songs})
    out = _music_mod.music_play_tool({"kw": "x", "index": 3})
    assert out._meta["music"]["song"]["id"] == 3
    out = _music_mod.music_play_tool({"kw": "x", "index": 99})
    assert out._meta["music"]["song"]["id"] == 3
    out = _music_mod.music_play_tool({"kw": "x", "index": "垃圾"})
    assert out._meta["music"]["song"]["id"] == 1    # 落回第一个免费候选


def test_music_play_all_vip_still_picks_first(monkeypatch):
    """全 VIP: 照播第一首, 由前端拿不到直链时自动跳。"""
    songs = [_song(1, fee=1), _song(2, fee=1)]
    monkeypatch.setattr(_music_mod, "search_songs", lambda kw, limit: {"kw": kw, "songs": songs})
    out = _music_mod.music_play_tool({"kw": "x"})
    assert out._meta["music"]["song"]["id"] == 1


def test_music_play_empty_result_no_meta(monkeypatch):
    """没搜到: 纯文本回话, 不带 _meta（前端不该被触发）。"""
    monkeypatch.setattr(_music_mod, "search_songs", lambda kw, limit: {"kw": kw, "songs": []})
    out = _music_mod.music_play_tool({"kw": "不存在的歌"})
    assert "没有搜到" in str(out)
    assert getattr(out, "_meta", {}) == {}


@pytest.mark.parametrize("bad", [None, {}, {"kw": ""}, {"kw": "   "}])
def test_music_play_bad_kw_raises(bad):
    """空关键词直接 ValueError, 由 registry 转 tool_result 报给模型。"""
    with pytest.raises(ValueError):
        _music_mod.music_play_tool(bad)


def test_music_play_bili_source(monkeypatch):
    """source=bili: 搜 B 站视频, 队列项带 bili 标记, qname 是 B站点播。"""
    import bilibili as _bili_mod
    videos = [_bili_mod_bili_video("BV1AAAA1111", "科幻短片"),
              _bili_mod_bili_video("BV1BBBB2222", "纪录片")]
    monkeypatch.setattr(_bili_mod, "search_videos",
                        lambda kw, limit: {"kw": kw, "videos": videos})
    out = _music_mod.music_play_tool({"kw": "科幻", "source": "bili"})
    meta = out._meta["music"]
    assert meta["song"]["id"] == "BV1AAAA1111"
    assert meta["song"]["source"] == "bili"
    assert meta["qname"] == "B站点播"
    assert [s["id"] for s in meta["queue"]] == ["BV1AAAA1111", "BV1BBBB2222"]
    assert "视频" in str(out)


def _bili_mod_bili_video(bvid, name):
    return {"id": bvid, "name": name, "artist": "B站视频", "album": "",
            "duration": 1.0, "fee": 0, "pic": "", "source": "bili"}


def test_music_play_bili_no_result(monkeypatch):
    """B 站没搜到: 纯文本回话, 不带 meta。"""
    import bilibili as _bili_mod
    monkeypatch.setattr(_bili_mod, "search_videos",
                        lambda kw, limit: {"kw": kw, "videos": []})
    out = _music_mod.music_play_tool({"kw": "xxx", "source": "bili"})
    assert "没有搜到" in str(out) and "视频" in str(out)
    assert getattr(out, "_meta", {}) == {}


def test_music_play_registered_and_read_only():
    """TOOLS 有声明; 权限档登记为 READ_ONLY（= PermissionMode.PLAN, 值 1）。"""
    import main
    from permissions import READ_ONLY_MODE
    assert main.TOOL_REQUIREMENTS.get("music_play") == READ_ONLY_MODE
    assert any(t["name"] == "music_play" for t in main.TOOLS)
