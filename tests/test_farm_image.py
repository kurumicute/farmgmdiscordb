"""農場圖片拼接（app/discord_bot/farm_image.py）：預先產生的圖命中、即時拼圖、建築畫面與商店。不連資料庫。"""

import io

import pytest
from PIL import Image

from app.discord_bot import farm_image

ASSETS = farm_image.ASSETS


def size_of(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


def plot(crop=None, state="empty", progress=0.0, quality="normal", mutation="none"):
    return {"crop": crop, "state": state, "progress": progress, "quality": quality, "mutation": mutation}


def test_empty_field_uses_base_file():
    assert farm_image.farm_png([plot()] * 4) == (ASSETS / "field/base_04.png").read_bytes()


def test_same_crop_same_stage_uses_preset():
    plots = [plot("wheat", "ready")] * 4
    assert farm_image.farm_png(plots) == (ASSETS / "field/presets/04_wheat_ready.png").read_bytes()
    plots = [plot("carrot", "growing", 0.1)] * 16
    assert farm_image.farm_png(plots) == (ASSETS / "field/presets/16_sprout.png").read_bytes()


def test_preset_with_quality_badges_is_redrawn():
    plots = [plot("wheat", "ready", quality="legendary")] + [plot("wheat", "ready")] * 3
    png = farm_image.farm_png(plots)
    assert png != (ASSETS / "field/presets/04_wheat_ready.png").read_bytes()
    assert size_of(png) == (512, 502)


def test_mixed_and_unknown_crops_render():
    plots = [
        plot("wheat", "waiting_water"), plot("carrot", "growing", 0.5), plot("tomato", "growing", 0.9),
        plot("strawberry", "ready", 1, "rare", "variant"), plot("dragonfruit", "ready", 1, "good", "giant"),
        plot("dragonfruit", "withered"), plot(),
    ]
    assert size_of(farm_image.farm_png(plots)) == (512, 502)


def test_more_than_sixteen_plots_draws_sixteen():
    plots = [plot("wheat", "growing", 0.5), plot("carrot", "ready")] * 20
    assert size_of(farm_image.farm_png(plots)) == (512, 502)


def test_stage_thresholds():
    assert farm_image.stage_of(plot("wheat", "waiting_water")) == "seeds"
    assert farm_image.stage_of(plot("wheat", "growing", 0.39)) == "sprout"
    assert farm_image.stage_of(plot("wheat", "growing", 0.4)) == "young"
    assert farm_image.stage_of(plot("wheat", "growing", 0.75)) == "unripe"


def workshop(barn=0, mill=0, bakery=0, jam=0, busy=None):
    counts = {"barn": barn, "mill": mill, "bakery": bakery, "jam_workshop": jam}
    return {"counts": counts, "busy": busy or {}}


def test_idle_yard_uses_base_file():
    assert farm_image.yard_png(workshop(), []) == (ASSETS / "yard/base_0_000.png").read_bytes()
    assert farm_image.yard_png(workshop(2, 1, 0, 1), []) == (ASSETS / "yard/base_2_101.png").read_bytes()


def test_full_yard_with_animals_and_orders():
    busy = {"mill": {"state": "in_progress", "output_code": "flour"},
            "bakery": {"state": "ready", "output_code": "bread"},
            "jam_workshop": {"state": "ready", "output_code": "unknown_goods"}}
    animals = [{"animal_no": i, "code": ("chicken", "cow", "sheep", "dragon")[i % 4],
                "state": ("hungry", "ready", "producing")[i % 3], "product_code": ("egg", "milk", "wool")[i % 3]}
               for i in range(15)]  # 超過 3 座畜舍的容量 12 隻，未知動物用剪影
    assert size_of(farm_image.yard_png(workshop(3, 1, 1, 1, busy), animals)) == (640, 640)
    # 只有 1 座畜舍時，只畫前 4 隻
    assert size_of(farm_image.yard_png(workshop(1), animals)) == (640, 640)


def test_shop_is_fixed_file():
    assert farm_image.shop_png() == (ASSETS / "scenes/shop.png").read_bytes()


def test_casino_and_explore_scenes():
    from app.game import exploration

    assert farm_image.casino_png() == (ASSETS / "scenes/casino.png").read_bytes()
    for code in exploration.AREA_ORDER:  # 每個探險區域都有自己的圖
        assert farm_image.explore_png(code) == (ASSETS / f"scenes/explore_{code}.png").read_bytes()
    assert farm_image.explore_png("unknown_area") is None


def _solid(sprite: Image.Image) -> set[tuple[int, int]]:
    alpha = sprite.getchannel("A")
    return {(x, y) for y in range(sprite.height) for x in range(sprite.width) if alpha.getpixel((x, y)) > 40}


def test_animal_spots_do_not_overlap_buildings_or_each_other():
    """動物固定站在畜舍旁：12 個位置都不能和任何建築或其他動物的實心像素重疊（避免看起來像穿模）。"""
    m = farm_image.manifest()
    yard, size = m["yard"], m["yard"]["animal_size"]
    taken: set[tuple[int, int]] = set()
    rects = [(slot["rect"], "barn") for slot in yard["barn_slots"]]
    rects += [(info["rect"], code) for code, info in yard["machines"].items()]
    for rect, code in rects:
        sprite = farm_image.img(m["buildings"][code]).resize((rect[2] - rect[0], rect[3] - rect[1]))
        taken |= {(rect[0] + x, rect[1] + y) for x, y in _solid(sprite)}
    spots = [spot for barn in yard["animal_spots"] for spot in barn]
    assert len(spots) == len(yard["barn_slots"]) * 4
    for code in ("chicken", "cow", "sheep", "_generic"):
        sprite = farm_image.img(m["animals"][code]).resize((size, size), Image.LANCZOS)
        solid, flipped = _solid(sprite), _solid(sprite.transpose(Image.Transpose.FLIP_LEFT_RIGHT))
        occupied = set(taken)
        for cx, by, flip in spots:
            pixels = {(cx - size // 2 + x, by - size + y) for x, y in (flipped if flip else solid)}
            assert not pixels & occupied, (code, cx, by)
            assert all(0 <= x < yard["size"][0] and 0 <= y < yard["size"][1] for x, y in pixels)
            occupied |= pixels


def test_missing_assets_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(farm_image, "ASSETS", tmp_path)
    farm_image.reset_cache()
    try:
        with pytest.raises(OSError):
            farm_image.farm_png([plot()] * 4)
    finally:
        monkeypatch.undo()
        farm_image.reset_cache()
