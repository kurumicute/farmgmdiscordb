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


def test_missing_assets_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(farm_image, "ASSETS", tmp_path)
    farm_image.reset_cache()
    try:
        with pytest.raises(OSError):
            farm_image.farm_png([plot()] * 4)
    finally:
        monkeypatch.undo()
        farm_image.reset_cache()
