# -*- coding: utf-8 -*-
"""摸鱼电台 · B站视频来源 测试。

bilibili.py 的网络调用（搜索页/视频页/playurl）全部 monkeypatch 掉,
解析逻辑用内嵌 HTML fixture, 不出真网; server 路由走 TestClient。
"""
import json

import pytest
from fastapi.testclient import TestClient

import bilibili as _bili
import server

client = TestClient(server.app)


# ------------------------------------------------------------
# 搜索页 HTML 解析（SSR 卡片 fixture, 2026-09 实测结构）
# ------------------------------------------------------------

_SEARCH_HTML = """<!DOCTYPE html><html><head><title>搜索</title></head><body>
<div id="contents">
<div class="bili-video-card" style="">
<a href="//www.bilibili.com/video/BV1FHeE66Ew5/" class="" target="_blank" data-v-xyz>
  <picture class="v-img">
    <source srcset="//i0.hdslb.com/bfs/archive/98b07700.jpg@672w_378h_1c.png" type="image/webp">
  </picture>
  <span class="bili-video-card__stats__duration">03:25</span>
</a>
<h3 class="bili-video-card__info--tit" title="【别恋 | 官方MV 】派伟俊 最后一次温柔 是我不说再见&#39;" data-v-xyz>
</div>
<div class="bili-video-card" style="">
<a href="//www.bilibili.com/video/BV1rEeE6kEVm/" class="" target="_blank" data-v-xyz>
  <picture class="v-img">
    <source srcset="//i1.hdslb.com/bfs/archive/aaa111.jpg@672w_378h_1c.png" type="image/webp">
  </picture>
  <span class="bili-video-card__stats__duration">4:21</span>
</a>
<h3 class="bili-video-card__info--tit" title="第二支视频 标题里有 &amp; amp &amp; weird" data-v-xyz>
</div>
</div></body></html>"""


@pytest.fixture()
def no_net(monkeypatch):
    """把 _get_text / _get / _http_json 全部替换成受控假响应。"""
    def stub_get_text(url):
        if "search.bilibili.com" in url:
            return _SEARCH_HTML
        raise AssertionError(f"不该访问: {url}")
    monkeypatch.setattr(_bili, "_get_text", stub_get_text)
    monkeypatch.setattr(_bili, "_get", lambda url: b"")
    monkeypatch.setattr(_bili, "_http_json", lambda url: {"code": 0, "data": {}})


def test_search_parses_cards(no_net):
    """SSR 搜索页: 每张卡解析出 bvid/标题/封面/时长, 忽略重复链接。"""
    r = _bili.search_videos("周杰伦", 10)
    assert r["kw"] == "周杰伦"
    vs = r["videos"]
    assert len(vs) == 2
    v0, v1 = vs[0], vs[1]
    assert v0["id"] == "BV1FHeE66Ew5"
    assert v0["name"].startswith("【别恋")
    assert v0["duration"] == 205.0          # 03:25 → 205s
    assert v0["pic"] == "https://i0.hdslb.com/bfs/archive/98b07700.jpg@672w_378h_1c.png"
    assert v0["source"] == "bili" and v0["artist"] == "B站视频"
    assert v1["id"] == "BV1rEeE6kEVm"
    assert v1["duration"] == 261.0          # 4:21 → 261s
    assert "&amp" not in v1["name"]         # HTML 实体已反转义


def test_search_empty_kw(no_net):
    assert _bili.search_videos("   ") == {"kw": "", "videos": []}


def test_search_limit_clamped(no_net):
    r = _bili.search_videos("x", 999)       # 上限 50
    assert len(r["videos"]) == 2            # fixture 只有 2 张


# ------------------------------------------------------------
# 视频页 __INITIAL_STATE__ → cid; playurl 直链
# ------------------------------------------------------------

def test_video_cid_from_initial_state(no_net, monkeypatch):
    """视频页 __INITIAL_STATE__ 里取 cid。"""
    body = ('<script>window.__INITIAL_STATE__={"videoData":'
            '{"bvid":"BV1FHeE66Ew5","cid":41910144938}}</script>')
    monkeypatch.setattr(_bili, "_get_text", lambda url: body)
    assert _bili._video_cid("BV1FHeE66Ew5") == 41910144938


def test_get_audio_url_dash_preferred(no_net, monkeypatch):
    """有 DASH 音频用 audio 直链; 缓存后不重复请求。"""
    calls = []

    def fake_playurl(bvid):
        calls.append(bvid)
        return "https://upos-mirror/audio.m4s?sig=xyz"

    monkeypatch.setattr(_bili, "_playurl", fake_playurl)
    _bili._LINK_CACHE.clear()

    assert _bili.get_audio_url("BV1X") == "https://upos-mirror/audio.m4s?sig=xyz"
    assert _bili.get_audio_url("BV1X") == "https://upos-mirror/audio.m4s?sig=xyz"
    assert calls == ["BV1X"]               # 缓存命中, 不重复打上游


def test_get_audio_url_none_not_cached(no_net, monkeypatch):
    """无音频流返回 None, 且不写缓存（下次还能重试）。"""
    monkeypatch.setattr(_bili, "_playurl", lambda bvid: None)
    _bili._LINK_CACHE.clear()
    assert _bili.get_audio_url("BV1X") is None
    assert "BV1X" not in _bili._LINK_CACHE


def test_playurl_picks_highest_bandwidth(no_net, monkeypatch):
    """DASH 多音轨时挑 bandwidth 最高的。"""

    def fake_json(url):
        return {"code": 0, "data": {"dash": {"audio": [
            {"id": 1, "bandwidth": 30000, "baseUrl": "https://a/low"},
            {"id": 2, "bandwidth": 128000, "baseUrl": "https://a/high"},
        ]}}}

    monkeypatch.setattr(_bili, "_video_cid", lambda bvid: 123)
    monkeypatch.setattr(_bili, "_http_json", fake_json)
    _bili._LINK_CACHE.clear()
    u = _bili.get_audio_url("BV1X")
    assert u == "https://a/high"


# ------------------------------------------------------------
# 音频流代理: token / 防盗链 / 逐块转发
# ------------------------------------------------------------

class _FakeResp:
    status = 200
    headers = {"Content-Type": "audio/mp4", "Content-Range": "bytes 0-99/1000"}

    def __init__(self):
        self._left = 100

    def read(self, n):
        if self._left <= 0:
            return b""
        out = (b"data" * 4)[:n]
        self._left -= len(out)
        return out

    def close(self):
        pass


def test_stream_audio_forwards_with_referer(no_net, monkeypatch):
    """本地代理带 B 站 Referer 转发音频流; Range 透传; 返回媒体类型与迭代器。"""
    import urllib.request

    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["referer"] = req.get_header("Referer")
        sent["range"] = req.get_header("Range")
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(_bili, "get_audio_url", lambda bvid: "https://upos/m.m4s")
    _bili._STREAM_TOKENS.clear()

    bvid = "BV1STREAM"
    tok = _bili.make_stream_token(bvid)
    status, ctype, crange, chunks = _bili.stream_audio(tok, "bytes=0-1023")

    assert status == 200 and ctype == "audio/mp4" and crange == "bytes 0-99/1000"
    assert sent["referer"] == "https://www.bilibili.com/"
    assert sent["range"] == "bytes=0-1023"
    first = b"".join(c for c in chunks)
    assert first.count(b"data") > 0         # 真的逐块转发


def test_stream_audio_bad_token(no_net):
    _bili._STREAM_TOKENS.clear()
    with pytest.raises(_bili.BiliError):
        status, ctype, crange, chunks = _bili.stream_audio("nope")


def test_stream_audio_no_audio_url(no_net, monkeypatch):
    """直链拉不到（无音频流）→ BiliError, 不产生转发。"""
    monkeypatch.setattr(_bili, "get_audio_url", lambda bvid: None)
    tok = _bili.make_stream_token("BV1NOAUDIO")
    with pytest.raises(_bili.BiliError):
        status, ctype, crange, chunks = _bili.stream_audio(tok)


# ------------------------------------------------------------
# server 路由（/api/bili/*）
# ------------------------------------------------------------

def test_api_bili_search(monkeypatch):
    monkeypatch.setattr(_bili, "search_videos",
                        lambda kw, limit: {"kw": kw, "videos": [
                            {"id": "BV1FHeE66Ew5", "name": "科幻", "source": "bili"}]})
    r = client.get("/api/bili/search", params={"kw": "科幻", "limit": 30})
    assert r.status_code == 200
    assert r.json()["videos"][0]["id"] == "BV1FHeE66Ew5"


def test_api_bili_search_upstream_fail_502(monkeypatch):
    def boom(kw, limit=30):
        raise _bili.BiliError("B 站请求失败: timeout")

    monkeypatch.setattr(_bili, "search_videos", boom)
    r = client.get("/api/bili/search", params={"kw": "x"})
    assert r.status_code == 502
    assert "timeout" in r.json()["detail"]


def test_api_bili_url_gives_token(monkeypatch):
    """url 路由返回 token + 可播标志; 无音频时 url 为 None 仍给 token（前端跳过）。"""
    monkeypatch.setattr(_bili, "get_audio_url", lambda bvid: "https://upos/m.m4s")
    r = client.get("/api/bili/url", params={"bvid": "BV1X"})
    assert r.status_code == 200
    j = r.json()
    assert j["id"] == "BV1X" and j["url"].startswith("https://")
    assert j["token"]

    monkeypatch.setattr(_bili, "get_audio_url", lambda bvid: None)
    r2 = client.get("/api/bili/url", params={"bvid": "BV1NONE"})
    assert r2.json()["url"] is None and r2.json()["token"]


def test_api_bili_stream_proxy(monkeypatch):
    """stream 路由: 收到媒体流响应, 透传 Content-Range。"""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _FakeResp())
    monkeypatch.setattr(_bili, "get_audio_url", lambda bvid: "https://upos/m.m4s")
    tok = _bili.make_stream_token("BV1X")

    r = client.get("/api/bili/stream", params={"token": tok},
                   headers={"Range": "bytes=0-1023"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mp4"
    assert r.headers["content-range"] == "bytes 0-99/1000"
    assert b"data" in r.content


def test_api_bili_stream_bad_token_502(monkeypatch):
    r = client.get("/api/bili/stream", params={"token": "expired"})
    assert r.status_code == 502