# --- 网易云音乐公开接口封装（摸鱼电台后端）---
# 在线部分全部走 music.163.com 的公开 Web 接口, 无需登录; 只带 Referer/UA 两个头。
# 仅在线流式播放免费曲库: VIP/无版权歌曲拿播放直链会失败, 由前端标记跳过。
# 在线只提供 搜索/播放直链/歌词 三个只读代理;
# 收藏与自定义歌单是本机数据（LIB_FILE）, 由本模块负责持久化。
#
# 依赖策略: 项目 venv 没有 httpx/requests, 这里用标准库 urllib.request,
# 不为听歌引入新依赖。播放直链/歌词不缓存（直链带时效签名, 复用会 403）。

import json
import urllib.parse
import urllib.request
from typing import Any, Optional

import config
from fsatomic import atomic_write_text

MUSIC_BASE = "https://music.163.com"
_HEADERS = {
    "Referer": "https://music.163.com",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
}

# ============================================================
# 本地曲库: 收藏 + 自定义播放列表（~/.x-code/music-library.json）
# ============================================================
# 形状: {"favorites": [歌曲简报...],
#        "playlists": [{"id", "name", "songs": [歌曲简报...]}]}
# 歌曲简报即 _song_brief 的字段（id/name/artist/album/duration/fee/pic）,
# 客户端传回什么原样存, 换歌时不需要再问一次上游。
# 单用户桌面应用, 与 settings.json 的读写约定一致: 读-改-写, 不加锁。

LIB_FILE = config.USER_DIR / "music-library.json"

FAVORITES_MAX = 500
PLAYLISTS_MAX = 50
PLAYLIST_NAME_MAX = 40
PLAYLIST_SONGS_MAX = 500

_SONG_FIELDS = ("id", "name", "artist", "album", "duration", "fee", "pic")


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


def _norm_song(raw: Any) -> Optional[dict]:
    """把客户端传来的歌曲压成白名单字段。必须能对出 int id + 非空 name,
    否则返回 None（拒收）——防止任意 JSON 原样落盘。"""
    if not isinstance(raw, dict):
        return None
    try:
        sid = int(raw.get("id"))
    except (TypeError, ValueError):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    out = {k: raw.get(k) for k in _SONG_FIELDS}
    out["id"] = sid
    out["name"] = name
    return out


def _norm_songs(raw_songs: Any) -> list[dict]:
    """批量清洗, 丢掉不合格条目; 且不是 list 直接当空列表。"""
    if not isinstance(raw_songs, list):
        return []
    songs, seen = [], set()
    for raw in raw_songs:
        s = _norm_song(raw)
        if s and s["id"] not in seen:
            seen.add(s["id"])
            songs.append(s)
    return songs


def _empty_library() -> dict:
    return {"favorites": [], "playlists": []}


def _load_library() -> dict:
    """读本地曲库; 缺文件/损坏/形状不对一律当空库, 不让听歌功能因坏文件崩掉。"""
    try:
        data = json.loads(LIB_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_library()
    if not isinstance(data, dict):
        return _empty_library()
    lib = _empty_library()
    favs = _norm_songs(data.get("favorites"))
    lib["favorites"] = favs[:FAVORITES_MAX]
    pls = data.get("playlists")
    if isinstance(pls, list):
        for p in pls:
            if not isinstance(p, dict):
                continue
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            lib["playlists"].append({
                "id": int(p.get("id") or 0),
                "name": name[:PLAYLIST_NAME_MAX],
                "songs": _norm_songs(p.get("songs"))[:PLAYLIST_SONGS_MAX],
            })
        lib["playlists"] = lib["playlists"][:PLAYLISTS_MAX]
    return lib


def _save_library(lib: dict) -> None:
    LIB_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(LIB_FILE, json.dumps(lib, ensure_ascii=False, indent=2))


def list_library() -> dict:
    """整个曲库原样给前端（前端据此渲染收藏/歌单并维护 ❤ 实心态）。"""
    return _load_library()


def add_favorites(songs: Any) -> dict:
    """收藏歌曲: 按 id 去重（已在收藏里的跳过）, 超上限裁掉多余的并提示数量。"""
    lib = _load_library()
    existing = {s["id"] for s in lib["favorites"]}
    added = 0
    for s in _norm_songs(songs):
        if s["id"] in existing:
            continue
        if len(lib["favorites"]) >= FAVORITES_MAX:
            break
        lib["favorites"].append(s)
        existing.add(s["id"])
        added += 1
    if added:
        _save_library(lib)
    return {"added": added, "favorites": lib["favorites"]}


def remove_favorite(song_id: int) -> dict:
    song_id = int(song_id)
    lib = _load_library()
    before = len(lib["favorites"])
    lib["favorites"] = [s for s in lib["favorites"] if s["id"] != song_id]
    if len(lib["favorites"]) == before:
        raise KeyError(song_id)
    _save_library(lib)
    return {"removed": 1, "favorites": lib["favorites"]}


def _find_playlist(lib: dict, pid: int) -> dict:
    for p in lib["playlists"]:
        if p["id"] == pid:
            return p
    raise KeyError(pid)


def _ensure_unique_name(lib: dict, name: str, exclude_pid: Optional[int] = None) -> None:
    """歌单名唯一: 与现有歌单重名一律 ValueError（新建/重命名共用）。"""
    for p in lib["playlists"]:
        if p["id"] != exclude_pid and p["name"] == name:
            raise ValueError(f"已存在同名歌单「{name}」")


def create_playlist(name: str, songs: Any = None) -> dict:
    """新建歌单。名称非空且不与现有歌单重名; 歌单数到上限抛 ValueError。"""
    name = str(name or "").strip()[:PLAYLIST_NAME_MAX]
    if not name:
        raise ValueError("歌单名不能为空")
    lib = _load_library()
    _ensure_unique_name(lib, name)
    if len(lib["playlists"]) >= PLAYLISTS_MAX:
        raise ValueError(f"歌单数量已达上限（{PLAYLISTS_MAX} 个）")
    pid = max((p["id"] for p in lib["playlists"]), default=0) + 1
    pl = {"id": pid, "name": name, "songs": _norm_songs(songs)[:PLAYLIST_SONGS_MAX]}
    lib["playlists"].append(pl)
    _save_library(lib)
    return pl


def rename_playlist(pid: int, name: str) -> dict:
    """重命名。非空、不与其他歌单重名; 改回自己的名字放行。"""
    name = str(name or "").strip()[:PLAYLIST_NAME_MAX]
    if not name:
        raise ValueError("歌单名不能为空")
    pid = int(pid)
    lib = _load_library()
    pl = _find_playlist(lib, pid)
    _ensure_unique_name(lib, name, exclude_pid=pid)
    pl["name"] = name
    _save_library(lib)
    return {"id": pid, "name": name}


def delete_playlist(pid: int) -> dict:
    pid = int(pid)
    lib = _load_library()
    _find_playlist(lib, pid)
    lib["playlists"] = [p for p in lib["playlists"] if p["id"] != pid]
    _save_library(lib)
    return {"removed": pid, "playlists": lib["playlists"]}


def add_to_playlist(pid: int, songs: Any) -> dict:
    """往歌单追加歌曲: 按 id 去重（歌单里已有的跳过）, 超上限截断。"""
    lib = _load_library()
    pl = _find_playlist(lib, int(pid))
    existing = {s["id"] for s in pl["songs"]}
    added = 0
    for s in _norm_songs(songs):
        if s["id"] in existing:
            continue
        if len(pl["songs"]) >= PLAYLIST_SONGS_MAX:
            break
        pl["songs"].append(s)
        existing.add(s["id"])
        added += 1
    if added:
        _save_library(lib)
    return {"id": pl["id"], "name": pl["name"], "songs": pl["songs"], "added": added}


def remove_from_playlist(pid: int, song_ids: Any) -> dict:
    """从歌单移除歌曲（可一次多个 id）。歌单不存在抛 KeyError。"""
    ids = {int(i) for i in song_ids} if isinstance(song_ids, (list, tuple)) else {int(song_ids)}
    lib = _load_library()
    pl = _find_playlist(lib, int(pid))
    pl["songs"] = [s for s in pl["songs"] if s["id"] not in ids]
    _save_library(lib)
    return {"id": pl["id"], "name": pl["name"], "songs": pl["songs"]}


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
