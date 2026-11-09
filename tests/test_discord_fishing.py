"""Discord 面板的釣魚功能：商店分頁、買釣具、拋竿 → 拉竿 → 拉扯、魚簍出售（確認）、分享到頻道、圖鑑與排行榜。"""

import discord
import pytest

from app.discord_bot.commands import FarmCommands
from app.discord_bot.runner import ServiceRunner
from app.game import fishing
from app.services.admin_service import AdminService
from tests.test_discord_bot import ALICE, BOB, GUILD_A, embed_text
from tests.test_discord_panel import Player, make_player, run


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def cmds(admin):
    runner = ServiceRunner(4)
    yield FarmCommands(admin, runner)
    runner.shutdown()

ALL_IN = {**fishing.DEFAULT_SETTINGS, "base_success_pct": 100, "reaction_seconds": 15}


def fisher(cmds, admin, user_id=ALICE, name="玩家"):
    p = make_player(cmds, user_id, name=name)
    p.panel.fish_timers_enabled = False  # 測試不等真實時間
    admin.set_farm_values("test", p.panel.player_id, gold=50_000, level=10)
    return p


def buy(p, code, quantity=None):
    p.go("fish_shop")
    p.choose("選擇要買的釣具", f"fish:{code}")
    if quantity is None:
        p.click("確認購買")
    else:
        p.click(f"買 {quantity}")
    assert "❌" not in p.text and "無法完成" not in p.text, p.text


def ready_to_fish(p):
    buy(p, "bamboo_rod")
    buy(p, "basic_line")
    buy(p, "worm_bait", 10)


def fight_until_done(p, admin, clock):
    """依畫面的 💬 提示操作，直到釣魚結束。"""
    for _ in range(200):
        view = admin.fishing_state(p.panel.player_id)
        if view["phase"] != "fighting":
            return view
        state = {k: view[k] for k in fishing.POLICY_KEYS}
        action = fishing.smart_action(state, None, {"reel_power": 10, "control_pct": 0})
        p.click(fishing.ACTION_NAMES[action])
        clock.advance(1)
    raise AssertionError("沒有結束")


def test_shop_has_two_tabs_and_gear_affects_overview(cmds, admin):
    p = fisher(cmds, admin)
    p.go("shop")
    assert "農場商店" in p.labels() and "釣具商店" in p.labels()
    assert "種子" in p.text  # 原本的商品仍在
    p.click("釣具商店")
    assert "竹製魚竿" in p.text and "傳說魚竿" in p.text and "🔒" not in p.text  # Lv.10 全部解鎖
    ready_to_fish(p)
    p.go("fishing")
    assert "竹製魚竿" in p.text and "普通魚線" in p.text and "**10** 個" in p.text and "準備釣魚" in p.text
    p.go("fish_shop")
    p.click("農場商店")
    assert "農場商店" in p.text and "種子" in p.text


def test_main_menu_has_fishing_entry(cmds, admin):
    p = fisher(cmds, admin)
    p.go("main")
    assert "釣魚" in p.labels()
    p.click("釣魚")
    assert "釣魚中心" in p.text and "尚未裝備" in p.text


def test_full_fishing_flow_sell_and_share(cmds, admin, clock):
    admin.update_fishing_settings("test", ALL_IN)
    p = fisher(cmds, admin)
    ready_to_fish(p)
    p.go("fishing")
    p.click("開始釣魚")
    assert "你已經拋出魚竿" in p.text and "水面很平靜" in p.text
    assert p.button("拉竿").disabled
    view = admin.fishing_state(p.panel.player_id)
    clock._now = view["bite_at"]
    clock.advance(0.5)
    p.go("fish_game")
    assert "魚上鉤了" in p.text and not p.button("拉竿").disabled
    p.click("拉竿")
    assert "正在與魚搏鬥" in p.text and "魚的體力" in p.text and "魚線張力" in p.text
    assert {"收線", "放線", "等待"} <= set(p.labels())
    result = fight_until_done(p, admin, clock)
    assert result["status"] == "landed"
    assert "釣到了" in p.text and "已放進魚簍" in p.text
    # 結算畫面：剩餘魚餌與魚簍容量（這隻魚已算進魚簍）
    overview = admin.fishing_overview(p.panel.player_id)
    assert overview["basket"] == 1
    assert f"剩 **{overview['bait_quantity']}** 個" in p.text
    assert f"**1 / {overview['basket_capacity']}** 隻" in p.text
    assert f"還能放 **{overview['basket_capacity'] - 1}** 隻" in p.text
    # 分享到頻道：公開訊息，魚不會消失
    share = p.click("分享到群組")
    sent = [kw for kind, _c, kw in share.log if kind == "followup"]
    assert sent and sent[-1].get("ephemeral") is not True
    card = embed_text(sent[-1])
    assert "捕獲玩家" in card and result["fish"]["name"] in card and "價值" in card
    assert "已分享到這個頻道" in p.text
    # 魚簍：單獨出售需要確認
    p.go("fish_basket")
    assert result["fish"]["name"] in p.text and "1 / 20" in p.text
    p.choose("選一隻魚", str(result["fish"]["id"]))
    p.click("出售")
    assert "再按一次" in p.text and "確認出售" in p.labels()
    gold = admin.fishing_overview(p.panel.player_id)["gold"]
    p.click("確認出售")
    assert admin.fishing_overview(p.panel.player_id)["gold"] == gold + result["fish"]["price"]
    assert "0 / 20" in p.text


def test_multi_select_and_sell_all_need_confirmation(cmds, admin, clock):
    admin.update_fishing_settings("test", ALL_IN)
    p = fisher(cmds, admin)
    ready_to_fish(p)
    for _ in range(3):
        view = admin.start_fishing(p.panel.player_id)
        clock._now = view["bite_at"]
        admin.hook_fish(p.panel.player_id, view["id"])
        while (v := admin.fishing_state(p.panel.player_id))["phase"] == "fighting":
            state = {k: v[k] for k in fishing.POLICY_KEYS}
            admin.fishing_action(p.panel.player_id, v["id"], fishing.smart_action(state, None, {"reel_power": 10}),
                                 v["turn"])
    basket = admin.fish_basket(p.panel.player_id)["fish"]
    assert len(basket) == 3
    p.go("fish_basket")
    multi = next(c for c in p.panel.children if isinstance(c, discord.ui.Select) and "勾選" in c.placeholder)
    interaction = p._interaction()
    run(multi.farm_handler(interaction, [str(basket[0]["id"]), str(basket[1]["id"])]))
    p._update(interaction)
    assert "確認出售" in p.text
    labels = p.labels()
    assert any(lbl.startswith("確認出售 2 隻") for lbl in labels)
    p.click("確認出售 2 隻")
    assert len(admin.fish_basket(p.panel.player_id)["fish"]) == 1
    p.click("全部出售")
    assert "再按一次" in p.text
    p.click("確認全部出售")
    assert admin.fish_basket(p.panel.player_id)["fish"] == []


def test_missed_bite_shows_failure(cmds, admin, clock):
    p = fisher(cmds, admin)
    ready_to_fish(p)
    p.go("fishing")
    p.click("開始釣魚")
    view = admin.fishing_state(p.panel.player_id)
    clock._now = view["react_deadline"]
    clock.advance(10)
    p.go("fish_game")
    assert "沒有及時拉竿" in p.text and "再釣一次" in p.labels()
    assert "魚餌" in p.text and "魚簍" in p.text  # 沒釣到也顯示剩餘資源


def test_double_click_does_not_repeat_action(cmds, admin, clock):
    admin.update_fishing_settings("test", ALL_IN)
    p = fisher(cmds, admin)
    ready_to_fish(p)
    view = admin.start_fishing(p.panel.player_id)
    clock._now = view["bite_at"]
    p.go("fish_game")
    p.click("拉竿")
    old_wait = p.button("等待")
    p.click("等待")
    assert admin.fishing_state(p.panel.player_id)["turn"] == 1
    interaction = p._interaction()
    run(old_wait.callback(interaction))  # 舊畫面上的同一個按鈕再按一次
    p._update(interaction)
    assert admin.fishing_state(p.panel.player_id)["turn"] == 1
    assert "已經處理過了" in p.text


def test_other_player_cannot_use_my_panel(cmds, admin):
    p = fisher(cmds, admin)
    p.go("fishing")
    intruder = Player.__new__(Player)
    intruder.user_id, intruder.guild_id, intruder.name, intruder.panel = BOB, GUILD_A, "壞人", p.panel
    interaction = intruder._interaction()
    assert run(p.panel.interaction_check(interaction)) is False


@pytest.mark.parametrize("page", ["fishing", "fish_shop", "fish_basket", "fish_codex", "fish_rank"])
def test_fishing_pages_render_within_component_limits(cmds, admin, page):
    p = fisher(cmds, admin)
    p.go(page)
    assert "無法顯示" not in p.text, p.text
    assert len(p.panel.children) <= 25 and all(c.row is None or c.row <= 4 for c in p.panel.children)
    for row in range(5):
        items = [c for c in p.panel.children if c.row == row]
        width = sum(5 if isinstance(c, discord.ui.Select) else 1 for c in items)
        assert width <= 5, (page, row, [getattr(c, "label", None) for c in items])


def test_codex_and_rank_pages(cmds, admin, clock):
    admin.update_fishing_settings("test", ALL_IN)
    p = fisher(cmds, admin)
    ready_to_fish(p)
    view = admin.start_fishing(p.panel.player_id)
    clock._now = view["bite_at"]
    admin.hook_fish(p.panel.player_id, view["id"])
    p.go("fish_game")
    result = fight_until_done(p, admin, clock)
    p.go("fish_codex")
    assert "已發現 **1 /" in p.text and result["fish"]["name"] in p.text and "❓" in p.text
    p.go("fish_rank")
    assert "本伺服器" in p.text and "玩家" in p.text
    p.click("看全部玩家")
    assert "全部玩家" in p.text


def test_auto_refresh_does_not_repeat_for_same_past_deadline(cmds, admin):
    from datetime import UTC, datetime, timedelta

    p = fisher(cmds, admin)
    panel = p.panel
    panel.fish_timers_enabled, panel.page = True, "fish_game"
    past = datetime.now(UTC) - timedelta(seconds=10)

    async def schedule_twice():
        panel.fish_due = (past, 0.0)
        panel._schedule_fish_timer(p._interaction())
        first = panel.fish_timer
        panel.fish_due = (past, 0.0)
        panel._schedule_fish_timer(p._interaction())
        second = panel.fish_timer
        if first is not None:
            first.cancel()
        return first, second

    first, second = run(schedule_twice())
    assert first is not None and second is None
