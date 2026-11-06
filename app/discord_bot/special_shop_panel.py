"""農場面板「🏪 商店」的第三個分頁「🎁 特賣與升級」（FarmPanel 的 mixin）：溫室、每週特賣、稱號商店。

- 購買都經 picker（與農場商店相同）：溫室與稱號價格高，需要再按一次確認；每週特賣用數量按鈕。
- 規則與數值在 game/special_shop.py；服務在 ProgressService（溫室升級在 GameService）。
"""

from datetime import datetime

import discord

GOLD = 0xF1C40F
LINE = "━━━━━━━━━━━━━━"


def _ts(dt: datetime | None, style: str = "R") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else "—"


def _money(n: int) -> str:
    return f"{n:,}"


class SpecialShopPages:
    """需要 FarmPanel 提供 svc、player_id、picker、select、button、nav、quantity_buttons、refresh、_noop。"""

    def _load_shop_special(self):
        return self.svc.special_shop(self.player_id)

    def _build_shop_special(self, d):
        selected = (self.picker or {}).get("key")

        def mark(key):
            return "▶ " if key == selected else ""

        g = d["greenhouse"]
        if g["next_price"] is None:
            greenhouse = f"🏡 **溫室**　Lv **{g['level']}/{g['max']}**（生長時間 -{g['pct']}%）　✅ 已達最高等級"
        else:
            greenhouse = (f"{mark('greenhouse')}🏡 **溫室**　Lv **{g['level']}/{g['max']}**（目前 -{g['pct']}%）"
                          f"　💰 升級 **{_money(g['next_price'])}** 金 → -{g['next_pct']}%")
        greenhouse += f"\n　📝 永久升級：每一級讓之後澆水的作物生長時間再少 {g['per_level']}%（與限時活動的加速相加）"

        w = d["weekly"]
        deals = []
        for o in w["offers"]:
            left = o["limit"] - o["bought"]
            tag = "🌟 稀有種子（平常不販售）" if o["rare"] else f"🏷️ 原價 {o['original_price']}"
            sold_out = "　❌ 本週已買完" if left <= 0 else ""
            deals.append(f"{mark('weekly:' + o['crop_code'])}🌰 **{o['name']}**　💰 **{_money(o['price'])}** 金"
                         f"　{tag}\n　🛒 本週已買 {o['bought']}/{o['limit']}{sold_out}")
        titles = [
            f"{mark('title:' + t['title'])}🎖️ **{t['title']}**　"
            + ("✅ 已擁有" if t["owned"] else f"💰 **{_money(t['price'])}** 金")
            for t in d["titles"]
        ]
        e = discord.Embed(title="🎁 ✦ 特賣與升級 ✦", color=GOLD, description=(
            f"💰 你的金幣：**{_money(d['gold'])}**\n{LINE}\n"
            f"**🏡 溫室**\n{greenhouse}\n\n"
            f"**🎁 本週特賣**（{_ts(w['ends_at'])}換新商品，每人每週限購）\n" + ("\n".join(deals) or "本週沒有特賣。")
            + "\n\n**🎖️ 稱號商店**（買到後到「🏅 成就圖鑑 → 🎖️ 稱號」選用）\n" + "\n".join(titles)
        )[:4000])
        if self.picker:
            hint = "👇 按「確認購買」完成購買" if self.picker.get("confirm") else "👇 按下方的數量按鈕購買"
            e.add_field(name="🛒 已選擇", inline=False,
                        value=f"**{self.picker['label']}**（{self.picker['price_text']}）\n{hint}")
        else:
            e.set_footer(text="👇 先從選單挑一樣商品")

        offers = {o["crop_code"]: o for o in w["offers"]}
        title_prices = {t["title"]: t["price"] for t in d["titles"]}

        async def pick(interaction, key):
            kind, code = key.split(":", 1) if ":" in key else (key, "")
            pid = self.player_id
            if kind == "greenhouse":
                self.picker = {"key": key, "label": f"🏡 溫室升到 {g['level'] + 1} 級",
                               "price_text": f"{_money(g['next_price'] or 0)} 金", "confirm": True, "max": 1,
                               "presets": (), "run": lambda q: self.svc.upgrade_greenhouse(pid).message}
            elif kind == "title":
                self.picker = {"key": key, "label": f"🎖️ 稱號「{code}」",
                               "price_text": f"{_money(title_prices[code])} 金", "confirm": True, "max": 1,
                               "presets": (), "run": lambda q: self.svc.buy_title(pid, code).message}
            else:
                o = offers[code]
                left = o["limit"] - o["bought"]
                self.picker = {"key": key, "label": f"🌰 {o['name']}（本週特賣）",
                               "price_text": f"每顆 {o['price']} 金", "verb": "買",
                               "max": max(1, min(left, d["gold"] // max(1, o["price"]))),
                               "presets": (1, 3, 10), "keep": True,
                               "run": lambda q: self.svc.buy_weekly_deal(pid, code, q).message}
            await self.refresh(interaction)

        options = []
        if g["next_price"] is not None:
            options.append((f"溫室升到 {g['level'] + 1} 級", "greenhouse",
                            f"🏡 生長時間 -{g['next_pct']}%｜💰 {_money(g['next_price'])} 金", "🏡"))
        options += [(f"{o['name']}（本週特賣）", f"weekly:{o['crop_code']}",
                     f"🎁 剩 {o['limit'] - o['bought']} 顆｜💰 {o['price']} 金", "🌰")
                    for o in w["offers"] if o["limit"] > o["bought"]]
        options += [(f"稱號「{t['title']}」", f"title:{t['title']}", f"🎖️ 稱號｜💰 {_money(t['price'])} 金", "🎖️")
                    for t in d["titles"] if not t["owned"]]
        self.select("🛒 選擇要買的商品…", options, pick, row=0, selected=selected)
        self.quantity_buttons(row=1)
        self.nav("農場商店", "shop", emoji="🌾", row=4)
        self.nav("釣具商店", "fish_shop", emoji="🎣", row=4)
        self.button("特賣與升級", self._noop, emoji="🎁", row=4, style=discord.ButtonStyle.primary, disabled=True)
        self.nav("回總覽", "main", emoji="🏠", row=4)
        return e
