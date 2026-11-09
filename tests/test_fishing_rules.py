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
    assert naive("legendary", MID, 12)[0] > starter[3]
    assert naive("legendary", BEST, 13)[0] > naive("legendary", MID, 14)[0]
    # 較好的魚竿收線更快：依提示操作時拉扯次數較少（亂按時入門裝備很快就斷線，次數反而少，不能比較）
    def smart_turns(gear, seed):
        return fishing.success_rate(no_fate, "epic", *gear, fishing.smart_action, random.Random(seed).random, 1000)[1]

    assert smart_turns(BEST, 15) < smart_turns(STARTER, 16)


@pytest.mark.parametrize("difficulty", [1, 3])
def test_best_gear_cannot_land_common_fish_by_only_reeling(difficulty):
    """裝備再好，普通魚也不能只靠一直收線釣起來。"""
    no_fate = {**S, "base_success_pct": 100}
    rng = random.Random(f"spam-{difficulty}").random
    rate, _ = fishing.success_rate(no_fate, "common", *BEST, fishing.reel_only_action, rng, 3000, difficulty)
    assert rate < 0.15
    smart, _ = fishing.success_rate(no_fate, "common", *BEST, fishing.smart_action, rng, 3000, difficulty)
    assert smart >= 0.99  # 依提示操作不受影響


def test_fixed_rhythm_script_is_much_worse_than_reading_the_screen():
    no_fate = {**S, "base_success_pct": 100}
    rng = random.Random("rhythm").random
    script, _ = fishing.success_rate(no_fate, "common", *BEST, fishing.alternate_action, rng, 3000)
    smart, _ = fishing.success_rate(no_fate, "common", *BEST, fishing.smart_action, rng, 3000)
    assert script < 0.4 and smart >= 0.99


def fight_state(**changes) -> dict:
    return {"stamina": 40, "max_stamina": 40, "distance": 90, "tension": 0, "max_tension": 100, "turn": 0,
            "slip_turn": None, "intent": "calm", "last": None, "reel_streak": 0, "dodge": None, **changes}


def NO_DODGE() -> float:  # 固定亂數：不觸發竄動
    return 0.99


def test_consecutive_reels_raise_tension_in_proportion_to_line():
    diff = {**S["difficulty"]["common"]}
    state = fight_state()
    added = []
    for _ in range(3):
        before = state["tension"]
        state, result = fishing.step(state, "reel", diff, STARTER[0], NO_DODGE)
        assert result is None
        added.append(state["tension"] - before + 6)  # 魚平靜時張力 -6
    assert added == [14, 28, 42]  # 每多連續一次多 14（最大張力 100 的 14%）
    # 魚線上限越高，連續收線的額外張力也越高：好魚線不能抵銷這個機制
    strong = fight_state(max_tension=130, reel_streak=2)
    assert fishing.reel_tension(strong) == pytest.approx(14 + 2 * 130 * 0.14)


def test_wait_only_cools_down_half_a_step_and_release_resets_streak():
    diff = S["difficulty"]["common"]
    waited, _ = fishing.step(fight_state(reel_streak=2), "wait", diff, STARTER[0], NO_DODGE)
    assert waited["reel_streak"] == 1.5
    released, _ = fishing.step(fight_state(reel_streak=2), "release", diff, STARTER[0], NO_DODGE)
    assert released["reel_streak"] == 0


def test_last_reel_cannot_land_when_it_snaps_the_line():
    diff = S["difficulty"]["common"]
    state = fight_state(distance=5, stamina=1, tension=90, reel_streak=3)
    _, result = fishing.step(state, "reel", diff, BEST[0], NO_DODGE)
    assert result == "line_broken"


def test_fish_with_stamina_cannot_be_landed_even_with_strongest_rod():
    diff = S["difficulty"]["common"]
    state, result = fishing.step(fight_state(distance=5), "reel", diff, BEST[0], NO_DODGE)
    assert result is None and state["distance"] == fishing.LAND_GATE_DISTANCE and "還有力氣" in state["last"]["text"]
    _, result = fishing.step(fight_state(distance=5, stamina=10), "reel", diff, BEST[0], NO_DODGE)
    assert result == "landed"


def test_dodge_rewards_matching_direction_and_punishes_everything_else():
    diff = S["difficulty"]["common"]
    base = fight_state(distance=50, tension=40, dodge="left")
    hit, result = fishing.step(base, "left", diff, STARTER[0], NO_DODGE)
    assert result is None and hit["tension"] == 25 and hit["stamina"] == 30 and hit["distance"] == 47
    assert hit["dodge"] is None
    wrong, _ = fishing.step(base, "right", diff, STARTER[0], NO_DODGE)
    assert wrong["tension"] == 40 - 6 + fishing.DODGE_MISS["tension"] and wrong["distance"] == 58
    reeled, _ = fishing.step(base, "reel", diff, STARTER[0], NO_DODGE)
    assert reeled["tension"] == 40 + 14 - 6 + fishing.DODGE_MISS["tension"]
    idle, _ = fishing.step(fight_state(tension=40), "left", diff, STARTER[0], NO_DODGE)
    assert idle["tension"] == 40 + fishing.STEER_IDLE_TENSION - 6


def test_dodge_never_repeats_and_tired_fish_does_not_dodge():
    diff = S["difficulty"]["legendary"]
    def always() -> float:
        return 0.0

    assert fishing.next_dodge(fight_state(), diff, None, always) == "left"
    assert fishing.next_dodge(fight_state(), diff, "left", always) is None
    assert fishing.next_dodge(fight_state(stamina=0), diff, None, always) is None
    assert fishing.dodge_pct(S["difficulty"]["legendary"]) > fishing.dodge_pct(S["difficulty"]["common"])


def test_old_sessions_without_new_fields_still_work():
    """升級時進行中的釣魚：舊 state 沒有 reel_streak／dodge。"""
    state = fight_state()
    del state["reel_streak"], state["dodge"]
    new, result = fishing.step(state, "reel", S["difficulty"]["common"], STARTER[0], NO_DODGE)
    assert result is None and new["reel_streak"] == 1


def test_regular_rhythm_detection():
    assert not fishing.regular_rhythm([1.0] * 5)  # 樣本不足
    assert fishing.regular_rhythm([1.0, 1.02, 0.98, 1.01, 0.99, 1.0])
    assert not fishing.regular_rhythm([0.9, 2.5, 1.2, 4.0, 1.5, 3.1])


def test_guard_lock_doubles_and_is_capped():
    assert [fishing.guard_lock_minutes(n) for n in (1, 2, 3, 9)] == [5, 10, 20, fishing.GUARD_LOCK_MAX_MINUTES]


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
