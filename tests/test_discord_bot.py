"""Discord Bot 介面層：以模擬的互動物件測試（未連到真實 Discord）。

驗證：身分取自互動來源、一人一座農場（含並行）、DM 情境、所有指令都能對應到服務、
3 秒規則（defer）、錯誤只回覆安全訊息、群組轉移確認流程、偷菜對象、資料庫中斷與重試。
"""

import asyncio
import threading
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, OperationalError

from app.db.models import Farm, GameTransaction, Group, Player, RankingAward, RankingSettlement
from app.db.session import make_engine, make_session_factory
from app.discord_bot import runner as runner_mod
from app.discord_bot.commands import MSG_NEED_GUILD, MSG_NO_FARM, FarmCommands, TransferView, register
from app.discord_bot.render import Reply, to_messages
from app.discord_bot.runner import MSG_DB_DOWN, MSG_UNEXPECTED, RetryingService, ServiceRunner, player_message
from app.services.admin_service import AdminService

GUILD_A, GUILD_B = 111111111111111111, 222222222222222222
ALICE, BOB, CAROL = 900000000000000001, 900000000000000002, 900000000000000003


# ---------- 模擬互動 ----------


class FakeResponse:
    def __init__(self, log):
        self.log = log
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kw):
        assert not self._done, "同一互動只能首次回應一次"
        self._done = True
        self.log.append(("send", content, kw))

    async def send_modal(self, modal):
        assert not self._done
        self._done = True
        self.log.append(("modal", None, {"modal": modal}))

    async def defer(self, **kw):
        assert not self._done
        self._done = True
        self.log.append(("defer", None, kw))

    async def edit_message(self, **kw):
        """直接以新畫面回覆元件互動（等同 defer＋編輯原訊息，少一次往返）。"""
        assert not self._done
        self._done = True
        self.log.append(("edit", kw.get("content"), {**kw, "direct": True}))


class FakeMessage:
    def __init__(self, log):
        self.log = log

    async def edit(self, **kw):
        self.log.append(("message_edit", kw.get("content"), kw))


class FakeFollowup:
    def __init__(self, log):
        self.log = log

    async def send(self, content=None, **kw):
        self.log.append(("followup", content, kw))
        return FakeMessage(self.log)


def embed_text(kw) -> str:
    e = kw.get("embed")
    if e is None:
        return ""
    parts = [e.title or "", e.description or ""] + [f"{f.name}\n{f.value}" for f in e.fields]
    return "\n".join(parts)


class FakeInteraction:
    def __init__(self, user_id, guild_id=None, *, name="玩家", guild_name="測試伺服器"):
        self.log = []
        self.user = SimpleNamespace(id=user_id, display_name=name)
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id, name=guild_name) if guild_id else None
        self.response = FakeResponse(self.log)
        self.followup = FakeFollowup(self.log)

    async def delete_original_response(self):
        self.log.append(("delete", None, {}))

    async def edit_original_response(self, **kw):
        self.log.append(("edit", kw.get("content"), kw))

    @property
    def texts(self):
        return [t for _kind, c, kw in self.log for t in (c, embed_text(kw)) if t]

    @property
    def text(self):
        return "\n".join(self.texts)

    def last_kwargs(self):
        return [kw for kind, c, kw in self.log if kind in ("send", "followup")][-1]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


@pytest.fixture
def bot(admin):
    runner = ServiceRunner(4)
    cmds = FarmCommands(admin, runner, fast_reply_seconds=5)
    tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    register(tree, cmds, advanced=True)
    yield SimpleNamespace(cmds=cmds, tree=tree, runner=runner)
    runner.shutdown()


def command(bot, name):
    parts = name.split()
    cmd = bot.tree.get_command(parts[0])
    for part in parts[1:]:
        cmd = cmd.get_command(part)
    return cmd


def call(bot, qualified_name, interaction, /, **kwargs):
    run(command(bot, qualified_name).callback(interaction, **kwargs))
    return interaction


def start(bot, user_id, guild_id, name="玩家"):
    return call(bot, "farm-cmd start", FakeInteraction(user_id, guild_id, name=name), name=None)


# ---------- 玩家、農場與伺服器對應 ----------


def test_farm_start_maps_discord_ids_as_strings(bot, session_factory):
    out = start(bot, ALICE, GUILD_A, "Alice")
    assert "建立完成" in out.text and "測試伺服器" in out.text
    with session_factory() as s:
        player = s.scalars(select(Player)).one()
        assert (player.external_platform, player.external_user_id) == ("discord", str(ALICE))
        group = s.scalars(select(Group)).one()
        assert group.discord_guild_id == str(GUILD_A) and isinstance(group.discord_guild_id, str)
        assert s.scalar(select(Farm.home_group_id)) == group.id
    again = start(bot, ALICE, GUILD_B)
    assert "已經擁有農場" in again.text
    assert again.last_kwargs()["ephemeral"] is True


def test_concurrent_farm_start_creates_one_farm(bot, session_factory):
    async def both():
        a, b = FakeInteraction(ALICE, GUILD_A), FakeInteraction(ALICE, GUILD_B)
        await asyncio.gather(
            command(bot, "farm-cmd start").callback(a, name=None), command(bot, "farm-cmd start").callback(b, name=None)
        )
        return a, b

    a, b = run(both())
    results = sorted(["建立完成" in x.text for x in (a, b)])
    assert results == [False, True]
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Player)) == 1
        assert s.scalar(select(func.count()).select_from(Farm)) == 1


def test_dm_and_missing_farm_messages(bot):
    dm = call(bot, "farm-cmd start", FakeInteraction(ALICE), name=None)
    assert dm.text == MSG_NEED_GUILD and dm.last_kwargs()["ephemeral"] is True
    none = call(bot, "farm-cmd status", FakeInteraction(ALICE, GUILD_A))
    assert MSG_NO_FARM in none.text
    start(bot, ALICE, GUILD_A)
    # 自己農場的指令在私訊中可用；需要群組的指令則說明原因
    assert "金幣" in call(bot, "farm-cmd status", FakeInteraction(ALICE)).text
    assert call(bot, "steal targets", FakeInteraction(ALICE), page=1).text == MSG_NEED_GUILD


def test_every_command_maps_to_services(bot, clock):
    """逐一執行所有 Slash Command：不能出現 CMD 的「用法」錯誤或未預期錯誤（遊戲規則錯誤可以）。"""
    start(bot, ALICE, GUILD_A)
    start(bot, BOB, GUILD_A)
    samples = {
        "item": "wheat", "quantity": 1, "plot": 1, "crop_name": "wheat", "animal": "chicken", "number": 1,
        "order_id": 1, "quest_id": 1, "competition_id": 1, "start_plot": 1, "enabled": True,
        "target": SimpleNamespace(id=BOB),
    }
    seen = 0
    for top in bot.tree.get_commands():
        for cmd in top.commands if isinstance(top, app_commands.Group) else [top]:
            kwargs = {}
            for p in cmd.parameters:
                if not p.required:
                    continue
                kwargs[p.name] = p.choices[0] if p.choices else samples[p.name]
            out = call(bot, cmd.qualified_name, FakeInteraction(ALICE, GUILD_A), **kwargs)
            assert out.texts, cmd.qualified_name
            assert "用法：" not in out.text, (cmd.qualified_name, out.text)
            assert MSG_UNEXPECTED not in out.text, (cmd.qualified_name, out.text)
            seen += 1
    assert seen >= 50


def test_gameplay_round_trip(bot, clock, session_factory):
    start(bot, ALICE, GUILD_A)
    assert "已購買 1 顆小麥種子" in call(bot, "buy", FakeInteraction(ALICE, GUILD_A), item="小麥", quantity=1).text
    assert "種下" in call(bot, "plant", FakeInteraction(ALICE, GUILD_A), plot=1, crop_name="wheat").text
    assert "澆水" in call(bot, "water", FakeInteraction(ALICE, GUILD_A), plot="all").text
    clock.advance(61)
    assert "收成第 1 格" in call(bot, "harvest", FakeInteraction(ALICE, GUILD_A)).text
    quality = app_commands.Choice(name="普通", value="normal")
    sold = call(bot, "sell", FakeInteraction(ALICE, GUILD_A), item="wheat", quantity=2, quality=quality)
    assert "已出售 2 個小麥" in sold.text
    assert sold.texts[0].startswith("```")  # 表格輸出以程式碼區塊顯示
    with session_factory() as s:
        assert s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))
        # 在農場所屬伺服器操作：記錄群組上下文（任務與競賽計分依此判斷）
        ctx = s.scalars(select(GameTransaction.context_group_id).where(GameTransaction.event_type == "sell")).one()
        assert ctx is not None


def test_steal_target_is_discord_member(bot, clock, admin):
    start(bot, ALICE, GUILD_A)
    start(bot, BOB, GUILD_A)
    call(bot, "buy", FakeInteraction(BOB, GUILD_A), item="wheat", quantity=1)
    call(bot, "plant", FakeInteraction(BOB, GUILD_A), plot=1, crop_name="wheat")
    call(bot, "water", FakeInteraction(BOB, GUILD_A), plot="1")
    clock.advance(61)
    carol = SimpleNamespace(id=CAROL)
    no_farm = call(bot, "steal crop", FakeInteraction(ALICE, GUILD_A), target=carol, plot=1, quantity=1)
    assert "對方還沒有農場" in no_farm.text
    # 在其他伺服器（Bob 不是成員）不能偷
    other = call(bot, "steal crop", FakeInteraction(ALICE, GUILD_B), target=SimpleNamespace(id=BOB), plot=1, quantity=1)
    assert "[失敗]" in other.text
    ok = call(bot, "steal crop", FakeInteraction(ALICE, GUILD_A), target=SimpleNamespace(id=BOB), plot=1, quantity=1)
    assert "[失敗]" not in ok.text, ok.text
    again = call(bot, "steal crop", FakeInteraction(ALICE, GUILD_A), target=SimpleNamespace(id=BOB), plot=1, quantity=1)
    assert "[失敗]" in again.text  # 同一株每人限偷一次


# ---------- 群組轉移 ----------


def test_transfer_group_confirm_flow(bot, clock, admin):
    start(bot, ALICE, GUILD_A)
    same = call(bot, "farm-cmd transfer-group", FakeInteraction(ALICE, GUILD_A))
    assert "已經屬於這個伺服器" in same.text

    preview = call(bot, "farm-cmd transfer-group", FakeInteraction(ALICE, GUILD_B, guild_name="B 伺服器"))
    assert "確認要把農場轉移" in preview.text and "B 伺服器" in preview.text
    kwargs = preview.last_kwargs()
    assert kwargs["ephemeral"] is True
    view = kwargs["view"]
    assert isinstance(view, TransferView)

    async def click():
        intruder = FakeInteraction(BOB, GUILD_B)
        assert await view.interaction_check(intruder) is False
        owner1, owner2 = FakeInteraction(ALICE, GUILD_B), FakeInteraction(ALICE, GUILD_B)
        await asyncio.gather(view.confirm.callback(owner1), view.confirm.callback(owner2))
        return intruder, owner1, owner2

    intruder, owner1, owner2 = run(click())
    assert "只有發起轉移的玩家" in intruder.text
    assert sum("已從" in x.text for x in (owner1, owner2)) == 1  # 連點只執行一次
    ctx = admin.external_context("discord", str(ALICE), str(GUILD_B), "B 伺服器")
    assert admin.get_home_group(ctx.player_id)[0] == ctx.group_id

    back = call(bot, "farm-cmd transfer-group", FakeInteraction(ALICE, GUILD_A))
    assert "冷卻中" in back.text and "下次可轉移時間" in back.text
    assert "冷卻中" in call(bot, "farm-cmd group", FakeInteraction(ALICE, GUILD_A)).text


def test_transfer_cancel_and_timeout(bot, admin):
    start(bot, ALICE, GUILD_A)
    preview = call(bot, "farm-cmd transfer-group", FakeInteraction(ALICE, GUILD_B))
    view = preview.last_kwargs()["view"]
    click = FakeInteraction(ALICE, GUILD_B)
    run(view.cancel.callback(click))
    assert "已取消" in click.text
    preview2 = call(bot, "farm-cmd transfer-group", FakeInteraction(ALICE, GUILD_B))
    view2 = preview2.last_kwargs()["view"]
    run(view2.on_timeout())
    assert "確認逾時" in preview2.text
    ctx = admin.external_context("discord", str(ALICE))
    assert admin.group_transfer_status(ctx.player_id)["last_transfer_at"] is None


# ---------- 回覆流程、錯誤與資料庫中斷 ----------


def test_slow_command_defers_then_follows_up(admin):
    runner = ServiceRunner(2)
    cmds = FarmCommands(admin, runner, fast_reply_seconds=0)
    try:
        ok = FakeInteraction(ALICE, GUILD_A)
        run(cmds.respond(ok, lambda: "完成"))
        assert [k for k, _c, _kw in ok.log] == ["defer", "followup"]

        def fail():
            raise RuntimeError("SELECT secret FROM internal")

        bad = FakeInteraction(ALICE, GUILD_A)
        run(cmds.respond(bad, fail))
        kinds = [k for k, _c, _kw in bad.log]
        assert kinds == ["defer", "delete", "followup"]  # 公開的思考中訊息刪除後改送私密錯誤
        assert bad.last_kwargs()["ephemeral"] is True
        assert MSG_UNEXPECTED in bad.text and "SELECT" not in bad.text
    finally:
        runner.shutdown()


def test_database_outage_reports_safe_message(clock):
    engine = make_engine("postgresql+psycopg://farmgm:x@127.0.0.1:1/none_test")
    runner = ServiceRunner(1)
    try:
        cmds = FarmCommands(AdminService(make_session_factory(engine), clock), runner)
        out = FakeInteraction(ALICE, GUILD_A)
        run(cmds.cli(out, ["farm", "status"]))
        assert MSG_DB_DOWN in out.text and "127.0.0.1" not in out.text
        assert out.last_kwargs()["ephemeral"] is True
    finally:
        runner.shutdown()
        engine.dispose()


def test_retrying_service_only_retries_deadlocks():
    def db_error(state):
        return DBAPIError("stmt", {}, SimpleNamespace(sqlstate=state))

    calls = []

    class Svc:
        def deadlocked(self):
            calls.append(1)
            if len(calls) < 3:
                raise db_error("40P01")
            return "ok"

        def broken(self):
            calls.append(1)
            raise db_error("23505")

        def in_group_context(self, gid):
            return self

    svc = RetryingService(Svc(), sleep=lambda _s: None)
    assert svc.in_group_context(1).deadlocked() == "ok" and len(calls) == 3
    calls.clear()
    with pytest.raises(DBAPIError):
        svc.broken()
    assert len(calls) == 1
    calls.clear()
    with pytest.raises(DBAPIError):
        RetryingService(SimpleNamespace(deadlocked=lambda: (calls.append(1), (_ for _ in ()).throw(db_error("40001")))),
                        sleep=lambda _s: None).deadlocked()
    assert len(calls) == runner_mod.RETRY_ATTEMPTS
    assert player_message(OperationalError("stmt", {}, Exception("conn refused password=x"))) == MSG_DB_DOWN
    assert player_message(ValueError("boom")) == MSG_UNEXPECTED


def test_long_output_is_split_or_attached(bot):
    assert to_messages(Reply("x" * 30 + "\n")) == ["```\n" + "x" * 30 + "\n```"]
    lines = "\n".join(f"第 {i} 行 " + "資料" * 20 for i in range(60))
    chunks = to_messages(Reply(lines))
    assert chunks and len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)
    assert to_messages(Reply(lines * 10)) is None
    out = FakeInteraction(ALICE, GUILD_A)
    run(bot.cmds.respond(out, lambda: lines * 10))
    assert "附件" in out.text and out.last_kwargs()["files"]
    help_out = call(bot, "farm-help", FakeInteraction(ALICE))
    assert "/farm-cmd transfer-group" in help_out.text and "/steal crop target plot quantity" in help_out.text


# ---------- 背景結算（多程序同時執行） ----------


def test_background_settlement_concurrent_no_duplicate(admin, clock, session_factory, database_url):
    a = admin.create_group("A").id
    from tests.conftest import grow, make_farmer

    pid = make_farmer(admin, "甲", a)
    grow(admin, pid, 1, "wheat")
    clock.advance(61)
    admin.harvest_crop(pid, 1)
    admin.sell_crop(pid, "wheat", 2)
    admin.create_ranking_rule(
        "root",
        {"name": "日王", "scope": "individual", "metric": "sales", "period": "daily",
         "rewards": [{"reward_type": "gold", "amount": 100}]},
    )
    clock.advance(2 * 86400)
    engine = make_engine(database_url)  # 第二個「程序」：獨立的連線池
    other = AdminService(make_session_factory(engine), clock)
    results = []
    barrier = threading.Barrier(2)

    def go(svc):
        barrier.wait()
        results.append(svc.run_background_settlement("system"))

    try:
        threads = [threading.Thread(target=go, args=(svc,)) for svc in (admin, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 再跑一次（模擬重啟／Gateway 重連後再觸發）不會重複
        assert admin.run_background_settlement("system") == {"rankings": [], "competitions": []}
    finally:
        engine.dispose()
    created = [sid for r in results if r for sid in r["rankings"]]
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(RankingAward)) == 1  # 只有有販售的那一天發獎
        assert s.scalar(select(func.count()).select_from(RankingSettlement)) == len(created) == len(set(created))
