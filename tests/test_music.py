"""摸鱼电台（网易云代理层）测试。

music.py 的网络调用全部 monkeypatch 掉, 不出真网;
server 路由走 TestClient 验证响应结构与错误映射。

运行方式（在 x-code 目录下）:
    .venv/Scripts/python.exe -m pytest tests/test_music.py -v
"""

import pytest
from fastapi.testclient import TestClient

import music as _music_mod
import server


@pytest.fixture()
def client():
    return TestClient(server.app)


# ------------------------------------------------------------
# music.py 纯逻辑（_song_brief / 缓存 / 归一化）
# ------------------------------------------------------------

def _song_like(**over):
    """一份网易云原始结构的歌曲条目（榜单形态: artists/album/duration）。"""
    s = {
        "id": 42,
        "name": "测试歌",
        "artists": [{"name": "张三"}, {"name": "李四"}],
        "album": {"name": "测试专辑", "picUrl": "http://p1.music.126.net/x.jpg"},
        "duration": 213000,
        "fee": 8,
    }
    s.update(over)
    return s


def test_song_brief_maps_fields_and_https():
    b = _music_mod._song_brief(_song_like())
    assert b == {
        "id": 42, "name": "测试歌", "artist": "张三 / 李四",
        "album": "测试专辑", "duration": 213.0, "fee": 8,
        "pic": "https://p1.music.126.net/x.jpg",
    }


def test_song_brief_accepts_v3_shape():
    """歌单 v3 接口形态: ar / al / dt。"""
    b = _music_mod._song_brief({
        "id": 7, "name": "V3", "ar": [{"name": "歌手"}],
        "al": {"name": "专辑", "picUrl": "http://x/y.png"}, "dt": 300500, "fee": 1,
    })
    assert b["artist"] == "歌手" and b["duration"] == 300.5 and b["fee"] == 1


def test_playlist_cache_avoids_refetch(monkeypatch):
    _music_mod._playlist_cache.clear()
    calls = {"n": 0}
    payload = {"code": 200, "result": {"name": "热歌榜", "tracks": [_song_like()]}}

    def fake_http(path, params, timeout=8.0):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(_music_mod, "_http_json", fake_http)
    r1 = _music_mod.playlist_songs(_music_mod.HOT_PLAYLIST_ID)
    r2 = _music_mod.playlist_songs(_music_mod.HOT_PLAYLIST_ID)
    assert calls["n"] == 1                    # 第二次命中缓存
    assert r1["name"] == "热歌榜" and r1["songs"][0]["id"] == 42
    assert r1 == r2
    _music_mod._playlist_cache.clear()


# ------------------------------------------------------------
# server 路由: monkeypatch 掉 music 的网络函数
# ------------------------------------------------------------

def test_api_music_playlist(client, monkeypatch):
    monkeypatch.setattr(_music_mod, "playlist_songs",
                        lambda pid: {"id": pid, "name": "榜", "songs": []})
    r = client.get("/api/music/playlist/3778678")
    assert r.status_code == 200
    assert r.json() == {"id": 3778678, "name": "榜", "songs": []}


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


def test_api_music_builtin_lists_builtins(client):
    r = client.get("/api/music/builtin").json()
    keys = [p["key"] for p in r["playlists"]]
    assert "hot" in keys and "new" in keys
