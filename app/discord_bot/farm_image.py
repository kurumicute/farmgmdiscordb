"""農場圖片：依狀態拼出農田（4～16 格）、建築畫面與商店頁面的 PNG（素材與規則見 farm/拼接說明.md）。

只依賴 Pillow 與素材資料夾；拼圖在 ServiceRunner 的執行緒中呼叫，不在事件迴圈執行。
素材只讀一次並快取；預先產生的圖或底圖命中時沿用（第一次轉成 PANEL_COLORS 色後快取，之後直接回傳）。
"""

import functools
import io
import json
from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parent / "farm"  # 素材資料夾（測試可改指到其他位置）

# 農田與建築畫面上傳時的色數：64 色比 256 色小約 3～5 成（農田 117→59 KB），像素風畫面肉眼幾乎看不出差別。
# 固定場景圖（商店、賭場、山洞、世界 Boss）不受影響，仍直接上傳原檔
PANEL_COLORS = 64

STATE_BADGE = {"seeds": "need_water", "sprout": "timer", "young": "timer", "unripe": "timer", "ready": "ready"}

_manifest: dict | None = None
_cache: dict[str, Image.Image] = {}


def manifest() -> dict:
    global _manifest
    if _manifest is None:
        _manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
    return _manifest


def reset_cache() -> None:
    """清除已讀取的 manifest 與素材（改變 ASSETS 後使用）。"""
    global _manifest
    _manifest = None
    _cache.clear()
    world_boss_png.cache_clear()
    file_png.cache_clear()
    preset_with_pets_png.cache_clear()


def img(rel: str) -> Image.Image:
    if rel not in _cache:
        _cache[rel] = Image.open(ASSETS / rel).convert("RGBA")
    return _cache[rel]


def encode(image: Image.Image) -> bytes:
    buf = io.BytesIO()  # 調色盤 PNG（PANEL_COLORS 色）：比全彩小很多，編碼也快
    image.quantize(PANEL_COLORS, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(buf, "PNG")
    return buf.getvalue()


@functools.lru_cache(maxsize=512)
def file_png(rel: str) -> bytes:
    """預先產生的農田圖／底圖（素材檔為 256 色）轉成 PANEL_COLORS 色後快取；每個檔案只轉一次。"""
    return encode(img(rel))


# ---------- 農田 ----------


def plots_from_status(status) -> list[dict]:
    """把服務層的 FarmStatus 轉成拼圖用的資料，依 plot_number 排序；state 直接用服務層算好的結果。"""
    out = []
    for p in sorted(status.plots, key=lambda p: p.plot_number):
        progress = 0.0
        if p.watered_at and p.ready_at and p.ready_at > p.watered_at:
            progress = (status.now - p.watered_at) / (p.ready_at - p.watered_at)
        out.append({"crop": p.crop_code, "state": str(p.state), "progress": min(1.0, max(0.0, progress)),
                    "quality": p.quality, "mutation": p.mutation})
    return out


def stage_of(plot: dict) -> str:
    """畫面階段：empty／seeds／sprout／young／unripe／ready／withered。"""
    state = plot["state"]
    if state == "waiting_water":
        return "seeds"
    if state == "growing":
        th = manifest()["layering"]["growing_thresholds"]
        p = plot.get("progress", 0)
        return "sprout" if p < th["sprout"] else "young" if p < th["young"] else "unripe"
    return state


def plant_layer(plot: dict, stage: str) -> str | None:
    crops = manifest()["crops"]
    crop = crops.get(plot.get("crop") or "", crops["_generic"])  # 網站新增的作物沒有專屬素材
    if stage == "empty":
        return None
    if stage in ("seeds", "sprout", "young"):
        return crops["_generic"][stage]
    if stage == "ready" and plot.get("mutation") == "variant" and "variant" in crop:
        return crop["variant"]["ready"]
    return crop[stage]


def attr_badges(plot: dict) -> list[str]:
    """品質與突變標記（只有可收成時才畫）。"""
    badges = manifest()["badges"]
    out = []
    if plot.get("quality") in badges["quality"]:
        out.append(badges["quality"][plot["quality"]])
    if plot.get("mutation") in badges["mutation"]:
        out.append(badges["mutation"][plot["mutation"]])
    return out


def draw_attr_badges(canvas: Image.Image, rect: list[int], plot: dict) -> None:
    for i, rel in enumerate(attr_badges(plot)):
        canvas.alpha_composite(img(rel), (rect[0] + 2 + i * 22, rect[1] + 2))


def draw_plot(canvas: Image.Image, rect: list[int], plot: dict) -> None:
    m = manifest()
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    stage = stage_of(plot)
    if plot["state"] in ("growing", "ready"):  # 1 濕土
        canvas.alpha_composite(img(m["field"]["wet_overlay"]).resize((w, h)), (x0, y0))
    layer = plant_layer(plot, stage)  # 2 植物
    if layer:
        sprite = img(layer)
        canvas.alpha_composite(sprite, (x0 + (w - sprite.width) // 2, y1 - sprite.height - 2))
    if stage in STATE_BADGE:  # 3 右上：狀態
        canvas.alpha_composite(img(m["badges"][STATE_BADGE[stage]]), (x1 - 24 - 2, y0 + 2))
    if stage == "ready":  # 4 左上：品質、突變
        draw_attr_badges(canvas, rect, plot)


def preset_for(plots: list[dict]) -> tuple[str, bool] | None:
    """全部同作物、同階段時，回傳 (預先產生的圖, 是否還要補品質／突變標記)。"""
    presets = manifest()["field"]["presets"].get(str(len(plots)))
    if presets is None:
        return None
    stages = {stage_of(p) for p in plots}
    if len(stages) != 1:
        return None
    stage = stages.pop()
    if stage == "empty":
        return presets["empty"], False
    crops = {p.get("crop") for p in plots}
    if len(crops) != 1 or (crop := crops.pop()) not in presets:
        return None
    if stage == "ready" and any(p.get("mutation") == "variant" for p in plots):
        return None  # 變種外觀不同，要即時拼
    return presets[crop][stage], stage == "ready" and any(attr_badges(p) for p in plots)


def farm_png(plots: list[dict], pets: dict | None = None) -> bytes:
    """plots 依 plot_number 排序（plots_from_status 的結果）；pets：寵物代碼 → 等級（有寵物時畫在上方草地）。
    回傳要上傳的 PNG 內容。"""
    field = manifest()["field"]
    plots = plots[:field["slots"]]  # 最多畫 16 格（超過上限的舊農場只畫前 16 格）
    hit = preset_for(plots)
    if hit and not hit[1]:
        # 預先產生的圖：沒有寵物直接用快取；有寵物時依「有哪幾隻」快取（圖上不畫等級）
        return preset_with_pets_png(hit[0], tuple(sorted(pets))) if pets else file_png(hit[0])
    if hit:  # 預先產生的圖（+ 補標記）
        canvas = img(hit[0]).copy()
        if hit[1]:
            for k, plot in enumerate(plots):
                draw_attr_badges(canvas, field["cells"][k]["rect"], plot)
    else:
        n = min(max(len(plots), field["min_plots"]), field["slots"])  # 底圖 4～16
        canvas = img(field["bases"][str(n)]).copy()
        for k, plot in enumerate(plots):  # 第 k 塊土地放第 k 格（plot_number 可能不連續）
            draw_plot(canvas, field["cells"][k]["rect"], plot)
    return encode(with_pets(canvas, pets) if pets else canvas)


@functools.lru_cache(maxsize=512)
def preset_with_pets_png(rel: str, pet_codes: tuple[str, ...]) -> bytes:
    """預先產生的農田圖加上寵物（寵物組合只有幾種，快取後不必每次重新合成）。"""
    return encode(with_pets(img(rel).copy(), dict.fromkeys(pet_codes, 0)))


def with_pets(field_canvas: Image.Image, pets: dict) -> Image.Image:
    """在農田圖上方加一條草地，狗在左上、貓在右上（站在欄杆外的草地上，不壓到欄杆與農田）。"""
    cfg = manifest()["pets"]
    band = cfg["band_height"]
    w, h = field_canvas.size
    canvas = Image.new("RGBA", (w, h + band))
    tile = img(cfg["band_tile"])
    for x in range(0, w, tile.width):
        for y in range(0, band, tile.height):
            canvas.alpha_composite(tile, (x, y))
    canvas.alpha_composite(field_canvas, (0, band))
    for code, sprite_cfg in cfg["sprites"].items():
        if code not in pets:
            continue
        sprite = img(sprite_cfg["file"])
        x = sprite_cfg["margin"] if sprite_cfg["anchor"] == "left" else w - sprite.width - sprite_cfg["margin"]
        canvas.alpha_composite(sprite, (x, band - sprite.height + sprite_cfg["bottom"]))
    return canvas


def farm_png_from_status(status) -> bytes:
    return farm_png(plots_from_status(status), getattr(status, "pets", None))


# ---------- 建築畫面 ----------


def yard_key(counts: dict) -> str:
    """建築數量 → 底圖 key，例如 {"barn": 2, "mill": 1, "bakery": 0, "jam_workshop": 1} → "2-101"。"""
    yard = manifest()["yard"]
    barns = min(counts.get("barn", 0), len(yard["barn_slots"]))
    return f"{barns}-" + "".join("1" if counts.get(code, 0) else "0" for code in yard["machine_order"])


def draw_bubble(canvas: Image.Image, pos: list[int], icon_rel: str) -> None:
    """32×32 對話泡泡＋中央 20×20 圖示。"""
    bubble = img(manifest()["badges"]["bubble"]).copy()
    icon = img(icon_rel).resize((20, 20), Image.LANCZOS)
    bubble.alpha_composite(icon, (6, 5))
    canvas.alpha_composite(bubble, tuple(pos))


def yard_png(workshop: dict, animals: list[dict]) -> bytes:
    """workshop = workshop_status()；animals = list_animals()。回傳建築畫面 PNG。"""
    m = manifest()
    yard, badges = m["yard"], m["badges"]
    base_rel = yard["bases"][yard_key(workshop["counts"])]
    canvas: Image.Image | None = None

    def target() -> Image.Image:
        nonlocal canvas
        if canvas is None:
            canvas = img(base_rel).copy()
        return canvas

    # 加工機器：加工中 → 沙漏；可領取 → 成品圖示
    for code, info in yard["machines"].items():
        order = workshop["busy"].get(code)
        if not workshop["counts"].get(code) or not order:
            continue
        if order["state"] == "in_progress":
            draw_bubble(target(), info["bubble"], badges["timer"])
        elif order["state"] == "ready":
            draw_bubble(target(), info["bubble"], m["goods"].get(order["output_code"], badges["ready"]))

    # 動物：依 animal_no 排序，第 i 隻站在第 i // 4 座畜舍旁的第 i % 4 個固定位置（[中心 x, 腳底 y, 是否左右翻轉]，
    # 位置已避開建築與其他動物），超過容量的不畫；由後往前畫，泡泡最後畫才不會被其他動物蓋住
    barns = min(workshop["counts"].get("barn", 0), len(yard["barn_slots"]))
    size = yard["animal_size"]
    placed = [(yard["animal_spots"][i // 4][i % 4], a)
              for i, a in enumerate(sorted(animals, key=lambda a: a["animal_no"])[:barns * 4])]
    placed.sort(key=lambda t: t[0][1])
    for (cx, by, flip), a in placed:
        sprite = img(m["animals"].get(a["code"], m["animals"]["_generic"])).resize((size, size), Image.LANCZOS)
        if flip:
            sprite = sprite.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        target().alpha_composite(sprite, (cx - size // 2, by - size))
    for (cx, by, _flip), a in placed:
        bubble = [cx + size // 2 - 20, by - size - 12]  # 頭部右上方，盡量不蓋到其他動物
        if a["state"] == "hungry":
            draw_bubble(target(), bubble, badges["hungry"])
        elif a["state"] == "ready":
            draw_bubble(target(), bubble, m["products"].get(a.get("product_code"), badges["ready"]))

    if canvas is None:
        return file_png(base_rel)  # 沒有動物、機器都閒置：直接用底圖
    return encode(canvas)


def scene_png(name: str) -> bytes:
    """固定場景圖（manifest 的 scenes），直接上傳檔案。"""
    return (ASSETS / manifest()["scenes"][name]).read_bytes()


def shop_png() -> bytes:
    return scene_png("shop")


def casino_png() -> bytes:
    return scene_png("casino")


@functools.lru_cache(maxsize=1)
def world_boss_png() -> bytes:
    """世界 Boss 頁的固定圖（由 dragon.png 縮成 1024×576、256 色）。

    只讀一次；內容不變，面板會沿用訊息上已上傳的附件。"""
    return scene_png("world_boss")


def explore_png(area_code: str) -> bytes | None:
    """山洞冒險區域圖；沒有對應圖片的區域回傳 None（面板只顯示文字）。"""
    name = f"explore_{area_code}"
    return scene_png(name) if name in manifest()["scenes"] else None
