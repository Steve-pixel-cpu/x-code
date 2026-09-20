# --- 网易云音乐公开接口封装（摸鱼电台后端）---
# 全部走 music.163.com 的公开 Web 接口, 无需登录; 只带 Referer/UA 两个头即可。
# 仅在线流式播放免费曲库: VIP/无版权歌曲拿播放直链会失败, 由前端标记跳过。
#
# 依赖策略: 项目 venv 没有 httpx/requests, 这里用标准库 urllib.request,
# 不为听歌引入新依赖。榜单结果做小缓存（榜单几分钟才更新一次）,
# 播放直链/歌词不缓存（直链带时效签名, 复用会 403）。

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

MUSIC_BASE = "https://music.163.com"
_HEADERS = {
    "Referer": "https://music.163.com",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}

# 内置榜单: 热歌榜 / 新歌榜（摸鱼电台的默认曲库入口）
HOT_PLAYLIST_ID = 3778678
NEW_PLAYLIST_ID = 3779629
BUILTIN_PLAYLISTS = {
    "hot": HOT_PLAYLIST_ID,
    "new": NEW_PLAYLIST_ID,
}

# 榜单缓存: id → (落盘时间, 响应)。TTL 内重复请求不再出网。
_CACHE_TTL = 600
_playlist_cache: dict[int, tuple[float, dict]] = {}


class MusicApiError(Exception):
    """上游请求失败/响应不可解析。server 层转 502。"""


def _http_json(path: str, params: dict[str, Any], timeout: float = 8.0) -> dict:
    """GET 一次公开接口并解析 JSON。非 200 / 解析失败抛 MusicApiError。"""
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{MUSIC_BASE}{path}?{qs}", headers=_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise MusicApiError(f"网易接口 HTTP {resp.status}")
            raw = resp.read()
    except MusicApiError:
        raise
    except Exception as e:
        raise MusicApiError(f"网易接口请求失败: {e}") from e
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise MusicApiError("网易接口返回的不是 JSON") from e
    if not isinstance(data, dict):
        raise MusicApiError("网易接口响应结构异常")
    return data


def _https(url: Any) -> str:
    """封面等图片地址归一为 https（页面是 https/localhost 混合内容敏感场景）。"""
    s = str(url or "")
    return ("https://" + s[len("http://"):]) if s.startswith("http://") else s


def _song_brief(s: dict) -> dict:
    """把榜单/搜索结果里的歌曲压成前端要的几个字段。fee: 0/8=免费, 1=VIP。"""
    artists = s.get("artists") or s.get("ar") or []
    album = s.get("album") or s.get("al") or {}
    return {
        "id": s.get("id"),
        "name": (s.get("name") or "").strip(),
        "artist": " / ".join(a.get("name") or "" for a in artists[:3]) or "未知歌手",
        "album": (album.get("name") or "").strip(),
        # 毫秒 → 秒
        "duration": round((s.get("duration") or s.get("dt") or 0) / 1000, 1),
        "fee": s.get("fee") or 0,
        "pic": _https(album.get("picUrl") or ""),
    }


def playlist_songs(pid: int) -> dict:
    """歌单/榜单详情。内置榜单走 10 分钟缓存, 自定义歌单直连。"""
    pid = int(pid)
    cached = _playlist_cache.get(pid)
    now = time.time()
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    data = _http_json("/api/playlist/detail", {"id": pid})
    result = data.get("result") or {}
    if not result and data.get("code") not in (200, None):
        raise MusicApiError(f"歌单不存在或不可读（code={data.get('code')}）")
    songs = [_song_brief(s) for s in (result.get("tracks") or []) if s.get("id")]
    out = {"id": pid, "name": (result.get("name") or "歌单").strip(), "songs": songs}
    _playlist_cache[pid] = (now, out)
    return out


def search_songs(kw: str, limit: int = 30) -> dict:
    """关键词搜歌（type=1 单曲）。"""
    kw = (kw or "").strip()
    if not kw:
        return {"kw": "", "songs": []}
    limit = max(1, min(int(limit), 50))
    data = _http_json("/api/search/get/web",
                      {"s": kw, "type": 1, "limit": limit, "offset": 0})
    result = data.get("result") or {}
    songs = [_song_brief(s) for s in (result.get("songs") or []) if s.get("id")]
    return {"kw": kw, "songs": songs}


def song_url(song_id: int, br: int = 128000) -> dict:
    """播放直链。VIP/无版权歌 url 为 None（上游 code -110/404）,
    由前端按「跳过」处理。直链本身带时效签名, 不缓存。"""
    song_id = int(song_id)
    br = int(br) if br in (128000, 192000, 320000) else 128000
    data = _http_json("/api/song/enhance/player/url",
                      {"ids": f"[{song_id}]", "br": br})
    items = data.get("data") or []
    first = items[0] if items else {}
    url = first.get("url") or None
    if url and url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return {
        "id": song_id,
        "url": url,
        "br": first.get("br") or 0,
        "size": first.get("size") or 0,
        "type": first.get("type") or "mp3",
    }


def song_lyric(song_id: int) -> dict:
    """LRC 歌词原样透传; 没有歌词时 lyc 为空串（前端显示占位）。"""
    song_id = int(song_id)
    data = _http_json("/api/song/lyric", {"id": song_id, "lv": 1, "kv": 1, "tv": -1})
    return {"id": song_id, "lrc": (data.get("lrc") or {}).get("lyric") or ""}
