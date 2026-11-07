"""農場面板的釣魚頁面（FarmPanel 的 mixin）：釣魚中心、釣魚過程、魚簍、魚的詳細資料、圖鑑、排行榜與釣具商店。

- 不使用圖片，全部以 Emoji、文字與數值呈現。
- 釣魚過程的狀態存在資料庫（fishing_sessions），面板只負責顯示；Bot 重啟後重新開啟面板即可繼續。
- 等魚上鉤、拉竿反應與拉扯操作的時間到時，面板會自動更新（_schedule_fish_timer）；伺服器端另外依時間判定逾時。
- 拉扯操作附上畫面上的回合數，重複點擊不會重複執行；分享沿用「分享農場」的方式（在目前頻道發送公開訊息）。
"""

import asyncio
import logging
from datetime import UTC, datetime

import discord

from app.discord_bot.runner import player_message
from app.game import fishing
from app.game.errors import GameError, ValidationError

logger = logging.getLogger(__name__)

SEA, GOLD, GREEN, RED, GREY = 0x3498DB, 0xF1C40F, 0x57F287, 0xED4245, 0x99AAB5
RARITY_COLOR = {"common": 0xBDC3C7, "rare": 0x3498DB, "epic": 0x9B59B6, "legendary": 0xF1C40F}
LINE = "━━━━━━━━━━━━━━"
BASKET_PAGE = 25
TIMER_SLACK_SECONDS = 0.4  # 上鉤時間到後多等一點再更新
# 期限（拉竿、拉扯操作）到期時，伺服器另有網路延遲寬限，要等寬限過後再更新才會看到結果，避免連續重新整理
DEADLINE_SLACK_SECONDS = fishing.REACTION_GRACE_SECONDS + 0.5
CELEBRATE = {
    "common": "🎣 收穫不錯！", "rare": "✨ 釣到稀有魚了！", "epic": "🔥 太厲害了，是史詩魚！",
    "legendary": "🎉 恭喜獲得傳說魚！",
}


def _ts(dt: datetime | None, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "—"


def _bar(value: int, total: int, width: int = 10) -> str:
    filled = 0 if total <= 0 else min(width, max(0, round(width * value / total)))
    return "▰" * filled + "▱" * (width - filled)


def rarity_tag(r: str) -> str:
    return f"{fishing.RARITY_EMOJI[r]} {fishing.RARITY_NAMES[r]}"


def fish_line(f: dict, n: int | None = None) -> str:
    num = f"`{n:>2}` " if n is not None else ""
    record = "　🏆" if f.get("record") else ""
    return (f"{num}{f['rarity_emoji']} {f['emoji']} **{f['name']}**　⚖️ {f['weight_text']}"
            f"　💰 {f['price']} 金{record}")


class FishingPages:
    """需要 FarmPanel 提供 svc、run、button、nav、select、refresh、act、show_error、_in_time、quantity_buttons。"""

    fish_cache: dict | None = None
    fish_timer: asyncio.Task | None = None
    fish_due: tuple[datetime, float] | None = None  # (自動更新的時間點, 額外等待秒數)
    fish_last_due: datetime | None = None
    fish_timers_enabled = True
    fish_selected: int | None = None
    fish_sell_ids: list[int] | None = None
    fish_page = 0
    fish_rank_global = False

    # ---------- 共用 ----------

    async def _fish_call(self, interaction: discord.Interaction, work, notice: str | None = None) -> None:
        """執行釣魚操作（在執行緒中），結果直接顯示在釣魚畫面，不再重複查詢資料庫。"""
        loop = asyncio.get_running_loop()
        started = loop.time()

        async def perform():
            try:
                return await self.run(work), None
            except Exception as exc:
                if not isinstance(exc, GameError):
                    logger.exception("釣魚操作失敗（玩家 #%s）", self.player_id)
                return None, player_message(exc)

        view, error = await self._in_time(interaction, perform())
        if error is not None:
            await self.show_error(interaction, error)
            return
        if view.get("stale"):
            notice = "⚠️ 剛才那一下已經處理過了，請依最新畫面操作。"
        self.fish_cache = view
        self.page, self.picker, self.confirm = "fish_game", None, None
        from app.discord_bot.panels import DIRECT_REPLY_SECONDS  # 避免循環匯入

        await self.refresh(interaction, notice, budget=DIRECT_REPLY_SECONDS - (loop.time() - started))

    def _schedule_fish_timer(self, interaction: discord.Interaction) -> None:
        """釣魚畫面在上鉤、反應期限或拉扯逾時的時間點自動更新（同一面板只保留一個計時器）。"""
        current = asyncio.current_task()
        if self.fish_timer is not None and self.fish_timer is not current and not self.fish_timer.done():
            self.fish_timer.cancel()
        self.fish_timer = None
        if self.page != "fish_game" or self.fish_due is None or not self.fish_timers_enabled:
            return
        due, slack = self.fish_due
        self.fish_due = None
        delay = (due - datetime.now(UTC)).total_seconds() + slack
        if delay <= 0 and due == self.fish_last_due:
            return  # 同一個期限已經自動更新過一次：不再重複（玩家可以手動操作）
        self.fish_last_due = due

        async def wake():
            await asyncio.sleep(max(0.0, delay))
            if self.page == "fish_game" and not self.is_finished():
                try:
                    await self.refresh(interaction, metric=None)  # 自動更新不算玩家操作的延遲
                except Exception:  # 計時更新失敗（例如互動權杖過期）不影響遊戲，玩家可手動重新整理
                    logger.warning("釣魚畫面自動更新失敗", exc_info=True)

        try:
            self.fish_timer = asyncio.get_running_loop().create_task(wake())
        except RuntimeError:
            self.fish_timer = None

    def _cancel_fish_timer(self) -> None:
        if self.fish_timer is not None and not self.fish_timer.done():
            self.fish_timer.cancel()
        self.fish_timer = None

    # ======================================================================
    # 釣魚中心
    # ======================================================================

    def _load_fishing(self):
        return {"o": self.svc.fishing_overview(self.player_id), "shop": self.svc.fishing_shop(self.player_id)}

    def _build_fishing(self, d):
        o = d["o"]
        rod = f"{o['rod']['emoji']} {o['rod']['name']}" if o["rod"] else "❌ 尚未裝備"
        line = f"{o['line']['emoji']} {o['line']['name']}" if o["line"] else "❌ 尚未裝備"
        bait = f"{o['bait']['name']} **{o['bait_quantity']}** 個" if o["bait"] else "❌ 尚未裝備"
        st = o["stats"]
        rate = f"{st['landed'] * 100 // st['sessions']}%" if st["sessions"] else "—"
        chances = "　".join(f"{fishing.RARITY_EMOJI[r]}{o['chances'][r]:.1f}%" for r in fishing.RARITIES)
        if o["active"]:
            status = "🎣 釣魚中！按「繼續釣魚」回到釣竿旁"
        elif not (o["rod"] and o["line"] and o["bait"]):
            status = "🛒 先到「釣具商店」買魚竿、魚線和魚餌"
        elif o["bait_quantity"] <= 0:
            status = "🪱 魚餌用完了，到「釣具商店」補充"
        elif o["basket"] >= o["basket_capacity"]:
            status = "🧺 魚簍滿了，先賣掉一些魚"
        else:
            status = "準備釣魚"
        e = discord.Embed(title="🎣 釣魚中心", color=SEA, description=(
            f"{LINE}\n"
            f"🎋 魚竿：{rod}\n🪢 魚線：{line}\n🪱 魚餌：{bait}\n"
            f"🧺 魚簍：**{o['basket']} / {o['basket_capacity']}**\n"
            f"{LINE}\n"
            f"🌊 目前狀態：**{status}**\n\n"
            f"🎲 這次魚餌的出現機率：{chances}\n"
            f"✅ 基礎成功率 **{o['base_success_pct']}%**（稀有度只影響拉扯難度）\n"
            f"📊 你的紀錄：釣魚 {st['sessions']} 次｜成功 {st['landed']}（{rate}）｜🟡 傳說 {st['legendary']}"
        ))
        e.set_footer(text="魚上鉤後要在時限內按「拉竿」，拉扯時看 💬 提示：魚要衝刺就放線或等待，平靜時收線。")

        async def cast(interaction):
            await self._fish_call(interaction, lambda: self.svc.start_fishing(self.player_id))

        async def resume(interaction):
            self.fish_cache = None
            await self.goto(interaction, "fish_game")

        if o["active"]:
            self.button("繼續釣魚", resume, emoji="🎣", style=discord.ButtonStyle.success, row=0)
        else:
            self.button("開始釣魚", cast, emoji="🎣", style=discord.ButtonStyle.success, row=0)
        self.nav("我的魚簍", "fish_basket", emoji="🎒", row=0)
        self.nav("魚類圖鑑", "fish_codex", emoji="📖", row=0)
        self.nav("釣具商店", "fish_shop", emoji="🛒", row=1)
        self.nav("釣魚排行榜", "fish_rank", emoji="🏆", row=1)
        self.nav("回總覽", "main", emoji="🏠", row=1)
        self._equip_select(d["shop"]["items"], row=2)
        return e

    def _equip_select(self, items: list[dict], row: int) -> None:
        options = [
            (f"裝備{i['kind_name']}：{i['name']}" + (f"（{i['quantity']} 個）" if i["kind"] == "bait" else ""),
             i["code"], i["effects_text"], i["emoji"])
            for i in items if i["owned"] and not i["equipped"]
        ]

        async def equip(interaction, code):
            await self.act(interaction, lambda: self.svc.equip_fishing_gear(self.player_id, code).message)

        self.select("🔧 更換裝備…", options, equip, row=row)

    # ======================================================================
    # 釣魚過程
    # ======================================================================

    def _load_fish_game(self):
        if self.fish_cache is not None:
            view, self.fish_cache = self.fish_cache, None
            return view
        view = self.svc.fishing_state(self.player_id)
        if view is None:
            raise ValidationError("還沒有開始釣魚，請到「🎣 釣魚中心」按「開始釣魚」。")
        return view

    def _build_fish_game(self, v):
        self.fish_due = None
        phase = v["phase"]
        if phase == "waiting":
            return self._build_waiting(v)
        if phase == "bite":
            return self._build_bite(v)
        if phase == "fighting":
            return self._build_fight(v)
        return self._build_result(v)

    def _give_up_button(self, v, row: int) -> None:
        async def give_up(interaction):
            await self._fish_call(interaction, lambda: self.svc.give_up_fishing(self.player_id, v["id"]))

        self.button("收竿放棄", give_up, emoji="🛑", row=row, style=discord.ButtonStyle.danger)

    def _build_waiting(self, v):
        self.fish_due = (v["bite_at"], TIMER_SLACK_SECONDS)
        e = discord.Embed(title="🎣 你已經拋出魚竿！", color=SEA, description=(
            f"🌊 水面很平靜……\n\n🐟 好像有東西靠近了！\n\n{LINE}\n"
            f"🎋 {v['rod']}　🪢 {v['line']}　🪱 {v['bait']}\n"
            "👀 魚上鉤時這個畫面會自動更新，「🎣 拉竿」按鈕亮起就快按！"
        ))
        self.button("拉竿", self._noop, emoji="🎣", row=0, disabled=True)
        self._give_up_button(v, row=0)
        return e

    async def _noop(self, interaction):
        await interaction.response.defer()

    def _build_bite(self, v):
        self.fish_due = (v["react_deadline"], DEADLINE_SLACK_SECONDS)

        async def hook(interaction):
            await self._fish_call(interaction, lambda: self.svc.hook_fish(self.player_id, v["id"]))

        e = discord.Embed(title="‼️ 魚上鉤了！", color=RED, description=(
            f"⚡ **快點拉竿！**\n\n⏰ {_ts(v['react_deadline'])}之前按下「🎣 拉竿」\n\n{LINE}"
        ))
        self.button("拉竿", hook, emoji="🎣", row=0, style=discord.ButtonStyle.success)
        self._give_up_button(v, row=0)
        return e

    def _build_fight(self, v):
        self.fish_due = (v["action_deadline"], DEADLINE_SLACK_SECONDS)
        tension_warn = " ⚠️" if v["tension"] >= v["max_tension"] * 0.75 else ""
        far_warn = " ⚠️" if v["distance"] >= v["escape_distance"] * 0.8 else ""
        e = discord.Embed(title="🎣 正在與魚搏鬥！", color=RARITY_COLOR[v["rarity"]], description=(
            f"🐟 對手：**{rarity_tag(v['rarity'])}**級的魚　🔁 第 {v['turn'] + 1} 回合\n"
            f"{LINE}\n"
            f"🐟 魚的體力：**{v['stamina']} / {v['max_stamina']}**\n{_bar(v['stamina'], v['max_stamina'])}\n\n"
            f"📏 魚的距離：**{v['distance']} / {v['escape_distance']}**{far_warn}\n"
            f"{_bar(v['distance'], v['escape_distance'])}\n\n"
            f"🪢 魚線張力：**{v['tension']} / {v['max_tension']}**{tension_warn}\n"
            f"{_bar(v['tension'], v['max_tension'])}\n"
            f"{LINE}\n"
            f"💬 **{v['intent_text']}**\n"
            + (f"📝 {v['last_text']}\n" if v.get("last_text") else "")
            + f"⏰ {_ts(v['action_deadline'])}內要操作，不然魚會跑掉"
        ))
        e.set_footer(text="收線：拉近、張力↑、消耗魚的體力｜放線：張力大降、魚會游遠｜等待：張力下降、穩住魚竿")
        turn = v["turn"]
        for action, emoji, style in (("reel", "🎣", discord.ButtonStyle.primary),
                                     ("release", "↩️", discord.ButtonStyle.secondary),
                                     ("wait", "⏳", discord.ButtonStyle.secondary)):
            async def do(interaction, action=action):
                await self._fish_call(
                    interaction, lambda: self.svc.fishing_action(self.player_id, v["id"], action, turn))

            self.button(fishing.ACTION_NAMES[action], do, emoji=emoji, row=0, style=style)
        self._give_up_button(v, row=1)
        return e

    def _build_result(self, v):
        async def again(interaction):
            await self._fish_call(interaction, lambda: self.svc.start_fishing(self.player_id))

        fish = v.get("fish")
        if v["status"] == "landed" and fish:
            rec = v.get("record") or {}
            record = ("🏆 個人紀錄：你釣過最重的" + ("魚！" if rec.get("overall") else f"{fish['name']}！")
                      if rec.get("species") else "")
            e = discord.Embed(title=f"🎉 釣到了 {fish['emoji']} {fish['name']}！", color=RARITY_COLOR[fish["rarity"]],
                              description=(
                                  f"{LINE}\n"
                                  f"🐟 魚類：**{fish['name']}**\n"
                                  f"{fish['rarity_emoji']} 稀有度：**{fish['rarity_name']}**\n"
                                  f"⚖️ 重量：**{fish['weight_text']}**\n"
                                  f"💰 價值：**{fish['price']}** 金幣\n"
                                  + (f"{record}\n" if record else "")
                                  + f"{LINE}\n{CELEBRATE[fish['rarity']]}\n🧺 已放進魚簍"
                              ))
            self.fish_selected = fish["id"]
            self.button("再釣一次", again, emoji="🎣", style=discord.ButtonStyle.success, row=0)

            async def share(interaction):
                await self._share_fish(interaction, fish["id"])

            self.button("分享到群組", share, emoji="📢", row=0, disabled=self.group_id is None)
        else:
            e = discord.Embed(title=f"😢 {v['status_name']}", color=GREY, description=(
                f"{v.get('last_text') or ''}\n\n{LINE}\n"
                f"這次的對手是 **{rarity_tag(v['rarity'])}** 級的魚。\n"
                "💡 拉扯時看 💬 提示：魚要衝刺或猛烈掙扎時先放線或等待，平靜、累了的時候收線。"
            ))
            self.button("再釣一次", again, emoji="🎣", style=discord.ButtonStyle.success, row=0)
        self.nav("我的魚簍", "fish_basket", emoji="🎒", row=0)
        self.nav("釣魚中心", "fishing", emoji="↩️", row=0)
        return e

    # ======================================================================
    # 魚簍、魚的詳細資料與販售
    # ======================================================================

    def _load_fish_basket(self):
        return self.svc.fish_basket(self.player_id)

    def _build_fish_basket(self, d):
        fish = d["fish"]
        pages = max(1, -(-len(fish) // BASKET_PAGE))
        self.fish_page = min(self.fish_page, pages - 1)
        shown = fish[self.fish_page * BASKET_PAGE:(self.fish_page + 1) * BASKET_PAGE]
        lines = [fish_line(f, self.fish_page * BASKET_PAGE + i + 1) for i, f in enumerate(shown)]
        e = discord.Embed(title="🎒 我的魚簍", color=SEA, description=(
            f"🧺 **{len(fish)} / {d['capacity']}** 隻　💰 全部價值 **{d['total_value']:,}** 金　"
            f"💳 金幣 **{d['gold']:,}**\n{LINE}\n"
            + ("\n".join(lines) if lines else "魚簍空空的，去釣魚吧！🎣")
        )[:4000])
        if pages > 1:
            e.set_footer(text=f"第 {self.fish_page + 1} / {pages} 頁　🏆 = 該魚種的個人最重紀錄")
        else:
            e.set_footer(text="🏆 = 該魚種的個人最重紀錄。選一隻魚可以單獨出售或分享到群組。")

        async def detail(interaction, value):
            self.fish_selected = int(value)
            await self.goto(interaction, "fish_detail")

        self.select("🐟 選一隻魚查看、出售或分享…",
                    [(f"{f['name']} {f['weight_text']}", str(f["id"]), f"{f['rarity_name']}｜💰 {f['price']} 金",
                      f["rarity_emoji"]) for f in shown], detail, row=0)
        if shown:
            sell = discord.ui.Select(placeholder="💰 勾選多隻魚一起出售…", row=1, min_values=1,
                                     max_values=len(shown), options=[
                                         discord.SelectOption(label=f"{f['name']} {f['weight_text']}"[:100],
                                                              value=str(f["id"]),
                                                              description=f"💰 {f['price']} 金"[:100],
                                                              emoji=f["rarity_emoji"])
                                         for f in shown])

            async def picked(interaction):
                await self._pick_sell(interaction, [int(x) for x in sell.values])

            sell.callback = picked
            sell.farm_handler = lambda i, values: self._pick_sell(i, [int(x) for x in values])  # 測試用
            self.add_item(sell)
        if self.fish_sell_ids:
            chosen = [f for f in fish if f["id"] in self.fish_sell_ids]
            total = sum(f["price"] for f in chosen)

            async def confirm_selected(interaction):
                ids, self.fish_sell_ids = self.fish_sell_ids, None
                await self.act(interaction, lambda: self.svc.sell_fish(self.player_id, ids).message)

            async def cancel(interaction):
                self.fish_sell_ids = None
                await self.refresh(interaction)

            e.add_field(name="⚠️ 確認出售", inline=False, value=(
                "\n".join(fish_line(f) for f in chosen)[:900] + f"\n共 **{len(chosen)}** 隻，**{total:,}** 金幣"))
            self.button(f"確認出售 {len(chosen)} 隻（{total:,} 金）", confirm_selected, emoji="✅", row=2,
                        style=discord.ButtonStyle.danger)
            self.button("取消", cancel, emoji="✖️", row=2)

        async def sell_all(interaction):
            if self.confirm != "fish_sell_all":
                self.confirm = "fish_sell_all"
                await self.refresh(interaction, f"⚠️ 確定要把魚簍裡全部 {len(fish)} 隻魚賣掉嗎（共 "
                                                f"{d['total_value']:,} 金幣）？再按一次「確認全部出售」。")
                return
            await self.act(interaction, lambda: self.svc.sell_fish(self.player_id, None).message)

        confirming = self.confirm == "fish_sell_all"
        self.button("確認全部出售" if confirming else "全部出售", sell_all, emoji="💰", row=3, disabled=not fish,
                    style=discord.ButtonStyle.danger if confirming else discord.ButtonStyle.success)

        async def show_best(interaction):
            await self._share_fish(interaction, fish[0]["id"], showcase=True)

        self.button("展示最珍貴的魚", show_best, emoji="🏆", row=3, disabled=not fish or self.group_id is None)
        if pages > 1:
            async def turn_page(interaction, step):
                self.fish_page = (self.fish_page + step) % pages
                self.fish_sell_ids = None
                await self.refresh(interaction)

            self.button("上一頁", lambda i: turn_page(i, -1), emoji="◀️", row=3)
            self.button("下一頁", lambda i: turn_page(i, 1), emoji="▶️", row=3)
        self.nav("釣魚中心", "fishing", emoji="🎣", row=4)
        self.nav("回總覽", "main", emoji="🏠", row=4)
        return e

    async def _pick_sell(self, interaction, ids: list[int]) -> None:
        self.fish_sell_ids = ids
        self.confirm = None
        await self.refresh(interaction, "👇 確認要賣的魚，再按「確認出售」。")

    def _load_fish_detail(self):
        if self.fish_selected is None:
            raise ValidationError("請先在魚簍選一隻魚。")
        basket = self.svc.fish_basket(self.player_id)
        fish = next((f for f in basket["fish"] if f["id"] == self.fish_selected), None)
        if fish is None:
            raise ValidationError("這隻魚已經不在魚簍裡了（可能已經賣掉）。")
        return fish

    def _build_fish_detail(self, f):
        e = discord.Embed(title=f"{f['emoji']} {f['name']}", color=RARITY_COLOR[f["rarity"]], description=(
            f"{LINE}\n"
            f"{f['rarity_emoji']} 稀有度：**{f['rarity_name']}**\n"
            f"⚖️ 重量：**{f['weight_text']}**\n"
            f"💰 價值：**{f['price']}** 金幣\n"
            f"🕒 釣到時間：{_ts(f['caught_at'], 'f')}\n"
            + ("🏆 這是你釣過最重的" + f"{f['name']}！\n" if f.get("record") else "")
            + LINE
        ))

        async def share(interaction):
            await self._share_fish(interaction, f["id"])

        async def sell(interaction):
            if self.confirm != "fish_sell_one":
                self.confirm = "fish_sell_one"
                await self.refresh(
                    interaction, f"⚠️ 確定要賣掉這隻{f['name']}（{f['price']} 金幣）嗎？再按一次「確認出售」。")
                return
            await self.act(interaction, lambda: self.svc.sell_fish(self.player_id, [f["id"]]).message,
                           page="fish_basket")

        confirming = self.confirm == "fish_sell_one"
        self.button("分享到群組", share, emoji="📢", row=0, style=discord.ButtonStyle.primary,
                    disabled=self.group_id is None)
        self.button("確認出售" if confirming else f"出售（{f['price']} 金）", sell, emoji="💰", row=0,
                    style=discord.ButtonStyle.danger if confirming else discord.ButtonStyle.success)
        self.nav("回魚簍", "fish_basket", emoji="↩️", row=0)
        return e

    # ---------- 分享（沿用分享農場：在目前頻道發送公開訊息） ----------

    async def _share_fish(self, interaction, fish_id: int, *, showcase: bool = False) -> None:
        await interaction.response.defer()
        try:
            card = await self.run(self.svc.share_fish, self.player_id, fish_id)
        except Exception as exc:
            if not isinstance(exc, GameError):
                logger.exception("分享魚類失敗（玩家 #%s）", self.player_id)
            await self.show_error(interaction, player_message(exc))
            return
        title = (f"🏆 {card['player_name']} 展示最珍貴的收藏！" if showcase
                 else f"🎣 {card['player_name']} 釣到了一隻{card['rarity_name']}魚！")
        rec = card.get("record") or {}
        record = "🏆 個人紀錄：目前最大捕獲重量\n" if rec.get("species") else ""
        e = discord.Embed(title=title, color=RARITY_COLOR[card["rarity"]], description=(
            f"{LINE}\n"
            f"{card['emoji']} 魚類：**{card['name']}**\n"
            f"{card['rarity_emoji']} 稀有度：**{card['rarity_name']}**\n"
            f"⚖️ 重量：**{card['weight_text']}**\n"
            f"💰 價值：**{card['price']}** 金幣\n"
            f"{record}"
            f"{LINE}\n"
            f"{CELEBRATE[card['rarity']]}\n"
            f"👤 捕獲玩家：**{card['player_name']}**"
        ))
        try:
            await interaction.followup.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
            notice = "📢 已分享到這個頻道！（分享不會消耗魚）"
        except discord.HTTPException as exc:
            logger.warning("分享魚類失敗：%s", type(exc).__name__)
            notice = "⚠️ 訊息沒有送出（Bot 可能沒有這個頻道的發言權限）。"
        await self.refresh(interaction, notice)

    # ======================================================================
    # 圖鑑與排行榜
    # ======================================================================

    def _load_fish_codex(self):
        return self.svc.fish_codex(self.player_id)

    def _build_fish_codex(self, d):
        blocks = []
        for r in fishing.RARITIES:
            rows = [x for x in d["entries"] if x["rarity"] == r]
            if not rows:
                continue
            lines = [
                (f"{x['emoji']} **{x['name']}** ×{x['count']}　最重 {fishing.weight_text(x['max_weight_g'])}"
                 f"　💰 {x['min_price']}–{x['max_price']}")
                if x["caught"] else f"❓ ？？？　💰 {x['min_price']}–{x['max_price']}"
                for x in rows
            ]
            lo, hi = fishing.price_span(rows, r)
            blocks.append(f"**{rarity_tag(r)}**（{lo}–{hi} 金）\n" + "\n".join(lines))
        e = discord.Embed(title="📖 魚類圖鑑", color=SEA, description=(
            f"已發現 **{d['discovered']} / {d['total']}** 種\n{LINE}\n" + "\n\n".join(blocks))[:4000])
        e.set_footer(text="釣到一次就會登記；賣掉的魚也算。❓ 是還沒釣過的魚。")
        self.nav("釣魚中心", "fishing", emoji="🎣", row=0)
        self.nav("回總覽", "main", emoji="🏠", row=0)
        return e

    def _load_fish_rank(self):
        scope = None if self.fish_rank_global or self.group_id is None else self.group_id
        return {"board": self.svc.fishing_leaderboard(scope), "global": scope is None}

    def _build_fish_rank(self, d):
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        b = d["board"]
        catches = [f"{medals.get(i, f'`{i:>2}`')} **{x['player_name']}**　🐟 {x['count']} 隻　🟡 {x['legendary']}"
                   for i, x in enumerate(b["catches"], 1)]
        best = [f"{medals.get(i, f'`{i:>2}`')} **{x['player_name']}**　{fishing.RARITY_EMOJI[x['rarity']]}"
                f"{x['emoji']} {x['fish_name']} {fishing.weight_text(x['weight_g'])}　💰 {x['price']}"
                for i, x in enumerate(b["best"], 1)]
        scope = "全部玩家" if d["global"] else "本伺服器"
        e = discord.Embed(title=f"🏆 釣魚排行榜（{scope}）", color=GOLD, description=(
            "**🐟 捕獲數**\n" + ("\n".join(catches) or "還沒有人釣到魚")
            + f"\n\n{LINE}\n**💎 最有價值的一隻魚**\n" + ("\n".join(best) or "還沒有人釣到魚"))[:4000])

        async def toggle(interaction):
            self.fish_rank_global = not self.fish_rank_global
            await self.refresh(interaction)

        if self.group_id is not None:
            self.button("看本伺服器" if d["global"] else "看全部玩家", toggle, emoji="🔁", row=0)
        self.nav("釣魚中心", "fishing", emoji="🎣", row=0)
        self.nav("回總覽", "main", emoji="🏠", row=0)
        return e

    # ======================================================================
    # 釣具商店（商店的第二個分頁）
    # ======================================================================

    def _load_fish_shop(self):
        return self.svc.fishing_shop(self.player_id)

    def _build_fish_shop(self, d):
        selected = (self.picker or {}).get("key")
        blocks = {}
        for i in d["items"]:
            if not i["active"] and not i["owned"]:
                continue
            if not i["unlocked"]:
                state = f"🔒 農場 Lv.{i['required_level']} 解鎖"
            elif i["kind"] == "bait":
                state = f"🎒 持有 **{i['quantity']}**" + ("　🟢 裝備中" if i["equipped"] else "")
            elif i["owned"]:
                state = "✅ 已擁有" + ("　🟢 裝備中" if i["equipped"] else "")
            else:
                state = "🛒 可購買"
            stock = f"　📦 庫存 {i['stock']}" if i["stock"] is not None else ""
            mark = "▶ " if f"fish:{i['code']}" == selected else ""
            blocks.setdefault(i["kind"], []).append(
                f"{mark}{i['emoji']} **{i['name']}**　💰 **{i['price']:,}** 金{' ／個' if i['kind'] == 'bait' else ''}"
                f"　{state}{stock}\n　📝 {i['description']}\n　✨ {i['effects_text']}"
            )
        parts = [f"💰 你的金幣：**{d['gold']:,}**　⭐ 農場 Lv **{d['level']}**"]
        for kind in fishing.GEAR_KINDS:
            if blocks.get(kind):
                parts.append(f"**{fishing.GEAR_KIND_EMOJI[kind]} {fishing.GEAR_KIND_NAMES[kind]}**\n"
                             + "\n\n".join(blocks[kind]))
        e = discord.Embed(title="🎣 ✦ 釣具商店 ✦", color=SEA, description="\n\n".join(parts)[:4000])
        if self.picker:
            hint = "👇 按「確認購買」完成購買" if self.picker.get("confirm") else "👇 按下方的數量按鈕購買"
            e.add_field(name="🛒 已選擇", inline=False,
                        value=f"**{self.picker['label']}**（{self.picker['price_text']}）\n{hint}")
        else:
            e.set_footer(text="魚竿、魚線、魚餌可以分別裝備；買第一件時會自動裝備。使用農場金幣購買。")
        items = {i["code"]: i for i in d["items"]}

        async def pick(interaction, key):
            code = key.split(":", 1)[1]
            i = items[code]

            def buy(q, code=code):
                r = self.svc.buy_fishing_gear(self.player_id, code, q)
                return f"{r.message}\n💰 剩餘金幣 **{r.gold:,}**"

            label = f"{i['emoji']} {i['name']}"
            if i["kind"] == "bait":
                self.picker = {"key": key, "label": label, "price_text": f"每個 {i['price']} 金", "verb": "買",
                               "max": max(1, min(fishing.BAIT_MAX_BUY, d["gold"] // max(1, i["price"]))),
                               "presets": (1, 10, 50), "keep": True, "run": buy}
            else:
                self.picker = {"key": key, "label": label, "price_text": f"{i['price']:,} 金", "confirm": True,
                               "max": 1, "presets": (), "run": buy}
            await self.refresh(interaction)

        options = [
            (i["name"], f"fish:{i['code']}", f"{i['kind_name']}｜💰 {i['price']:,} 金｜{i['effects_text']}", i["emoji"])
            for i in d["items"]
            if i["active"] and i["unlocked"] and (i["kind"] == "bait" or not i["owned"])
            and (i["stock"] is None or i["stock"] > 0)
        ]
        self.select("🛒 選擇要買的釣具…", options, pick, row=0, selected=selected)
        self.quantity_buttons(row=1)
        self._equip_select(d["items"], row=2)
        self.button("農場商店", self._goto_farm_shop, emoji="🌾", row=4)
        self.button("釣具商店", self._noop, emoji="🎣", row=4, style=discord.ButtonStyle.primary, disabled=True)
        self.nav("特賣與升級", "shop_special", emoji="🎁", row=4)
        self.nav("釣魚中心", "fishing", emoji="🎣", row=4)
        self.nav("回總覽", "main", emoji="🏠", row=4)
        return e

    async def _goto_farm_shop(self, interaction):
        await self.goto(interaction, "shop")
