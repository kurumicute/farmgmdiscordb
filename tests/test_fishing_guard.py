"""釣魚防腳本：定期人機確認、操作時間異常、答錯暫停、按鈕打亂；只影響拋竿，不扣資產。"""

import pytest
from sqlalchemy import select

from app.db.models import FishingGuard
from app.discord_bot.commands import FarmCommands
from app.discord_bot.runner import ServiceRunner
from app.game import fishing
from app.game.errors import ValidationError
from app.services.admin_service import AdminService
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import make_player, run
from tests.test_fishing import equip_starter, settings_with


@pytest.fixture(autouse=True)
def guard_on(monkeypatch):
    monkeypatch.setattr(fishing, "GUARD_ENABLED", True)


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


def guard_row(session_factory):
    with session_factory() as s:
        return s.scalars(select(FishingGuard)).one()


def force_check(session_factory, **values):
    with session_factory.begin() as s:
        guard = s.scalars(select(FishingGuard)).one()
        guard.casts_since_check = guard.check_after
        for k, v in values.items():
            setattr(guard, k, v)


def answer_of(session_factory) -> str:
    return guard_row(session_factory).challenge["answer"]


def finish_cast(admin, pid, view):
    admin.give_up_fishing(pid, view["id"])


def test_periodic_check_blocks_cast_without_using_bait(admin, player_id, clock, session_factory):
    equip_starter(admin, player_id, bait=10)
    view = admin.start_fishing(player_id)
    assert view["phase"] == "waiting" and guard_row(session_factory).casts_since_check == 1
    finish_cast(admin, player_id, view)
    force_check(session_factory)
    bait = admin.fishing_overview(player_id)["bait_quantity"]
    check = admin.start_fishing(player_id)
    assert check["phase"] == "check" and len(check["options"]) == fishing.GUARD_OPTIONS
    assert admin.fishing_overview(player_id)["bait_quantity"] == bait  # 沒有拋竿，不扣魚餌
    # 題目只有中文名稱，選項沒有名稱
    assert check["target"] in {name for _emoji, name in fishing.GUARD_CREATURES.values()}
    assert all(set(o) == {"code", "emoji"} for o in check["options"])
    # 再按一次拋竿拿到同一題，不能換題碰運氣
    assert admin.start_fishing(player_id)["target"] == check["target"]


def test_correct_answer_casts_and_resets(admin, player_id, clock, session_factory):
    equip_starter(admin, player_id)
    finish_cast(admin, player_id, admin.start_fishing(player_id))
    force_check(session_factory, suspicion=9)
    admin.start_fishing(player_id)
    view = admin.answer_fishing_check(player_id, answer_of(session_factory))
    assert view["phase"] == "waiting"
    guard = guard_row(session_factory)
    assert guard.challenge is None and guard.suspicion == 0 and guard.casts_since_check == 1
    assert guard.passed_total == 1
    lo, hi = fishing.GUARD_CHECK_EVERY_CASTS
    assert lo <= guard.check_after <= hi


def test_wrong_answer_locks_fishing_but_costs_nothing(admin, player_id, clock, session_factory):
    equip_starter(admin, player_id)
    finish_cast(admin, player_id, admin.start_fishing(player_id))
    force_check(session_factory)
    admin.start_fishing(player_id)
    before = admin.fishing_overview(player_id)
    wrong = next(c for c in guard_row(session_factory).challenge["options"]
                 if c != answer_of(session_factory))
    failed = admin.answer_fishing_check(player_id, wrong)
    assert failed["phase"] == "check_failed" and failed["minutes"] == fishing.GUARD_LOCK_MINUTES
    after = admin.fishing_overview(player_id)
    assert (after["gold"], after["bait_quantity"]) == (before["gold"], before["bait_quantity"])
    with pytest.raises(ValidationError, match="暫停"):
        admin.start_fishing(player_id)
    with pytest.raises(ValidationError, match="暫停"):  # 不能靠重複送出答題繞過暫停
        admin.answer_fishing_check(player_id, wrong)
    # 時間到後要重新確認，連續答錯暫停加倍
    clock.advance(fishing.GUARD_LOCK_MINUTES * 60 + 1)
    assert admin.start_fishing(player_id)["phase"] == "check"
    wrong = next(c for c in guard_row(session_factory).challenge["options"]
                 if c != answer_of(session_factory))
    assert admin.answer_fishing_check(player_id, wrong)["minutes"] == fishing.GUARD_LOCK_MINUTES * 2


def test_expired_challenge_gets_a_new_one_without_penalty(admin, player_id, clock, session_factory):
    equip_starter(admin, player_id)
    finish_cast(admin, player_id, admin.start_fishing(player_id))
    force_check(session_factory)
    admin.start_fishing(player_id)
    clock.advance(fishing.GUARD_CHALLENGE_SECONDS + 1)
    view = admin.answer_fishing_check(player_id, "anything")
    assert view["phase"] == "check" and "過期" in view["note"]
    assert guard_row(session_factory).failed_total == 0


def test_inhuman_timing_triggers_check_but_normal_play_does_not(admin, player_id, clock, session_factory):
    admin.update_fishing_settings("test", settings_with(base_success_pct=100, reaction_seconds=15))
    equip_starter(admin, player_id, bait=20)
    # 正常操作：上鉤 1 秒後拉竿，每步間隔不規律且超過門檻
    view = admin.start_fishing(player_id)
    clock._now = view["bite_at"]
    clock.advance(1)
    v = admin.hook_fish(player_id, view["id"])
    for gap in (1.2, 3.0, 0.9, 2.2):
        clock.advance(gap)
        v = admin.fishing_action(player_id, v["id"], "wait", v["turn"])
    admin.give_up_fishing(player_id, v["id"])
    assert guard_row(session_factory).suspicion == 0
    # 腳本：一上鉤就拉竿，之後每步都立刻送出
    view = admin.start_fishing(player_id)
    clock._now = view["bite_at"]
    v = admin.hook_fish(player_id, view["id"])
    assert guard_row(session_factory).suspicion == fishing.GUARD_SCORE["fast_hook"]
    for _ in range(4):
        v = admin.fishing_action(player_id, v["id"], "wait", v["turn"])
    admin.give_up_fishing(player_id, v["id"])
    assert guard_row(session_factory).suspicion >= fishing.GUARD_SUSPICION_LIMIT
    assert admin.start_fishing(player_id)["phase"] == "check"


def test_duplicate_click_does_not_count_as_fast_action(admin, player_id, clock, session_factory):
    admin.update_fishing_settings("test", settings_with(base_success_pct=100, reaction_seconds=15))
    equip_starter(admin, player_id)
    view = admin.start_fishing(player_id)
    clock._now = view["bite_at"]
    clock.advance(2)
    v = admin.hook_fish(player_id, view["id"])
    clock.advance(2)
    admin.fishing_action(player_id, v["id"], "wait", 0)
    admin.fishing_action(player_id, v["id"], "wait", 0)  # 連點：畫面還沒更新
    assert guard_row(session_factory).suspicion == 0


def test_panel_shows_check_and_shuffled_fight_buttons(admin, clock, session_factory):
    runner = ServiceRunner(2)
    try:
        cmds = FarmCommands(admin, runner)
        p = make_player(cmds, ALICE, name="玩家")
        p.panel.fish_timers_enabled = False
        pid = p.panel.player_id
        admin.update_fishing_settings("test", settings_with(base_success_pct=100, reaction_seconds=15))
        equip_starter(admin, pid, bait=30)
        # 拉扯畫面：五個操作都在，而且順序會變
        orders = set()
        for _ in range(12):
            view = admin.start_fishing(pid)
            clock._now = view["bite_at"]
            clock.advance(1)
            admin.hook_fish(pid, view["id"])
            p.go("fish_game")
            labels = p.labels()
            assert {"收線", "放線", "等待", "左壓", "右壓"} <= set(labels)
            orders.add(tuple(labels))
            admin.give_up_fishing(pid, view["id"])
        assert len(orders) > 1
        # 人機確認畫面：題目與 emoji 按鈕；按對就拋竿
        force_check(session_factory)
        p.go("fishing")
        p.click("開始釣魚")
        assert "人機確認" in p.text
        answer = answer_of(session_factory)
        emoji = fishing.GUARD_CREATURES[answer][0]
        assert fishing.GUARD_CREATURES[answer][1] in p.text
        button = next(c for c in p.panel.children if str(getattr(c, "emoji", "")) == emoji)
        interaction = p._interaction()
        run(button.callback(interaction))
        p._update(interaction)
        assert "你已經拋出魚竿" in p.text
    finally:
        runner.shutdown()
