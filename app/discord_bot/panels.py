"""互動式農場面板：用 Embed、按鈕、下拉選單與數量輸入框操作整個遊戲，玩家不需要記指令。

- `/farm` 開出只有自己看得到的面板；同一則訊息依按鈕切換頁面（總覽、農田、背包、商店、販售、動物、加工、
  防守、偷菜、排行榜、任務競賽、群組、紀錄、說明）。只有開啟面板的玩家可以操作。
- 身分與群組在開啟時由互動來源決定（不接受玩家輸入的 ID）；所有操作都呼叫既有的遊戲服務，
  在 ServiceRunner 的執行緒池中執行，交易完成後才更新 Discord 訊息。
- 一鍵操作（全部澆水、全部收成、全部賣出…）是依序呼叫多個服務方法，每一步各自是一筆完整交易；
  中途失敗時已完成的步驟保留，結果訊息會列出成功與失敗的項目。
- 時間以 Discord 動態時間戳 <t:秒數:R> 顯示，玩家端會自動倒數。
- 總覽、農田、動物、加工、建造與商店頁附上圖片（farm_image，在執行緒中拼圖）；產生失敗時只顯示文字。
"""

import io
import logging
import re
from collections.abc import Callable
from datetime import datetime

import discord

from app.cli.commands import CommandHandler
from app.discord_bot import farm_image
from app.discord_bot.runner import player_message
from app.game import balance, exploration, rules
from app.game.errors import GameError, ValidationError
from app.game.rules import EVENT_TYPE_LABELS, PlotState
from app.services.game_service import genetics_label, long_duration

logger = logging.getLogger(__name__)

PANEL_TIMEOUT_SECONDS = 15 * 60  # Discord 私密訊息的互動權杖有效 15 分鐘
FARM_IMAGE = "farm.png"  # 面板圖片的附件檔名（Embed 以 attachment:// 引用）
GREEN, BLUE, GOLD, RED, GREY = 0x57F287, 0x5865F2, 0xF1C40F, 0xED4245, 0x99AAB5

CROP_EMOJI = {"wheat": "🌾", "carrot": "🥕", "tomato": "🍅", "strawberry": "🍓", "amber_melon": "🍈",
              "moonflower": "🌙"}
ANIMAL_EMOJI = {"chicken": "🐔", "cow": "🐄", "sheep": "🐑"}
MATERIAL_EMOJI = {"wood": "🪵", "stone": "🪨", "copper_ore": "🟠", "iron_ore": "⛓️", "resin": "🍯", "herb": "🌿",
                  "gear": "⚙️", "crystal": "💎", "ancient_core": "🌳", "machine_core": "🔩", "fake_coin": "🪙"}
ITEM_EMOJI = {"fertilizer": "🧪", "earthworm": "🪱", "auto_waterer": "🚿", "patrol_ticket": "🔦",
              "auto_planter": "🤖", "auto_seller": "🏧"}
PLOT_EMOJI = {
    PlotState.EMPTY: "⬜", PlotState.WAITING_WATER: "💧", PlotState.GROWING: "🌱",
    PlotState.READY: "✅", PlotState.WITHERED: "🥀",
}
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
KIND_NAMES = {"money": "金幣", "sales": "農產品販售", "goods": "商品總販售"}
PERIOD_NAMES = {"all": "全部期間", "day": "本日", "week": "本週", "month": "本月"}
SCOPE_NAMES = {"global": "個人總排行", "group": "本伺服器排行", "groups": "群組總排行"}


# ---------- 顯示工具 ----------


def ts(dt: datetime | None, style: str = "R") -> str:
    """Discord 動態時間戳（玩家端依自己的時區顯示並自動倒數）。"""
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "—"


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def bar(value: int, total: int, width: int = 10) -> str:
    filled = 0 if total <= 0 else min(width, round(width * value / total))
    return "▰" * filled + "▱" * (width - filled)


def crop_emoji(code: str | None) -> str:
    return CROP_EMOJI.get(code or "", "🌱")


def money(n: int) -> str:
    return f"{n:,}"


def lines_or(lines: list[str], empty: str, limit: int = 1024) -> str:
    """把多行文字放進 Embed 欄位（上限 1024 字），超過時截斷並註明。"""
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit - 20:
            out.append(f"…另外 {len(lines) - i} 項")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or empty


def sections(*parts) -> str:
    """把頁面內容排成有標題、有空行的區塊，避免文字擠在一起。

    每個 part 可以是文字段落，或 (標題, 行列表[, 行間分隔])；空段落與沒有內容的區塊會略過。
    """
    blocks = []
    for part in parts:
        if isinstance(part, tuple):
            title, lines = part[0], [x for x in part[1] if x]
            sep = part[2] if len(part) > 2 else "\n"
            if lines:
                blocks.append(f"**{title}**\n{sep.join(lines)}" if title else sep.join(lines))
        elif part:
            blocks.append(part)
    return cut("\n\n".join(blocks), 4000)


# CMD 介面的指令提示，例如「（workshop build 磨坊）」「（buy wheat 1）」，在 Discord 面板中沒有意義
_CLI_HINT = re.compile(r"（[a-z][a-z_]*(?: [^）]*)?）")


def clean_message(text: str) -> str:
    return _CLI_HINT.sub("", text)


def plot_line(p) -> str:
    emoji = PLOT_EMOJI[p.state]
    if p.state == PlotState.EMPTY:
        return f"{emoji} `#{p.plot_number:>2}` 空地"
    genes = genetics_label(p.quality, p.mutation) + ("【珍稀變種】" if p.mutation == "variant" else "")
    name = f"{crop_emoji(p.crop_code)} {p.crop_name}{genes}"
    if p.state == PlotState.WAITING_WATER:
        tail = "等待澆水"
    elif p.state == PlotState.GROWING:
        tail = f"成熟 {ts(p.ready_at)}" + (" 🧪" if p.fertilized else "")
    elif p.state == PlotState.READY:
        tail = f"可收成 ×{p.remaining_quantity}（{ts(rules.wither_time(p.ready_at))}乾枯）"
    else:
        tail = "已乾枯，請清除"
    return f"{emoji} `#{p.plot_number:>2}` {name} · {tail}"


def summary_counts(plots) -> str:
    counts = {s: sum(1 for p in plots if p.state == s) for s in PlotState}
    return "　".join(f"{PLOT_EMOJI[s]}{rules.PLOT_STATE_LABELS[s]} {n}" for s, n in counts.items() if n)


# ---------- 數量輸入框 ----------


class QuantityModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, maximum: int, on_submit: Callable):
        super().__init__(title=cut(title, 45), timeout=300)
        self.maximum = maximum
        self._handler = on_submit
        self.amount = discord.ui.TextInput(label=cut(label, 45), placeholder=f"1–{maximum}", max_length=6)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip()
        if not raw.isdigit() or not 1 <= int(raw) <= self.maximum:
            await interaction.response.send_message(f"請輸入 1–{self.maximum} 的整數。", ephemeral=True)
            return
        await self._handler(interaction, int(raw))


class NameModal(discord.ui.Modal):
    def __init__(self, on_submit: Callable):
        super().__init__(title="為農場取名", timeout=300)
        self._handler = on_submit
        self.name = discord.ui.TextInput(label="農場名稱", max_length=balance.FARM_NAME_MAX_LEN, required=False)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction, self.name.value.strip() or None)


# ---------- 面板 ----------


class FarmPanel(discord.ui.View):
    """一個玩家的私密農場面板。頁面內容每次都重新從資料庫讀取，網站或其他介面的修改會立即反映。"""

    def __init__(self, cmds, owner_id: int, who, ctx) -> None:
        super().__init__(timeout=PANEL_TIMEOUT_SECONDS)
        self.cmds = cmds
        self.owner_id = owner_id
        self.who = who
        self.player_id = ctx.player_id
        self.group_id = ctx.group_id
        self.group_name = ctx.group_name
        self.page = "main" if ctx.has_farm else "welcome"
        self.message = None
        # 頁面狀態：數量選擇、排行榜條件、偷菜分頁、二次確認
        self.picker: dict | None = None
        self.board = {"scope": "global", "kind": "money", "period": "all"}
        self.steal_page = 0
        self.confirm: str | None = None
        self.detail: dict | None = None
        self.bet: dict | None = None  # 賭場：已選的作物與下注數量
        self.avatar_url: str | None = None
        # 山洞：目前查看的區域、最近一次探索結果、探索進行中（擋住同一面板的連續點擊）
        self.cave_area: str | None = None
        self.cave_result: dict | None = None
        self.exploring = False
        self.files: list[discord.File] = []  # 本次畫面要附上的圖片；每次 render 重建

    @property
    def svc(self):
        # 操作所在的群組 = 開啟面板的伺服器（只做紀錄與任務計分判斷，不影響農場歸屬）
        return self.cmds.service.in_group_context(self.group_id)

    # ---------- 互動基礎 ----------

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("這是別人的農場面板，請用 /farm 開啟自己的面板。", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="面板已逾時，請重新輸入 /farm。", view=self)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.exception("面板操作失敗（使用者 %s）", interaction.user.id, exc_info=error)
        try:
            await interaction.followup.send(f"❌ {player_message(error)}", ephemeral=True)
        except discord.HTTPException:
            pass

    async def run(self, fn, *args):
        return await self.cmds.runner.run(fn, *args)

    async def render(self, notice: str | None = None) -> discord.Embed:
        """讀取目前頁面的資料並重建元件；讀取失敗時顯示錯誤並提供回總覽按鈕。"""
        self.clear_items()
        self.files = []  # 換頁時舊圖片也要移除
        if self.page != "welcome":
            notice = await self._run_machines(notice)
        try:
            data = await self.run(getattr(self, f"_load_{self.page}"))
            embed = getattr(self, f"_build_{self.page}")(data)
        except Exception as exc:
            if not isinstance(exc, GameError):
                logger.exception("面板讀取失敗（頁面 %s）", self.page)
            self.clear_items()
            self.files = []
            embed = discord.Embed(title="⚠️ 無法顯示", color=RED,
                                  description=f"### {clean_message(player_message(exc))}")
            self._nav_back(row=0)
        if notice:
            embed.description = cut(f"{notice}\n\n{embed.description or ''}".strip(), 4096)
        # Discord 限制單一 Embed 總長度 6000 字：超過時縮短主要內容（例如農田非常多時）
        overflow = len(embed) - 5900
        if overflow > 0 and embed.description:
            embed.description = cut(embed.description, max(200, len(embed.description) - overflow))
        return embed

    def _register_items(self, interaction: discord.Interaction) -> None:
        """在編輯訊息前先把新元件登記到 discord.py 的 View store。

        discord.py 要等編輯的 HTTP 回應回來才登記；附圖片時回應較慢，玩家若在這段時間點新出現的元件
        （例如擴建後才出現的種植選單），互動會被直接丟棄，Discord 顯示「此交互失敗」。"""
        state, message = getattr(interaction, "_state", None), getattr(interaction, "message", None)
        if state is not None and message is not None and self.is_dispatchable():
            state.store_view(self, message.id)

    async def _run_machines(self, notice: str | None) -> str | None:
        """自動收割機／自動加工機在開啟或切換頁面時運作；失敗不影響面板顯示。"""
        try:
            note = await self.run(self.svc.run_machines, self.player_id)
        except GameError:
            return notice
        except Exception:
            logger.exception("自動機器執行失敗（玩家 #%s）", self.player_id)
            return notice
        if not note:
            return notice
        return f"{notice}\n{note}" if notice else note

    async def refresh(self, interaction: discord.Interaction, notice: str | None = None) -> None:
        embed = await self.render(notice)
        self._register_items(interaction)
        try:
            # attachments：傳新檔案會取代舊圖，傳 [] 會移除
            await interaction.edit_original_response(embed=embed, view=self, attachments=self.files)
        except discord.HTTPException as exc:
            logger.warning("更新面板失敗：%s", type(exc).__name__)

    async def goto(self, interaction: discord.Interaction, page: str, notice: str | None = None) -> None:
        await interaction.response.defer()
        self.page, self.picker, self.confirm = page, None, None
        await self.refresh(interaction, notice)

    async def act(
        self, interaction: discord.Interaction, work: Callable[[], str], page: str | None = None, *, keep: bool = False,
        prefix: str = "✅ ",
    ) -> None:
        """執行一個操作（在執行緒中）。成功時結果以 ✅ 顯示在頁面頂端並重新整理；
        失敗時改顯示獨立的錯誤畫面，只有錯誤原因與「返回」按鈕，返回後保留剛才的選擇。"""
        if not interaction.response.is_done():
            await interaction.response.defer()
        self.confirm = None
        try:
            notice = f"{prefix}{clean_message(await self.run(work))}"
        except Exception as exc:
            if not isinstance(exc, GameError):
                logger.exception("面板操作失敗（玩家 #%s）", self.player_id)
            await self.show_error(interaction, clean_message(player_message(exc)))
            return
        if not keep:  # 購買後保留已選商品與數量按鈕，方便確認與加購
            self.picker = None
        if page:
            self.page = page
        await self.refresh(interaction, cut(notice, 1500))

    async def show_error(self, interaction: discord.Interaction, message: str) -> None:
        self.clear_items()
        embed = discord.Embed(title="⚠️ 無法完成", color=RED, description=f"### {cut(message, 1500)}")
        embed.set_footer(text="按「返回」回到剛才的畫面")

        async def back(i):
            await i.response.defer()
            await self.refresh(i)

        self.button("返回", back, emoji="↩️", style=discord.ButtonStyle.primary)
        self._register_items(interaction)
        try:
            await interaction.edit_original_response(embed=embed, view=self, attachments=[])
        except discord.HTTPException as exc:
            logger.warning("更新面板失敗：%s", type(exc).__name__)

    # ---------- 圖片 ----------

    def attach_image(self, embed: discord.Embed, png: bytes | None) -> None:
        if png:  # discord.File 只能送出一次，每次都建新的
            self.files = [discord.File(io.BytesIO(png), filename=FARM_IMAGE)]
            embed.set_image(url=f"attachment://{FARM_IMAGE}")

    def _image(self, what: str, make: Callable[[], bytes]) -> bytes | None:
        """在執行緒中產生圖片；素材缺漏或拼圖失敗不能讓面板壞掉，退回純文字。"""
        try:
            return make()
        except Exception:
            logger.exception("%s圖片產生失敗（玩家 #%s）", what, self.player_id)
            return None

    def _farm_image(self, status) -> bytes | None:
        return self._image("農田", lambda: farm_image.farm_png_from_status(status))

    def _yard_image(self, workshop, animals) -> bytes | None:
        return self._image("建築畫面", lambda: farm_image.yard_png(workshop, animals))

    # ---------- 元件工具 ----------

    def button(self, label, handler, *, emoji=None, style=discord.ButtonStyle.secondary, row=0, disabled=False):
        b = discord.ui.Button(label=cut(label, 80), emoji=emoji, style=style, row=row, disabled=disabled)
        b.callback = handler
        self.add_item(b)
        return b

    def nav(self, label, page, *, emoji=None, row=0, style=discord.ButtonStyle.secondary):
        async def go(interaction):
            await self.goto(interaction, page)

        return self.button(label, go, emoji=emoji, row=row, style=style)

    def _nav_back(self, row=4):
        self.nav("回總覽", "main", emoji="🏠", row=row)

    def select(self, placeholder, options: list[tuple], handler, *, row=0, selected: str | None = None):
        """options：(label, value, description, emoji)；最多 25 項。handler(interaction, value)。

        selected：重新整理後仍顯示為已選取的選項（Discord 每次更新訊息都會重設選單，需明確指定）。"""
        if not options:
            return None
        s = discord.ui.Select(
            placeholder=cut(placeholder, 150),
            row=row,
            options=[
                discord.SelectOption(label=cut(lbl, 100), value=val, description=cut(desc, 100) if desc else None,
                                     emoji=emo, default=val == selected)
                for lbl, val, desc, emo in options[:25]
            ],
        )

        async def cb(interaction):
            await handler(interaction, s.values[0])

        s.callback = cb
        s.farm_handler = handler  # 測試用：直接以選項值呼叫
        self.add_item(s)
        return s

    def quantity_buttons(self, row: int) -> None:
        """依 self.picker 顯示數量按鈕（1／5／10／全部／自訂）。picker：{label, max, presets, run(q) -> str}"""
        p = self.picker
        if not p:
            return
        if p.get("confirm"):  # 高價物品：只有一個確認按鈕
            async def confirm(interaction):
                await self.act(interaction, lambda: p["run"](1))

            self.button(f"確認購買（{p['price_text']}）", confirm, emoji="✅", row=row,
                        style=discord.ButtonStyle.success)
            return
        for q in p["presets"]:
            if q <= p["max"]:
                async def pick(interaction, q=q):
                    await self.act(interaction, lambda: p["run"](q), keep=p.get("keep", False))

                self.button(f"{p.get('verb', '×')} {q}", pick, row=row, style=discord.ButtonStyle.primary)

        async def custom(interaction):
            async def submitted(modal_interaction, q):
                await self.act(modal_interaction, lambda: p["run"](q), keep=p.get("keep", False))

            await interaction.response.send_modal(QuantityModal(p["label"], "數量", p["max"], submitted))

        self.button("自訂數量…", custom, emoji="✏️", row=row)

    # ======================================================================
    # 歡迎（尚未建立農場）
    # ======================================================================

    def _load_welcome(self):
        return {}

    def _build_welcome(self, _data):
        e = discord.Embed(title="🌾 歡迎來到農場！", color=GREEN)
        if self.group_id is None:
            e.description = "你還沒有農場。請到 Discord 伺服器中輸入 /farm 建立（農場會屬於那個伺服器）。"
            return e
        e.description = (
            f"你還沒有農場。按下面的按鈕，就會在「{self.group_name}」建立你的農場。\n"
            f"開始時有 {balance.INITIAL_GOLD} 金幣、{balance.INITIAL_PLOTS} 格農田。\n"
            "每個 Discord 帳號只有一座農場；之後可以轉移到其他伺服器（每 7 天一次）。"
        )

        async def create(interaction, name=None):
            if not interaction.response.is_done():
                await interaction.response.defer()
            w = self.who
            try:
                await self.run(self.cmds.service.start_external_farm, "discord", w.user_id, w.user_name,
                               w.guild_id, w.guild_name, name)
                ctx = await self.run(self.cmds.service.external_context, "discord", w.user_id, w.guild_id,
                                     w.guild_name)
                self.player_id, self.page = ctx.player_id, "main"
            except Exception as exc:
                if not isinstance(exc, GameError):
                    logger.exception("建立農場失敗（使用者 %s）", w.user_id)
                await self.show_error(interaction, clean_message(player_message(exc)))
                return
            await self.refresh(interaction, "🎉 農場建立完成！先到「🏪 商店」買種子，或在「🌱 農田」直接選作物種下。")

        async def named(interaction):
            await interaction.response.send_modal(NameModal(create))

        self.button("建立農場", create, emoji="🌱", style=discord.ButtonStyle.success)
        self.button("自訂名稱建立", named, emoji="✏️")
        return e

    # ======================================================================
    # 總覽
    # ======================================================================

    def _load_main(self):
        svc, pid = self.svc, self.player_id
        status = svc.get_farm_status(pid)
        return {
            "status": status,
            "image": self._farm_image(status),
            "animals": svc.animal_status(pid),
            "orders": svc.craft_orders(pid),
            "home": svc.get_home_group(pid),
            "events": svc.active_events(),
        }

    def _build_main(self, d):
        s = d["status"]
        growing = [p.ready_at for p in s.plots if p.state == PlotState.GROWING]
        ready = sum(1 for p in s.plots if p.state == PlotState.READY)
        waiting = sum(1 for p in s.plots if p.state == PlotState.WAITING_WATER)
        header = (
            f"👨‍🌾 **{s.player_name}**　⭐ **Lv {s.level}**　💰 **{money(s.gold)}** 金\n"
            f"━━━━━━━━ 🌱 **農田（{len(s.plots)} 格）** ━━━━━━━━"
        )
        e = discord.Embed(title=f"🌾 ✦ {s.name} ✦", color=GREEN,
                          description=f"{header}\n" + "\n".join(plot_line(p) for p in s.plots))
        if self.avatar_url:
            e.set_thumbnail(url=self.avatar_url)
        self.attach_image(e, d["image"])
        used = f"{s.warehouse_used}/{s.warehouse_capacity}"
        e.add_field(name="📦 倉庫", value=f"{used}\n{bar(s.warehouse_used, s.warehouse_capacity, 8)}")
        home = d["home"]
        e.add_field(name="🏠 所屬群組", value=home[1] if home else "未設定")
        e.add_field(name="📊 農田統計", value=summary_counts(s.plots).replace("　", "\n") or "—")
        a = d["animals"]
        if a["used"]:
            c = a["counts"]
            animal = f"🐾 {a['used']}/{a['capacity']} 隻\n✅ 可收集 {c['ready']}｜🍽️ 待餵 {c['hungry']}"
            if a["next_ready_at"]:
                animal += f"\n⏰ {ts(a['next_ready_at'])}"
        else:
            animal = "還沒有動物"
        e.add_field(name="🐔 動物", value=animal)
        done = sum(1 for o in d["orders"] if o["state"] == "ready")
        busy = [o for o in d["orders"] if o["state"] == "in_progress"]
        craft = [f"✅ {done} 張可領取"] if done else []
        craft += [f"⚙️ {o['recipe_name']} {ts(o['ready_at'])}" for o in busy[:3]]
        e.add_field(name="🏭 加工", value="\n".join(craft) or "💤 閒置")
        events = [
            f"🔥 **{ev['name']}**{'（限' + ev['crop_name'] + '）' if ev['crop_name'] else ''}\n"
            f"　✨ 效果：{ev['effects_text']}" + (f"\n　📝 {ev['description']}" if ev["description"] else "")
            for ev in d["events"]
        ]
        e.add_field(name="🎉 現在進行中的活動", inline=False,
                    value=lines_or(events, "目前沒有活動，平常心種田吧 🌾"))
        hints = []
        if ready:
            hints.append(f"✅ **{ready} 格可以收成！**")
        if waiting:
            hints.append(f"💧 {waiting} 格等待澆水")
        if growing:
            hints.append(f"⏰ 下一批成熟 {ts(min(growing))}")
        if not any(p.state != PlotState.EMPTY for p in s.plots):
            hints.append("🌱 農田都空著，到「🌱 農田」選作物種下吧！")
        e.add_field(name="📋 待辦", value="\n".join(hints) or "一切就緒 👍", inline=False)
        e.set_footer(text="面板只有你看得到；15 分鐘後逾時，可再輸入 /farm 開啟。")

        self.button("全部澆水", self._water_all, emoji="💧", style=discord.ButtonStyle.primary, row=0,
                    disabled=not waiting)
        self.button("全部收成", self._harvest_all, emoji="🧺", style=discord.ButtonStyle.success, row=0,
                    disabled=not ready)

        async def reload(interaction):
            await self.goto(interaction, "main")

        self.button("重新整理", reload, emoji="🔄", row=0)
        self.button("分享到頻道", self._share, emoji="📢", row=0, disabled=self.group_id is None)
        self.nav("農田", "plots", emoji="🌱", row=1)
        self.nav("背包", "backpack", emoji="🎒", row=1)
        self.nav("商店", "shop", emoji="🏪", row=1)
        self.nav("販售", "sell", emoji="💰", row=1)
        self.nav("動物", "animals", emoji="🐔", row=2)
        self.nav("加工", "craft", emoji="🏭", row=2)
        self.nav("防守", "defense", emoji="🛡️", row=2)
        self.nav("偷菜", "steal", emoji="🥷", row=2)
        self.nav("排行榜", "board", emoji="🏆", row=3)
        self.nav("任務競賽", "quests", emoji="📜", row=3)
        self.nav("群組", "group", emoji="👥", row=3)
        self.nav("紀錄", "log", emoji="🧾", row=3)
        self.nav("作物價格", "prices", emoji="📈", row=4)
        self.nav("賭場", "casino", emoji="🎰", row=4, style=discord.ButtonStyle.primary)
        self.nav("山洞", "cave", emoji="⛰️", row=4, style=discord.ButtonStyle.primary)
        self.nav("說明", "help", emoji="❓", row=4)
        return e

    async def _share(self, interaction):
        await interaction.response.defer()
        try:
            status = await self.run(self.svc.get_farm_status, self.player_id)
            png = await self.run(self._farm_image, status)
        except Exception as exc:
            await interaction.followup.send(f"❌ {player_message(exc)}", ephemeral=True)
            return
        e = discord.Embed(title=f"🌾 {status.name}（Lv {status.level}）", color=GREEN,
                          description="\n".join(plot_line(p) for p in status.plots)[:4000])
        e.set_author(name=status.player_name)
        e.add_field(name="💰 金幣", value=money(status.gold))
        kwargs = {}
        if png:
            e.set_image(url=f"attachment://{FARM_IMAGE}")
            kwargs["file"] = discord.File(io.BytesIO(png), filename=FARM_IMAGE)
        try:
            await interaction.followup.send(embed=e, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        except discord.HTTPException as exc:
            logger.warning("分享農場失敗：%s", type(exc).__name__)

    # ---------- 一鍵操作（服務層在同一筆交易中整批完成） ----------

    async def _water_all(self, interaction):
        await self.act(interaction, lambda: self.svc.water_all(self.player_id).message)

    async def _harvest_all(self, interaction):
        await self.act(interaction, lambda: self.svc.harvest_all(self.player_id).message)

    async def _clear_all(self, interaction):
        await self.act(interaction, lambda: self.svc.clear_all_withered(self.player_id).message)

    # ======================================================================
    # 農田
    # ======================================================================

    def _load_plots(self):
        svc, pid = self.svc, self.player_id
        status = svc.get_farm_status(pid)
        return {"status": status, "image": self._farm_image(status), "backpack": svc.get_backpack(pid),
                "crops": svc.get_market_prices()}

    def _build_plots(self, d):
        s, bp = d["status"], d["backpack"]
        seeds = {x.code: x.quantity for x in bp.seeds}
        seed_text = "　".join(f"{crop_emoji(x.code)}{x.name} ×**{x.quantity}**" for x in bp.seeds) or "沒有種子"
        e = discord.Embed(title=f"🌱 ✦ 農田（{len(s.plots)} 格）✦", color=GREEN, description=sections(
            f"💰 金幣 **{money(s.gold)}**\n🌰 {seed_text}",
            ("農田", [plot_line(p) for p in s.plots]),
            f"📊 {summary_counts(s.plots)}",
        ))
        e.set_footer(text="選作物會種滿所有空地（種子不夠時自動購買）；種下後記得澆水。")
        self.attach_image(e, d["image"])
        empty = sum(1 for p in s.plots if p.state == PlotState.EMPTY)
        if empty:
            options = [
                (f"{c.name}（種子 {c.seed_price} 金，持有 {seeds.get(c.code, 0)}）" if c.shop_buyable
                 else f"{c.name}（稀有種子，持有 {seeds.get(c.code, 0)}）", c.code,
                 f"⏱️ {long_duration(c.grow_seconds)}｜🧺 收成 ×{c.harvest_quantity} {c.name}｜🏷️ {c.sell_price} 金／個",
                 crop_emoji(c.code))
                for c in d["crops"] if c.shop_buyable or seeds.get(c.code)
            ]

            async def plant(interaction, code):
                await self.act(interaction, lambda: self.svc.plant_all(self.player_id, code).message)

            self.select(f"🌱 選擇作物，種到 {empty} 格空地…", options, plant, row=0)
        counts = {st: sum(1 for p in s.plots if p.state == st) for st in PlotState}
        self.button("全部澆水", self._water_all, emoji="💧", style=discord.ButtonStyle.primary, row=1,
                    disabled=not counts[PlotState.WAITING_WATER])
        self.button("全部收成", self._harvest_all, emoji="🧺", style=discord.ButtonStyle.success, row=1,
                    disabled=not counts[PlotState.READY])
        self.button("清除乾枯", self._clear_all, emoji="🥀", row=1, disabled=not counts[PlotState.WITHERED])

        price = balance.upgrade_price(len(s.plots))

        async def upgrade(interaction):
            if self.confirm != "upgrade":
                await interaction.response.defer()
                self.confirm = "upgrade"
                await self.refresh(
                    interaction,
                    f"⚠️ 擴建需要 {money(price or 0)} 金幣（+{balance.UPGRADE_PLOTS_ADDED} 格），"
                    "再按一次「確認擴建」。",
                )
                return
            await self.act(interaction, lambda: self.svc.upgrade_farm(self.player_id).message)

        confirming = self.confirm == "upgrade"
        full = price is None
        label = "已達上限" if full else ("確認擴建" if confirming else f"擴建（{money(price)} 金）")
        self.button(label, upgrade, emoji="⬆️", row=1, disabled=full,
                    style=discord.ButtonStyle.danger if confirming else discord.ButtonStyle.secondary)
        growing = [p for p in s.plots if p.state == PlotState.GROWING and not p.fertilized]
        fert = next((x.quantity for x in bp.consumables if x.code == balance.ITEM_FERTILIZER), 0)
        if growing and fert:
            async def fertilize(interaction, n):
                await self.act(interaction, lambda: self.svc.use_fertilizer(self.player_id, int(n)).message)

            self.select(
                f"🧪 施化肥（持有 {fert}，剩餘時間減半）…",
                [(f"#{p.plot_number} {p.crop_name}", str(p.plot_number), "成熟時間減半", "🧪") for p in growing],
                fertilize, row=2,
            )
        worms = next((x.quantity for x in bp.consumables if x.code == balance.ITEM_EARTHWORM), 0)
        if worms:
            async def earthworm(interaction):
                await self.act(interaction, lambda: self.svc.use_earthworm(self.player_id).message)

            self.button(f"使用蚯蚓（持有 {worms}）", earthworm, emoji="🪱", row=3)
        self._nav_back()
        return e

    # ======================================================================
    # 背包
    # ======================================================================

    def _load_backpack(self):
        return {"backpack": self.svc.get_backpack(self.player_id)}

    def _build_backpack(self, d):
        bp = d["backpack"]
        tools = [f"{'✅' if t.enabled else '⏸️'} {t.name}（{'啟用中' if t.enabled else '已停用'}）" if t.owned
                 else f"🔒 {t.name}（未購買）" for t in bp.tools]
        effects = [f"🪱 {x.name}：{ts(x.expires_at)}結束" for x in bp.effects if x.status == "active"]
        e = discord.Embed(title="🎒 ✦ 背包 ✦", color=BLUE, description=sections(
            f"📦 倉庫 **{bp.used}/{bp.capacity}**\n{bar(bp.used, bp.capacity, 16)}",
            ("🌰 種子", [f"{crop_emoji(x.code)} {x.name}　×**{x.quantity}**" for x in bp.seeds]),
            ("🧺 農產品", [f"{crop_emoji(x.code)} {x.name}{genetics_label(x.quality, x.mutation)}　×**{x.quantity}**"
                          f"　（{x.price} 金／個）" for x in bp.produce]),
            ("🥚 動物產品與加工品", [f"🥚 {x.name}　×**{x.quantity}**　（{x.price} 金／個）" for x in bp.goods]),
            ("🧪 道具", [f"{ITEM_EMOJI.get(x.code, '🎁')} {x.name}　×**{x.quantity}**" for x in bp.consumables]),
            ("🏆 賭場獎品", [f"🏆 {p['name']}　×**{p['quantity']}**"
                           f"　（價值 ×{p['multiplier']}，{p['unit_price']} 金／個）" for p in bp.prizes]),
            (f"⛰️ 素材背包 {bp.material_used}/{bp.material_capacity}（不佔倉庫）",
             [f"{MATERIAL_EMOJI.get(x.code, '🪨')} {x.name}　×**{x.quantity}**　（{x.description}）"
              for x in bp.materials]),
            ("🔧 工具", tools),
            ("✨ 生效中的效果", effects),
        ))
        for t in bp.tools:
            if t.owned:
                async def toggle(interaction, t=t):
                    await self.act(interaction, lambda: self.svc.set_tool_enabled(self.player_id, t.code,
                                                                                  not t.enabled).message)

                self.button(f"{'停用' if t.enabled else '啟用'}{t.name}", toggle, emoji="🔧", row=0)
        self.nav("前往販售", "sell", emoji="💰", row=1)
        self.nav("商店", "shop", emoji="🏪", row=1)
        coins = next((x.quantity for x in bp.materials if x.code == exploration.FAKE_COIN), 0)
        if coins:
            async def exchange(interaction):
                self.picker = {"key": "fake_coin", "label": f"💱 兌換假幣（1 個 = {exploration.FAKE_COIN_VALUE} 金）",
                               "verb": "換", "max": coins, "presets": (1, 5, 10),
                               "run": lambda q: self.svc.exchange_fake_coins(self.player_id, q).message}
                await interaction.response.defer()
                await self.refresh(interaction)

            self.button(f"兌換假幣（持有 {coins}）", exchange, emoji="💱", row=1, style=discord.ButtonStyle.success)
            if self.picker and self.picker.get("key") == "fake_coin":
                self.picker["max"] = coins
                e.add_field(name="💱 兌換假幣", inline=False,
                            value=f"持有 **{coins}** 個，1 個 = **{exploration.FAKE_COIN_VALUE}** 金\n👇 選擇兌換數量")
                self.quantity_buttons(row=2)
        self._nav_back()
        return e

    # ======================================================================
    # 商店（種子與道具在同一個選單）
    # ======================================================================

    def _load_shop(self):
        svc = self.svc
        bp = svc.get_backpack(self.player_id)
        held = {f"seed:{x.code}": x.quantity for x in bp.seeds}
        held |= {f"item:{x.code}": x.quantity for x in bp.consumables}
        held |= {f"item:{t.code}": 1 for t in bp.tools if t.owned}
        status = svc.get_farm_status(self.player_id)
        crops = [c for c in svc.get_market_prices() if c.shop_buyable]  # 稀有作物只能探索或培育取得
        return {"crops": crops, "events": svc.active_events(), "gold": status.gold,
                "capacity": status.warehouse_capacity, "used": status.warehouse_used, "held": held,
                "image": self._image("商店", farm_image.shop_png)}

    def _held_after(self, key: str) -> str:
        """購買後重新讀取持有數量，讓玩家確定真的買到了。"""
        bp = self.svc.get_backpack(self.player_id)
        kind, code = key.split(":", 1)
        if kind == "seed":
            n = next((x.quantity for x in bp.seeds if x.code == code), 0)
        else:
            n = next((x.quantity for x in bp.consumables if x.code == code), 0)
        return f"🎒 現在持有 **{n}**　💰 剩餘金幣 **{money(self.svc.get_farm_status(self.player_id).gold)}**"

    def _build_shop(self, d):
        held, selected = d["held"], (self.picker or {}).get("key")

        def mark(key):
            return "▶ " if key == selected else ""

        seeds = [
            f"{mark('seed:' + c.code)}{crop_emoji(c.code)} **{c.name}種子**　💰 **{c.seed_price}** 金"
            f"　🎒 持有 **{held.get('seed:' + c.code, 0)}**\n"
            f"　⏱️ {long_duration(c.grow_seconds)}　🧺 收成 ×{c.harvest_quantity} {crop_emoji(c.code)}{c.name}"
            f"　🏷️ 售價 {c.sell_price} 金／個"
            for c in d["crops"]
        ]
        consumables, tools = [], []
        for code, (name, price, kind, desc, _a) in balance.ITEMS.items():
            have = held.get(f"item:{code}", 0)
            if kind == "tool":
                tag = "💎 奢侈品" if code in (balance.TOOL_AUTO_PLANTER, balance.TOOL_AUTO_SELLER) else "♾️ 永久工具"
                own = "✅ 已擁有" if have else "🔒 未擁有"
                tools.append(f"{mark('item:' + code)}{ITEM_EMOJI.get(code, '🎁')} **{name}**　💰 **{money(price)}** 金"
                             f"　{tag}　{own}\n　📝 {desc}")
            else:
                consumables.append(f"{mark('item:' + code)}{ITEM_EMOJI.get(code, '🎁')} **{name}**　💰 **{price}** 金"
                                   f"　🎒 持有 **{have}**\n　📝 {desc}")
        expand_price = balance.warehouse_expansion_price(d["capacity"])
        ladder = " → ".join(money(p) for p in balance.WAREHOUSE_EXPANSION_PRICES)
        warehouse = (
            f"{mark('special:warehouse')}📦 **倉庫擴大卷**　"
            + (f"💰 **{money(expand_price)}** 金" if expand_price else "✅ 已達最大容量")
            + f"　🎒 倉庫 **{d['used']}/{d['capacity']}**\n"
            f"　📝 立即 +{balance.WAREHOUSE_EXPANSION_STEP} 格（最大 {balance.WAREHOUSE_MAX_CAPACITY} 格），"
            f"價格逐次提高：{ladder}"
        )
        events = [f"🔥 **{ev['name']}**{'（限' + ev['crop_name'] + '）' if ev['crop_name'] else ''}"
                  f"\n　{ev['effects_text']}" for ev in d["events"]]
        e = discord.Embed(title="🏪 ✦ 農場商店 ✦", color=GOLD, description=sections(
            f"💰 你的金幣：**{money(d['gold'])}**",
            ("🌰 種子", seeds, "\n\n"),
            ("🧪 道具", consumables, "\n\n"),
            ("📦 倉庫", [warehouse]),
            ("💎 工具與奢侈品", tools, "\n\n"),
            ("🎉 今天的活動", events, "\n\n"),
        ))
        if self.picker:
            hint = "👇 按「確認購買」完成購買" if self.picker.get("confirm") else "👇 按下方的數量按鈕購買"
            e.add_field(name="🛒 已選擇", inline=False,
                        value=f"**{self.picker['label']}**（{self.picker['price_text']}）\n{hint}")
        else:
            e.set_footer(text="👇 先從選單挑一樣商品，再選數量")

        async def pick(interaction, key):
            kind, code = key.split(":", 1)
            if kind == "special":
                self.picker = {"key": key, "label": "📦 倉庫擴大卷", "price_text": f"{money(expand_price or 0)} 金",
                               "confirm": True, "max": 1, "presets": (),
                               "run": lambda q: self.svc.expand_warehouse(self.player_id).message}
            elif kind == "seed":
                crop = next(c for c in d["crops"] if c.code == code)
                def buy_seed(q):
                    return f"{self.svc.buy_seeds(self.player_id, code, q).message}\n{self._held_after(key)}"

                self.picker = {
                    "key": key, "label": f"{crop_emoji(code)} {crop.name}種子",
                    "price_text": f"每顆 {crop.seed_price} 金", "verb": "買",
                    "max": max(1, min(balance.MAX_TRADE_QUANTITY, d["gold"] // crop.seed_price)),
                    "presets": (1, 5, 10), "keep": True, "run": buy_seed,
                }
            else:
                name, price, item_kind, _desc, _a = balance.ITEMS[code]
                label = f"{ITEM_EMOJI.get(code, '🎁')} {name}"
                if item_kind == "tool":
                    # 永久工具與奢侈品價格高：需要再按一次確認才購買
                    self.picker = {"key": key, "label": label, "price_text": f"{money(price)} 金", "confirm": True,
                                   "max": 1, "presets": (),
                                   "run": lambda q: self.svc.buy_item(self.player_id, code, 1).message}
                else:
                    def buy_item(q):
                        return f"{self.svc.buy_item(self.player_id, code, q).message}\n{self._held_after(key)}"

                    self.picker = {
                        "key": key, "label": label, "price_text": f"每個 {price} 金", "verb": "買",
                        "max": max(1, min(balance.MAX_TRADE_QUANTITY, d["gold"] // price)), "presets": (1, 5, 10),
                        "keep": True, "run": buy_item,
                    }
            await interaction.response.defer()
            await self.refresh(interaction)

        options = [(f"{c.name}種子", f"seed:{c.code}", f"🌰 種子｜💰 {c.seed_price} 金", crop_emoji(c.code))
                   for c in d["crops"]]
        options += [(name, f"item:{code}", f"🧪 道具｜💰 {price} 金", ITEM_EMOJI.get(code))
                    for code, (name, price, kind, _desc, _a) in balance.ITEMS.items() if kind != "tool"]
        if expand_price:
            options.append(("倉庫擴大卷", "special:warehouse", f"📦 +{balance.WAREHOUSE_EXPANSION_STEP} 格｜💰 "
                            f"{money(expand_price)} 金", "📦"))
        options += [(name, f"item:{code}", f"💎 工具｜💰 {money(price)} 金", ITEM_EMOJI.get(code))
                    for code, (name, price, kind, _desc, _a) in balance.ITEMS.items()
                    if kind == "tool" and not held.get(f"item:{code}")]
        self.select("🛒 選擇要買的商品…", options, pick, row=0, selected=selected)
        self.quantity_buttons(row=1)
        self._nav_back()
        self.attach_image(e, d["image"])
        return e

    def _cli_text(self, *args) -> str:
        handler = CommandHandler(self.svc)
        handler.player_id, handler.group_id = self.player_id, self.group_id
        return handler.execute(list(args))

    def _load_prices(self):
        return {"crops": self.svc.crop_catalog()}

    def _build_prices(self, d):
        blocks = []
        for c in d["crops"]:
            p = c["prices"]
            normal = "｜".join(f"{balance.QUALITIES[q]} **{p[q]['none']}**" for q in balance.QUALITIES)
            golden = "｜".join(f"{balance.QUALITIES[q]} **{p[q]['golden']}**" for q in balance.QUALITIES)
            variant = "｜".join(f"{balance.QUALITIES[q]} **{p[q]['variant']}**" for q in balance.QUALITIES)
            blocks.append(
                f"{crop_emoji(c['code'])} **{c['name']}**　種子 {c['seed_price']} 金\n"
                f"　⏱️ {long_duration(c['grow_seconds'])}　🧺 收成 ×{c['harvest_quantity']} "
                f"{crop_emoji(c['code'])}{c['name']}\n"
                f"　🏷️ 一般：{normal}\n"
                f"　🌟 黃金作物：{golden}\n"
                f"　💎 珍稀變種「{c['variant_name']}」：{variant}"
            )
        e = discord.Embed(title="📈 ✦ 市場價格 ✦", color=GOLD, description=sections(
            "每個農產品的單價依 **品質** 與 **突變** 而不同（金幣／個）。",
            ("作物", blocks, "\n\n"),
            "🍀 巨型作物：收成數量變多，單價同一般｜⚡ 快速生長：只縮短時間，單價同一般",
        ))
        self.nav("品質與突變", "genetics", emoji="🧬", row=0)
        self._nav_back(row=0)
        return e

    def _load_genetics(self):
        return {"quality": self.svc.quality_table(self.player_id), "mutations": self.svc.mutation_table()}

    def _build_genetics(self, d):
        t = d["quality"]
        quality = [
            f"**{name}**　機率 {t['base_bp'][q] / 100:g}%"
            + (f"（你的實際機率 **{t['effective_bp'][q] / 100:g}%**）" if t["effective_bp"] else "")
            + f"　售價 ×{t['price_pct'][q] / 100:g}"
            for q, name in balance.QUALITIES.items()
        ]
        mutations = [f"**{m['name']}**　機率 {m['bp'] / 100:g}%\n　{m['effect']}" for m in d["mutations"]]
        e = discord.Embed(title="🧬 ✦ 品質與突變 ✦", color=GOLD, description=sections(
            "品質與突變在 **播種時** 決定，之後不會改變。",
            ("⭐ 品質", quality),
            f"農場每升 1 級，有 {t['level_bonus_bp'] / 100:g}% 機率從普通升到較高品質（上限 "
            f"{t['level_bonus_max_bp'] / 100:g}%）。你的農場等級：**{t['level']}**",
            ("🧪 突變（每批最多一種）", mutations, "\n\n"),
        ))
        self.nav("價目表", "prices", emoji="📈", row=0)
        self._nav_back(row=0)
        return e

    # ======================================================================
    # 販售
    # ======================================================================

    def _load_sell(self):
        return {"backpack": self.svc.get_backpack(self.player_id)}

    @staticmethod
    def _sellables(bp) -> list[dict]:
        rows = []
        for x in bp.produce:
            # 珍稀變種以變種代碼販售（服務層會自動判定突變），其他農產品指定品質與突變
            mutation = "none" if x.mutation == "variant" else x.mutation
            rows.append({"key": f"p|{x.code}|{x.quality}|{x.mutation}", "code": x.code, "quality": x.quality,
                         "mutation": mutation, "name": x.name + genetics_label(x.quality, x.mutation),
                         "quantity": x.quantity, "price": x.price, "emoji": crop_emoji(x.code)})
        for x in bp.goods:
            rows.append({"key": f"g|{x.code}", "code": x.code, "quality": "normal", "mutation": "none",
                         "name": x.name, "quantity": x.quantity, "price": x.price, "emoji": "🥚"})
        return rows

    def _sell_one(self, row: dict, quantity: int) -> str:
        return self.svc.sell(self.player_id, row["code"], quantity, row["quality"], row["mutation"]).message

    def _sell_all(self) -> str:
        rows = self._sellables(self.svc.get_backpack(self.player_id))
        if not rows:
            raise ValidationError("沒有可以賣的東西。")
        income, sold, failed = 0, 0, []
        for row in rows:
            try:
                result = self.svc.sell(self.player_id, row["code"], row["quantity"], row["quality"], row["mutation"])
                income += result.gold_delta or 0
                sold += row["quantity"]
            except GameError as exc:
                failed.append(f"{row['name']}：{exc}")
        text = f"賣出 {sold} 個商品，獲得 {money(income)} 金幣。"
        return text + (" 未賣出：" + "；".join(failed[:3]) if failed else "")

    def _build_sell(self, d):
        rows = self._sellables(d["backpack"])
        total = sum(r["quantity"] * r["price"] for r in rows)
        selected = (self.picker or {}).get("key")
        lines = [
            f"{'▶ ' if r['key'] == selected else ''}{r['emoji']} **{r['name']}**　×**{r['quantity']}**\n"
            f"　🏷️ 單價 {r['price']}　💰 小計 **{money(r['quantity'] * r['price'])}**"
            for r in rows
        ]
        # 賭場獎品另外列出；「全部賣出」不會賣掉獎品（玩家可能想保留展示）
        prize_rows = [
            {"key": f"z|{p['id']}", "prize_id": p["id"], "name": f"{p['name']}（賭場 ×{p['multiplier']}）",
             "quantity": p["quantity"], "price": p["unit_price"], "emoji": "🏆"}
            for p in d["backpack"].prizes
        ]
        prize_lines = [
            f"{'▶ ' if r['key'] == selected else ''}🏆 **{r['name']}**　×**{r['quantity']}**\n"
            f"　🏷️ 單價 {r['price']}　💰 小計 **{money(r['quantity'] * r['price'])}**"
            for r in prize_rows
        ]
        e = discord.Embed(title="💰 ✦ 販售 ✦", color=GOLD, description=sections(
            ("可以賣的東西", lines, "\n\n") if rows else "背包裡沒有可以賣的農產品或商品。",
            f"💎 全部價值（依目前單價）：**{money(total)}** 金" if rows else "",
            ("🏆 賭場獎品（「全部賣出」不會賣掉，可以留著展示）", prize_lines, "\n\n"),
        ))
        if self.picker:
            e.add_field(name="🛒 已選擇", inline=False, value=f"**{self.picker['label']}**\n👇 按下方的數量按鈕販售")

        async def pick(interaction, key):
            row = next(r for r in rows + prize_rows if r["key"] == key)
            half = max(1, row["quantity"] // 2)
            label = f"{row['emoji']} {row['name']}（持有 {row['quantity']}，單價 {row['price']}）"
            if "prize_id" in row:
                def run(q):
                    return self.svc.sell_prize(self.player_id, row["prize_id"], q).message
            else:
                def run(q):
                    return self._sell_one(row, q)
            self.picker = {"key": key, "label": label,
                           "verb": "賣", "max": row["quantity"], "presets": sorted({1, half, row["quantity"]}),
                           "run": run}
            await interaction.response.defer()
            await self.refresh(interaction)

        self.select("💰 選擇要賣的東西…", [(f"{r['name']} ×{r['quantity']}", r["key"], f"🏷️ 單價 {r['price']}",
                                          r["emoji"]) for r in rows + prize_rows], pick, row=0, selected=selected)
        self.quantity_buttons(row=1)

        async def sell_everything(interaction):
            if self.confirm != "sell_all":
                await interaction.response.defer()
                self.confirm = "sell_all"
                await self.refresh(
                    interaction, f"⚠️ 確定要把背包裡所有農產品與商品賣掉嗎（約 {money(total)} 金幣）？再按一次確認。"
                )
                return
            await self.act(interaction, self._sell_all)

        confirming = self.confirm == "sell_all"
        self.button("確認全部賣出" if confirming else "全部賣出", sell_everything, emoji="💰", row=2, disabled=not rows,
                    style=discord.ButtonStyle.danger if confirming else discord.ButtonStyle.success)
        self._nav_back()
        return e

    # ======================================================================
    # 動物
    # ======================================================================

    def _load_animals(self):
        svc, pid = self.svc, self.player_id
        d = {"animals": svc.list_animals(pid), "status": svc.animal_status(pid), "types": svc.animal_types(),
             "workshop": svc.workshop_status(pid)}
        d["image"] = self._yard_image(d["workshop"], d["animals"])
        return d

    def _collect_all(self) -> str:
        ready = [a["animal_no"] for a in self.svc.list_animals(self.player_id) if a["state"] == "ready"]
        if not ready:
            raise ValidationError("目前沒有可以收集的動物產品。")
        ok, failed = [], []
        for n in ready:
            try:
                ok.append(self.svc.collect_animal(self.player_id, n).message)
            except GameError as exc:
                failed.append(f"#{n}：{exc}")
        return f"收集 {len(ok)} 隻動物的產品。" + (" 未完成：" + "；".join(failed[:3]) if failed else "")

    def _build_animals(self, d):
        st = d["status"]
        lines = []
        for a in d["animals"]:
            emoji = ANIMAL_EMOJI.get(a["code"], "🐾")
            if a["state"] == "producing":
                tail = f"⏳ 生產中，{ts(a['ready_at'])}完成"
            elif a["state"] == "ready":
                tail = f"✅ 可收集 ×{a['product_quantity']} {a['product_name']}"
            else:
                tail = f"🍽️ 待餵食（{a['feed_cost']} 金）"
            lines.append(f"{emoji} `#{a['animal_no']:>2}` **{a['name']}**　{tail}")
        barns = d["workshop"]["counts"]["barn"]
        bname, bprice, bmax, _a = balance.BUILDINGS["barn"]
        shop = [
            f"{ANIMAL_EMOJI.get(t['code'], '🐾')} **{t['name']}**　💰 **{t['price']}** 金\n"
            f"　🍽️ 餵食 {t['feed_cost']} 金　⏱️ {long_duration(t['cycle_seconds'])}"
            f"　🥚 產 ×{t['product_quantity']} {t['product_name']}（{t['product_price']} 金／個）"
            for t in d["types"]
        ]
        empty = "還沒有動物。先建造畜舍，再購買動物。" if not barns else "還沒有動物，從下方選單購買。"
        e = discord.Embed(title=f"🐔 ✦ 動物（{st['used']}/{st['capacity']}）✦", color=GREEN, description=sections(
            f"🏚️ {bname} **{barns}/{bmax}** 座（每座 {balance.BARN_SLOTS} 個欄位，{bprice} 金）",
            ("我的動物", lines) if lines else empty,
            ("🛒 動物商店", shop, "\n\n"),
        ))
        if self.picker:
            e.add_field(name="🛒 已選擇", inline=False, value=f"**{self.picker['label']}**\n👇 按下方的數量按鈕購買")
        c = st["counts"]

        async def feed(interaction):
            await self.act(interaction, lambda: self.svc.feed_all(self.player_id).message)

        async def collect(interaction):
            await self.act(interaction, self._collect_all)

        async def build_barn(interaction):
            await self.act(interaction, lambda: self.svc.build(self.player_id, "barn").message)

        self.button(f"全部餵食（{st['feed_cost_all']} 金）", feed, emoji="🍽️", row=0, disabled=not c["hungry"],
                    style=discord.ButtonStyle.primary)
        self.button("收集全部", collect, emoji="🧺", row=0, disabled=not c["ready"], style=discord.ButtonStyle.success)
        self.button(f"建造{bname}（{bprice} 金）", build_barn, emoji="🏚️", row=0, disabled=barns >= bmax)

        async def pick(interaction, code):
            t = next(x for x in d["types"] if x["code"] == code)
            free = max(1, st["capacity"] - st["used"])
            self.picker = {"key": code, "label": f"{ANIMAL_EMOJI.get(code, '🐾')} {t['name']}（每隻 {t['price']} 金，"
                                                 f"剩餘欄位 {st['capacity'] - st['used']}）",
                           "verb": "買", "max": min(balance.ANIMAL_MAX_BUY, free), "presets": (1, 2, 4),
                           "run": lambda q: self.svc.buy_animals(self.player_id, code, q).message}
            await interaction.response.defer()
            await self.refresh(interaction)

        self.select("🛒 購買動物…", [(t["name"], t["code"], f"💰 {t['price']} 金｜產{t['product_name']}",
                                      ANIMAL_EMOJI.get(t["code"], "🐾")) for t in d["types"]], pick, row=1,
                    selected=(self.picker or {}).get("key"))
        self.quantity_buttons(row=2)
        self._nav_back()
        self.attach_image(e, d["image"])
        return e

    # ======================================================================
    # 加工（開始加工）與建造設施（分開兩頁）
    # ======================================================================

    def _load_craft(self):
        svc, pid = self.svc, self.player_id
        d = {"workshop": svc.workshop_status(pid), "recipes": svc.recipes(pid), "orders": svc.craft_orders(pid)}
        d["image"] = self._yard_image(d["workshop"], svc.list_animals(pid))
        return d

    def _collect_crafts(self) -> str:
        ready = [o["order_id"] for o in self.svc.craft_orders(self.player_id) if o["state"] == "ready"]
        if not ready:
            raise ValidationError("沒有已完成的加工訂單。")
        msgs = [self.svc.collect_craft(self.player_id, oid).message for oid in ready]
        return " ".join(msgs)

    def _build_craft(self, d):
        ws = d["workshop"]
        facilities = []
        for code, (name, price, _max, _a) in balance.BUILDINGS.items():
            if code == "barn":
                continue
            busy = ws["busy"][code]
            if not ws["counts"][code]:
                state = f"🔒 未建造（{price} 金）"
            elif busy and busy["state"] == "in_progress":
                state = f"⚙️ 加工中：{busy['recipe_name']} ×{busy['output_quantity']}，{ts(busy['ready_at'])}完成"
            elif busy:
                state = f"✅ {busy['recipe_name']} 完成，可以領取"
            else:
                state = "💤 閒置中"
            facilities.append(f"🏭 **{name}**　{state}")
        recipes = [
            f"{'✅' if r['built'] else '🔒'} **{r['name']}**（{r['building_name']}）\n"
            f"　📥 " + "、".join(f"×{v} {k}" for k, v in r["inputs"].items())
            + f"　➡️ 📤 ×{r['output_quantity']} {r['output_name']}"
            + f"\n　⏱️ {long_duration(r['seconds'])}／份"
            + (f"　🏷️ 售價 {r['sell_price']}" if r["sell_price"] else "")
            for r in d["recipes"]
        ]
        built = [r for r in d["recipes"] if r["built"]]
        e = discord.Embed(title="🏭 ✦ 加工 ✦", color=BLUE, description=sections(
            ("設施狀態", facilities),
            ("📖 配方（✅ 可以加工、🔒 需要先建造設施）", recipes, "\n\n"),
            "" if built else "💡 還沒有任何加工設施，請先按「🏗️ 建造設施」。",
        ))
        if self.picker:
            e.add_field(name="🛒 已選擇", inline=False,
                        value=f"**{self.picker['label']}**\n👇 按下方的數量按鈕開始加工")
        ready = sum(1 for o in d["orders"] if o["state"] == "ready")

        async def pick(interaction, code):
            r = next(x for x in d["recipes"] if x["code"] == code)
            self.picker = {"key": code, "label": f"🏭 {r['name']}（{r['building_name']}）", "verb": "做",
                           "max": balance.CRAFT_MAX_QUANTITY, "presets": (1, 5, balance.CRAFT_MAX_QUANTITY),
                           "run": lambda q: self.svc.start_craft(self.player_id, code, q).message}
            await interaction.response.defer()
            await self.refresh(interaction)

        async def collect(interaction):
            await self.act(interaction, self._collect_crafts)

        self.select("🏭 開始加工（只列出已建造設施的配方）…",
                    [(r["name"], r["code"], f"{r['building_name']}｜{long_duration(r['seconds'])}／份", "🏭")
                     for r in built], pick, row=0, selected=(self.picker or {}).get("key"))
        self.quantity_buttons(row=1)
        self.nav("建造設施", "build", emoji="🏗️", row=2, style=discord.ButtonStyle.primary)
        self.button(f"領取完成品（{ready}）", collect, emoji="📥", row=2, disabled=not ready,
                    style=discord.ButtonStyle.success)
        self.nav("機器", "machines", emoji="🔧", row=2)
        self._nav_back()
        self.attach_image(e, d["image"])
        return e

    def _load_build(self):
        svc, pid = self.svc, self.player_id
        d = {"workshop": svc.workshop_status(pid), "recipes": svc.recipes(pid)}
        d["image"] = self._yard_image(d["workshop"], svc.list_animals(pid))
        return d

    def _build_build(self, d):
        ws = d["workshop"]
        lines = []
        for code, (name, price, mx, _a) in balance.BUILDINGS.items():
            if code == "barn":
                use = f"安置動物（每座 {balance.BARN_SLOTS} 隻）"
            else:
                use = "可加工：" + "、".join(r["name"] for r in d["recipes"] if r["building_code"] == code)
            owned = ws["counts"][code]
            state = "✅ 已達上限" if owned >= mx else f"🏗️ 可建造（{owned}/{mx}）"
            lines.append(f"🏭 **{name}**　💰 **{price}** 金　{state}\n　📝 {use}")
        e = discord.Embed(title="🏗️ ✦ 建造設施 ✦", color=BLUE, description=sections(
            f"💰 你的金幣：**{money(ws['gold'])}**",
            ("設施", lines, "\n\n"),
        ))

        async def build(interaction, code):
            await self.act(interaction, lambda: self.svc.build(self.player_id, code).message)

        self.select("🏗️ 選擇要建造的設施…",
                    [(f"{name}（{price} 金）", code, f"已有 {ws['counts'][code]}/{mx}", "🏗️")
                     for code, (name, price, mx, _a) in balance.BUILDINGS.items() if ws["counts"][code] < mx],
                    build, row=0)
        self.nav("回加工", "craft", emoji="↩️", row=1)
        self._nav_back(row=1)
        self.attach_image(e, d["image"])
        return e

    # ======================================================================
    # 防守
    # ======================================================================

    def _load_defense(self):
        svc, pid = self.svc, self.player_id
        bp = svc.get_backpack(pid)
        tickets = next((x.quantity for x in bp.consumables if x.code == balance.ITEM_PATROL_TICKET), 0)
        return {"status": svc.defense_status(pid), "tickets": tickets}

    def _build_defense(self, d):
        st = d["status"]
        devices = [f"🛡️ **{x['name']} #{x['slot']}**　守護" + ("全部農田" if x["end_plot"] is None
                                                             else f"第 {x['start_plot']}–{x['end_plot']} 格")
                   for x in st["devices"]]
        chances = "　".join(f"`#{p['plot_number']}` {p['chance']:.0%}" for p in st["plots"])
        shop = [
            f"🛡️ **{name}**　💰 **{price}** 金　上限 {mx}\n　🚫 阻止機率 {chance:.0%}　📍 "
            + ("守護全部農田" if rng is None else f"守護連續 {rng} 格（可調整）")
            for _c, (name, price, mx, chance, rng, _a) in balance.DEFENSES.items()
        ]
        e = discord.Embed(title="🛡️ ✦ 防守 ✦", color=BLUE, description=sections(
            ("已設置的設施", devices) if devices else "還沒有防守設施。",
            ("各農田阻止機率", [chances]),
            ("🛒 防守商店", shop, "\n\n"),
            f"🔦 巡查券：**{d['tickets']}** 張（可查看近 {balance.PATROL_DAYS} 天誰來偷過菜）",
        ))

        async def buy(interaction, code):
            if code == balance.ITEM_PATROL_TICKET:
                await self.act(interaction, lambda: self.svc.buy_item(self.player_id, code, 1).message)
            else:
                await self.act(interaction, lambda: self.svc.buy_defense(self.player_id, code).message)

        tname, tprice = balance.ITEMS[balance.ITEM_PATROL_TICKET][:2]
        self.select("🛒 購買防守設施或巡查券…",
                    [(f"{name}（{price} 金）", code, f"持有 {st['owned'][code]}/{mx}", "🛡️")
                     for code, (name, price, mx, _ch, _r, _a) in balance.DEFENSES.items()]
                    + [(f"{tname}（{tprice} 金）", balance.ITEM_PATROL_TICKET, "買 1 張", "🔦")], buy, row=0)
        placeable = [x for x in st["devices"] if x["end_plot"] is not None]
        if placeable:
            async def place(interaction, value):
                code, slot = value.split("|")

                async def submitted(modal_interaction, start):
                    await self.act(modal_interaction, lambda: self.svc.place_defense(self.player_id, code, start,
                                                                                    int(slot)).message)

                await interaction.response.send_modal(QuantityModal("調整守護範圍", "起始農田編號", 999, submitted))

            self.select("📍 調整稻草人／圍欄的守護範圍…",
                        [(f"{x['name']} #{x['slot']}", f"{x['code']}|{x['slot']}",
                          f"目前第 {x['start_plot']}–{x['end_plot']} 格", "📍") for x in placeable], place, row=1)

        async def patrol(interaction):
            await self._report(interaction, "🔦 巡查報告", "patrol")

        async def log(interaction):
            await self._report(interaction, "📋 防守紀錄", "defense", "log")

        self.button(f"巡查（消耗 1 張，持有 {d['tickets']}）", patrol, emoji="🔦", row=2, disabled=not d["tickets"])
        self.button("被阻止的偷菜紀錄", log, emoji="📋", row=2)
        self._nav_back()
        return e

    async def _report(self, interaction, title: str, *args) -> None:
        """執行一個 CMD 報表指令並以私密訊息回覆（不改變面板）。"""
        await interaction.response.defer()
        try:
            text = await self.run(self._cli_text, *args)
            await interaction.followup.send(embed=discord.Embed(title=title, color=BLUE,
                                                                description=cut(f"```\n{text}\n```", 4096)),
                                            ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"❌ {clean_message(player_message(exc))}", ephemeral=True)
        if args[0] == "patrol":
            await self.refresh(interaction)

    # ======================================================================
    # 賭場
    # ======================================================================

    def _load_casino(self):
        svc = self.svc
        return {"options": svc.gamble_options(self.player_id), "prizes": svc.list_prizes(self.player_id),
                "image": self._image("賭場", farm_image.casino_png)}

    def _build_casino(self, d):
        options = d["options"]
        rows = {f"{o['item_code']}|{o['quality']}|{o['mutation']}": o for o in options}
        if self.bet and self.bet["key"] not in rows:
            self.bet = None  # 已經全部押完或賣掉
        bet = self.bet
        if bet:
            bet["qty"] = max(1, min(bet["qty"], rows[bet["key"]]["quantity"]))
        lines = [
            f"{'▶ ' if bet and bet['key'] == key else ''}{crop_emoji(o['crop_code'])} **{o['name']}**"
            f"　×**{o['quantity']}**"
            f"\n　🏷️ 價目表 {o['unit_price']} 金／個　🎲 比大小贏了 {o['unit_price'] * balance.GAMBLE_HILO_MULTIPLIER}"
            f"　🎯 猜中 {o['unit_price'] * balance.GAMBLE_EXACT_MULTIPLIER}"
            for key, o in rows.items()
        ]
        prizes = [f"🏆 {crop_emoji(p['crop_code'])} {p['name']}　×**{p['quantity']}**　（×{p['multiplier']}，"
                  f"{p['unit_price']} 金／個）" for p in d["prizes"]]
        rules_text = (
            f"🎲 系統從 **{balance.GAMBLE_MIN}–{balance.GAMBLE_MAX}** 擲一個數字\n"
            f"⬆️ **比大**（{balance.GAMBLE_BIG_FROM}–{balance.GAMBLE_MAX}）／⬇️ **比小**"
            f"（{balance.GAMBLE_MIN}–{balance.GAMBLE_BIG_FROM - 1}）："
            f"贏了價值 **×{balance.GAMBLE_HILO_MULTIPLIER}**（機率 50%）\n"
            f"🎯 **猜數字**：猜中價值 **×{balance.GAMBLE_EXACT_MULTIPLIER}**（機率 10%）\n"
            "💀 輸了：下注的作物會被沒收\n"
            "🏆 贏得的作物存進倉庫的「賭場獎品」，可以在「💰 販售」賣出或留著展示\n"
            "✅ 只能押：稀有、傳說品質，或巨型作物、黃金作物、珍稀變種"
        )
        e = discord.Embed(title="🎰 ✦ 幸運賭場 ✦", color=0x9B59B6, description=sections(
            ("規則", [rules_text]),
            ("可以下注的作物", lines, "\n\n") if lines else "背包裡沒有可以下注的作物（需要稀有以上品質或特殊突變）。",
            ("🏆 我的賭場獎品", prizes),
        ))
        if bet:
            o = rows[bet["key"]]
            e.add_field(name="🎲 這次下注", inline=False, value=(
                f"**{o['name']} ×{bet['qty']}**（價值 {money(o['unit_price'] * bet['qty'])} 金）\n"
                "👇 選擇下注數量，然後按「比大」「比小」或從選單猜一個數字"
            ))
        else:
            e.set_footer(text="👇 先從選單選一種作物")

        async def choose(interaction, key):
            self.bet = {"key": key, "qty": 1}
            await interaction.response.defer()
            await self.refresh(interaction)

        self.select("🎰 選擇要下注的作物…",
                    [(f"{o['name']} ×{o['quantity']}", key, f"🏷️ {o['unit_price']} 金／個",
                      crop_emoji(o["crop_code"])) for key, o in rows.items()],
                    choose, row=0, selected=bet["key"] if bet else None)
        if bet:
            have = rows[bet["key"]]["quantity"]
            for label, qty in (("×1", 1), ("一半", max(1, have // 2)), ("全部", have)):
                async def set_qty(interaction, qty=qty):
                    bet["qty"] = qty
                    await interaction.response.defer()
                    await self.refresh(interaction)

                current = qty == bet["qty"]
                self.button(f"{label}（{qty}）", set_qty, row=1,
                            style=discord.ButtonStyle.primary if current else discord.ButtonStyle.secondary)

            async def custom_qty(interaction):
                async def submitted(modal_interaction, q):
                    bet["qty"] = q
                    await modal_interaction.response.defer()
                    await self.refresh(modal_interaction)

                await interaction.response.send_modal(QuantityModal("下注數量", "數量", have, submitted))

            self.button("自訂數量…", custom_qty, emoji="✏️", row=1)
            code, quality, mutation = bet["key"].split("|")

            def play(mode, guess=None):
                async def handler(interaction, *_):
                    qty = bet["qty"]
                    await self.act(
                        interaction,
                        lambda: self.svc.gamble(self.player_id, code, quality, mutation, qty, mode, guess)["message"],
                        prefix="",
                    )
                return handler

            self.button(f"比大（{balance.GAMBLE_BIG_FROM}–{balance.GAMBLE_MAX}）", play("big"), emoji="⬆️", row=2,
                        style=discord.ButtonStyle.success)
            self.button(f"比小（{balance.GAMBLE_MIN}–{balance.GAMBLE_BIG_FROM - 1}）", play("small"), emoji="⬇️", row=2,
                        style=discord.ButtonStyle.danger)

            async def exact(interaction, value):
                await play("exact", int(value))(interaction)

            self.select(f"🎯 猜一個數字（中了 ×{balance.GAMBLE_EXACT_MULTIPLIER}）…",
                        [(f"猜 {n}", str(n), None, "🎯") for n in range(balance.GAMBLE_MIN, balance.GAMBLE_MAX + 1)],
                        exact, row=3)
        self.nav("賣獎品", "sell", emoji="🏆", row=4)
        self._nav_back()
        self.attach_image(e, d["image"])
        return e

    # ======================================================================
    # 偷菜
    # ======================================================================

    def _load_steal(self):
        if self.group_id is None:
            raise ValidationError("偷菜需要在 Discord 伺服器中使用（只能偷同一伺服器成員的作物）。")
        return {"targets": self.svc.steal_targets(self.group_id, self.player_id)}

    def _build_steal(self, d):
        targets = d["targets"]
        pages = max(1, -(-len(targets) // 25))
        self.steal_page = min(self.steal_page, pages - 1)
        shown = targets[self.steal_page * 25:(self.steal_page + 1) * 25]
        lines = [f"🥷 **{t.player_name}**　農田 `#{t.plot_number}`　{t.crop_name}\n"
                 f"　🧺 剩 ×{t.remaining_quantity}　👜 這次最多偷 ×{t.max_steal}" for t in shown]
        e = discord.Embed(title=f"🥷 ✦ 偷菜｜{self.group_name} ✦", color=RED, description=sections(
            ("可以偷的成熟作物", lines, "\n\n") if lines else "目前沒有可以偷的成熟作物。"
        ))
        e.set_footer(text=f"每株作物每人只能偷一次，每次最多 {balance.STEAL_MAX_PER_ACTION} 個；"
                          f"每天只能偷同一位玩家 {balance.STEAL_PLOTS_PER_VICTIM_PER_DAY} 格田；對方可能有防守設施。"
                          + (f"第 {self.steal_page + 1}/{pages} 頁" if pages > 1 else ""))

        async def steal(interaction, value):
            victim, plot, qty = (int(x) for x in value.split("|"))
            await self.act(interaction, lambda: self.svc.steal_crop(self.group_id, self.player_id, victim, plot,
                                                                    qty).message)

        self.select("🥷 選擇要偷的作物…", [(f"{t.player_name} #{t.plot_number} {t.crop_name}",
                                          f"{t.player_id}|{t.plot_number}|{t.max_steal}",
                                          f"剩 {t.remaining_quantity}，偷 {t.max_steal} 個", "🥷") for t in shown],
                    steal, row=0)
        if pages > 1:
            async def move(interaction, delta):
                self.steal_page = (self.steal_page + delta) % pages
                await self.goto(interaction, "steal")

            self.button("上一頁", lambda i: move(i, -1), emoji="◀️", row=1)
            self.button("下一頁", lambda i: move(i, 1), emoji="▶️", row=1)
        self.nav("重新整理", "steal", emoji="🔄", row=1)
        self._nav_back()
        return e

    # ======================================================================
    # 排行榜
    # ======================================================================

    def _load_board(self):
        b, svc = self.board, self.svc
        if b["scope"] == "group":
            if self.group_id is None:
                raise ValidationError("本伺服器排行需要在 Discord 伺服器中查看。")
            rows = svc.leaderboard(b["kind"], group_id=self.group_id, player_id=self.player_id)
        elif b["scope"] == "groups":
            svc.settle_due_rankings()  # 查看時順便結算已到期的排名獎勵
            rows = svc.group_leaderboard(b["kind"], b["period"])
        elif b["period"] == "all":
            rows = svc.leaderboard(b["kind"])
        else:
            rows = svc.period_leaderboard(b["kind"], b["period"])
        return {"rows": rows}

    def _build_board(self, d):
        b = self.board
        period = "全部期間" if b["scope"] == "group" else PERIOD_NAMES[b["period"]]
        e = discord.Embed(title=f"🏆 {SCOPE_NAMES[b['scope']]}｜{KIND_NAMES[b['kind']]}｜{period}", color=GOLD)
        lines = []
        for r in d["rows"]:
            medal = MEDALS.get(r["rank"], f"`{r['rank']:>2}.`")
            name = r.get("group_name") or r.get("player_name")
            if b["kind"] == "money":
                net = b["period"] != "all" and b["scope"] != "group"
                value = f"{money(r['score'])} 金" + ("（淨賺）" if net else "")
            elif b["kind"] == "sales":
                value = f"{money(r['sold_quantity'])} 個（{money(r['sales_income'])} 金）"
            else:
                value = f"{money(r['sales_income'])} 金（{money(r['sold_quantity'])} 個）"
            extra = f"｜{r['member_count']} 人" if "member_count" in r else ""
            lines.append(f"{medal} **{name}**　{value}{extra}")
        e.description = sections(("", lines, "\n\n")) if lines else "目前沒有資料。"

        async def set_value(interaction, key, value):
            self.board[key] = value
            await self.goto(interaction, "board")

        self.select(f"範圍：{SCOPE_NAMES[b['scope']]}", [(v, k, None, None) for k, v in SCOPE_NAMES.items()],
                    lambda i, v: set_value(i, "scope", v), row=0)
        self.select(f"項目：{KIND_NAMES[b['kind']]}", [(v, k, None, None) for k, v in KIND_NAMES.items()],
                    lambda i, v: set_value(i, "kind", v), row=1)
        if b["scope"] != "group":
            self.select(f"期間：{PERIOD_NAMES[b['period']]}", [(v, k, None, None) for k, v in PERIOD_NAMES.items()],
                        lambda i, v: set_value(i, "period", v), row=2)
        self.nav("排名獎勵", "rewards", emoji="🎁", row=3)
        self._nav_back()
        return e

    def _load_rewards(self):
        return {"text": self._cli_text("leaderboard", "rewards")}

    def _build_rewards(self, d):
        self.nav("回排行榜", "board", emoji="🏆", row=0)
        self._nav_back(row=0)
        return discord.Embed(title="🎁 排名獎勵", color=GOLD, description=cut(f"```\n{d['text']}\n```", 4096))

    # ======================================================================
    # 任務與競賽
    # ======================================================================

    def _load_quests(self):
        svc = self.svc
        return {"quests": svc.player_quests(self.player_id),
                "competitions": [c for c in svc.competitions() if c["state"] in ("active", "scheduled")]}

    def _build_quests(self, d):
        quests = d["quests"]
        quest_lines = [
            f"{'✅' if q['state'] == 'completed' else '⏳'} **{q['name']}**［{q['group_name']}］\n"
            f"　{bar(q['progress'], q['target'])}　{q['progress']}/{q['target']}（{q['percent']}%）\n"
            f"　🙋 我的貢獻 {q['my_contribution']}　⏰ {ts(q['ends_at'])}截止\n"
            f"　🎁 獎勵：{q['reward_text']}"
            for q in quests
        ]
        comp_lines = [
            f"⚔️ **{c['name']}**（{c['type_name']}）\n　{c['state_label']}　⏰ {ts(c['ends_at'])}結束"
            for c in d["competitions"]
        ]
        e = discord.Embed(title="📜 ✦ 群組任務與競賽 ✦", color=BLUE, description=sections(
            ("📋 群組任務", quest_lines, "\n\n") if quest_lines else "📋 目前沒有任務。",
            ("⚔️ 競賽", comp_lines, "\n\n") if comp_lines else "⚔️ 目前沒有進行中的競賽。",
        ))
        e.set_footer(text="只有在農場所屬伺服器中的操作才會計入該群組的任務與競賽。")
        claimable = [q for q in quests if q["state"] == "completed" and q["my_contribution"] > 0]

        async def claim(interaction, value):
            gid, qid = (int(x) for x in value.split("|"))
            await self.act(interaction, lambda: self.svc.claim_quest(gid, self.player_id, qid).message)

        self.select("🎁 領取任務獎勵…", [(q["name"], f"{q['group_id']}|{q['quest_id']}", q["reward_text"], "🎁")
                                         for q in claimable], claim, row=0)

        async def detail(interaction, cid):
            self.detail = {"competition_id": int(cid)}
            await self.goto(interaction, "competition")

        self.select("⚔️ 查看競賽排名…", [(c["name"], str(c["competition_id"]), c["type_name"], "⚔️")
                                         for c in d["competitions"]], detail, row=1)
        self.nav("已結算的競賽", "history", emoji="📚", row=2)
        self._nav_back()
        return e

    def _load_competition(self):
        return {"text": self._cli_text("competition", "leaderboard", self.detail["competition_id"]),
                "status": self._cli_text("competition", "status", self.detail["competition_id"])}

    def _build_competition(self, d):
        self.nav("回任務競賽", "quests", emoji="📜", row=0)
        self._nav_back(row=0)
        return discord.Embed(title="⚔️ 競賽", color=BLUE,
                             description=cut(f"```\n{d['status']}\n\n{d['text']}\n```", 4096))

    def _load_history(self):
        return {"text": self._cli_text("competition", "history")}

    def _build_history(self, d):
        self.nav("回任務競賽", "quests", emoji="📜", row=0)
        self._nav_back(row=0)
        return discord.Embed(title="📚 已結算的競賽", color=BLUE, description=cut(f"```\n{d['text']}\n```", 4096))

    # ======================================================================
    # 群組與轉移
    # ======================================================================

    def _load_group(self):
        if self.group_id is None:
            raise ValidationError("群組資訊需要在 Discord 伺服器中查看。")
        svc = self.svc
        return {"group": svc.get_group(self.group_id), "members": svc.group_members(self.group_id),
                "transfer": svc.group_transfer_status(self.player_id, self.group_id)}

    def _build_group(self, d):
        g, t = d["group"], d["transfer"]
        members = sorted((m for m in d["members"] if m["farm"]), key=lambda m: -m["farm"]["gold"])
        e = discord.Embed(title=f"👥 ✦ {g.name} ✦", color=BLUE, description=sections(
            f"👥 成員 **{g.member_count}** 人",
            ("成員（依金幣）", [
                f"{'🏠' if m['farm']['ranked_in_group'] else '👤'} **{m['player_name']}**　⭐ Lv {m['farm']['level']}\n"
                f"　💰 {money(m['farm']['gold'])} 金　✅ 可收成 {m['farm']['ready']} 格" for m in members[:20]
            ], "\n\n"),
        ))
        here = t["home_group_id"] == self.group_id
        if here:
            note = "你的農場屬於這個伺服器。"
        elif t["can_transfer"]:
            note = f"你的農場屬於「{t['home_group_name']}」，可以轉移到這裡。"
        else:
            note = f"你的農場屬於「{t['home_group_name']}」；轉移冷卻中，{ts(t['next_transfer_at'])}可再轉移。"
        e.add_field(name="🏠 我的所屬群組", value=note, inline=False)
        e.set_footer(text="🏠 = 農場屬於此群組（列入群組排行）。轉移後需等 7 天才能再轉移。")

        async def transfer(interaction):
            if self.confirm != "transfer":
                await interaction.response.defer()
                self.confirm = "transfer"
                await self.refresh(
                    interaction,
                    f"⚠️ 確定把農場從「{t['home_group_name']}」轉移到「{g.name}」嗎？"
                    "金幣、物品、農田都不變；之後的群組排行、任務與競賽改以新群組計算；"
                    f"轉移後 {long_duration(balance.GROUP_TRANSFER_COOLDOWN_SECONDS)}內不能再轉移。"
                    "再按一次「確認轉移」。",
                )
                return
            await self.act(interaction, lambda: self.svc.transfer_home_group(self.player_id, self.group_id).message)

        if not here:
            confirming = self.confirm == "transfer"
            self.button("確認轉移" if confirming else "把農場轉移到這個伺服器", transfer, emoji="🔁", row=0,
                        disabled=not t["can_transfer"],
                        style=discord.ButtonStyle.danger if confirming else discord.ButtonStyle.primary)
        self._nav_back()
        return e

    # ======================================================================
    # 山洞冒險
    # ======================================================================

    @staticmethod
    def _area_status(a, trip) -> str:
        if not a["unlocked"]:
            return f"🔒 農場 Lv.{a['unlock_level']} 解鎖"
        if a["exploring"]:
            return "📦 已經回來了，等你領取獎勵" if trip["finished"] else f"🧭 探險中，{ts(trip['ends_at'])}回來"
        if trip:
            return f"⏸️ 正在{trip['area_name']}探險，回來後才能出發"
        return "✅ 可以出發"

    @staticmethod
    def _trip_text(trip) -> str:
        if trip is None:
            return "🏕️ 目前沒有在探險，選一個區域出發吧！"
        if trip["finished"]:
            return f"📦 {trip['emoji']} **{trip['area_name']}**探險回來了！按「領取探險獎勵」把物品放進背包。"
        return (f"🧭 正在 {trip['emoji']} **{trip['area_name']}**探險，{ts(trip['ends_at'])}回來"
                f"（{ts(trip['ends_at'], 'f')}）")

    def _claim_button(self, trip, row: int) -> None:
        if trip and trip["finished"]:
            async def claim(interaction):
                await self.act(interaction, self._claim, page="cave_result")

            self.button("領取探險獎勵", claim, emoji="📦", row=row, style=discord.ButtonStyle.success)

    def _area_image(self, area_code: str) -> bytes | None:
        return self._image("探險區域", lambda: farm_image.explore_png(area_code))

    def _load_cave(self):
        d = self.svc.exploration_overview(self.player_id)
        d["image"] = self._area_image(d["trip"]["area"]) if d["trip"] else None  # 探險中顯示所在區域
        return d

    def _build_cave(self, d):
        trip = d["trip"]
        e = discord.Embed(title="⛰️ ✦ 山洞冒險 ✦", color=GOLD, description=sections(
            f"👨‍🌾 **{d['player_name']}**　⭐ 農場 **Lv {d['level']}**",
            "探索三個區域收集素材、稀有種子與假幣，素材可以製作自動化機器（「🏭 加工」→「🔧 機器」）。\n"
            "一次只能去一個區域；出發後要等探險時間結束才會回來，回來後領取獎勵才能再出發。\n"
            "素材與假幣放進獨立的素材背包，不佔倉庫。",
            self._trip_text(trip),
        ))
        for a in d["areas"]:
            e.add_field(
                name=f"{a['emoji']} {a['name']}（Lv.{a['unlock_level']}）", inline=False,
                value=f"{self._area_status(a, trip)}　🕒 探險 {long_duration(a['duration_seconds'])}\n"
                      f"🪨 主要素材：{'、'.join(a['common'][:3])}",
            )
        for a in d["areas"]:
            async def open_area(interaction, code=a["code"]):
                self.cave_area = code
                await self.goto(interaction, "cave_area")

            style = discord.ButtonStyle.success if a["ready"] or a["exploring"] else discord.ButtonStyle.secondary
            self.button(a["name"], open_area, emoji=a["emoji"], row=0, style=style)
        self._claim_button(trip, row=1)
        self.nav("探索紀錄", "cave_log", emoji="📖", row=1)
        self.nav("機器", "machines", emoji="🔧", row=1)
        self.nav("返回主選單", "main", emoji="↩️", row=1)
        self.attach_image(e, d["image"])
        return e

    def _load_cave_area(self):
        d = self.svc.exploration_overview(self.player_id)
        d["area"] = next(a for a in d["areas"] if a["code"] == (self.cave_area or exploration.AREA_ORDER[0]))
        d["image"] = self._area_image(d["area"]["code"])
        return d

    def _build_cave_area(self, d):
        a, level, trip = d["area"], d["level"], d["trip"]

        def unlocked(min_level):
            return "" if level >= min_level else f"（Lv.{min_level}）"

        notes = [f"{'✅' if level >= lv else '🔒'} Lv.{lv} {text}" for lv, text in a["level_notes"]]
        kind = "🌟 高級探索" if a["advanced"] else f"普通探索（Lv.{a['advanced_level']} 起高級探索）"
        e = discord.Embed(title=f"{a['emoji']} ✦ {a['name']} ✦", color=GREEN if a["ready"] else GREY,
                          description=sections(
                              a["description"],
                              f"🔑 解鎖：農場 **Lv.{a['unlock_level']}**（你是 Lv {level}）\n"
                              f"🕒 探險時間：{long_duration(a['duration_seconds'])}　"
                              f"🎲 每次 {a['rolls']} 份獎勵　{kind}\n"
                              f"📌 狀態：{self._area_status(a, trip)}",
                              ("🪨 主要素材", ["、".join(a["common"])]),
                              ("💎 稀有素材", ["、".join(a["rare"]) or "—"]),
                              ("🌰 稀有種子", ["、".join(a["seeds"]) or "這裡沒有稀有種子"]),
                              ("🎁 特殊發現", [f"{name}{unlocked(lv)}" for name, lv in a["events"]] + ["🪙 假幣"]),
                              ("⭐ 等級解鎖", notes),
                          ))

        async def explore(interaction):
            if self.exploring:  # 同一面板連點：第一次還在處理，忽略後續點擊（後端另有一次一趟的保護）
                await interaction.response.defer()
                return
            self.exploring = True
            try:
                await self.act(interaction, lambda: self._explore(a["code"]), page="cave")
            finally:
                self.exploring = False

        if a["exploring"] and trip["finished"]:
            self._claim_button(trip, row=0)
        else:
            if not a["unlocked"]:
                label = "尚未解鎖"
            elif a["exploring"]:
                label = "探險中"
            elif trip:
                label = "正在其他區域探險"
            else:
                label = "出發探險"
            self.button(label, explore, emoji="🧭", row=0, disabled=not a["ready"],
                        style=discord.ButtonStyle.success if a["ready"] else discord.ButtonStyle.secondary)
        self.nav("返回冒險介面", "cave", emoji="↩️", row=0)
        self._nav_back(row=0)
        self.attach_image(e, d["image"])
        return e

    def _explore(self, code: str) -> str:
        r = self.svc.explore(self.player_id, code)
        return (f"{r['emoji']} 出發前往{r['area_name']}！探險 {long_duration(r['duration_seconds'])}，"
                f"{ts(r['ends_at'])}回來後記得領取獎勵。")

    def _claim(self) -> str:
        self.cave_result = self.svc.claim_exploration(self.player_id)
        return f"{self.cave_result['emoji']} 從{self.cave_result['area_name']}探險回來了！"

    def _load_cave_result(self):
        if self.cave_result is None:
            raise ValidationError("目前沒有探索結果，請回冒險介面選擇區域探索。")
        return {**self.cave_result, "image": self._area_image(self.cave_result["area"])}

    def _build_cave_result(self, r):
        items = [f"{MATERIAL_EMOJI.get(i['code'], crop_emoji(i['code'].removeprefix('seed_')))} {i['name']}"
                 f"　×**{i['quantity']}**　（{i['rarity']}）" for i in r["items"]]
        seeds = "、".join(f"{n} ×{q}" for n, q in r["seeds"].items())
        coins = f"🎉 {r['fake_coins']} 個" if r["fake_coins"] else "沒有"
        e = discord.Embed(title=f"{r['emoji']} ✦ {r['area_name']}探索結果 ✦", color=GOLD, description=sections(
            ("🎒 獲得物品", items or ["這次什麼都沒找到…"]),
            f"🌰 稀有種子：{'🎉 ' + seeds if seeds else '沒有'}\n"
            f"🪙 假幣：{coins}\n"
            f"🎁 特殊事件：{'、'.join(r['events']) if r['events'] else '沒有'}"
            + ("\n🌟 高級探索" if r["advanced"] else ""),
            "🧭 獎勵已放進背包，現在可以再出發探險了！",
        ))
        self.nav("返回冒險介面", "cave", emoji="🔄", row=0, style=discord.ButtonStyle.primary)
        self.nav("返回主選單", "main", emoji="🏠", row=0)
        self.attach_image(e, r["image"])
        return e

    def _load_cave_log(self):
        return {"logs": self.svc.exploration_logs(self.player_id, 10)}

    def _build_cave_log(self, d):
        lines = [
            f"{x['emoji']} **{x['area_name']}**　{ts(x['explored_at'])}{'　🌟高級' if x['advanced'] else ''}\n"
            "　🎒 " + ("、".join(f"{n} ×{q}" for n, q in x["rewards"].items()) or "一無所獲")
            + (f"\n　🎁 {'、'.join(x['events'])}" if x["events"] else "")
            for x in d["logs"]
        ]
        e = discord.Embed(title="📖 ✦ 探索紀錄 ✦", color=BLUE,
                          description=sections(("最近 10 次探索", lines, "\n\n"), "" if lines else "還沒有探索紀錄。"))
        self.nav("返回冒險介面", "cave", emoji="↩️", row=0)
        self._nav_back(row=0)
        return e

    # ======================================================================
    # 機器（素材製作；種子製造機、稀有作物培育器、自動加工機設定）
    # ======================================================================

    def _load_machines(self):
        svc, pid = self.svc, self.player_id
        return {"status": svc.machines_status(pid), "backpack": svc.get_backpack(pid),
                "incubator": svc.incubator_recipes(), "recipes": svc.recipes(pid)}

    def _build_machines(self, d):
        st, bp = d["status"], d["backpack"]
        machines = {m["code"]: m for m in st["machines"]}
        blocks = []
        for m in st["machines"]:
            if m["owned"]:
                state = "✅ 已擁有，" + ("啟用中" if m["enabled"] else "已停用")
            elif not m["unlocked"]:
                state = f"🔒 農場 Lv.{m['unlock_level']} 解鎖"
            elif m["missing"]:
                state = "🧺 材料不足"
            else:
                state = "🛠️ 可以製作"
            mats = "、".join(f"{'✅' if have >= need else '❌'}{name} {have}/{need}"
                            for name, need, have in m["materials"])
            extra = ""
            if m["code"] == "auto_processor" and m["owned"]:
                recipe = m["settings"].get("recipe")
                extra = f"\n　⚙️ 加工項目：{balance.RECIPES[recipe][0] if recipe in balance.RECIPES else '未設定'}"
            blocks.append(f"{m['emoji']} **{m['name']}**（Lv.{m['unlock_level']}）　{state}\n　📝 {m['description']}"
                          + ("" if m["owned"] else f"\n　🧱 {mats}") + extra)
        e = discord.Embed(title="🔧 ✦ 機器 ✦", color=BLUE, description=sections(
            f"⭐ 農場 Lv {st['level']}　素材從「⛰️ 山洞」探索取得",
            ("機器", blocks, "\n\n"),
        ))
        if self.picker:
            e.add_field(name="🛒 已選擇", inline=False, value=f"**{self.picker['label']}**\n👇 選擇數量")

        def build_hint(m) -> str:
            if not m["unlocked"]:
                return "等級不足"
            if m["missing"]:
                return "缺少 " + "、".join(f"{k}×{v}" for k, v in m["missing"].items())
            return "可以製作"

        async def build(interaction, code):
            await self.act(interaction, lambda: self.svc.build_machine(self.player_id, code).message)

        self.select("🛠️ 製作機器（消耗素材）…",
                    [(m["name"], m["code"], f"Lv.{m['unlock_level']}｜{build_hint(m)}", m["emoji"])
                     for m in st["machines"] if not m["owned"]], build, row=0)

        options = []
        if machines["seed_maker"]["owned"]:
            rare = {c for c, *_rest in exploration.RARE_CROPS}
            for x in bp.produce:
                if (x.quality in exploration.SEED_MAKER_QUALITIES and x.mutation in exploration.SEED_MAKER_MUTATIONS
                        and (exploration.SEED_MAKER_RARE_CROPS or x.code not in rare)):
                    options.append((f"種子製造：{x.name}（持有 {x.quantity}）", f"seed:{x.code}",
                                    f"1 個{x.name} → {exploration.SEED_MAKER_RATIO} 顆種子", "🌱"))
        if machines["rare_incubator"]["owned"]:
            options += [(f"培育：{r['name']}", f"incubate:{r['code']}",
                         "、".join(f"{k}×{v}" for k, v in r["inputs"].items()) + f"｜{long_duration(r['seconds'])}／份",
                         "🧬") for r in d["incubator"]]
        if machines["auto_processor"]["owned"]:
            options += [(f"自動加工設為：{r['name']}", f"proc:{r['code']}", r["building_name"], "🏭")
                        for r in d["recipes"]]
            options.append(("自動加工：不自動開單", "proc:", "只自動領取完成的加工品", "🏭"))

        async def use(interaction, value):
            kind, code = value.split(":", 1)
            if kind == "proc":
                await self.act(interaction, lambda: self.svc.set_processor_recipe(self.player_id, code or None).message)
                return
            if kind == "seed":
                stack = next((x for x in bp.produce if x.code == code and x.quality == "normal"
                              and x.mutation == "none"), None)
                name, have = (stack.name, stack.quantity) if stack else (code, 1)
                ratio = exploration.SEED_MAKER_RATIO
                self.picker = {"key": value, "label": f"🌱 種子製造：{name}（1 個 → {ratio} 顆）", "verb": "用",
                               "max": max(1, min(have, exploration.SEED_MAKER_MAX)), "presets": (1, 5, 10),
                               "run": lambda q: self.svc.make_seeds(self.player_id, code, q).message}
            else:
                r = next(x for x in d["incubator"] if x["code"] == code)
                self.picker = {"key": value, "label": f"🧬 {r['name']}", "verb": "培育",
                               "max": exploration.INCUBATOR_MAX_QUANTITY, "presets": (1, 3, 5),
                               "run": lambda q: self.svc.start_incubation(self.player_id, code, q).message}
            await interaction.response.defer()
            await self.refresh(interaction)

        self.select("⚙️ 使用機器…", options[:25], use, row=1, selected=(self.picker or {}).get("key"))
        self.quantity_buttons(row=2)
        for m in st["machines"]:
            if m["owned"]:
                async def toggle(interaction, m=m):
                    await self.act(interaction, lambda: self.svc.set_machine_enabled(
                        self.player_id, m["code"], not m["enabled"]).message)

                self.button(f"{'停用' if m['enabled'] else '啟用'}{m['name']}", toggle, emoji=m["emoji"], row=3)
        self.nav("加工", "craft", emoji="🏭", row=4)
        self.nav("山洞", "cave", emoji="⛰️", row=4)
        self._nav_back(row=4)
        return e

    # ======================================================================
    # 紀錄與說明
    # ======================================================================

    def _load_log(self):
        return {"txs": self.svc.get_recent_transactions(self.player_id, 15)}

    def _build_log(self, d):
        lines = []
        for t in d["txs"]:
            delta = f"`{t.gold_delta:+,}`" if t.gold_delta else ""
            label = EVENT_TYPE_LABELS.get(t.event_type, t.event_type)
            lines.append(f"🕒 {ts(t.created_at)}　**{label}** {delta}\n　{cut(t.description, 120)}")
        self._nav_back(row=0)
        return discord.Embed(title="🧾 ✦ 最近的操作紀錄 ✦", color=GREY,
                             description=sections(("", lines, "\n\n")) if lines else "目前沒有紀錄。")

    def _load_help(self):
        return {}

    def _build_help(self, _d):
        self._nav_back(row=0)
        return help_embed()


def help_embed() -> discord.Embed:
    e = discord.Embed(title="❓ 怎麼玩", color=BLUE, description=(
        "輸入 **/farm** 打開農場面板，所有操作都用按鈕和選單完成，面板只有你看得到。"))
    e.add_field(name="🌱 種田", inline=False, value=(
        "「🌱 農田」選作物會種滿空地（種子不夠會自動買）→「💧 全部澆水」→ 等成熟 →「🧺 全部收成」→「💰 販售」。\n"
        f"農田最多 {balance.MAX_PLOTS} 格。成熟後 16 小時沒收成會乾枯。\n"
        "化肥讓剩餘時間減半；蚯蚓讓之後澆水的作物長得更快；自動澆水器種下就澆水。"))
    e.add_field(name="🐔 養殖與加工", inline=False, value=(
        "先建造畜舍再買動物，餵食後等待生產再收集。磨坊、烘焙坊、果醬工坊可以把作物加工成更值錢的商品。"))
    e.add_field(name="🥷 偷菜與防守", inline=False, value=(
        "可以偷同一伺服器成員已成熟的作物；稻草人、警戒犬、防盜圍欄能阻止別人偷你的菜，巡查券可以查是誰來偷。"))
    e.add_field(name="⛰️ 山洞冒險", inline=False, value=(
        "總覽「⛰️ 山洞」探索青青草原（Lv.1）、古老森林（Lv.5）、神秘礦坑（Lv.10），分別探險 8／16／24 小時，"
        "一次只能去一個區域，回來後領取獎勵才能再出發；可取得素材、稀有種子與假幣（背包兌換，1 個 = 100 金）。"
        "素材可在「🔧 機器」製作種子製造機、自動收割機、"
        "自動加工機與稀有作物培育器。"))
    e.add_field(name="👥 群組", inline=False, value=(
        "每個 Discord 伺服器是一個群組。你的農場屬於建立時的伺服器，只有在那裡的操作才計入群組任務與競賽；"
        "可在「👥 群組」把農場轉移到目前的伺服器（每 7 天一次）。"))
    e.add_field(name="🏆 排行與獎勵", inline=False, value=(
        "排行榜有個人、本伺服器與群組總排行，可切換金幣、農產品販售、商品總販售與期間；排名獎勵會自動發放。"))
    e.add_field(name="🎵 聽音樂", inline=False, value=(
        "先進入語音頻道，再用 **/play** 點歌（關鍵字、YouTube／Spotify 連結）。\n"
        "/nowplaying 顯示控制面板（暫停、跳過、循環、音量）、/queue 看清單；完整說明請用 **/music**。"))
    return e
