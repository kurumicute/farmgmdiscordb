"""農場面板的世界 Boss 頁面（FarmPanel 的 mixin），分成兩個區域：

- 介紹區（boss、boss_board、boss_help）：附固定圖（farm/scenes/world_boss.png，由 dragon.png 縮小），
  顯示 Boss 簡介、活動說明、上一場的排行榜（boss_board）與「⚔️ 進入（Boss 名稱）」；
  區域內換頁沿用訊息上的附件（FarmPanel._attachments 比對雜湊）。
- 戰鬥區（boss_battle、boss_donate、boss_live_board）：不附圖片，只有數值面板（HP、補給、戰況、個人貢獻）、
  捐獻流程與這一場目前的排行榜，
  按鈕操作不必等圖片載入。
- 活動資料（HP、時間、補給、捐獻、領獎）都在 PostgreSQL；面板每次切換頁面都重新讀取，Bot 重啟後重新開啟即可。
- 看得到全服活動（所有伺服器的玩家一起打，例如固定每 2 天出現的深淵魔龍）與開啟面板的伺服器自己的活動
  （群組由互動來源決定，服務層再次檢查群組與成員）；同時有多場時可以切換。
- 配色參考 dragon.png 的紫色與火焰色。
- 捐獻流程：選類別 → 選物品 → 選數量 → 確認。確認時帶畫面產生的 request_id，重複點擊同一個確認按鈕
  只會處理一次（面板先清除待確認狀態，服務層再以唯一限制擋住）；取消不會扣任何物品。
"""

import uuid

import discord

from app.discord_bot import farm_image
from app.game import world_boss as wb
from app.game.errors import GameError, ValidationError

# 配色取自 dragon.png：魔龍的紫色、火焰的橘紅色、皇冠的金色
BOSS_PURPLE, BOSS_FLAME, BOSS_GOLD, BOSS_GREY = 0x5B2A86, 0xE8590C, 0xF1C40F, 0x99AAB5
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
STATE_EMOJI = {"scheduled": "⏳", "active": "🔥", "paused": "⏸️", "ended": "🏁", "defeated": "💀"}
CLAIM_LABELS = {
    "pending": "⏳ 活動結束後可領取", "ready": "🎁 可以領取！", "claimed": "✅ 已領取", "none": "— 沒有參與，無獎勵",
    "below_min": "— 積分未達參與門檻，無獎勵",
}
QUANTITY_PRESETS = (1, 5, 10)


def _ts(dt, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "—"


def _points_text(o: dict) -> str:
    if o["points_min"] == o["points_max"]:
        return f"{o['points_min']} 分"
    return f"{o['points_min']}–{o['points_max']} 分"


class WorldBossPages:
    """需要 FarmPanel 提供 svc、run、button、nav、select、refresh、goto、act、group_id、player_id。"""

    boss_event_id: int | None = None
    boss_selected: int | None = None  # 玩家切換查看的活動；None = 自動選擇進行中的活動
    boss_category: str | None = None
    boss_options: list[dict] | None = None
    boss_item: dict | None = None
    # 待確認的捐獻：{"item", "quantity", "request_id"}；漁獲勾選多種時為 {"batch": [同上…]}
    boss_pending: dict | None = None

    def _boss_group(self) -> int | None:
        """目前所在的群組（私訊中為 None：只看得到全服活動）。"""
        return self.group_id

    def _boss_view(self, image: bool = True):
        if image:
            self._boss_image()  # 第一次讀檔在執行緒中完成（之後從快取取得，不阻塞事件迴圈）
        return self.svc.boss_current(self._boss_group(), self.player_id, self.boss_selected)

    def _boss_image(self) -> bytes | None:
        return self._image("世界 Boss", farm_image.world_boss_png)

    def _boss_embed(self, *, image: bool = True, **kw) -> discord.Embed:
        """世界 Boss 各頁共用。介紹區附上固定圖（同一張圖，區域內換頁不會重新上傳）；戰鬥區 image=False 不附圖。"""
        e = discord.Embed(**kw)
        if image:
            self.attach_image(e, self._boss_image())
        return e

    def _boss_reset(self) -> None:
        self.boss_category = self.boss_options = self.boss_item = self.boss_pending = None

    async def _boss_open(self, interaction) -> None:
        """回介紹區。"""
        self._boss_reset()
        await self.goto(interaction, "boss")

    async def _boss_battle_open(self, interaction) -> None:
        """進入（或回到）戰鬥區。"""
        self._boss_reset()
        await self.goto(interaction, "boss_battle")

    def _boss_claim_button(self, v, row: int) -> None:
        async def claim(interaction):
            event_id = v["event_id"]
            await self.act(interaction, lambda: self.svc.boss_claim(self._boss_group(), self.player_id,
                                                                    event_id).message)

        self.button("領取獎勵", claim, emoji="🎁", style=discord.ButtonStyle.primary, row=row,
                    disabled=v["me"]["claim_state"] != "ready")

    # ---------- 介紹區（附圖） ----------

    def _load_boss(self):
        view = self._boss_view()
        self.boss_event_id = view["event_id"] if view else None
        return view

    def _boss_header(self, v) -> str:
        state = v["state"]
        if state == "scheduled":
            when = f"⏳ 開始時間：{_ts(v['starts_at'], 'f')}（{_ts(v['starts_at'])}）"
        elif state in ("ended", "defeated"):
            when = f"🏁 結束時間：{_ts(v['ended_at'], 'f')}"
        else:
            when = f"⏰ 剩餘時間：{_ts(v['ends_at'])}（{_ts(v['ends_at'], 'f')} 結束）"
        return (
            f"### {v['name']}\n"
            f"{v['scope_label']}　狀態：{STATE_EMOJI[state]} **{v['state_label']}**\n"
            f"❤️ HP **{v['hp']:,}** / {v['max_hp']:,}（{v['hp_pct']}%）\n"
            f"`{wb.bar(v['hp'], v['max_hp'], 20)}`\n{when}"
        )

    @staticmethod
    def _supply_lines(v) -> str:
        return "\n".join(
            f"{s['emoji']} **{s['name']}**（{s['effect']}）\n"
            f"`{wb.bar(min(s['progress'], s['target']), s['target'], 12)}` {s['progress']:,} / {s['target']:,}"
            f"（{s['ratio_pct']}%）"
            for s in v["supplies"]
        )

    @staticmethod
    def _boss_color(state: str) -> int:
        return {"defeated": BOSS_GOLD, "ended": BOSS_GREY, "active": BOSS_FLAME}.get(state, BOSS_PURPLE)

    @staticmethod
    def _boss_mine_extra(v) -> str:
        """參與門檻與稱號（介紹區與戰鬥區的個人欄共用）。"""
        me, extra = v["me"], ""
        need = (v.get("rewards_text") or {}).get("min_points", 0)
        if need and me["claim_state"] in ("pending", "none", "below_min"):
            extra += f"\n🎯 參與門檻：積分達 **{need:,}** 才有獎勵"
        if me.get("title"):
            extra += f"\n🎖️ 稱號：**{me['title']}**"
        return extra

    def _build_boss(self, v):
        if v is None:
            e = self._boss_embed(title="🐉 世界 Boss", color=BOSS_GREY, description=(
                "目前沒有世界 Boss 活動。\n活動由管理員在監控網站建立或排定，出現後這裡會顯示 Boss 與補給進度。"))
            self.button("活動說明", self._boss_help_page, emoji="📖", row=0)
            self.button("更新狀態", self._boss_open, emoji="🔄", row=0)
            self._nav_back(row=0)
            return e
        state, me = v["state"], v["me"]
        e = self._boss_embed(title=f"🐉 世界 Boss｜{v['name']}", color=self._boss_color(state),
                             description=self._boss_header(v))
        enter = (f"按「⚔️ 進入{v['name']}」前往戰鬥區：查看補給與戰況，捐獻物資支援前線。"
                 if state == "active" else f"按「⚔️ 進入{v['name']}」查看補給、戰況與個人貢獻。")
        e.add_field(name="🗺️ 前往戰場", value=enter, inline=False)
        rank = f"第 {me['rank']} 名 / {me['participants']} 人" if me["rank"] else "尚未上榜"
        mine = f"🏅 貢獻積分 **{me['points']:,}**　📊 {rank}\n🎁 獎勵：{CLAIM_LABELS[me['claim_state']]}"
        e.add_field(name="🙋 我的參與", value=mine + self._boss_mine_extra(v), inline=False)
        e.set_footer(text="你是農場後勤：捐獻農作物、漁獲與素材支援前線，Boss 戰鬥由系統自動進行。")

        async def board(interaction):
            await self.goto(interaction, "boss_board")

        self.button(f"進入{v['name']}", self._boss_battle_open, emoji="⚔️", style=discord.ButtonStyle.success,
                    row=0)
        self.button("上一場排行榜", board, emoji="🏆", row=0)
        self.button("活動說明", self._boss_help_page, emoji="📖", row=0)
        self._boss_claim_button(v, row=0)
        self.button("更新狀態", self._boss_open, emoji="🔄", row=0)

        async def claim_old(interaction, value):
            event_id = int(value)
            await self.act(interaction, lambda: self.svc.boss_claim(self._boss_group(), self.player_id,
                                                                    event_id).message)

        self.select("🎁 領取之前活動的獎勵…", [
            (c["name"], str(c["event_id"]), "已擊敗" if c["defeated"] else "已結束", "🎁") for c in v["claimable"]
        ], claim_old, row=1)

        async def switch(interaction, value):
            self.boss_selected = int(value)
            self._boss_reset()
            await self.goto(interaction, "boss")

        choices = v["choices"]
        if len(choices) > 1 or (choices and all(c["event_id"] != v["event_id"] for c in choices)):
            self.select("🔀 切換世界 Boss…", [
                (c["name"], str(c["event_id"]), f"{c['scope_label']}｜{c['state_label']}", "🐉") for c in choices
            ], switch, row=2, selected=str(v["event_id"]))
        self._nav_back(row=4)
        return e

    # ---------- 戰鬥區（不附圖：數值面板） ----------

    def _load_boss_battle(self):
        view = self._boss_view(image=False)
        if view is None:
            raise ValidationError("目前沒有世界 Boss 活動。")
        self.boss_event_id = view["event_id"]
        return view

    def _build_boss_battle(self, v):
        state, me = v["state"], v["me"]
        e = self._boss_embed(image=False, title=f"⚔️ 戰鬥區｜{v['name']}", color=self._boss_color(state),
                             description=self._boss_header(v))
        scope = "全服" if v["group_id"] is None else "群組"
        e.add_field(name=f"🚚 {scope}後勤補給進度", value=self._supply_lines(v), inline=False)
        e.add_field(name="⚔️ 前線戰況", value=(
            f"攻擊倍率 ×{v['attack_mult']}\n出勤率 {v['uptime_pct']}%\n每小時傷害 {v['damage_per_hour']:,}"))
        e.add_field(name=f"👥 {scope}參與", value=(
            f"參與人數 {v['participants']}\n捐獻總數 {v['total_quantity']:,}\n總積分 {v['total_points']:,}"))
        rank = f"第 {me['rank']} 名 / {me['participants']} 人" if me["rank"] else "尚未上榜"
        mine = (f"👤 **{me['player_name']}**\n🏅 貢獻積分 **{me['points']:,}**\n📦 捐獻總數 {me['quantity']:,}\n"
                f"📊 個人排名 {rank}\n🎁 獎勵：{CLAIM_LABELS[me['claim_state']]}")
        e.add_field(name="🙋 個人貢獻", value=mine + self._boss_mine_extra(v), inline=False)
        e.set_footer(text="捐獻會立即造成傷害並推進補給；前線依補給自動作戰。")

        async def donate(interaction):
            self._boss_reset()
            await self.goto(interaction, "boss_donate")

        self.button("捐獻物資", donate, emoji="📦", style=discord.ButtonStyle.success, row=0,
                    disabled=state != "active")
        self._boss_claim_button(v, row=0)
        self.button("更新狀態", self._boss_battle_open, emoji="🔄", row=0)
        self.button("回介紹區", self._boss_open, emoji="🐉", row=0)

        async def live_board(interaction):
            await self.goto(interaction, "boss_live_board")

        self.button("目前排行", live_board, emoji="🏆", row=1, style=discord.ButtonStyle.primary)
        self._nav_back(row=1)
        return e

    async def _boss_help_page(self, interaction):
        await self.goto(interaction, "boss_help")

    # ---------- 捐獻 ----------

    def _load_boss_donate(self):
        view = self._boss_view(image=False)
        if view is None:
            raise ValidationError("目前沒有世界 Boss 活動。")
        self.boss_event_id = view["event_id"]
        options = None
        if self.boss_category and view["state"] == "active":
            options = self.svc.boss_donation_options(self._boss_group(), self.player_id, view["event_id"],
                                                     self.boss_category)
        return {"view": view, "options": options}

    def _build_boss_donate(self, d):
        v, options = d["view"], d["options"]
        self.boss_options = options
        supply_names = {s["code"]: f"{s['emoji']} {s['name']}" for s in v["supplies"]}
        limits = v["settings"]["limits"]
        e = self._boss_embed(image=False, title=f"📦 捐獻物資｜{v['name']}", color=BOSS_PURPLE)
        lines = [f"❤️ HP {v['hp']:,} / {v['max_hp']:,}　`{wb.bar(v['hp'], v['max_hp'], 12)}`",
                 self._supply_lines(v), ""]
        if v["state"] != "active":
            lines.append(f"⚠️ 活動{v['state_label']}，現在不能捐獻。")
        elif not self.boss_category:
            lines.append("① 先選擇要捐獻的類別。")
        elif not options:
            lines.append("背包裡沒有這個類別可以捐獻的物品。")
        elif self.boss_category == "fish" and self.boss_pending is None:
            lines.append(f"② 勾選要捐的魚種，可以一次勾選多種：勾選的魚種會**全部捐出**"
                         f"（每種單次最多 {limits['per_action_max']} 隻，每種每場最多 {limits['per_item_max']} 隻）。")
        elif self.boss_item is None and self.boss_pending is None:
            lines.append("② 選擇要捐獻的物品（顯示持有數量、每個積分與對應的補給）。")
        elif self.boss_pending is None:
            lines.append(f"③ 選擇要捐獻的數量（單次最多 {limits['per_action_max']} 個，"
                         f"每種物品每場最多 {limits['per_item_max']} 個）。")
        if self.boss_item is not None:
            o = self.boss_item
            lines.append(f"\n已選：**{o['name']}**　持有 {o['held']}　每個 {_points_text(o)}　"
                         f"→ {supply_names[o['supply']]}　本場還可捐 {o['remaining']}")
        if self.boss_pending is not None and "batch" in self.boss_pending:
            batch = self.boss_pending["batch"]
            low = sum(x["item"]["points_min"] * x["quantity"] for x in batch)
            high = sum(x["item"]["points_max"] * x["quantity"] for x in batch)
            items = "\n".join(f"・{x['item']['name']} ×{x['quantity']}" for x in batch)
            est = f"{low:,}" if low == high else f"約 {low:,}–{high:,}"
            total = sum(x["quantity"] for x in batch)
            lines.append(f"\n### 確認捐獻以下 {len(batch)} 種漁獲（共 {total:,} 隻）？\n"
                         f"{items}\n預計獲得 {est} 積分。確認後才會扣除；按「取消」不會扣除任何物品。")
        elif self.boss_pending is not None:
            p = self.boss_pending
            o = p["item"]
            est = (f"{o['points_min'] * p['quantity']:,}" if o["points_min"] == o["points_max"]
                   else f"約 {o['points_min'] * p['quantity']:,}–{o['points_max'] * p['quantity']:,}")
            lines.append(f"\n### 確認捐獻 {o['name']} ×{p['quantity']}？\n預計獲得 {est} 積分。"
                         "確認後才會扣除物品；按「取消」不會扣除任何物品。")
        e.description = "\n".join(lines)[:4096]
        e.set_footer(text="作物與魚依售價換算積分；同一種物品捐越多不會無限加成，補給超過目標的部分效果有上限。")

        if v["state"] == "active":
            async def pick_category(interaction, value):
                self._boss_reset()
                self.boss_category = value
                await self.goto(interaction, "boss_donate")

            self.select(
                "① 捐獻類別…",
                [(name, code, None, emoji) for code, (emoji, name) in wb.CATEGORY_INFO.items()],
                pick_category, row=0, selected=self.boss_category,
            )
            if options and self.boss_category == "fish":
                self._boss_fish_multi(options, limits["per_action_max"], supply_names)
            elif options:
                async def pick_item(interaction, value):
                    idx = int(value)
                    if self.boss_options is None or not 0 <= idx < len(self.boss_options):
                        await self.goto(interaction, "boss_donate")
                        return
                    self.boss_item, self.boss_pending = self.boss_options[idx], None
                    await self.goto(interaction, "boss_donate")

                selected = None
                if self.boss_item is not None:
                    selected = next((str(i) for i, o in enumerate(options)
                                     if (o["code"], o["quality"], o["mutation"]) == (
                                         self.boss_item["code"], self.boss_item["quality"],
                                         self.boss_item["mutation"])), None)
                self.select("② 選擇物品…", [
                    (o["name"], str(i), f"持有 {o['held']}｜每個 {_points_text(o)}｜→ {supply_names[o['supply']]}"
                     f"｜還可捐 {o['remaining']}", None)
                    for i, o in enumerate(options)
                ], pick_item, row=1, selected=selected)
            if self.boss_item is not None and self.boss_pending is None:
                self._boss_quantity_buttons(self.boss_item, limits["per_action_max"])
            if self.boss_pending is not None:
                self.button("確認捐獻", self._boss_confirm, emoji="✅", style=discord.ButtonStyle.success, row=3)
                self.button("取消", self._boss_cancel, emoji="❌", style=discord.ButtonStyle.danger, row=3)
        self.button("回戰鬥區", self._boss_battle_open, emoji="⚔️", row=4)
        self._nav_back(row=4)
        return e

    def _boss_fish_multi(self, options: list[dict], per_action: int, supply_names: dict) -> None:
        """漁獲：勾選多個魚種，勾選的魚種全部捐出（數量 = 持有、本場剩餘、單次上限的最小值）。"""
        shown = [(i, o) for i, o in enumerate(options[:25]) if min(o["held"], o["remaining"], per_action) > 0]
        if not shown:
            return
        chosen = {x["item"]["code"] for x in (self.boss_pending or {}).get("batch", [])}
        multi = discord.ui.Select(
            placeholder="② 勾選要全部捐出的魚種（可複選）…", row=1, min_values=1, max_values=len(shown),
            options=[discord.SelectOption(
                label=o["name"][:100], value=str(i), default=o["code"] in chosen,
                description=(f"持有 {o['held']}｜捐 {min(o['held'], o['remaining'], per_action)} 隻｜"
                             f"每隻 {_points_text(o)}｜→ {supply_names[o['supply']]}")[:100],
            ) for i, o in shown],
        )

        async def picked(interaction, values):
            batch = []
            for value in values:
                idx = int(value)
                if self.boss_options is None or not 0 <= idx < len(self.boss_options):
                    continue
                o = self.boss_options[idx]
                q = min(o["held"], o["remaining"], per_action)
                if q > 0:
                    batch.append({"item": o, "quantity": q, "request_id": uuid.uuid4().hex})
            self.boss_item = None
            self.boss_pending = {"batch": batch} if batch else None
            await self.goto(interaction, "boss_donate")

        async def callback(interaction):
            await picked(interaction, multi.values)

        multi.callback = callback
        multi.farm_handler = picked  # 測試用：直接以選項值清單呼叫
        self.add_item(multi)

    def _boss_quantity_buttons(self, item: dict, per_action: int) -> None:
        maximum = min(item["held"], item["remaining"], per_action)
        if maximum <= 0:
            return

        async def choose(interaction, q):
            self.boss_pending = {"item": item, "quantity": q, "request_id": uuid.uuid4().hex}
            await self.goto(interaction, "boss_donate")

        for q in QUANTITY_PRESETS:
            if q < maximum:
                async def pick(interaction, q=q):
                    await choose(interaction, q)

                self.button(f"× {q}", pick, style=discord.ButtonStyle.primary, row=2)

        async def pick_max(interaction):
            await choose(interaction, maximum)

        self.button(f"最多 × {maximum}", pick_max, style=discord.ButtonStyle.primary, row=2)

        async def custom(interaction):
            from app.discord_bot.panels import QuantityModal  # 避免循環匯入

            await interaction.response.send_modal(QuantityModal("捐獻數量", "數量", maximum, choose))

        self.button("自訂數量…", custom, emoji="✏️", row=2)

    async def _boss_confirm(self, interaction):
        pending, event_id = self.boss_pending, self.boss_event_id
        # 先清除待確認狀態：同一面板重複點擊時第二下不會再送出（服務層另以 request_id 防重複）
        self.boss_pending = None
        if pending is None or event_id is None:
            await self.refresh(interaction, "⚠️ 這筆捐獻已經送出或已取消，請依最新畫面操作。")
            return
        if "batch" in pending:
            batch = pending["batch"]

            def work_batch() -> str:
                # 每種魚各自一筆捐獻（各自的 request_id，重送不會重複扣）；某一種失敗不影響已完成的其他種
                lines = []
                for x in batch:
                    o = x["item"]
                    try:
                        result = self.svc.boss_donate(self._boss_group(), self.player_id, event_id, o["category"],
                                                      o["code"], x["quantity"], x["request_id"])
                        lines.append(result["message"])
                    except GameError as exc:
                        lines.append(f"❌ {o['name']}：{exc}")
                return "\n".join(lines)

            self.boss_item = None
            await self.act(interaction, work_batch, page="boss_donate")
            return
        item = pending["item"]

        def work() -> str:
            result = self.svc.boss_donate(
                self._boss_group(), self.player_id, event_id, item["category"], item["code"], pending["quantity"],
                pending["request_id"], item["quality"], item["mutation"],
            )
            return result["message"]

        self.boss_item = None
        await self.act(interaction, work, page="boss_donate")

    async def _boss_cancel(self, interaction):
        self.boss_pending = None
        await self.refresh(interaction, "已取消，沒有扣除任何物品。")

    # ---------- 排行榜與說明 ----------

    def _with_titles(self, board: dict | None) -> dict | None:
        if board is None:
            return None
        return {**board, "titles": self.svc.player_titles([r["player_id"] for r in board["rows"]])}

    def _load_boss_board(self):
        """介紹區：上一場（最近一場已結束）活動的排行。"""
        view = self._boss_view()
        if view is None:
            raise ValidationError("目前沒有世界 Boss 活動。")
        board = self.svc.boss_last_leaderboard(self._boss_group(), self.player_id, view["event_id"])
        return {"board": self._with_titles(board), "current_name": view["name"]}

    def _build_boss_board(self, d):
        b = d["board"]
        if b is None:
            e = self._boss_embed(title="🏆 上一場貢獻排行", color=BOSS_GOLD, description=(
                f"還沒有已結束的{d['current_name']}活動。\n這一場的即時排行請到「⚔️ 戰鬥區」→「🏆 目前排行」。"))
            self.button("回介紹區", self._boss_open, emoji="🐉", row=0)
            self._nav_back(row=0)
            return e
        result = "💀 已擊敗" if b["defeated"] else "🏁 時間到未擊敗"
        e = self._board_embed(b, title=f"🏆 上一場貢獻排行｜{b['name']}",
                              head=f"{result}　結束於 {_ts(b['ended_at'], 'f')}", image=True)
        self.button("回介紹區", self._boss_open, emoji="🐉", row=0)
        self._nav_back(row=0)
        return e

    def _load_boss_live_board(self):
        """戰鬥區：這一場目前的排行。"""
        view = self._boss_view(image=False)
        if view is None:
            raise ValidationError("目前沒有世界 Boss 活動。")
        board = self.svc.boss_leaderboard(self._boss_group(), self.player_id, view["event_id"])
        return self._with_titles(board)

    def _build_boss_live_board(self, d):
        e = self._board_embed(d, title=f"🏆 目前貢獻排行｜{d['name']}",
                              head="這一場的即時排行（每次開啟或重新整理時更新）", image=False)
        self.button("重新整理", self._boss_live_board_open, emoji="🔄", row=0)
        self.button("回戰鬥區", self._boss_battle_open, emoji="⚔️", row=0)
        self._nav_back(row=0)
        return e

    async def _boss_live_board_open(self, interaction) -> None:
        await self.goto(interaction, "boss_live_board")

    def _board_embed(self, d, *, title: str, head: str, image: bool) -> discord.Embed:
        lines = []
        for r in d["rows"]:
            medal = MEDALS.get(r["rank"]) or f"`{r['rank']:>2}.`"
            badge = d["titles"].get(r["player_id"])
            name = f"{r['player_name']}〔{badge}〕" if badge else r["player_name"]
            lines.append(f"{medal} **{name}**　🏅 {r['points']:,} 分　📦 {r['quantity']:,} 個")
        body = "\n".join(lines) if lines else "還沒有人捐獻，成為第一個支援前線的人吧！"
        e = self._boss_embed(image=image, title=title, color=BOSS_GOLD, description=f"{head}\n\n{body}")
        me = d["me"]
        e.add_field(name="🙋 我的名次", value=(
            f"第 {me['rank']} 名 / {d['participants']} 人　🏅 {me['points']:,} 分　📦 {me['quantity']:,} 個"
            if me else "沒有捐獻"), inline=False)
        e.set_footer(text="依貢獻積分排序；同分時先捐獻的玩家在前。")
        return e

    def _load_boss_help(self):
        return self._boss_view()

    def _build_boss_help(self, v):
        s = v["settings"] if v else wb.DEFAULT_SETTINGS
        eff, lim = s["effects"], s["limits"]
        supply = "\n".join(f"{e} **{n}** → {f}：{d}" for e, n, f, d in wb.SUPPLY_INFO.values())
        how = (
            "1️⃣ 世界 Boss 分成🌏 **全服**（所有伺服器的玩家一起打，例如固定出現的深淵魔龍）與🏠 **伺服器**活動，"
            "大家一起當**農場後勤**。\n"
            "2️⃣ 在介紹區按「⚔️ 進入（Boss 名稱）」到戰鬥區，再按「📦 捐獻物資」，"
            "從背包選農作物、漁獲、素材或加工品捐給前線（確認後才扣除）。\n"
            "3️⃣ 前線依四種補給的達成率**自動作戰**，每小時扣 Boss HP；每次捐獻也會立即造成一次傷害。\n"
            "4️⃣ 活動結束（擊敗 Boss 或時間到）後，按「🎁 領取獎勵」。每人每場只能領一次。"
        )
        rules = (
            f"🌾 作物與 🐟 魚：每 {s['gold_per_point']} 金幣售價換 1 積分（無條件進位）\n"
            f"⚙️ 素材、🍲 加工品與 🧪 特殊材料：依活動指定的固定積分\n"
            f"📦 單次最多 {lim['per_action_max']} 個；每種物品每場最多 {lim['per_item_max']} 個\n"
            f"📈 補給最多算到目標的 {eff['ratio_cap_pct']}%；沒有糧食時出勤率只有 {eff['base_uptime_pct']}%\n"
            f"⚔️ 戰鬥補給滿額傷害 +{eff['combat_attack_pct']}%、🛡️ 裝備補給滿額 +{eff['equipment_attack_pct']}%、"
            f"🩹 醫療補給滿額救回 {eff['medical_recovery_pct']}% 缺勤時間"
        )
        e = self._boss_embed(title="📖 世界 Boss 活動說明", color=BOSS_PURPLE, description=how)
        e.add_field(name="🚚 補給類型與效果", value=supply, inline=False)
        e.add_field(name="🧮 積分與限制", value=rules, inline=False)
        if v and v.get("rewards_text"):
            r = v["rewards_text"]
            ranks = "\n".join(f"{MEDALS.get(i) or f'第 {i} 名'} {t}" for i, t in enumerate(r["rank"], start=1))
            reward = (f"🎯 參與門檻：個人積分達 {r['min_points']:,} 才有以下任何獎勵\n" if r.get("min_points") else "")
            reward += (f"🏆 擊敗 Boss：{r['victory']}\n🤝 未擊敗（參與獎）：{r['participation']}\n"
                      f"⭐ 個人積分達 {r['bonus_points']:,}：{r['bonus']}\n{ranks}")
            if r["title"]:
                reward += f"\n🎖️ 擊敗時前 {r['title_top']} 名獲得稱號「{r['title']}」"
            e.add_field(name="🎁 獎勵制度", value=reward[:1024], inline=False)
        e.set_footer(text="只有捐獻過的玩家可以領獎；所有紀錄存在資料庫，Bot 重啟也不會遺失。")
        self.button("回介紹區", self._boss_open, emoji="🐉", row=0)
        self._nav_back(row=0)
        return e
