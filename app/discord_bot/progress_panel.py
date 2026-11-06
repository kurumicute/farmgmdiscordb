"""農場面板的長期進度頁面（FarmPanel 的 mixin）：成就與圖鑑總覽、成就、稱號、圖鑑。

- 總覽頁「🏅 成就圖鑑」按鈕進入 progress（總覽），再分到成就、稱號與圖鑑；不使用圖片。
- 每日簽到在開啟面板時自動完成（panels._checkin_note），這裡只顯示連續天數。
"""

import discord

from app.game import achievements

PURPLE, GOLD = 0x9B59B6, 0xF1C40F
LINE = "━━━━━━━━━━━━━━"
CODEX_SELECT = "選擇圖鑑分類"
ACH_SELECT = "選擇成就分類"
TITLE_SELECT = "選擇要顯示的稱號"
NO_TITLE = "__none__"
STATE_ICON = {"claimed": "✅", "ready": "🎁", "progress": "⬜"}


def _bar(value: int, total: int, width: int = 10) -> str:
    filled = 0 if total <= 0 else min(width, max(0, round(width * value / total)))
    return "▰" * filled + "▱" * (width - filled)


def _pct(value: int, total: int) -> int:
    return 0 if total <= 0 else value * 100 // total


class ProgressPages:
    """需要 FarmPanel 提供 svc、player_id、nav、select、button、refresh、act。"""

    codex_category = "crop"
    ach_category = "farm"

    # ======================================================================
    # 成就與圖鑑總覽
    # ======================================================================

    def _load_progress(self):
        return self.svc.progress_overview(self.player_id)

    def _build_progress(self, d):
        c, k, a, t = d["codex"], d["checkin"], d["achievements"], d["titles"]
        sign = (f"✅ 今天已簽到｜連續 **{k['streak']}** 天" if k["checked_today"]
                else f"連續 **{k['streak']}** 天（開啟面板自動簽到）")
        cats = [
            f"{x['emoji']} {x['name']}　{_bar(x['discovered'], x['total'], 8)} **{x['discovered']}/{x['total']}**"
            for x in c["categories"]
        ]
        ready = f"　🎁 **{a['ready']} 個可領取！**" if a["ready"] else ""
        title = f"**{t['current']}**" if t["current"] else "未設定"
        e = discord.Embed(title="🏅 ✦ 成就與圖鑑 ✦", color=PURPLE, description=(
            f"🏅 **成就**　{_bar(a['claimed'], a['total'], 8)} {a['claimed']}/{a['total']}{ready}\n"
            f"🎖️ 目前稱號：{title}（擁有 {len(t['owned'])} 個）\n{LINE}\n"
            f"📅 每日簽到：{sign}\n🎁 下一次：{k['next_reward_text']}\n{LINE}\n"
            f"📖 **圖鑑**　{c['discovered']}/{c['total']}（{_pct(c['discovered'], c['total'])}%）\n" + "\n".join(cats)
        )[:4000])
        e.set_footer(text="成就進度包含過去的紀錄；達成後到「成就」頁領取獎勵。取得物品會自動登記到圖鑑。")
        self.nav("成就", "achievements", emoji="🏅", row=0, style=discord.ButtonStyle.primary)
        self.nav("稱號", "titles", emoji="🎖️", row=0)
        self.nav("圖鑑", "codex", emoji="📖", row=0, style=discord.ButtonStyle.primary)
        self.nav("回總覽", "main", emoji="🏠", row=0)
        return e

    # ======================================================================
    # 成就
    # ======================================================================

    def _load_achievements(self):
        return self.svc.achievements(self.player_id)

    def _build_achievements(self, d):
        cats = {key: (emoji, name) for key, emoji, name in achievements.CATEGORIES}
        cat = self.ach_category if self.ach_category in cats else achievements.CATEGORIES[0][0]
        blocks = []
        for r in (x for x in d["rows"] if x["category"] == cat):
            if r["state"] == "progress":
                status = f"{_bar(r['value'], r['target'], 8)} {r['value']:,}/{r['target']:,} {r['unit']}"
            elif r["state"] == "ready":
                status = "**可以領取！**"
            else:
                status = "已領取"
            blocks.append(f"{STATE_ICON[r['state']]} {r['emoji']} **{r['name']}**　{r['description']}\n"
                          f"　{status}　🎁 {r['reward_text']}")
        emoji, name = cats[cat]
        ready = f"　🎁 可領取 **{d['ready']}** 個" if d["ready"] else ""
        e = discord.Embed(title=f"🏅 ✦ 成就｜{emoji} {name} ✦", color=GOLD, description=(
            f"已完成 **{d['claimed']}/{d['total']}**{ready}\n{LINE}\n" + "\n\n".join(blocks)
        )[:4000])
        e.set_footer(text="✅ 已領取　🎁 可領取　⬜ 進行中｜進度包含成就上線前的紀錄。")

        async def choose(interaction, value):
            self.ach_category = value
            await self.refresh(interaction)

        counts: dict[str, list[int]] = {}
        for r in d["rows"]:
            done_total = counts.setdefault(r["category"], [0, 0])
            done_total[0] += r["state"] == "claimed"
            done_total[1] += 1
        self.select(ACH_SELECT, [
            (f"{n}（{counts.get(k, [0, 0])[0]}/{counts.get(k, [0, 0])[1]}）", k, None, em)
            for k, em, n in achievements.CATEGORIES
        ], choose, row=0, selected=cat)

        async def claim(interaction):
            await self.act(interaction, lambda: self.svc.claim_achievements(self.player_id).message)

        self.button(f"領取全部（{d['ready']}）", claim, emoji="🎁", style=discord.ButtonStyle.success, row=1,
                    disabled=not d["ready"])
        self.nav("稱號", "titles", emoji="🎖️", row=1)
        self.nav("成就與圖鑑", "progress", emoji="🏅", row=1)
        self.nav("回總覽", "main", emoji="🏠", row=1)
        return e

    # ======================================================================
    # 稱號
    # ======================================================================

    def _load_titles(self):
        return self.svc.titles(self.player_id)

    def _build_titles(self, d):
        lines = [f"{'👉' if t['title'] == d['current'] else '🎖️'} **{t['title']}**　來源：{t['source']}"
                 for t in d["owned"]]
        current = f"**{d['current']}**" if d["current"] else "未設定"
        e = discord.Embed(title="🎖️ ✦ 稱號 ✦", color=PURPLE, description=(
            f"目前顯示：{current}\n{LINE}\n"
            + ("\n".join(lines) or "還沒有稱號。完成成就，或在世界 Boss 擊敗 Boss 並名列前茅就能取得。")
        )[:4000])
        e.set_footer(text="稱號會顯示在總覽、排行榜與世界 Boss 貢獻排行。")

        async def choose(interaction, value):
            title = None if value == NO_TITLE else value
            await self.act(interaction, lambda: self.svc.set_title(self.player_id, title).message)

        if d["owned"]:
            options = [("不顯示稱號", NO_TITLE, None, "🚫")] + [
                (t["title"], t["title"], t["source"], "🎖️") for t in d["owned"][:24]
            ]
            self.select(TITLE_SELECT, options, choose, row=0, selected=d["current"] or NO_TITLE)
        self.nav("成就", "achievements", emoji="🏅", row=1)
        self.nav("成就與圖鑑", "progress", emoji="🏅", row=1)
        self.nav("回總覽", "main", emoji="🏠", row=1)
        return e

    # ======================================================================
    # 圖鑑
    # ======================================================================

    def _load_codex(self):
        return self.svc.codex(self.player_id)

    def _build_codex(self, d):
        cats = {x["key"]: x for x in d["categories"]}
        cat = cats.get(self.codex_category) or d["categories"][0]
        lines = []
        for x in cat["entries"]:
            if x["got"]:
                best = f"　最高 {x['best_quality']}" if x["best_quality"] else ""
                lines.append(f"✅ **{x['name']}**　×{x['quantity']:,}{best}")
            else:
                lines.append("❔ ？？？")
        e = discord.Embed(title=f"📖 ✦ 圖鑑｜{cat['emoji']} {cat['name']} ✦", color=GOLD, description=(
            f"已收集 **{cat['discovered']}/{cat['total']}**　{_bar(cat['discovered'], cat['total'])}\n{LINE}\n"
            + ("\n".join(lines) or "這個分類目前沒有項目。")
        )[:4000])
        e.set_footer(text="❔ 是還沒取得過的物品。數量是累計取得數（收成、偷菜、收集、加工、探險、釣魚）。")

        async def choose(interaction, value):
            self.codex_category = value
            await self.refresh(interaction)

        self.select(CODEX_SELECT, [
            (f"{x['name']}（{x['discovered']}/{x['total']}）", x["key"], None, x["emoji"])
            for x in d["categories"]
        ], choose, row=0, selected=cat["key"])
        self.nav("成就與圖鑑", "progress", emoji="🏅", row=1)
        self.nav("回總覽", "main", emoji="🏠", row=1)
        return e
