# --- B 站视频抓取层（摸鱼电台 B站来源）---
# 与 music.py 同策略: 只用标准库 urllib, 不引第三方依赖。
# 上游三件套（2026 实测）:
#   1. 搜索: https://search.bilibili.com/all?keyword= 是 SSR, 结果卡片直接
#      在 HTML 里, 解析卡片即可, 不需要过 JS; api 搜索接口会被风控 412。
#   2. 视频页: 需要 Accept-Encoding: gzip（B 站对 urllib 默认给 gzip 流,
#      不解压会拿到乱码挡板）; 页面内嵌 window.__INITIAL_STATE__, 里面有 cid。
#   3. 播放直链: /x/player/playurl 公开接口, 带 bvid+cid 直接返回 DASH 流
#      （无需 wbi 签名, 实测 200）。音频直链带防盗链: 无 Referer 403,
#      带 B 站 Referer 200 → 由后端流式转发给前端, 前端不直连 CDN。
#
# 音频策略: 取 DASH 里的音频流（纯音轨）, 配合前端迷你播放条「听视频」;
# 没有 DASH 时退回 durl 的 mp4 直链, 前端同样当音频播（画面不进电台）。

import gzip
import html
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

BILI_BASE = "https://www.bilibili.com"
_SEARCH_URL = "https://search.bilibili.com/all"
_PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com/",
    "Accept-Encoding": "gzip",
}

# bvid → 直链（带时效, 缓存 30 分钟, key 是 bvid 而不是 url —— 换一次号就失效）
_LINK_CACHE: dict[str, tuple[str, float]] = {}
_LINK_TTL = 30 * 60
# 转发 token → bvid（token 给前端换来本地代理地址, 2 小时有效）
_STREAM_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_TTL = 2 * 60 * 60

_BVID_RE = re.compile(r"BV1[0-9A-Za-z]{9}")


class BiliError(Exception):
    """上游抓取失败/响应不可解析。server 层转 502。"""


def _get(url: str, referer: str = "https://www.bilibili.com/") -> bytes:
    """GET 一次上游页面/接口, 自动解 gzip。非 200 抛 BiliError。"""
    headers = dict(_HEADERS)
    headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise BiliError(f"B 站接口 HTTP {resp.status}")
            raw = resp.read()
    except BiliError:
        raise
    except Exception as e:
        raise BiliError(f"B 站请求失败: {e}") from e
    if raw[:2] == b"\x1f\x8b":          # B 站给 urllib 的 gzip 流
        try:
            raw = gzip.decompress(raw)
        except OSError as e:
            raise BiliError(f"gzip 解压失败: {e}") from e
    return raw


def _get_text(url: str) -> str:
    return _get(url).decode("utf-8", "replace")


def _http_json(url: str) -> dict:
    raw = _get(url)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        raise BiliError("B 站接口返回的不是 JSON") from e
    if not isinstance(data, dict):
        raise BiliError("B 站接口响应结构异常")
    if data.get("code") not in (0, None):
        raise BiliError(f"B 站接口返回错误: code={data.get('code')} "
                        f"msg={data.get('message')}")
    return data


def _https(url: Any) -> str:
    """封面等图片地址归一为 https（页面是 https/localhost 混合内容敏感场景）。"""
    s = str(url or "")
    if s.startswith("//"):
        return "https:" + s
    return ("https://" + s[len("http://"):]) if s.startswith("http://") else s


def _dur_secs(text: Optional[str]) -> float:
    """'03:20' / '1:02:33' → 秒。解析不了给 0。"""
    if not text:
        return 0
    parts = [p for p in text.split(":") if p.isdigit()]
    if not parts:
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + int(p)
    return float(secs)


# 搜索卡片是 SSR, 2026-09 实测结构:
#   卡片起点 = <a href="//www.bilibili.com/video/BVxxx/" class="" target="_blank">
#   （每张卡里这个链接出现两次: 封面一次、标题一次; 只认封面那个 class="" 的,
#     否则同卡会被拆成两半。）
#   封面: <source srcset="//i0.hdslb.com/bfs/archive/<hash>.jpg@672w...">
#   时长: <span class="bili-video-card__stats__duration">03:20</span>
#   标题: <h3 class="bili-video-card__info--tit" title="..." data-v-...>
#   卡内没有 UP 主字段（只有日期）, artist 用占位。
_CARD_LINK_RE = re.compile(
    r'<a href="//www\.bilibili\.com/video/(BV1[0-9A-Za-z]{9})/" class="" '
    r'target="_blank"')
_TITLE_RE = re.compile(r'class="bili-video-card__info--tit" title="([^"]*)"')
_COVER_RE = re.compile(r'<source srcset="(//i0\.hdslb\.com/[^"]+)"')
_DUR_RE = re.compile(r'class="bili-video-card__stats__duration"[^>]*>([\d:]+)')


def search_videos(kw: str, limit: int = 30) -> dict:
    """关键词搜 B 站视频（解析 SSR 卡片; 不碰 JS, 不碰会被 412 的搜索 API）。"""
    kw = (kw or "").strip()
    if not kw:
        return {"kw": "", "videos": []}
    limit = max(1, min(int(limit), 50))
    url = f"{_SEARCH_URL}?keyword={urllib.parse.quote(kw)}"
    body = _get_text(url)

    # 每个视频链接的位置即一张卡片起点; 该链接到下一张卡片起点的区间内取字段
    anchors = list(_CARD_LINK_RE.finditer(body))
    videos: list[dict] = []
    for i, m in enumerate(anchors[:limit]):
        bvid = m.group(1)
        nxt = anchors[i + 1].start() if i + 1 < len(anchors) else len(body)
        seg = body[m.start():nxt]

        tm = _TITLE_RE.search(seg)
        cm = _COVER_RE.search(seg)
        dm = _DUR_RE.search(seg)
        # 卡片里没有 UP 主字段（只有日期）, 用占位, 播放时也够看
        videos.append({
            "source": "bili",
            "id": bvid,
            "name": html.unescape(tm.group(1)).strip() if tm else "",
            "artist": "B站视频",
            "album": "",
            "duration": _dur_secs(dm.group(1) if dm else ""),
            "fee": 0,
            "pic": _https(cm.group(1) if cm else ""),
        })
    return {"kw": kw, "videos": videos}


def _video_cid(bvid: str) -> int:
    """视频页 __INITIAL_STATE__ 里取 cid（播放列表第一个分 P 的 cid 即主视频）。"""
    body = _get_text(f"{BILI_BASE}/video/{bvid}")
    m = re.search(r'"cid":(\d+)', body)
    if not m:
        raise BiliError(f"视频页拿不到 cid: {bvid}")
    return int(m.group(1))


def _pick_audio(dash: dict) -> Optional[dict]:
    """DASH 音频流里挑带宽最高的（多音轨时默认最优; 单个就是它）。"""
    audios = dash.get("audio") or []
    if not audios:
        return None
    return max(audios, key=lambda a: a.get("bandwidth") or 0)


def _playurl(bvid: str) -> Optional[str]:
    """playurl 接口拿音频直链（无登录无签名, 实测 200）。
    DASH 有 audio 用 audio; 只有 durl（低清 mp4, 音画同轨）退回它的地址。"""
    cid = _video_cid(bvid)
    url = (_PLAYURL_API + "?bvid=" + urllib.parse.quote(bvid)
           + f"&cid={cid}&fnval=16&fourk=1")
    data = _http_json(url).get("data") or {}
    dash = data.get("dash") or {}
    audio = _pick_audio(dash)
    if audio and audio.get("baseUrl"):
        return audio["baseUrl"]
    durls = data.get("durl") or []
    if durls and durls[0].get("url"):
        return durls[0]["url"]
    return None      # 无音频流（纯画面/合作方限制）→ 前端跳过


def get_audio_url(bvid: str) -> Optional[str]:
    """带缓存的直链获取。直链本身带时效签名（约 2-3 小时）, 缓存 30 分钟。"""
    cached = _LINK_CACHE.get(bvid)
    if cached and cached[1] > time.time():
        return cached[0]
    url = _playurl(bvid)
    if url:
        _LINK_CACHE[bvid] = (url, time.time() + _LINK_TTL)
    else:
        _LINK_CACHE.pop(bvid, None)
    return url


def make_stream_token(bvid: str) -> str:
    """给前端一个随机 token, 换本地代理地址（细粒度到单次播放）。"""
    token = secrets.token_urlsafe(16)
    _STREAM_TOKENS[token] = (bvid, time.time() + _TOKEN_TTL)
    return token


def stream_audio(token: str, range_header: Optional[str] = None):
    """音频流代理: 带 B 站 Referer 拉直链, 逐块 yield 给前端。
    直链防盗链只认 B 站 Referer, 浏览器从 localhost 直连必 403, 由后端消化。
    range_header 透传（浏览器 <audio> 会发 Range: bytes=0-, seek 也要 206）,
    返回 (status, content_type, content_range, 迭代器); 异常抛 BiliError。"""
    item = _STREAM_TOKENS.get(token)
    if not item or item[1] < time.time():
        raise BiliError("转发凭据无效或已过期, 重试一下")
    bvid = item[0]
    url = get_audio_url(bvid)
    if not url:
        raise BiliError("该视频没有可播音频")

    headers = dict(_HEADERS)
    headers["Referer"] = "https://www.bilibili.com/"
    if range_header:
        headers["Range"] = range_header
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        raise BiliError(f"音频流拉取失败: {e}") from e
    status = resp.status if hasattr(resp, "status") else 200
    ctype = (resp.headers.get("Content-Type") or "application/octet-stream")
    ctype = ctype.split(";")[0].strip() or "application/octet-stream"
    crange = resp.headers.get("Content-Range")

    def chunks():
        try:
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                yield block
        finally:
            resp.close()

    return status, ctype, crange, chunks()