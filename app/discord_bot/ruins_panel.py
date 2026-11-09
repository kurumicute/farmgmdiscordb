"""面板：古代遺跡修復頁與遺跡守護者（山洞 →「🏛️ 修復遺跡」，RuinsPages mixin）。

遺跡屬於農場的所屬群組，在哪個伺服器（或私訊）開面板都看得到同一座遺跡。修好後同一頁顯示本週的守護者，
用下拉選單選素材、數量按鈕攻擊（沿用捐獻的數量選擇）。
"""

import discord

from app.game import ruins

BAR_SLOTS = 10


def progress_bar(pct: int) -> str:
    filled = max(0, min(BAR_SLOTS, pct * BAR_SLOTS // 100))
    return "▰" * filled + "▱" * (BAR_SLOTS - filled) + f" {pct}%"


class RuinsPages:
    def _load_ruins(self):
        return self.svc.ruins_status(self.player_id)

    def _build_ruins(self, d):
        from app.discord_bot.panels import BLUE, GOLD, MATERIAL_EMOJI, RED, sections, ts  # 避免循環匯入

        if d["group_id"] is None:
            e = discord.Embed(title="🏛️ ✦ 古代遺跡 ✦", color=BLUE,
                              description="你的農場還沒有所屬群組，到 Discord 伺服器中開啟面板就會設定。")
            self.nav("返回冒險介面", "cave", emoji="↩️", row=0)
            return e
        stages = "\n".join(
            f"{'✅' if i < d['stage'] else ('🔨' if i == d['stage'] else '🔒')} 第 {i + 1} 階段：{name}"
            for i, name in enumerate(d["stage_names"])
        )
        top = [f"{i}. {name}{f'〔{title}〕' if title else ''}　{qty:,} 個"
               for i, (name, title, qty) in enumerate(d["top"], 1)]
        if d["completed"]:
            body = ("🎉 **修復完成！** 群組每個人（Lv.4 起）每天都能到山洞**額外**探索一次「🏛️ 古代遺跡」，"
                    "不佔一般探險的名額，立即拿到獎勵。\n"
                    f"出過力的成員都獲得了稱號「{d['title']}」。")
            mats = []
        else:
            body = f"🔨 目前階段：**{d['stage_name']}**\n{d['stage_description']}"
            if d["collapsed_at"]:
                body = (f"🏚️ 遺跡在 {ts(d['collapsed_at'], 'D')} 因為守護者沒被打倒而崩塌了，重新捐核心素材就能修好。\n"
                        + body)
            mats = [f"{'✅' if m['donated'] >= m['need'] else '🧱'} {MATERIAL_EMOJI.get(m['code'], '📦')} {m['name']}"
                    f"　{m['donated']:,}/{m['need']:,}　（背包 {m['have']:,}）" for m in d["materials"]]
        e = discord.Embed(title=f"🏛️ ✦ {d['group_name']}的古代遺跡 ✦", color=GOLD if d["completed"] else BLUE,
                          description=sections(
                              "群組成員一起捐山洞素材修復遺跡；三個階段都完成後，所有成員都能進入新的探險區域，"
                              f"捐過素材的人還會得到稱號「{d['title']}」。",
                              f"📊 修復進度　{progress_bar(d['percent'])}\n{stages}",
                              body,
                              ("🧱 這個階段需要", mats),
                              f"🙋 我累計捐了 **{d['mine']:,}** 個素材",
                              ("🏅 修復貢獻排行", top or ["還沒有人捐獻，當第一個吧！"]),
                              self._guardian_text(d["guardian"], ts),
                          ))
        g = d["guardian"]
        if g and g["active"] and not g["defeated"]:
            e.color = RED
        if self.picker and self.picker.get("key", "").startswith(("ruins:", "guardian:")):
            e.add_field(name="🏛️ 已選擇", inline=False, value=f"**{self.picker['label']}**\n👇 選擇數量")
        options = [(m["name"], m["code"], f"還差 {m['need'] - m['donated']}，背包 {m['have']}",
                    MATERIAL_EMOJI.get(m["code"], "📦"))
                   for m in d["materials"] if m["donated"] < m["need"] and m["have"] > 0]

        async def pick(interaction, code):
            m = next(x for x in d["materials"] if x["code"] == code)
            most = min(m["have"], m["need"] - m["donated"], ruins.DONATE_MAX)
            self.picker = {
                "key": f"ruins:{code}", "label": f"捐 {m['name']}（最多 {most}）", "verb": "捐", "max": most,
                "presets": tuple(sorted({q for q in (10, 50, most) if q <= most})),
                "run": lambda q: self.svc.donate_ruins(self.player_id, code, q).message,
            }
            await self.refresh(interaction)

        selected = self.picker["key"].split(":", 1)[-1] if self.picker and "key" in self.picker else None
        if options:
            self.select("🧱 選擇要捐的素材…", options, pick, row=0, selected=selected)
            self.quantity_buttons(row=1)
        elif g and g["active"] and not g["defeated"] and g["weapons"]:
            async def pick_weapon(interaction, code):
                w = next(x for x in g["weapons"] if x["code"] == code)
                left = g["hp_max"] - g["damage"]
                most = min(w["have"], -(-left // w["damage"]), ruins.ATTACK_MAX)
                self.picker = {
                    "key": f"guardian:{code}", "label": f"用 {w['name']} 攻擊（每個 {w['damage']} 傷害，最多 {most}）",
                    "verb": "用", "max": most, "presets": tuple(sorted({q for q in (10, 50, most) if q <= most})),
                    "run": lambda q: self.svc.attack_guardian(self.player_id, code, q).message,
                }
                await self.refresh(interaction)

            weapons = [(w["name"], w["code"], f"每個 {w['damage']} 傷害，背包 {w['have']}",
                        MATERIAL_EMOJI.get(w["code"], "📦")) for w in g["weapons"]]
            self.select("⚔️ 選擇攻擊守護者的素材…", weapons, pick_weapon, row=0, selected=selected)
            self.quantity_buttons(row=1)
        self.nav("返回冒險介面", "cave", emoji="↩️", row=2)
        self._nav_back(row=2)
        return e

    @staticmethod
    def _guardian_text(g, ts) -> str:
        if not g:
            return ""
        if not g["active"]:
            return (f"🗿 **{g['name']}** 會在 {ts(g['appears_at'], 'F')} 出現（之後每週一個）。"
                    "整週沒打倒，遺跡就會崩塌回第三階段。")
        left = g["hp_max"] - g["damage"]
        head = (f"🗿 **本週{g['name']}**　HP {progress_bar(left * 100 // g['hp_max'])}（{left:,}/{g['hp_max']:,}）\n"
                f"⏳ {ts(g['ends_at'])}前要打倒，否則遺跡崩塌")
        if g["defeated"]:
            head = (f"🗿 **本週{g['name']}已被打倒！** 🎉 本週遺跡探索每次多 {g['bonus_rolls']} 份獎勵；"
                    f"下一隻 {ts(g['ends_at'])}出現。")
        top = "、".join(f"{name} {dmg:,}" for name, dmg in g["top"]) or "還沒有人出手"
        return (f"{head}\n⚔️ 捐素材攻擊（越稀有傷害越高），每次探索遺跡也會自動造成 {g['expedition_damage']} 傷害\n"
                f"🙋 我本週造成 **{g['mine']:,}** 傷害　🏅 {top}")
