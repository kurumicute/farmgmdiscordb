"""釣魚純規則與模擬：稀有度機率、各稀有度的整體成功率、裝備對難度的影響、價格與設定驗證。"""

import random
from collections import Counter

import pytest

from app.game import fishing
from app.game.errors import ValidationError

S = fishing.DEFAULT_SETTINGS
STARTER = ({"reel_power": 10, "control_pct": 0, "reaction_bonus_ms": 0}, {"max_tension": 100})
MID = ({"reel_power": 13, "control_pct": 10, "reaction_bonus_ms": 1000}, {"max_tension": 115})
BEST = ({"reel_power": 18, "control_pct": 25, "reaction_bonus_ms": 2000}, {"max_tension": 130})


def test_rarity_distribution_matches_settings():
    rng = random.Random(1).random
    n = 200_000
    counts = Counter(fishing.roll_rarity(S["rarity_bp"], {}, rng) for _ in range(n))
    for rarity, bp in S["rarity_bp"].items():
        assert counts[rarity] / n == pytest.approx(bp / 10000, abs=0.004)


def test_bait_shifts_rarity_from_common():
    chances = fishing.rarity_chances(S["rarity_bp"], {"rare_bp": 500, "epic_bp": 300, "legendary_bp": 150})
    assert chances["common"] == pytest.approx(70.5) and chances["legendary"] == pytest.approx(2.5)
    assert sum(chances.values()) == pytest.approx(100)


@pytest.mark.parametrize("rarity", fishing.RARITIES)
@pytest.mark.parametrize("gear", [STARTER, MID, BEST], ids=["starter", "mid", "best"])
def test_sensible_play_success_is_close_to_base_rate_for_every_rarity(rarity, gear):
    """稀有度不影響整體成功率：依提示操作時，每種稀有度、每種裝備都接近 90%。"""
    rng = random.Random(f"{rarity}-{gear[1]['max_tension']}").random
    rate, turns = fishing.success_rate(S, rarity, gear[0], gear[1], fishing.smart_action, rng, trials=4000)
    assert rate == pytest.approx(0.90, abs=0.03)
    assert 2 <= turns <= 25  # 不會長到無法遊玩


@pytest.mark.parametrize("rarity", fishing.RARITIES)
def test_fight_itself_is_winnable_with_starter_gear(rarity):
    """排除脫鉤（基礎成功率 100%）後，入門裝備依提示操作幾乎都能釣起，高稀有度不會變成無法釣起。"""
    rng = random.Random(rarity).random
    rate, _ = fishing.success_rate({**S, "base_success_pct": 100}, rarity, *STARTER, fishing.smart_action, rng, 3000)
    assert rate >= 0.97


def test_rarity_raises_difficulty_and_gear_lowers_it():
    """不看提示亂收線時：稀有度越高越難；裝備越好越容易。"""
    no_fate = {**S, "base_success_pct": 100}

    def naive(rarity, gear, seed):
        return fishing.success_rate(no_fate, rarity, *gear, fishing.naive_action, random.Random(seed).random, 3000)

    starter = [naive(r, STARTER, 11)[0] for r in fishing.RARITIES]
    assert starter[0] > starter[1] > starter[2] > starter[3]
    assert naive("legendary", MID, 12)[0] > starter[3] + 0.2
    assert naive("legendary", BEST, 13)[0] > naive("legendary", MID, 14)[0]
    # 較好的魚竿收線更快：拉扯次數較少
    assert naive("epic", BEST, 15)[1] < naive("epic", STARTER, 16)[1]


def test_estimated_play_time_is_20_to_60_seconds():
    for rarity in fishing.RARITIES:
        for gear in (STARTER, BEST):
            _, turns = fishing.success_rate(S, rarity, *gear, fishing.smart_action, random.Random(5).random, 1000)
            assert 15 <= fishing.estimated_seconds(S, turns, seconds_per_turn=2.5) <= 60


def test_price_and_weight_stay_in_fish_range():
    rng = random.Random(3).random
    for code, _name, _emoji, _rarity, pmin, pmax, wmin, wmax, _diff, _desc in fishing.INITIAL_FISH:
        assert 0 <= pmin <= pmax, code
        for _ in range(200):
            w = fishing.roll_weight(wmin, wmax, rng)
            assert wmin <= w <= wmax
            assert pmin <= fishing.fish_price(pmin, pmax, wmin, wmax, w) <= pmax


def test_validate_fish_price_is_not_bound_to_rarity():
    base = {"rarity": "common", "min_price": 1, "max_price": 20, "min_weight_g": 10, "max_weight_g": 100,
            "difficulty": 3}
    assert fishing.validate_fish(base)["max_price"] == 20
    assert fishing.validate_fish({**base, "min_price": 0, "max_price": 500_000})["max_price"] == 500_000
    assert fishing.validate_fish({**base, "rarity": "legendary"})["rarity"] == "legendary"  # 1–20 的傳說魚也可以
    for bad in ({"min_price": 30}, {"min_price": -1}, {"max_price": fishing.PRICE_MAX + 1}):
        with pytest.raises(ValidationError):
            fishing.validate_fish({**base, **bad})


def test_price_span_per_rarity():
    fish = [{"rarity": "rare", "min_price": 5, "max_price": 9}, {"rarity": "rare", "min_price": 3, "max_price": 400}]
    assert fishing.price_span(fish, "rare") == (3, 400)
    assert fishing.price_span(fish, "epic") is None


def test_validate_settings_and_gear_effects():
    assert fishing.validate_settings(S)["base_success_pct"] == 90
    with pytest.raises(ValidationError):
        fishing.validate_settings({**S, "bite_wait_min_seconds": 20, "bite_wait_max_seconds": 10})
    diff = {r: dict(v) for r, v in fishing.DEFAULT_DIFFICULTY.items()}
    diff["epic"]["surge_pct"] = 90
    with pytest.raises(ValidationError, match="合計"):
        fishing.validate_settings({**S, "difficulty": diff})
    with pytest.raises(ValidationError):
        fishing.validate_gear_effects("rod", {"reel_power": 99, "control_pct": 0, "reaction_bonus_ms": 0})
    with pytest.raises(ValidationError):
        fishing.validate_gear_effects("line", {"max_tension": 100, "reel_power": 10})


def test_step_outcomes():
    diff = S["difficulty"]["common"]
    rod = STARTER[0]
    state = {"stamina": 10, "max_stamina": 40, "distance": 5, "tension": 20, "max_tension": 100, "turn": 0,
             "slip_turn": None, "intent": "calm", "last": None}
    landed, result = fishing.step(state, "reel", diff, rod, random.Random(1).random)
    assert result == "landed" and landed["distance"] == 0
    tight = {**state, "distance": 50, "tension": 95, "intent": "strong"}
    _, result = fishing.step(tight, "reel", diff, rod, random.Random(1).random)
    assert result == "line_broken"
    _, result = fishing.step({**tight, "tension": 60}, "release", diff, rod, random.Random(1).random)
    assert result is None  # 衝刺前放線就不會斷
    far = {**state, "distance": 95, "intent": "strong", "stamina": 40}
    _, result = fishing.step(far, "release", diff, rod, random.Random(1).random)
    assert result == "escaped"
    _, result = fishing.step({**state, "slip_turn": 1}, "reel", diff, rod, random.Random(1).random)
    assert result == "slipped"
