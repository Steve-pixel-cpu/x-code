# -*- coding: utf-8 -*-
"""上游探针:模拟 bilibili.py 的真实抓取路径,验证可行性。

用项目同款手段(urllib + UA/Referer, 标准库无依赖)探三件事:
1. 搜索页 search.bilibili.com HTML 是否可达(不 412)
2. 视频页 www.bilibili.com/video/BVxxx 是否可达, 是否内嵌 __playinfo__
3. __playinfo__ 里的 DASH 音频流直链(带时效签名)

纯探测, 只打印关键信息, 不写任何文件。跑:
    .venv/Scripts/python.exe scripts/probe_bilibili.py
"""
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")


def get(url: str, referer: str = "") -> tuple[int, str]:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:120]
    except Exception as e:
        return 0, f"EXC: {e}"


def main() -> None:
    kw = urllib.parse.quote("周杰伦")
    print("== 1) 搜索页 ==")
    st, body = get("https://search.bilibili.com/all?keyword=" + kw,
                   referer="https://www.bilibili.com/")
    print(f"status={st} bytes={len(body)}")
    has_init = "window.__INITIAL_STATE__" in body
    has_bvid = bool(re.search(r'"bvid":"BV[0-9A-Za-z]+"', body))
    print(f"__INITIAL_STATE__ 内嵌: {has_init} | 内嵌 bvid: {has_bvid}")
    ids = re.findall(r'"bvid":"(BV[0-9A-Za-z]+)"', body)
    print(f"首批 bvid: {ids[:5]}")

    print("\n== 2) 视频页 ==")
    bvid = ids[0] if ids else "BV1xx411c7mD"
    st, body = get(f"https://www.bilibili.com/video/{bvid}",
                   referer="https://www.bilibili.com/")
    print(f"bvid={bvid} status={st} bytes={len(body)}")
    m = re.search(r"window\.__playinfo__\s*=\s*(\{.*?\})</script>", body, re.S)
    if not m:
        print("__playinfo__: 未找到")
        return
    try:
        play = json.loads(m.group(1))
    except Exception as e:
        print(f"__playinfo__ JSON 解析失败: {e}")
        return
    print("__playinfo__ 找到, 顶层键:", list(play.keys()))
    data = play.get("data") or {}
    dash = data.get("dash") or {}
    audios = dash.get("audio") or []
    print(f"dash.audio 条数: {len(audios)}")
    if audios:
        a = audios[0]
        print("首个音频流:", {k: a.get(k) for k in ("id", "bandwidth", "codecs") if k in a},
              "baseUrl 前 100 字符:", (a.get("baseUrl") or "")[:100])
        print("baseUrl 是否 https:", (a.get("baseUrl") or "").startswith("https://"))


if __name__ == "__main__":
    main()