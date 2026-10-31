"""Discord Slash Commands：只負責接收互動、驗證情境、呼叫遊戲服務與格式化回覆。

- 身分一律取自互動來源：玩家 = interaction.user.id，群組 = interaction.guild_id；不接受玩家輸入的 ID。
  偷菜對象以 Discord 成員選擇器指定，再由服務層驗證是同一群組成員。
- 遊戲規則與文字輸出重用 CMD（app/cli/commands.CommandHandler）與同一批服務方法，不另寫一套邏輯；
  參數由程式組成清單直接傳入（不經字串解析），只開放下列對應的子指令。
- 服務呼叫在 ServiceRunner 的執行緒池中執行；FAST_REPLY_SECONDS 內完成就直接回覆，
  否則先 defer（Discord 規定 3 秒內必須首次回應），完成後再 followup。
- 交易完成後才回覆 Discord：不會在持有資料庫鎖時等待 Discord 網路。
"""

import asyncio
import io
import logging
from collections.abc import Callable
from dataclasses import dataclass

import discord
from discord import app_commands

from app.cli.commands import CommandHandler
from app.clock import TAIPEI_TZ
from app.discord_bot.panels import FarmPanel, help_embed
from app.discord_bot.render import Reply, to_messages
from app.discord_bot.runner import RetryingService, ServiceRunner, player_message
from app.game import balance
from app.game.errors import GameError, ValidationError
from app.services.game_service import long_duration

logger = logging.getLogger(__name__)

PLATFORM = "discord"
FAST_REPLY_SECONDS = 2.0
CONFIRM_TIMEOUT_SECONDS = 60
NO_MENTIONS = discord.AllowedMentions.none()

MSG_NEED_GUILD = "這個指令需要在 Discord 伺服器中使用（伺服器就是遊戲中的群組）；私訊中無法判斷群組。"
MSG_NO_FARM = "你還沒有農場，請在 Discord 伺服器中使用 /farm start 建立（農場會屬於該伺服器的群組）。"


@dataclass(frozen=True)
class Who:
    """在事件迴圈中從互動取出的身分資料（執行緒中不碰 discord 物件）。"""

    user_id: str
    user_name: str
    guild_id: str | None
    guild_name: str | None


def who(interaction: discord.Interaction) -> Who:
    guild = interaction.guild
    return Who(
        str(interaction.user.id),
        interaction.user.display_name,
        str(interaction.guild_id) if interaction.guild_id is not None else None,
        guild.name if guild is not None else None,
    )


def _local(dt) -> str:
    return dt.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M") if dt else "-"


class FarmCommands:
    def __init__(self, service, runner: ServiceRunner, *, fast_reply_seconds: float = FAST_REPLY_SECONDS) -> None:
        # 每個公開服務方法 = 一筆交易，遇到死結／序列化失敗會在執行緒中有限次重試
        self.service = RetryingService(service)
        self.runner = runner
        self.fast_reply_seconds = fast_reply_seconds
        self.tree: app_commands.CommandTree | None = None

    # ---------- 回覆流程 ----------

    async def respond(
        self, interaction: discord.Interaction, work: Callable[[], str | Reply], *, ephemeral: bool = False
    ) -> None:
        task = asyncio.ensure_future(self.runner.run(work))
        deferred = False
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(task), self.fast_reply_seconds)
            except TimeoutError:
                deferred = True
                try:
                    await interaction.response.defer(ephemeral=ephemeral, thinking=True)
                except discord.HTTPException as exc:  # 互動已失效：仍等交易完成，不中斷資料庫工作
                    logger.warning("defer 失敗：%s", type(exc).__name__)
                result = await task
        except Exception as exc:
            if not isinstance(exc, GameError):
                logger.exception("指令執行失敗（使用者 %s）", interaction.user.id)
            await self._send(interaction, Reply(f"[失敗] {player_message(exc)}", code=False), True, deferred, ephemeral)
            return
        reply = result if isinstance(result, Reply) else Reply(result)
        await self._send(interaction, reply, ephemeral, deferred, ephemeral)

    async def _send(
        self, interaction: discord.Interaction, reply: Reply, ephemeral: bool, deferred: bool, deferred_ephemeral: bool
    ) -> None:
        if deferred and ephemeral and not deferred_ephemeral:
            # 公開的「思考中」訊息無法改成只有自己看得到：刪除後另送私密訊息
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
        view = reply.view_factory() if reply.view_factory else None
        messages = to_messages(reply)
        files = []
        if messages is None:
            files = [discord.File(io.BytesIO(reply.text.encode("utf-8")), filename="farm.txt")]
            messages = ["內容較長，請查看附件。"]
        try:
            for i, content in enumerate(messages):
                kwargs = {"ephemeral": ephemeral, "allowed_mentions": NO_MENTIONS}
                last = i == len(messages) - 1
                if last and files:
                    kwargs["files"] = files
                if last and view is not None:
                    kwargs["view"] = view
                if i == 0 and not deferred and not interaction.response.is_done():
                    await interaction.response.send_message(content, **kwargs)
                else:
                    await interaction.followup.send(content, **kwargs)
        except discord.HTTPException as exc:
            # 回覆失敗不影響已提交的資料；只記錄（不重試，避免重複訊息）
            logger.warning("回覆 Discord 失敗：%s %s", type(exc).__name__, getattr(exc, "status", ""))

    async def deny(self, interaction: discord.Interaction, message: str) -> None:
        await self._send(interaction, Reply(message, code=False), True, False, True)

    # ---------- 共用：執行 CMD 對應的指令 ----------

    def _handler(self, w: Who, *, needs_farm: bool) -> CommandHandler:
        ctx = self.service.external_context(PLATFORM, w.user_id, w.guild_id, w.guild_name)
        if needs_farm and not ctx.has_farm:
            raise ValidationError(MSG_NO_FARM)
        handler = CommandHandler(self.service)
        handler.player_id = ctx.player_id
        handler.group_id = ctx.group_id
        return handler

    async def cli(
        self,
        interaction: discord.Interaction,
        args: list,
        *,
        needs_farm: bool = True,
        needs_guild: bool = False,
        ephemeral: bool = False,
    ) -> None:
        w = who(interaction)
        if needs_guild and w.guild_id is None:
            await self.deny(interaction, MSG_NEED_GUILD)
            return
        args = [str(a) for a in args if a is not None]

        def work() -> str:
            return self._handler(w, needs_farm=needs_farm).execute(args)

        await self.respond(interaction, work, ephemeral=ephemeral)

    # ---------- 農場建立與群組轉移 ----------

    async def farm_start(self, interaction: discord.Interaction, name: str | None) -> None:
        w = who(interaction)
        if w.guild_id is None:
            await self.deny(interaction, MSG_NEED_GUILD)
            return

        def work() -> Reply:
            st = self.service.start_external_farm(PLATFORM, w.user_id, w.user_name, w.guild_id, w.guild_name, name)
            return Reply(
                f"農場「{st.name}」建立完成！所屬群組：「{w.guild_name or '此伺服器'}」。\n"
                f"金幣 {st.gold}，土地 {len(st.plots)} 格，倉庫容量 {st.warehouse_capacity}。\n"
                f"之後可在其他伺服器使用 /farm transfer-group 把農場轉移過去"
                f"（每次轉移後需等待 {long_duration(balance.GROUP_TRANSFER_COOLDOWN_SECONDS)}）。"
                "輸入 /help 查看所有指令。",
                code=False,
            )

        await self.respond(interaction, work)

    async def farm_group(self, interaction: discord.Interaction) -> None:
        w = who(interaction)

        def work() -> Reply:
            handler = self._handler(w, needs_farm=True)
            st = self.service.group_transfer_status(handler.player_id)
            if st["can_transfer"]:
                note = "目前可以轉移：在要轉入的伺服器使用 /farm transfer-group。"
            else:
                note = (
                    f"轉移冷卻中，還需要 {long_duration(st['remaining_seconds'])}，"
                    f"下次可轉移時間 {_local(st['next_transfer_at'])}（台灣時間）。"
                )
            last = _local(st["last_transfer_at"]) if st["last_transfer_at"] else "從未轉移"
            return Reply(
                f"農場所屬群組：「{st['home_group_name']}」\n上次轉移：{last}\n{note}", code=False
            )

        await self.respond(interaction, work, ephemeral=True)

    async def farm_transfer(self, interaction: discord.Interaction) -> None:
        w = who(interaction)
        if w.guild_id is None:
            await self.deny(interaction, MSG_NEED_GUILD)
            return
        owner_id = interaction.user.id

        def work() -> Reply:
            ctx = self.service.external_context(PLATFORM, w.user_id, w.guild_id, w.guild_name)
            if not ctx.has_farm:
                raise ValidationError(MSG_NO_FARM)
            st = self.service.group_transfer_status(ctx.player_id, ctx.group_id)
            if st["home_group_id"] == ctx.group_id:
                raise ValidationError("你的農場已經屬於這個伺服器的群組，不需要轉移。")
            if not st["can_transfer"]:
                raise ValidationError(
                    f"群組轉移冷卻中：還需要 {long_duration(st['remaining_seconds'])}，"
                    f"下次可轉移時間 {_local(st['next_transfer_at'])}（台灣時間）。"
                )
            cooldown = long_duration(balance.GROUP_TRANSFER_COOLDOWN_SECONDS)
            text = (
                f"確認要把農場轉移到這個伺服器嗎？\n"
                f"目前所屬群組：「{st['home_group_name']}」\n"
                f"轉移到：「{st['target_group_name']}」（這個伺服器）\n"
                f"影響：只變更農場所屬群組，金幣、物品、土地、動物都不變；之後的群組排行、任務與競賽計分改以新群組計算，"
                f"已結算的獎勵與歷史紀錄不會改寫。\n"
                f"轉移後需等待 {cooldown} 才能再次轉移。請在 {CONFIRM_TIMEOUT_SECONDS} 秒內確認。"
            )
            player_id, group_id = ctx.player_id, ctx.group_id
            return Reply(
                text,
                code=False,
                view_factory=lambda: TransferView(self, interaction, owner_id, player_id, group_id),
            )

        await self.respond(interaction, work, ephemeral=True)

    # ---------- 互動面板 ----------

    async def open_panel(self, interaction: discord.Interaction) -> None:
        """開啟私密的農場面板。先 defer（3 秒規則），讀取資料後以 followup 送出面板。"""
        w = who(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            ctx = await self.runner.run(self.service.external_context, PLATFORM, w.user_id, w.guild_id, w.guild_name)
        except Exception as exc:
            if not isinstance(exc, GameError):
                logger.exception("開啟面板失敗（使用者 %s）", interaction.user.id)
            await interaction.followup.send(f"❌ {player_message(exc)}", ephemeral=True)
            return
        panel = FarmPanel(self, interaction.user.id, w, ctx)
        avatar = getattr(interaction.user, "display_avatar", None)
        panel.avatar_url = str(avatar.url) if avatar is not None else None
        embed = await panel.render()
        try:
            panel.message = await interaction.followup.send(
                embed=embed, view=panel, ephemeral=True, wait=True, **({"files": panel.files} if panel.files else {})
            )
        except discord.HTTPException as exc:
            logger.warning("送出面板失敗：%s %s", type(exc).__name__, getattr(exc, "text", "")[:300])

    # ---------- 偷菜（對象以成員選擇器指定） ----------

    async def steal(self, interaction: discord.Interaction, target: discord.abc.User, plot: int, quantity: int) -> None:
        w = who(interaction)
        if w.guild_id is None:
            await self.deny(interaction, MSG_NEED_GUILD)
            return
        target_id = str(target.id)

        def work() -> str:
            handler = self._handler(w, needs_farm=True)
            victim = self.service.get_player_by_external(PLATFORM, target_id)
            if victim is None or not victim.has_farm:
                raise ValidationError("對方還沒有農場。")
            return handler.execute(["steal", str(victim.id), str(plot), str(quantity)])

        await self.respond(interaction, work)

    # ---------- 說明 ----------

    def help_text(self) -> str:
        lines = ["===== 農場指令（/指令 參數；[ ] 為選填）====="]
        for cmd in sorted(self.tree.get_commands(), key=lambda c: c.name) if self.tree else []:
            subs = cmd.commands if isinstance(cmd, app_commands.Group) else [cmd]
            for sub in subs:
                params = " ".join(p.name if p.required else f"[{p.name}]" for p in sub.parameters)
                lines.append(f"/{sub.qualified_name} {params}".rstrip() + f"  {sub.description}")
        lines += [
            "",
            "‧ 每個 Discord 帳號只有一座全域農場，建立時屬於執行 /farm start 的伺服器。",
            "‧ 只有在農場所屬的伺服器中進行的操作，才會計入該群組的任務與競賽。",
            "‧ 作物、道具可輸入代碼或中文名稱，例如 wheat／小麥、fertilizer／化肥。",
        ]
        return "\n".join(lines)


class TransferView(discord.ui.View):
    """群組轉移確認按鈕：只有發起者可以按；逾時自動取消；重複點擊只會執行一次（資料庫另有冷卻保護）。"""

    def __init__(self, cmds: FarmCommands, origin: discord.Interaction, owner_id: int, player_id: int, group_id: int):
        super().__init__(timeout=CONFIRM_TIMEOUT_SECONDS)
        self.cmds = cmds
        self.origin = origin
        self.owner_id = owner_id
        self.player_id = player_id
        self.group_id = group_id
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("只有發起轉移的玩家可以確認。", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, content: str) -> None:
        self.stop()
        try:
            await interaction.edit_original_response(content=content, view=None)
        except discord.HTTPException as exc:
            logger.warning("更新轉移確認訊息失敗：%s", type(exc).__name__)

    @discord.ui.button(label="確認轉移", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.done:
            await interaction.response.defer()
            return
        self.done = True
        await interaction.response.defer()  # 先確認收到按鈕，再執行交易
        try:
            result = await self.cmds.runner.run(self.cmds.service.transfer_home_group, self.player_id, self.group_id)
            content = result.message
        except Exception as exc:
            if not isinstance(exc, GameError):
                logger.exception("群組轉移失敗（玩家 #%s）", self.player_id)
            content = f"[失敗] {player_message(exc)}"
        await self._finish(interaction, content)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.done = True
        await interaction.response.defer()
        await self._finish(interaction, "已取消轉移，農場所屬群組沒有變更。")

    async def on_timeout(self) -> None:
        if not self.done:
            self.done = True
            try:
                await self.origin.edit_original_response(content="確認逾時，已取消轉移。", view=None)
            except discord.HTTPException:
                pass


# ---------- 指令定義 ----------

KINDS = [
    app_commands.Choice(name="金幣", value="money"),
    app_commands.Choice(name="農產品販售", value="sales"),
    app_commands.Choice(name="商品總販售", value="goods"),
]
PERIODS = [
    app_commands.Choice(name="全部期間", value="all"),
    app_commands.Choice(name="本日", value="day"),
    app_commands.Choice(name="本週", value="week"),
    app_commands.Choice(name="本月", value="month"),
]
QUALITIES = [app_commands.Choice(name=n, value=c) for c, n in balance.QUALITIES.items()]
MUTATIONS = [app_commands.Choice(name=n, value=c) for c, n in balance.MUTATIONS.items() if c != "variant"]
TOOLS = [app_commands.Choice(name=v[0], value=c) for c, v in balance.ITEMS.items() if v[2] == "tool"]
USABLE = [
    app_commands.Choice(name=balance.ITEMS[c][0], value=c) for c in (balance.ITEM_FERTILIZER, balance.ITEM_EARTHWORM)
]
DEFENSE_ITEMS = [app_commands.Choice(name=v[0], value=c) for c, v in balance.DEFENSES.items()] + [
    app_commands.Choice(name=balance.ITEMS[balance.ITEM_PATROL_TICKET][0], value=balance.ITEM_PATROL_TICKET)
]
PLACEABLE = [app_commands.Choice(name=v[0], value=c) for c, v in balance.DEFENSES.items() if v[4] is not None]
BUILDINGS = [app_commands.Choice(name=v[0], value=c) for c, v in balance.BUILDINGS.items()]
RECIPES = [app_commands.Choice(name=v[0], value=c) for c, v in balance.RECIPES.items()]
INVENTORY_VIEWS = [
    app_commands.Choice(name="全部", value="all"),
    app_commands.Choice(name="可堆疊物品", value="items"),
    app_commands.Choice(name="工具與效果", value="tools"),
]
Plot = app_commands.Range[int, 1, 10_000]
Quantity = app_commands.Range[int, 1, balance.MAX_TRADE_QUANTITY]
# 任務、競賽、訂單編號（遊戲內流水號）；Discord 整數選項上限需在 JavaScript 安全整數內，取 32 位元上限
RecordId = app_commands.Range[int, 1, 2**31 - 1]


def _v(choice):
    return choice.value if isinstance(choice, app_commands.Choice) else choice


def register(tree: app_commands.CommandTree, cmds: FarmCommands, *, advanced: bool = False) -> None:
    """建立 Slash Commands：預設只有 /farm（互動面板）與 /help；advanced=True 時另外註冊完整的文字指令。"""
    cmds.tree = tree

    @tree.command(name="farm", description="打開你的農場面板（種田、背包、商店、動物、排行…全部用按鈕操作）")
    async def farm_panel(interaction: discord.Interaction):
        await cmds.open_panel(interaction)

    @tree.command(name="help", description="遊戲玩法說明")
    async def help_(interaction: discord.Interaction):
        embed = help_embed()
        if advanced:
            embed.add_field(name="⌨️ 文字指令", value="另有完整的文字指令，輸入 / 查看。", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    if advanced:
        register_advanced(tree, cmds)


def register_advanced(tree: app_commands.CommandTree, cmds: FarmCommands) -> None:
    """完整的文字指令（與 CMD 一一對應），預設不註冊，避免指令清單過長。"""

    @tree.command(name="farm-help", description="列出所有文字指令")
    async def text_help(interaction: discord.Interaction):
        await cmds.respond(interaction, lambda: cmds.help_text(), ephemeral=True)

    # ----- 農場 -----
    farm = app_commands.Group(name="farm-cmd", description="農場（文字指令）")

    @farm.command(name="start", description="在這個伺服器建立你的農場（每個 Discord 帳號只能有一座）")
    @app_commands.describe(name="農場名稱（選填）")
    async def farm_start(interaction: discord.Interaction, name: app_commands.Range[str, 1, 40] | None = None):
        await cmds.farm_start(interaction, name)

    @farm.command(name="status", description="查看農場狀態")
    async def farm_status(interaction: discord.Interaction):
        await cmds.cli(interaction, ["farm", "status"])

    @farm.command(name="plots", description="查看土地與作物進度")
    async def farm_plots(interaction: discord.Interaction):
        await cmds.cli(interaction, ["plots"])

    @farm.command(name="group", description="查看農場所屬群組與轉移冷卻")
    async def farm_group(interaction: discord.Interaction):
        await cmds.farm_group(interaction)

    @farm.command(name="transfer-group", description="把農場轉移到這個伺服器（每次轉移後需等待 7 天）")
    async def farm_transfer(interaction: discord.Interaction):
        await cmds.farm_transfer(interaction)

    @farm.command(
        name="upgrade",
        description=(
            f"擴建土地（+{balance.UPGRADE_PLOTS_ADDED} 格，{balance.UPGRADE_PRICES[0]} 金幣起逐次漲價，"
            f"最多 {balance.MAX_PLOTS} 格）"
        ),
    )
    async def farm_upgrade(interaction: discord.Interaction):
        await cmds.cli(interaction, ["upgrade"])

    @farm.command(name="log", description="查看最近的操作紀錄")
    async def farm_log(interaction: discord.Interaction, count: app_commands.Range[int, 1, 50] = 10):
        await cmds.cli(interaction, ["log", count], ephemeral=True)

    tree.add_command(farm)

    # ----- 商店、市場與作物資料 -----
    @tree.command(name="shop", description="種子、道具價格與農產品售價")
    async def shop(interaction: discord.Interaction):
        await cmds.cli(interaction, ["shop"], needs_farm=False)

    @tree.command(name="market", description="各作物不同品質與突變的單價")
    async def market(interaction: discord.Interaction):
        await cmds.cli(interaction, ["market", "prices"], needs_farm=False)

    crop = app_commands.Group(name="crop", description="作物資料")

    @crop.command(name="info", description="作物資料、珍稀變種與各品質售價")
    async def crop_info(interaction: discord.Interaction, crop_name: str):
        await cmds.cli(interaction, ["crop", "info", crop_name], needs_farm=False)

    @crop.command(name="quality", description="品質機率與售價倍率")
    async def crop_quality(interaction: discord.Interaction):
        await cmds.cli(interaction, ["crop", "quality"], needs_farm=False)

    @crop.command(name="mutations", description="突變機率與效果")
    async def crop_mutations(interaction: discord.Interaction):
        await cmds.cli(interaction, ["crop", "mutations"], needs_farm=False)

    tree.add_command(crop)

    @tree.command(name="event", description="進行中的限時活動")
    async def event(interaction: discord.Interaction):
        await cmds.cli(interaction, ["event", "list"], needs_farm=False)

    # ----- 種植與交易 -----
    @tree.command(name="buy", description="購買種子或道具，例如 wheat 5、fertilizer 2")
    @app_commands.describe(item="作物或道具（代碼或中文名稱）", quantity="數量")
    async def buy(interaction: discord.Interaction, item: str, quantity: Quantity):
        await cmds.cli(interaction, ["buy", item, quantity])

    @tree.command(name="plant", description="在指定土地種植（種下後需澆水）")
    async def plant(interaction: discord.Interaction, plot: Plot, crop_name: str):
        await cmds.cli(interaction, ["plant", plot, crop_name])

    @tree.command(name="water", description="澆水（土地編號或 all）")
    @app_commands.describe(plot="土地編號，或輸入 all 全部澆水")
    async def water(interaction: discord.Interaction, plot: app_commands.Range[str, 1, 6] = "all"):
        await cmds.cli(interaction, ["water", plot])

    @tree.command(name="harvest", description="收成（土地編號或 all）")
    @app_commands.describe(plot="土地編號，或輸入 all 全部收成")
    async def harvest(interaction: discord.Interaction, plot: app_commands.Range[str, 1, 6] = "all"):
        await cmds.cli(interaction, ["harvest", plot])

    # 名稱避開音樂的 /clear（清空待播清單）
    @tree.command(name="clear-withered", description="清除乾枯的作物")
    async def clear(interaction: discord.Interaction, plot: Plot):
        await cmds.cli(interaction, ["clear", plot])

    @tree.command(name="sell", description="出售農產品或商品")
    @app_commands.describe(item="作物、珍稀變種或商品名稱", quantity="數量", quality="品質", mutation="突變")
    @app_commands.choices(quality=QUALITIES, mutation=MUTATIONS)
    async def sell(
        interaction: discord.Interaction,
        item: str,
        quantity: Quantity,
        quality: app_commands.Choice[str] | None = None,
        mutation: app_commands.Choice[str] | None = None,
    ):
        await cmds.cli(interaction, ["sell", item, quantity, _v(quality), _v(mutation)])

    @tree.command(name="use", description="使用道具（化肥需指定生長中的土地）")
    @app_commands.choices(item=USABLE)
    async def use(interaction: discord.Interaction, item: app_commands.Choice[str], plot: Plot | None = None):
        await cmds.cli(interaction, ["use", item.value, plot])

    @tree.command(name="tool", description="啟用／停用永久工具")
    @app_commands.choices(tool=TOOLS)
    async def tool(interaction: discord.Interaction, tool: app_commands.Choice[str], enabled: bool):
        await cmds.cli(interaction, ["tool", "on" if enabled else "off", tool.value])

    @tree.command(name="inventory", description="查看背包")
    @app_commands.choices(view=INVENTORY_VIEWS)
    async def inventory(interaction: discord.Interaction, view: app_commands.Choice[str] | None = None):
        await cmds.cli(interaction, ["inventory", _v(view) or "all"])

    # ----- 偷菜與防守 -----
    steal = app_commands.Group(name="steal", description="偷菜（只能偷同一伺服器成員的作物）")

    @steal.command(name="targets", description="列出這個伺服器可以偷的作物")
    async def steal_targets(interaction: discord.Interaction, page: app_commands.Range[int, 1, 1000] = 1):
        await cmds.cli(interaction, ["steal", "targets", page], needs_guild=True, ephemeral=True)

    @steal.command(name="crop", description=f"偷取成員的作物（每次最多 {balance.STEAL_MAX_PER_ACTION} 個）")
    @app_commands.describe(target="要偷的成員", plot="土地編號", quantity="數量")
    async def steal_crop(
        interaction: discord.Interaction,
        target: discord.User,
        plot: Plot,
        quantity: app_commands.Range[int, 1, balance.STEAL_MAX_PER_ACTION],
    ):
        await cmds.steal(interaction, target, plot, quantity)

    tree.add_command(steal)

    defense = app_commands.Group(name="defense", description="防守設施")

    @defense.command(name="status", description="防守設施、守護範圍與各土地阻止機率")
    async def defense_status(interaction: discord.Interaction):
        await cmds.cli(interaction, ["defense", "status"])

    @defense.command(name="shop", description="防守商店")
    async def defense_shop(interaction: discord.Interaction):
        await cmds.cli(interaction, ["defense", "shop"], needs_farm=False)

    @defense.command(name="buy", description="購買防守設施或巡查券")
    @app_commands.choices(item=DEFENSE_ITEMS)
    async def defense_buy(
        interaction: discord.Interaction, item: app_commands.Choice[str], quantity: Quantity | None = None
    ):
        await cmds.cli(interaction, ["defense", "buy", item.value, quantity])

    @defense.command(name="place", description="調整稻草人或防盜圍欄的守護範圍")
    @app_commands.choices(device=PLACEABLE)
    async def defense_place(
        interaction: discord.Interaction,
        device: app_commands.Choice[str],
        start_plot: Plot,
        slot: app_commands.Range[int, 1, 10] = 1,
    ):
        await cmds.cli(interaction, ["defense", "place", device.value, start_plot, slot])

    @defense.command(name="log", description="自己農場近期被阻止的偷菜（不含偷菜者）")
    async def defense_log(interaction: discord.Interaction):
        await cmds.cli(interaction, ["defense", "log"], ephemeral=True)

    tree.add_command(defense)

    @tree.command(name="patrol", description="消耗 1 張巡查券，查看自己農場近 7 天所有偷菜嘗試")
    async def patrol(interaction: discord.Interaction):
        await cmds.cli(interaction, ["patrol"], ephemeral=True)

    # ----- 動物與加工 -----
    animal = app_commands.Group(name="animal", description="動物養殖")

    @animal.command(name="shop", description="動物商店")
    async def animal_shop(interaction: discord.Interaction):
        await cmds.cli(interaction, ["animal", "shop"], needs_farm=False)

    @animal.command(name="list", description="我的動物")
    async def animal_list(interaction: discord.Interaction):
        await cmds.cli(interaction, ["animal", "list"])

    @animal.command(name="status", description="養殖容量與狀態")
    async def animal_status(interaction: discord.Interaction):
        await cmds.cli(interaction, ["animal", "status"])

    @animal.command(name="buy", description="購買動物（需先建造畜舍）")
    async def animal_buy(
        interaction: discord.Interaction, animal: str, quantity: app_commands.Range[int, 1, balance.ANIMAL_MAX_BUY]
    ):
        await cmds.cli(interaction, ["animal", "buy", animal, quantity])

    @animal.command(name="feed", description="餵食（動物編號或 all）")
    async def animal_feed(interaction: discord.Interaction, target: app_commands.Range[str, 1, 6] = "all"):
        await cmds.cli(interaction, ["animal", "feed", target])

    @animal.command(name="collect", description="收集動物產品")
    async def animal_collect(interaction: discord.Interaction, number: app_commands.Range[int, 1, 1000]):
        await cmds.cli(interaction, ["animal", "collect", number])

    tree.add_command(animal)

    workshop = app_commands.Group(name="workshop", description="設施")

    @workshop.command(name="status", description="查看設施")
    async def workshop_status(interaction: discord.Interaction):
        await cmds.cli(interaction, ["workshop"])

    @workshop.command(name="build", description="建造畜舍、磨坊、烘焙坊、果醬工坊")
    @app_commands.choices(building=BUILDINGS)
    async def workshop_build(interaction: discord.Interaction, building: app_commands.Choice[str]):
        await cmds.cli(interaction, ["workshop", "build", building.value])

    tree.add_command(workshop)

    @tree.command(name="recipe", description="加工配方")
    async def recipe(interaction: discord.Interaction):
        await cmds.cli(interaction, ["recipe", "list"], needs_farm=False)

    craft = app_commands.Group(name="craft", description="加工")

    @craft.command(name="start", description="開始加工")
    @app_commands.choices(recipe=RECIPES)
    async def craft_start(
        interaction: discord.Interaction,
        recipe: app_commands.Choice[str],
        quantity: app_commands.Range[int, 1, balance.CRAFT_MAX_QUANTITY],
    ):
        await cmds.cli(interaction, ["craft", recipe.value, quantity])

    @craft.command(name="status", description="加工中與待領取的訂單")
    async def craft_status(interaction: discord.Interaction):
        await cmds.cli(interaction, ["craft", "status"])

    @craft.command(name="collect", description="領取加工成品")
    async def craft_collect(interaction: discord.Interaction, order_id: RecordId):
        await cmds.cli(interaction, ["craft", "collect", order_id])

    tree.add_command(craft)

    # ----- 群組、排行、任務與競賽 -----
    group = app_commands.Group(name="group", description="這個伺服器的群組")

    @group.command(name="status", description="群組基本資訊")
    async def group_status(interaction: discord.Interaction):
        await cmds.cli(interaction, ["group", "status"], needs_farm=False, needs_guild=True)

    @group.command(name="members", description="群組成員（可用名稱搜尋）")
    async def group_members(
        interaction: discord.Interaction,
        keyword: app_commands.Range[str, 1, 32] | None = None,
        page: app_commands.Range[int, 1, 1000] = 1,
    ):
        await cmds.cli(interaction, ["group", "members", keyword, page], needs_farm=False, needs_guild=True)

    tree.add_command(group)

    board = app_commands.Group(name="leaderboard", description="排行榜")

    @board.command(name="global", description="全服個人排行")
    @app_commands.choices(kind=KINDS)
    async def lb_global(interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None):
        await cmds.cli(interaction, ["leaderboard", "global", _v(kind)], needs_farm=False)

    @board.command(name="group", description="這個伺服器的群組內排行（只計算所屬群組為此伺服器的成員）")
    @app_commands.choices(kind=KINDS)
    async def lb_group(interaction: discord.Interaction, kind: app_commands.Choice[str] | None = None):
        await cmds.cli(interaction, ["leaderboard", "group", _v(kind)], needs_guild=True)

    @board.command(name="groups", description="群組總排行（各群組成員加總）")
    @app_commands.choices(kind=KINDS, period=PERIODS)
    async def lb_groups(
        interaction: discord.Interaction,
        kind: app_commands.Choice[str] | None = None,
        period: app_commands.Choice[str] | None = None,
    ):
        await cmds.cli(
            interaction, ["leaderboard", "groups", _v(kind) or "money", _v(period) or "all"], needs_farm=False
        )

    @board.command(name="rewards", description="排名獎勵規則與我獲得的獎勵")
    async def lb_rewards(interaction: discord.Interaction):
        await cmds.cli(interaction, ["leaderboard", "rewards"], needs_farm=False, ephemeral=True)

    tree.add_command(board)

    quest = app_commands.Group(name="quest", description="群組任務（在農場所屬伺服器的操作才會計分）")

    @quest.command(name="list", description="我所屬各群組的任務")
    async def quest_list(interaction: discord.Interaction):
        await cmds.cli(interaction, ["quest", "list"], ephemeral=True)

    @quest.command(name="group", description="這個伺服器的全部任務")
    async def quest_group(interaction: discord.Interaction):
        await cmds.cli(interaction, ["quest", "group"], needs_guild=True)

    @quest.command(name="status", description="任務進度、個人貢獻與獎勵")
    async def quest_status(interaction: discord.Interaction, quest_id: RecordId):
        await cmds.cli(interaction, ["quest", "status", quest_id], needs_guild=True)

    @quest.command(name="claim", description="領取已完成任務的獎勵")
    async def quest_claim(interaction: discord.Interaction, quest_id: RecordId):
        await cmds.cli(interaction, ["quest", "claim", quest_id], needs_guild=True)

    tree.add_command(quest)

    comp = app_commands.Group(name="competition", description="群組競賽")

    @comp.command(name="list", description="競賽列表")
    async def comp_list(interaction: discord.Interaction):
        await cmds.cli(interaction, ["competition", "list"], needs_farm=False)

    @comp.command(name="status", description="競賽資訊與我的貢獻")
    async def comp_status(interaction: discord.Interaction, competition_id: RecordId):
        await cmds.cli(interaction, ["competition", "status", competition_id], needs_farm=False)

    @comp.command(name="leaderboard", description="競賽的群組排名與個人貢獻")
    async def comp_board(interaction: discord.Interaction, competition_id: RecordId):
        await cmds.cli(interaction, ["competition", "leaderboard", competition_id], needs_farm=False)

    @comp.command(name="history", description="已結算競賽的最終排名")
    async def comp_history(interaction: discord.Interaction):
        await cmds.cli(interaction, ["competition", "history"], needs_farm=False)

    tree.add_command(comp)
