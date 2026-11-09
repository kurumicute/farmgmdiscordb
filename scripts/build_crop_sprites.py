"""由根目錄 6farm.png 產生 6 種作物的農田圖（64×64，透明底）與 manifest 項目；換素材時重跑一次。

素材圖由左到右、由上到下：琥珀瓜、月光花、番茄、南瓜、葡萄、西瓜，每種兩張（成株、葉株）。
階段對應：sprout／young／unripe = 葉株由小到大；ready = 成株；withered = 成株轉枯黃；seeds 沿用共用的土堆圖。
番茄另外由成株把紅色轉紫黑，產生珍稀變種（黑鑽番茄）的 variant_ready。

用法：.\\.venv\\Scripts\\python.exe scripts/build_crop_sprites.py
"""

import colorsys
import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SHEET = ROOT / "6farm.png"
FARM = ROOT / "app" / "discord_bot" / "farm"
CROPS = ("amber_melon", "moonflower", "tomato", "pumpkin", "grape", "watermelon")
SIZE = 64
BOTTOM = 61  # 植物底部的 y（和既有素材相同）
MAX_WIDTH = 60
ALPHA_MIN = 40  # 低於這個透明度的邊緣不算進範圍
ROWS = 2
PER_ROW = 6
READY_HEIGHT = 60
LEAF_HEIGHTS = {"sprout": 26, "young": 40, "unripe": 54}


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """連續為 True 的區段 [開始, 結束)。"""
    out, start = [], None
    for i, flag in enumerate(flags + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            out.append((start, i))
            start = None
    return out


def split_sheet(sheet: Image.Image) -> list[tuple[Image.Image, Image.Image]]:
    """依透明的間隔切開，回傳每種作物的 (成株, 葉株)；格數不對就報錯，不猜。"""
    alpha = sheet.getchannel("A").point(lambda a: 255 if a >= ALPHA_MIN else 0)
    w, h = sheet.size
    rows = [r for r in _runs([alpha.crop((0, y, w, y + 1)).getbbox() is not None for y in range(h)])
            if r[1] - r[0] > 50]
    if len(rows) != ROWS:
        raise SystemExit(f"預期 {ROWS} 列，實際 {rows}")
    sprites = []
    for top, bottom in rows:
        band = alpha.crop((0, top, w, bottom))
        cols = [c for c in _runs([band.crop((x, 0, x + 1, bottom - top)).getbbox() is not None for x in range(w)])
                if c[1] - c[0] > 50]
        if len(cols) != PER_ROW:
            raise SystemExit(f"第 {top} 列預期 {PER_ROW} 張，實際 {cols}")
        for left, right in cols:
            box = alpha.crop((left, top, right, bottom)).getbbox()
            sprites.append(sheet.crop((left + box[0], top + box[1], left + box[2], top + box[3])))
    return [(sprites[i], sprites[i + 1]) for i in range(0, len(sprites), 2)]


def place(sprite: Image.Image, height: int) -> Image.Image:
    """等比例縮小後放進 64×64，水平置中、底部對齊 BOTTOM。"""
    scale = min(height / sprite.height, MAX_WIDTH / sprite.width)
    w, h = max(1, round(sprite.width * scale)), max(1, round(sprite.height * scale))
    canvas = Image.new("RGBA", (SIZE, SIZE))
    canvas.alpha_composite(sprite.resize((w, h), Image.LANCZOS), ((SIZE - w) // 2, BOTTOM - h))
    return canvas


def _recolor(image: Image.Image, fn) -> Image.Image:
    out = image.copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, a = px[x, y]
            if a:
                h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                nr, ng, nb = colorsys.hsv_to_rgb(*fn(h, s, v))
                px[x, y] = (round(nr * 255), round(ng * 255), round(nb * 255), a)
    return out


def _wither(h: float, s: float, v: float) -> tuple[float, float, float]:
    if 0.14 <= h <= 0.45:  # 葉子與莖：轉成枯黃褐色
        return 0.1, s * 0.6, min(1.0, v * 0.95)  # 不能太暗，農田的土也是褐色
    return h, s * 0.5, v * 0.8  # 果實與花：褪色


def _black_tomato(h: float, s: float, v: float) -> tuple[float, float, float]:
    if (h <= 0.06 or h >= 0.95) and s > 0.45:  # 紅色果實轉紫黑
        return 0.76, s * 0.75, v * 0.55
    return h, s, v


def main() -> None:
    sheet = Image.open(SHEET).convert("RGBA")
    manifest_path = FARM / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for code, (ready, leaf) in zip(CROPS, split_sheet(sheet), strict=True):
        folder = FARM / "crops" / code
        folder.mkdir(exist_ok=True)
        ready64 = place(ready, READY_HEIGHT)
        images = {stage: place(leaf, height) for stage, height in LEAF_HEIGHTS.items()}
        images["ready"] = ready64
        images["withered"] = _recolor(ready64, _wither)
        entry = manifest["crops"].get(code, {})
        for stage, image in images.items():
            image.save(folder / f"{stage}.png", optimize=True)
            entry[stage] = f"crops/{code}/{stage}.png"
        if code == "tomato":  # 成株換了，變種也要跟著換
            _recolor(ready64, _black_tomato).save(folder / "variant_ready.png", optimize=True)
        manifest["crops"][code] = entry
        for presets in manifest["field"]["presets"].values():  # 舊的全田預先產生圖不再適用，改為即時拼圖
            for rel in (presets.pop(code, None) or {}).values():
                if code in Path(rel).name:  # seeds／sprout／young 是各作物共用的檔案，不能刪
                    (FARM / rel).unlink(missing_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("完成：", "、".join(CROPS))


if __name__ == "__main__":
    main()
