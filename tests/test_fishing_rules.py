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


@pytest.mark.parametrize("rarity", fishing.RARITIES)
def test_pulls_needed_follow_rarity(rarity):
    """依提示操作時：普通 2–4 下、稀有 3–5、史詩 4–6、傳說 5–7；好裝備幾乎不用多等。"""
    no_fate = {**S, "base_success_pct": 100}
    low, high = fishing.PULLS[rarity]
    rng = random.Random(f"pulls-{rarity}").random
    needs = {fishing.new_fight(no_fate, rarity, 3, BEST[1], rng)["need"] for _ in range(300)}
    assert needs == set(range(low, high + 1))
    results = [fishing.simulate(no_fate, rarity, *BEST, fishing.smart_action, rng) for _ in range(2000)]
    assert all(r == "landed" and n >= low for r, n in results)
    assert sum(n <= high for _r, n in results) / len(results) >= 0.85
    starter = fishing.success_rate(no_fate, rarity, *STARTER, fishing.smart_action, rng, 1000)[1]
    best = fishing.success_rate(no_fate, rarity, *BEST, fishing.smart_action, rng, 1000)[1]
    assert best <= starter <= high + 4  # 入門裝備要多等幾下，但不會拖太久


@pytest.mark.parametrize("rarity", fishing.RARITIES)
@pytest.mark.parametrize("policy", [fishing.reel_only_action, fishing.alternate_action, fishing.naive_action])
def test_scripts_without_reading_the_screen_never_land(rarity, policy):
    """只按收線（或固定節奏、不看方向）一定釣不到：每場都有魚竄向一邊，只有壓同方向才過得去。"""
    no_fate = {**S, "base_success_pct": 100}
    rng = random.Random(f"spam-{rarity}-{policy.__name__}").random
    assert fishing.success_rate(no_fate, rarity, *BEST, policy, rng, 1000)[0] == 0


def test_every_fight_has_forced_dodges_within_its_pulls():
    rng = random.Random("dodges").random
    for rarity in fishing.RARITIES:
        for _ in range(200):
            state = fishing.new_fight(S, rarity, 3, STARTER[1], rng)
            assert len(state["dodge_at"]) == fishing.FORCED_DODGES[rarity]
            assert all(0 <= x < state["need"] for x in state["dodge_at"])
            assert (state["dodge"] is not None) == (0 in state["dodge_at"])


def fight_state(**changes) -> dict:
    return {"stamina": 40, "max_stamina": 40, "distance": 90, "start_distance": 90, "tension": 0, "max_tension": 100,
            "turn": 0, "slip_turn": None, "intent": "calm", "last": None, "reel_streak": 0, "dodge": None,
            "need": 10, "progress": 0, "dodge_at": [], "slack": 0, **changes}


def NO_DODGE() -> float:  # 固定亂數
    return 0.99


def test_consecutive_reels_raise_tension_and_count_as_pulls():
    diff = {**S["difficulty"]["common"]}
    state = fight_state()
    added = []
    for i in range(3):
        before = state["tension"]
        state, result = fishing.step(state, "reel", diff, STARTER[0], NO_DODGE)
        assert result is None and state["progress"] == i + 1
        added.append(state["tension"] - before + 6)  # 魚平靜時張力 -6
    assert added == [14, 18, 22]  # 每多連續一次多 4（最大張力 100 的 4%）
    strong = fight_state(max_tension=130, reel_streak=2)
    assert fishing.reel_tension(strong) == pytest.approx(14 + 2 * 130 * 0.04)
    assert state["stamina"] == 28 and state["distance"] == 63  # 跟著進度下降


def test_wait_and_release_cool_down_without_progress():
    diff = S["difficulty"]["common"]
    waited, _ = fishing.step(fight_state(reel_streak=3, tension=60, intent="strong"), "wait", diff, STARTER[0],
                             NO_DODGE)
    assert waited["reel_streak"] == 1 and waited["tension"] == 60 + fishing.WAIT_TENSION and waited["progress"] == 0
    released, _ = fishing.step(fight_state(reel_streak=2, tension=60), "release", diff, STARTER[0], NO_DODGE)
    assert released["reel_streak"] == 0 and released["tension"] == 30 and released["distance"] == 96


def test_last_pull_lands_but_not_when_it_snaps_the_line():
    diff = S["difficulty"]["common"]
    _, result = fishing.step(fight_state(progress=9, tension=90, reel_streak=3), "reel", diff, BEST[0], NO_DODGE)
    assert result == "line_broken"
    landed, result = fishing.step(fight_state(progress=9, tension=20), "reel", diff, BEST[0], NO_DODGE)
    assert result == "landed" and landed["distance"] == 0


def test_dodge_must_be_matched_and_keeps_coming_back():
    diff = S["difficulty"]["common"]
    base = fight_state(tension=40, dodge="left", dodge_at=[0])
    hit, result = fishing.step(base, "left", diff, STARTER[0], NO_DODGE)
    assert result is None and hit["tension"] == 25 and hit["progress"] == 1 and hit["dodge"] is None
    assert hit["dodge_at"] == []
    for action in ("right", "reel", "wait"):
        missed, _ = fishing.step(base, action, diff, STARTER[0], NO_DODGE)
        assert missed["progress"] == 0 and missed["dodge"] in fishing.STEER_ACTIONS  # 魚繼續竄
        assert "沒壓對方向" in missed["last"]["text"]
    reeled, _ = fishing.step(base, "reel", diff, STARTER[0], NO_DODGE)
    assert reeled["tension"] == 40 + 14 + fishing.DODGE_MISS["tension"] and reeled["distance"] == 98
    idle, _ = fishing.step(fight_state(tension=40), "left", diff, STARTER[0], NO_DODGE)
    assert idle["tension"] == 40 + fishing.STEER_IDLE_TENSION - 6 and idle["progress"] == 0
    # 進度到 dodge_at 時魚竄出來
    before, _ = fishing.step(fight_state(dodge_at=[1]), "reel", diff, STARTER[0], NO_DODGE)
    assert before["progress"] == 1 and before["dodge"] in fishing.STEER_ACTIONS


def test_old_sessions_without_new_fields_still_work():
    """升級時進行中的釣魚：舊 state 沒有 need 等欄位，再成功拉 2 下就上岸。"""
    state = fight_state()
    for key in ("reel_streak", "dodge", "need", "progress", "dodge_at", "slack", "start_distance"):
        del state[key]
    new, result = fishing.step(state, "reel", S["difficulty"]["common"], STARTER[0], NO_DODGE)
    assert result is None and new["reel_streak"] == 1 and new["progress"] == 1
    _, result = fishing.step(new, "reel", S["difficulty"]["common"], STARTER[0], NO_DODGE)
    assert result == "landed"


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
    state = fight_state(progress=9, tension=20, distance=5)
    landed, result = fishing.step(state, "reel", diff, rod, random.Random(1).random)
    assert result == "landed" and landed["distance"] == 0
    tight = fight_state(tension=95, intent="strong")
    _, result = fishing.step(tight, "reel", diff, rod, random.Random(1).random)
    assert result == "line_broken"
    _, result = fishing.step({**tight, "tension": 60}, "release", diff, rod, random.Random(1).random)
    assert result is None  # 衝刺前放線就不會斷
    far = fight_state(slack=fishing.ESCAPE_DISTANCE - 5)
    _, result = fishing.step(far, "release", diff, rod, random.Random(1).random)
    assert result == "escaped"
    _, result = fishing.step(fight_state(slip_turn=1), "reel", diff, rod, random.Random(1).random)
    assert result == "slipped"
