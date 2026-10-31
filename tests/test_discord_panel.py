"""互動式農場面板（/farm）：以模擬的互動物件點按鈕、選選單、填數量（未連到真實 Discord）。"""

import asyncio

import discord
import pytest
from discord import app_commands
from sqlalchemy import func, select

from app.db.models import Farm, GameTransaction
from app.discord_bot.commands import FarmCommands, register
from app.discord_bot.panels import FarmPanel
from app.discord_bot.runner import ServiceRunner
from app.game import balance
from app.repositories import farms as farms_repo
from app.services.admin_service import AdminService
from tests.test_discord_bot import ALICE, BOB, GUILD_A, GUILD_B, FakeInteraction, embed_text

PAGES = ("main", "plots", "backpack", "shop", "prices", "genetics", "sell", "animals", "craft", "build", "casino",
         "cave", "cave_area", "cave_log", "machines", "defense", "steal",
         "board", "rewards", "quests", "history", "group", "log", "help")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def cmds(admin):
    runner = ServiceRunner(4)
    commands = FarmCommands(admin, runner)
    yield commands
    runner.shutdown()


class Player:
    """一位玩家開著的面板；click／choose 模擬操作，text 為目前面板內容（含頂端的操作結果）。"""

    def __init__(self, cmds, user_id, guild_id=GUILD_A, name="玩家"):
        self.user_id, self.guild_id, self.name = user_id, guild_id, name
        first = self._interaction()
        run(cmds.open_panel(first))
        sent = [kw for kind, _c, kw in first.log if kind == "followup"][-1]
        assert sent["ephemeral"] is True
        self.panel: FarmPanel = sent["view"]
        self.text = embed_text(sent)

    def _interaction(self):
        return FakeInteraction(self.user_id, self.guild_id, name=self.name)

    def _update(self, interaction):
        edits = [kw for kind, _c, kw in interaction.log if kind == "edit" and "embed" in kw]
        if edits:
            self.text = embed_text(edits[-1])
        return interaction

    def labels(self):
        return [c.label for c in self.panel.children if isinstance(c, discord.ui.Button)]

    def button(self, label):
        for item in self.panel.children:
            if isinstance(item, discord.ui.Button) and label in (item.label or ""):
                return item
        raise AssertionError(f"找不到按鈕「{label}」：{self.labels()}")

    def click(self, label):
        interaction = self._interaction()
        run(self.button(label).callback(interaction))
        return self._update(interaction)

    def choose(self, placeholder, value):
        for item in self.panel.children:
            if isinstance(item, discord.ui.Select) and placeholder in (item.placeholder or ""):
                interaction = self._interaction()
                run(item.farm_handler(interaction, value))
                return self._update(interaction)
        raise AssertionError(f"找不到選單「{placeholder}」")

    def options(self, placeholder):
        for item in self.panel.children:
            if isinstance(item, discord.ui.Select) and placeholder in (item.placeholder or ""):
                return [o.value for o in item.options]
        return []

    def modal(self, label, value):
        """按下會開啟數量輸入框的按鈕，填入數值並送出。"""
        opener = self._interaction()
        run(self.button(label).callback(opener))
        modal = [kw["modal"] for kind, _c, kw in opener.log if kind == "modal"][-1]
        field = modal.children[0]
        field._value = str(value)
        submit = self._interaction()
        run(modal.on_submit(submit))
        return self._update(submit)

    def go(self, page):
        self.panel.page = page
        interaction = self._interaction()
        run(self.panel.goto(interaction, page))
        return self._update(interaction)


def make_player(cmds, user_id, guild_id=GUILD_A, name="玩家"):
    p = Player(cmds, user_id, guild_id, name)
    assert "歡迎來到農場" in p.text
    p.click("建立農場")
    assert "農場建立完成" in p.text and "Lv 1" in p.text and "農田（4 格）" in p.text
    return p


def test_default_registration_is_short():
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register(tree, FarmCommands(None, ServiceRunner(1)))
    assert sorted(c.name for c in tree.get_commands()) == ["farm", "help"]


def test_welcome_in_dm_and_named_creation(cmds, session_factory):
    dm = Player(cmds, ALICE, None)
    assert "請到 Discord 伺服器" in dm.text and "建立農場" not in dm.labels()
    p = Player(cmds, ALICE, GUILD_A)
    p.modal("自訂名稱建立", "開心農場")  # NameModal 只有一個欄位
    assert "開心農場" in p.text
    with session_factory() as s:
        assert s.scalar(select(Farm.name)) == "開心農場"


def test_every_page_renders(cmds, admin):
    p = make_player(cmds, ALICE)
    for page in PAGES:
        p.go(page)
        assert "無法顯示" not in p.text, (page, p.text)
        assert p.panel.children, page
        assert len(p.panel.children) <= 25 and all(c.row is None or c.row <= 4 for c in p.panel.children)


def test_farming_loop_with_buttons(cmds, clock, session_factory):
    p = make_player(cmds, ALICE)
    p.go("plots")
    assert "wheat" in p.options("選擇作物")
    p.choose("選擇作物", "wheat")  # 種子不夠會自動購買，種滿 4 格空地
    assert "在 4 格種下小麥（購買 4 顆種子）" in p.text and "💧" in p.text
    p.click("全部澆水")
    assert "已為 4 格澆水" in p.text and "成熟 <t:" in p.text
    clock.advance(61)
    p.go("main")
    assert "4 格可以收成" in p.text
    p.click("全部收成")
    assert "收成 4 格" in p.text
    p.go("sell")
    assert "**小麥**　×**8**" in p.text
    p.click("全部賣出")
    assert "再按一次確認" in p.text
    p.click("確認全部賣出")
    assert "賣出 8 個商品" in p.text
    with session_factory() as s:
        assert s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))


def test_shop_quantity_picker_and_custom_amount(cmds):
    p = make_player(cmds, ALICE)
    p.go("shop")
    p.choose("選擇要買的商品", "seed:carrot")
    assert "已選擇\n**🥕 胡蘿蔔種子**" in p.text
    p.click("買 5")
    assert "已購買 5 顆胡蘿蔔種子" in p.text
    p.choose("選擇要買的商品", "seed:wheat")
    p.modal("自訂數量", 3)
    assert "已購買 3 顆小麥種子" in p.text
    p.choose("選擇要買的商品", "item:auto_waterer")
    assert "[失敗]" not in p.text


def test_sell_picker_and_errors_show_inline(cmds, clock):
    p = make_player(cmds, ALICE)
    p.go("plots")
    p.choose("選擇作物", "wheat")
    p.click("全部澆水")
    clock.advance(61)
    p.click("全部收成")
    p.go("sell")
    key = p.options("選擇要賣的東西")[0]
    p.choose("選擇要賣的東西", key)
    p.click("賣 1")
    assert "已出售 1 個小麥" in p.text
    p.go("plots")
    p.click("擴建")
    assert "再按一次" in p.text
    p.click("確認擴建")
    assert "無法完成" in p.text and "金幣不足" in p.text  # 錯誤改為獨立畫面


def test_animals_and_craft_pages(cmds, admin):
    p = make_player(cmds, ALICE)
    admin.set_farm_values("root", p.panel.player_id, gold=50_000)
    p.go("animals")
    p.click("建造畜舍")
    assert "建造" in p.text and "無法完成" not in p.text
    p.choose("購買動物", "chicken")
    p.click("買 2")
    assert "無法完成" not in p.text
    p.click("全部餵食")
    assert "餵食" in p.text and "生產中" in p.text
    p.go("craft")
    p.go("build")
    p.choose("選擇要建造的設施", "mill")
    p.go("craft")
    assert "無法完成" not in p.text and "閒置中" in p.text


def test_steal_from_panel_only_same_guild(cmds, clock, admin):
    alice = make_player(cmds, ALICE, name="Alice")
    bob = make_player(cmds, BOB, name="Bob")
    bob.go("plots")
    bob.choose("選擇作物", "wheat")
    bob.click("全部澆水")
    clock.advance(61)
    alice.go("steal")
    targets = alice.options("選擇要偷的作物")
    assert targets and all(t.startswith(f"{bob.panel.player_id}|") for t in targets)
    alice.choose("選擇要偷的作物", targets[0])
    assert "無法完成" not in alice.text
    other = Player(cmds, ALICE, GUILD_B)  # 在 Bob 不在的伺服器：看不到 Bob 的作物
    other.go("steal")
    assert other.options("選擇要偷的作物") == []


def test_leaderboard_switches(cmds):
    p = make_player(cmds, ALICE, name="Alice")
    p.go("board")
    assert "Alice" in p.text and "🥇" in p.text
    p.choose("範圍", "groups")
    assert "群組總排行" in p.text
    p.choose("期間", "week")
    assert "本週" in p.text
    p.choose("範圍", "group")
    assert "本伺服器排行" in p.text and "期間" not in " ".join(
        i.placeholder or "" for i in p.panel.children if isinstance(i, discord.ui.Select))


def test_group_transfer_from_panel(cmds, admin):
    make_player(cmds, ALICE)
    p = Player(cmds, ALICE, GUILD_B)
    p.go("group")
    assert "可以轉移到這裡" in p.text
    p.click("把農場轉移到這個伺服器")
    assert "再按一次「確認轉移」" in p.text
    p.click("確認轉移")
    assert "轉移到" in p.text and "無法完成" not in p.text
    assert "你的農場屬於這個伺服器" in p.text
    back = Player(cmds, ALICE, GUILD_A)
    back.go("group")
    assert "轉移冷卻中" in back.text
    assert back.button("把農場轉移到這個伺服器").disabled


def test_only_owner_can_use_panel(cmds):
    p = make_player(cmds, ALICE)
    intruder = FakeInteraction(BOB, GUILD_A)
    assert run(p.panel.interaction_check(intruder)) is False
    assert "別人的農場面板" in intruder.text


def test_timeout_disables_panel(cmds):
    p = make_player(cmds, ALICE)
    p.panel.message = type("M", (), {"edits": [], "edit": None})()

    async def edit(**kw):
        p.panel.message.edits.append(kw)

    p.panel.message.edit = edit
    run(p.panel.on_timeout())
    assert all(c.disabled for c in p.panel.children)
    assert "逾時" in p.panel.message.edits[-1]["content"]


def test_large_farm_fits_discord_limits(cmds, session_factory):
    p = make_player(cmds, ALICE)
    with session_factory() as s:  # 上限 16 格之前建立的舊農場：直接加到 100 格（服務層不允許）
        farm_id = s.scalar(select(Farm.id))
        farms_repo.add_plots(s, farm_id, 96)
        s.commit()
    for page in ("plots", "defense", "main"):
        interaction = FakeInteraction(ALICE, GUILD_A)
        p.panel.page = page
        run(p.panel.goto(interaction, page))
        embed = [kw for kind, _c, kw in interaction.log if kind == "edit"][-1]["embed"]
        assert len(embed) <= 6000 and len(embed.description or "") <= 4096
        assert all(len(f.value) <= 1024 for f in embed.fields)


def test_shop_keeps_selection_and_shows_holdings(cmds):
    p = make_player(cmds, ALICE)
    p.go("shop")
    assert "持有" in p.text and "🧪" in p.text and "✦" in p.text
    p.choose("選擇要買的商品", "seed:carrot")
    selected = [o.label for i in p.panel.children if isinstance(i, discord.ui.Select)
                for o in i.options if o.default]
    assert selected == ["胡蘿蔔種子"] and "已選擇" in p.text
    p.click("買 5")
    assert "已購買 5 顆胡蘿蔔種子" in p.text and "現在持有 **5**" in p.text
    # 買完後仍停在胡蘿蔔，可以直接再買
    selected = [o.label for i in p.panel.children if isinstance(i, discord.ui.Select)
                for o in i.options if o.default]
    assert selected == ["胡蘿蔔種子"]
    p.click("買 1")
    assert "現在持有 **6**" in p.text


def test_overview_lists_crops_and_times(cmds):
    p = make_player(cmds, ALICE)
    p.go("plots")
    p.choose("選擇作物", "wheat")
    p.click("全部澆水")
    p.go("main")
    assert p.text.count("小麥") >= 4 and "成熟 <t:" in p.text and "📊 農田統計" in p.text


def test_error_screen_hides_page_and_returns_with_selection(cmds, admin):
    p = make_player(cmds, ALICE)
    admin.set_farm_values("root", p.panel.player_id, gold=50_000)
    p.go("craft")
    assert "還沒有任何加工設施" in p.text and p.options("開始加工") == []
    p.go("build")
    p.choose("選擇要建造的設施", "mill")
    p.go("craft")
    p.choose("開始加工", "flour")
    p.click("做 1")  # 沒有小麥：失敗
    assert p.text.startswith("⚠️ 無法完成") and "加工" not in p.text.split("\n", 1)[0]
    assert "workshop" not in p.text and "(" not in p.text  # 不顯示 CMD 指令提示
    assert [c.label for c in p.panel.children] == ["返回"]
    p.click("返回")
    assert "🏭 ✦ 加工 ✦" in p.text and "已選擇" in p.text  # 回到加工頁，保留剛才選的麵粉
    assert [o.label for i in p.panel.children if isinstance(i, discord.ui.Select) for o in i.options if o.default] \
        == ["麵粉"]


def test_shop_single_select_and_readable_prices(cmds):
    p = make_player(cmds, ALICE)
    p.go("shop")
    selects = [i for i in p.panel.children if isinstance(i, discord.ui.Select)]
    assert len(selects) == 1
    values = [o.value for o in selects[0].options]
    assert "seed:wheat" in values and "item:fertilizer" in values
    p.choose("選擇要買的商品", "item:fertilizer")
    p.click("買 1")
    assert "化肥" in p.text and "現在持有 **1**" in p.text
    p.go("prices")
    assert "```" not in p.text and "黃金作物" in p.text and "珍稀變種" in p.text
    p.go("genetics")
    assert "```" not in p.text and "傳說" in p.text and "巨型作物" in p.text


def test_shop_luxury_confirm_and_warehouse(cmds, admin):
    p = make_player(cmds, ALICE)
    admin.set_farm_values("root", p.panel.player_id, gold=2_000_000)
    p.go("shop")
    assert "收成 ×2 🌾小麥" in p.text and "🎒 持有 **0**" in p.text and "🔒 未擁有" in p.text
    p.choose("選擇要買的商品", "item:auto_seller")
    price = balance.ITEMS[balance.TOOL_AUTO_SELLER][1]
    assert [c.label for c in p.panel.children if isinstance(c, discord.ui.Button)][0] == f"確認購買（{price:,} 金）"
    p.click("確認購買")
    assert "已購買 1 個自動販售機" in p.text
    assert "item:auto_seller" not in p.options("選擇要買的商品")  # 已擁有的工具不再出現在選單
    p.choose("選擇要買的商品", "special:warehouse")
    p.click("確認購買")
    assert "倉庫擴大為 100 格" in p.text and "20,000" in p.text


def test_casino_bet_from_panel(admin, clock, session_factory):
    runner = ServiceRunner(2)
    try:
        lucky = FarmCommands(AdminService(session_factory, clock, dice=lambda: 8), runner)
        p = make_player(lucky, ALICE)
        assert "作物價格" in p.labels() and "賭場" in p.labels()
        admin.set_inventory("root", p.panel.player_id, "wheat", "rare", "none", 4)
        admin.set_inventory("root", p.panel.player_id, "carrot", "normal", "none", 9)  # 普通作物不能下注
        p.go("casino")
        assert "1–10" in p.text and "×2" in p.text and "×5" in p.text
        assert p.options("選擇要下注的作物") == ["wheat|rare|none"]
        p.choose("選擇要下注的作物", "wheat|rare|none")
        p.click("全部（4）")
        assert "小麥【稀有】 ×4" in p.text
        p.click("比大")  # 擲出 8：贏
        assert "擲出 **8**" in p.text and "你贏了" in p.text and "無法完成" not in p.text
        assert "🏆 我的賭場獎品" in p.text and "×2" in p.text
        p.go("sell")
        assert "賭場獎品" in p.text
        key = [v for v in p.options("選擇要賣的東西") if v.startswith("z|")][0]
        p.choose("選擇要賣的東西", key)
        p.click("賣 4")
        assert "已出售 4 個小麥【稀有】（賭場 ×2" in p.text
    finally:
        runner.shutdown()


def test_casino_loss_and_overview_events(admin, clock, session_factory):
    from datetime import timedelta

    runner = ServiceRunner(2)
    try:
        unlucky = FarmCommands(AdminService(session_factory, clock, dice=lambda: 8), runner)
        p = make_player(unlucky, ALICE)
        admin.create_event("root", {"name": "測試豐收", "description": "說明文字", "schedule_type": "once",
                                    "starts_at": clock.now() - timedelta(hours=1),
                                    "ends_at": clock.now() + timedelta(days=1), "free_water": 1})
        p.go("main")
        assert "現在進行中的活動" in p.text and "測試豐收" in p.text and "免澆水" in p.text and "說明文字" in p.text
        admin.set_inventory("root", p.panel.player_id, "carrot", "normal", "golden", 1)
        p.go("casino")
        p.choose("選擇要下注的作物", "carrot|normal|golden")
        p.choose("猜一個數字", "3")  # 擲出 8：沒猜中
        assert "你輸了" in p.text and "✅" not in p.text.split("\n")[1]  # 輸了不顯示成功符號
        assert p.options("選擇要下注的作物") == []
    finally:
        runner.shutdown()


# ---------- 圖片 ----------


def last_edit(interaction):
    return [kw for kind, _c, kw in interaction.log if kind == "edit"][-1]


def attachment_names(kw):
    return [f.filename for f in kw.get("attachments", [])]


def test_pages_attach_images(cmds):
    p = make_player(cmds, ALICE)
    for page, expected in (("main", 1), ("plots", 1), ("shop", 1), ("animals", 1), ("craft", 1), ("build", 1),
                           ("casino", 1), ("cave_area", 1), ("cave", 0), ("backpack", 0), ("board", 0), ("help", 0)):
        interaction = p.go(page)
        kw = last_edit(interaction)
        assert attachment_names(kw) == ["farm.png"] * expected, page
        assert (kw["embed"].image.url == "attachment://farm.png") == bool(expected), page


def test_open_panel_sends_farm_image(cmds):
    make_player(cmds, ALICE)
    first = FakeInteraction(ALICE, GUILD_A)
    run(cmds.open_panel(first))
    sent = [kw for kind, _c, kw in first.log if kind == "followup"][-1]
    assert [f.filename for f in sent["files"]] == ["farm.png"]


def test_missing_assets_fall_back_to_text(cmds, tmp_path, monkeypatch):
    from app.discord_bot import farm_image

    p = make_player(cmds, ALICE)
    monkeypatch.setattr(farm_image, "ASSETS", tmp_path)
    farm_image.reset_cache()
    try:
        for page in ("main", "plots", "shop", "animals"):
            interaction = p.go(page)
            kw = last_edit(interaction)
            assert "無法顯示" not in p.text and kw["attachments"] == [], page
    finally:
        monkeypatch.undo()
        farm_image.reset_cache()


def test_upgrade_button_disabled_at_max_plots(cmds, admin):
    p = make_player(cmds, ALICE)
    admin.add_plots("root", p.panel.player_id, balance.MAX_PLOTS - balance.INITIAL_PLOTS)
    p.go("plots")
    assert p.button("已達上限").disabled


def test_upgrade_adds_one_plot_and_plant_select_is_registered_before_edit(cmds, admin, session_factory):
    p = make_player(cmds, ALICE)
    with session_factory.begin() as s:
        s.execute(Farm.__table__.update().values(gold=10_000))
    p.go("plots")
    p.choose("選擇作物", "wheat")
    assert p.options("選擇作物") == []  # 種滿後沒有種植選單
    assert p.button(f"擴建（{balance.UPGRADE_PRICES[0]} 金）")
    p.click("擴建")
    p.click("確認擴建")
    assert "農田（5 格）" in p.text and "新增第 5 格" in p.text
    assert p.button(f"擴建（{balance.UPGRADE_PRICES[1]:,} 金）")

    # 擴建後才出現的種植選單，必須在編輯訊息之前就登記到 View store，否則玩家太快點會「此交互失敗」
    order = []

    class State:
        def store_view(self, view, message_id):
            order.append(("store", message_id, [c.custom_id for c in view.children]))

    interaction = p._interaction()
    interaction._state, interaction.message = State(), type("Msg", (), {"id": 42})()
    real_edit = interaction.edit_original_response

    async def edit(**kw):
        order.append(("edit",))
        await real_edit(**kw)

    interaction.edit_original_response = edit
    run(p.panel.refresh(interaction))
    select = next(c for c in p.panel.children if isinstance(c, discord.ui.Select))
    assert order[0][:2] == ("store", 42) and select.custom_id in order[0][2]
    assert order[1] == ("edit",)
    p._update(interaction)
    p.choose("選擇作物", "wheat")
    assert "在 1 格種下小麥" in p.text


# ---------- 山洞冒險 ----------


def cave_texts(p):
    return [c.label for c in p.panel.children if isinstance(c, discord.ui.Button)]


def test_cave_entry_areas_and_explore(cmds, admin, session_factory, clock):
    admin._explore_rng = lambda: 0.0  # 每份獎勵都是木材 ×2
    p = make_player(cmds, ALICE)
    assert "山洞" in p.labels()
    assert not {"青青草原", "古老森林", "神秘礦坑"} & set(p.labels())  # 主選單不直接顯示探索區域
    p.click("山洞")
    assert "山洞冒險" in p.text and "Lv 1" in p.text and "一次只能去一個區域" in p.text
    assert {"青青草原", "古老森林", "神秘礦坑", "探索紀錄", "返回主選單"} <= set(p.labels())
    assert "🔒 農場 Lv.5 解鎖" in p.text and "🔒 農場 Lv.10 解鎖" in p.text and "探險 8 小時" in p.text
    p.click("古老森林")
    assert "古老森林" in p.text and p.button("尚未解鎖").disabled
    p.click("返回冒險介面")
    p.click("青青草原")
    assert "✅ 可以出發" in p.text and "琥珀瓜種子" in p.text
    started = p.click("出發探險")
    assert "出發前往青青草原" in p.text and "🧭 探險中" in p.text and "領取探險獎勵" not in p.labels()
    assert attachment_names(last_edit(started)) == ["farm.png"]  # 探險中顯示所在區域的圖
    p.click("青青草原")
    assert p.button("探險中").disabled
    # 直接再呼叫一次出發（模擬重送的互動）會被擋下，也不會先發獎
    run(p.panel.act(p._interaction(), lambda: p.panel._explore("grassland")))
    p.go("backpack")
    assert "木材" not in p.text
    clock.advance(8 * 3600)
    p.go("cave")
    assert "探險回來了" in p.text
    claimed = p.click("領取探險獎勵")
    assert attachment_names(last_edit(claimed)) == ["farm.png"]
    assert "從青青草原探險回來了" in p.text and "探索結果" in p.text and "木材" in p.text and "×**6**" in p.text
    assert {"返回冒險介面", "返回主選單"} <= set(p.labels())
    p.click("返回冒險介面")
    assert "✅ 可以出發" in p.text and "領取探險獎勵" not in p.labels()
    p.go("backpack")
    assert "木材　×**6**" in p.text
    p.go("cave_log")
    assert "青青草原" in p.text and "木材 ×6" in p.text
    p.click("返回冒險介面")
    p.click("返回主選單")
    assert "農田" in p.text and "山洞" in p.labels()


def test_cave_other_area_blocked_while_exploring(cmds, admin, session_factory, clock):
    admin._explore_rng = lambda: 0.0
    p = make_player(cmds, ALICE)
    with session_factory.begin() as s:
        s.scalar(select(Farm)).level = 5
    p.go("cave")
    p.click("青青草原")
    p.click("出發探險")
    p.click("古老森林")
    assert "正在青青草原探險" in p.text and p.button("正在其他區域探險").disabled
    clock.advance(8 * 3600)
    p.click("返回冒險介面")
    p.click("青青草原")
    p.click("領取探險獎勵")  # 區域頁也可以直接領取
    assert "探索結果" in p.text
    p.click("返回冒險介面")
    p.click("古老森林")
    p.click("出發探險")
    assert "出發前往古老森林" in p.text and "探險 16 小時" in p.text


def test_cave_double_click_starts_once(cmds, admin, session_factory):
    admin._explore_rng = lambda: 0.0
    p = make_player(cmds, ALICE)
    p.go("cave")
    p.click("青青草原")
    button = p.button("出發探險")

    async def both():
        import asyncio
        await asyncio.gather(button.callback(p._interaction()), button.callback(p._interaction()))

    run(both())
    with session_factory() as s:
        from app.db.models import ExplorationLog
        assert s.scalar(select(func.count()).select_from(ExplorationLog)) == 1


def test_cave_players_are_independent(cmds, admin):
    admin._explore_rng = lambda: 0.0
    alice, bob = make_player(cmds, ALICE), make_player(cmds, BOB, name="小明")
    alice.go("cave")
    alice.click("青青草原")
    alice.click("出發探險")
    bob.go("cave")
    assert "✅ 可以出發" in bob.text  # Alice 的探險不影響 Bob
    other = FakeInteraction(BOB, GUILD_A)
    run(alice.panel.interaction_check(other))  # 別人不能操作 Alice 的面板
    assert "別人的農場面板" in other.text


def test_machines_page_build_and_fake_coin_exchange(cmds, admin, session_factory):
    from app.db.models import Farm
    from app.repositories import inventory as inventory_repo
    p = make_player(cmds, ALICE)
    with session_factory.begin() as s:
        farm = s.scalar(select(Farm))
        farm.level = 3
        for item, n in {"wood": 10, "stone": 10, "copper_ore": 5, "gear": 2, "fake_coin": 3, "wheat": 2}.items():
            inventory_repo.add(s, farm.id, item, n)
    p.go("craft")
    p.click("機器")
    assert "種子製造機" in p.text and "❌齒輪 2/3" in p.text
    p.choose("製作機器", "seed_maker")
    assert "材料不足，還缺：齒輪 ×1" in p.text
    with session_factory.begin() as s:
        inventory_repo.add(s, s.scalar(select(Farm.id)), "gear", 1)
    p.click("返回")
    p.choose("製作機器", "seed_maker")
    assert "種子製造機製作完成" in p.text and "停用種子製造機" in p.labels()
    p.choose("使用機器", "seed:wheat")
    p.click("用 1")
    assert "做出 2 顆小麥種子" in p.text
    p.go("backpack")
    assert "假幣　×**3**" in p.text
    p.click("兌換假幣")
    p.click("換 1")
    assert "獲得 100 金幣" in p.text
    with session_factory() as s:
        assert s.scalar(select(Farm.gold)) == 600
