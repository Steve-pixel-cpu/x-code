# -*- coding: utf-8 -*-
"""生成 x-code 内置桌宠(严格遵循 Codex 宠物格式)。

用法(仅开发期, 运行时不依赖 Pillow):
    .venv/Scripts/python.exe scripts/gen_default_pet.py

产物(提交入库, 后端只读):
    pets/xcode-cat/spritesheet.png   1536x1872, 8 列 x 9 行, 单格 192x208, 全透明底
    pets/xcode-cat/pet.json          标准 manifest

Codex 官方逐帧行为(渲染器在 static/pet.js 里有同一张表):
    行 0 idle            6 帧  280,110,110,140,140,320
    行 1 running-right   8 帧  120x7, 220
    行 2 running-left    8 帧  120x7, 220 (本图由行 1 镜像)
    行 3 waving          4 帧  140x3, 280
    行 4 jumping         5 帧  140x4, 280
    行 5 failed          8 帧  140x7, 240
    行 6 waiting         6 帧  150x5, 260
    行 7 running         6 帧  120x5, 220
    行 8 review          6 帧  150x5, 280
"""

import json
from pathlib import Path

from PIL import Image, ImageDraw

CELL_W, CELL_H = 192, 208
COLS, ROWS = 8, 9

# --- 配色: 跟随应用默认 accent 的紫色小猫 ---
BODY = (139, 128, 249, 255)        # #8b80f9
BODY_DARK = (104, 92, 220, 255)    # 描边/深部
BELLY = (172, 166, 253, 255)       # 肚皮浅一档
EAR_IN = (242, 184, 198, 255)      # 耳内粉
DARK = (43, 43, 51, 255)           # 眼/嘴
BLUSH = (242, 163, 179, 110)       # 腮红(半透明)
TEAR = (140, 176, 255, 230)        # 泪珠
GLASS = (210, 226, 255, 90)        # 放大镜镜片
DUST = (170, 170, 180, 90)         # 跑动扬尘

STATE_FRAMES = {  # 与 Codex 契约一致: 每行用到的列数
    "idle": 6, "running-right": 8, "running-left": 8, "waving": 4,
    "jumping": 5, "failed": 8, "waiting": 6, "running": 6, "review": 6,
}
STATE_ORDER = ["idle", "running-right", "running-left", "waving", "jumping",
               "failed", "waiting", "running", "review"]


def _ellipse(d, box, fill, outline=None, width=2):
    d.ellipse(box, fill=fill, outline=outline, width=width)


def draw_cat(pose):
    """按姿态参数画一只小猫, 返回单帧 RGBA 图(192x208, 透明底)。

    pose 字段(全部可选):
      dy        整体竖直偏移(呼吸/跳起为负)
      lean      整体水平偏移(跑动朝向)
      squash    0..1 落地压扁程度
      stretch   0..1 跳起拉伸程度
      eyes      open|blink|x|down|left|right
      ears      up|droop
      mouth     w|o|flat|wobble
      wave      None | "hi" | "mid"   右臂挥手高度
      watch     bool  左臂抬手看表(waiting)
      magnifier bool  举放大镜(review)
      legs      None | 0..1 相位(跑动四肢交替)
      tail      curl|stream
      tears     int 泪滴下落档位(0=无)
      sweat     bool 汗滴
      dust      bool 脚下扬尘
    """
    img = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    dy = pose.get("dy", 0)
    lean = pose.get("lean", 0)
    sq = pose.get("squash", 0)
    st = pose.get("stretch", 0)
    x0, x1 = 26 + lean, 166 + lean   # 猫体横向范围

    # --- 尾巴(压在身体后面画) ---
    if pose.get("tail", "curl") == "stream":   # 跑动: 尾巴向后拉直
        d.line([(x1 - 26, 150 + dy), (x1 - 2, 136 + dy), (x1 + 6, 118 + dy)],
               fill=BODY_DARK, width=9, joint="curve")
    else:
        d.arc([x1 - 46, 108 + dy, x1 + 2, 164 + dy], start=-70, end=100,
              fill=BODY_DARK, width=9)

    # --- 四肢(跑动相位交替, 前后腿画在身体下层) ---
    legs = pose.get("legs")
    if legs is not None:
        swing = 12 if legs < 0.5 else -12
        _ellipse(d, (74 + lean + swing, 176 + dy, 98 + lean + swing, 198 + dy), BODY, BODY_DARK)
        _ellipse(d, (98 + lean - swing, 176 + dy, 122 + lean - swing, 198 + dy), BODY, BODY_DARK)
    else:
        tap = pose.get("paw_tap", 0)
        _ellipse(d, (68 + lean, 180 + dy, 92 + lean, 198 + dy), BODY, BODY_DARK)
        _ellipse(d, (104 + lean, 180 + dy - tap, 128 + lean, 198 + dy - tap), BODY, BODY_DARK)

    # --- 身体(压扁/拉伸改变高宽) ---
    bw, bh = 76 * (1 + 0.10 * sq - 0.06 * st), 74 * (1 - 0.16 * sq + 0.12 * st)
    bx = 96 + lean
    body = (bx - bw / 2, 128 + dy + (74 - bh) * 0.4,
            bx + bw / 2, 128 + dy + (74 - bh) * 0.4 + bh)
    d.rounded_rectangle(body, radius=24, fill=BODY, outline=BODY_DARK, width=3)
    _ellipse(d, (bx - 20, 144 + dy, bx + 20, 186 + dy), BELLY)

    # --- 头(含耳朵) ---
    hy = dy + 3 * sq - 4 * st
    if pose.get("ears", "up") == "droop":
        pass   # 耷拉耳在头部之后画(叠在头侧外缘, 先画会被头盖住)
    else:
        for side in (-1, 1):   # 三角耳 + 耳内
            ax = 96 + lean + side * 34
            d.polygon([(ax - 15, 66 + hy), (ax, 18 + hy), (ax + 15, 66 + hy)],
                      fill=BODY, outline=BODY_DARK)
            d.polygon([(ax - 7, 60 + hy), (ax, 30 + hy), (ax + 7, 60 + hy)], fill=EAR_IN)
    head = (46 + lean, 40 + hy, 146 + lean, 138 + hy)
    _ellipse(d, head, BODY, BODY_DARK, width=3)
    if pose.get("ears", "up") == "droop":   # 折耳外翻下垂, 大半探出头的轮廓外
        d.polygon([(60 + lean, 46 + hy), (30 + lean, 80 + hy), (78 + lean, 64 + hy)],
                  fill=BODY, outline=BODY_DARK)
        d.polygon([(132 + lean, 46 + hy), (162 + lean, 80 + hy), (114 + lean, 64 + hy)],
                  fill=BODY, outline=BODY_DARK)

    # --- 眼睛 ---
    eyes = pose.get("eyes", "open")
    for ex in (78 + lean, 114 + lean):
        ey = 86 + hy
        if eyes == "blink":
            d.line([(ex - 6, ey), (ex + 6, ey)], fill=DARK, width=3)
        elif eyes == "x":
            d.line([(ex - 6, ey - 6), (ex + 6, ey + 6)], fill=DARK, width=3)
            d.line([(ex - 6, ey + 6), (ex + 6, ey - 6)], fill=DARK, width=3)
        else:
            off = {"down": (0, 3), "left": (-3, 1), "right": (3, 1)}.get(eyes, (0, 0))
            _ellipse(d, (ex - 6 + off[0], ey - 8 + off[1], ex + 6 + off[0], ey + 8 + off[1]), DARK)
            d.ellipse((ex - 4 + off[0], ey - 6 + off[1], ex - 1 + off[0], ey - 3 + off[1]),
                      fill=(255, 255, 255, 200))   # 高光

    # --- 腮红 + 嘴 ---
    if pose.get("mouth", "w") != "flat":
        _ellipse(d, (58 + lean, 102 + hy, 72 + lean, 110 + hy), BLUSH)
        _ellipse(d, (120 + lean, 102 + hy, 134 + lean, 110 + hy), BLUSH)
    mx, my = 96 + lean, 108 + hy
    mouth = pose.get("mouth", "w")
    if mouth == "o":
        _ellipse(d, (mx - 4, my - 5, mx + 4, my + 5), DARK)
    elif mouth == "flat":
        d.line([(mx - 6, my), (mx + 6, my)], fill=DARK, width=3)
    elif mouth == "wobble":
        _ellipse(d, (mx - 5, my - 4, mx + 5, my + 6), DARK)
    else:   # "ω": 两个小弧
        d.arc([mx - 10, my - 6, mx, my + 6], start=0, end=180, fill=DARK, width=3)
        d.arc([mx, my - 6, mx + 10, my + 6], start=0, end=180, fill=DARK, width=3)

    # --- 手臂: 挥手(右) / 看表(左) ---
    wave = pose.get("wave")
    if wave:
        wy = {"hi": 92, "mid": 112}[wave]
        d.line([(126 + lean, 140 + dy), (146 + lean, wy + 12)], fill=BODY_DARK, width=8)
        _ellipse(d, (138 + lean, wy, 158 + lean, wy + 20), BODY, BODY_DARK)
    if pose.get("watch"):
        d.line([(66 + lean, 140 + dy), (56 + lean, 116 + dy)], fill=BODY_DARK, width=8)
        _ellipse(d, (46 + lean, 104 + dy, 66 + lean, 124 + dy), BODY, BODY_DARK)
        _ellipse(d, (50 + lean, 106 + dy, 62 + lean, 118 + dy), None, DARK, 2)   # 表盘
        d.line([(56 + lean, 108 + dy), (56 + lean, 112 + dy)], fill=DARK, width=2)

    # --- 道具: 放大镜(review) ---
    if pose.get("magnifier"):
        lx, ly = 124 + lean, 116 + dy + pose.get("lens_dy", 0)
        d.line([(lx + 10, ly + 10), (lx + 34, ly + 42)], fill=(120, 96, 60, 255), width=6)
        _ellipse(d, (lx - 18, ly - 18, lx + 18, ly + 18), GLASS, BODY_DARK, 4)
        d.line([(lx - 8, ly - 2), (lx - 3, ly - 8), (lx + 4, ly + 2)], fill=(255, 255, 255, 200), width=2)

    # --- 情绪点缀: 泪滴 / 汗滴 / 扬尘 ---
    tear = pose.get("tears", 0)
    if tear:
        for ex in (78 + lean, 114 + lean):
            _ellipse(d, (ex - 5, 102 + tear * 4, ex + 5, 112 + tear * 4 + 6), TEAR)
    if pose.get("sweat"):
        _ellipse(d, (146 + lean, 66 + hy, 156 + lean, 80 + hy), TEAR)
    if pose.get("dust"):
        for ox, oy, r in ((x0 - 4, 190, 7), (x0 - 16, 182, 5), (x0 - 26, 192, 4)):
            _ellipse(d, (ox - r, oy - r, ox + r, oy + r), DUST)

    return img


# --- 每个状态的逐帧姿态 ---
def poses_for(state):
    """返回该状态每帧的 pose 字典列表。"""
    n = STATE_FRAMES[state]
    if state == "idle":
        out = []
        for i, dy in enumerate([0, 1, 2, 2, 1, 0]):
            out.append({"dy": dy, "eyes": "blink" if i == 5 else "open"})
        return out
    if state == "running-left":   # 姿态同右跑, build_sheet 里整体镜像
        return poses_for("running-right")
    if state == "running-right":
        return [{"dy": b, "lean": 5, "legs": (i % 2), "tail": "stream",
                 "mouth": "o", "dust": i % 2 == 1}
                for i, b in enumerate([0, 2, 4, 2, 0, 2, 4, 2])]
    if state == "waving":
        return [{"dy": dy, "wave": w}
                for dy, w in [(0, "mid"), (0, "hi"), (1, "hi"), (0, "mid")]]
    if state == "jumping":
        return [{"dy": 4, "squash": 1, "mouth": "o"},
                {"dy": -6, "stretch": 1},
                {"dy": -16, "stretch": 1, "mouth": "o"},
                {"dy": -6, "eyes": "down"},
                {"dy": 2, "squash": 0.5, "dust": True}]
    if state == "failed":
        return [{"dy": min(i, 6), "ears": "droop" if i >= 2 else "up",
                 "eyes": "x" if i >= 1 else "blink", "mouth": "wobble",
                 "tears": max(0, i - 2), "sweat": i in (1, 2)}
                for i in range(8)]
    if state == "waiting":
        return [{"dy": 0, "watch": True, "eyes": "left", "mouth": "flat",
                 "paw_tap": 4 if i % 2 else 0} for i in range(6)]
    if state == "running":
        return [{"dy": b, "legs": (i % 2), "mouth": "o"}
                for i, b in enumerate([0, 2, 4, 4, 2, 0])]
    if state == "review":
        return [{"magnifier": True, "eyes": "down", "lens_dy": ld}
                for ld in (0, 3, 6, 3, 0, 0)]
    raise ValueError(state)


def build_sheet():
    sheet = Image.new("RGBA", (COLS * CELL_W, ROWS * CELL_H), (0, 0, 0, 0))
    for row, state in enumerate(STATE_ORDER):
        poses = poses_for(state)
        for col in range(COLS):
            if col >= len(poses):
                break   # 该行剩余格保持全透明(Codex 契约)
            frame = draw_cat(poses[col])
            if state == "running-left":
                frame = frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            sheet.paste(frame, (col * CELL_W, row * CELL_H))
    return sheet


def main():
    out_dir = Path(__file__).resolve().parent.parent / "pets" / "xcode-cat"
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = build_sheet()
    sheet.save(out_dir / "spritesheet.png", optimize=True)
    (out_dir / "pet.json").write_text(json.dumps({
        "id": "xcode-cat",
        "displayName": "x-code 小紫猫",
        "description": "x-code 出厂的内置桌宠: 一只紫色小猫。",
        "spritesheetPath": "spritesheet.png",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK {out_dir / 'spritesheet.png'} ({sheet.width}x{sheet.height})")
    print(f"OK {out_dir / 'pet.json'}")


if __name__ == "__main__":
    main()
