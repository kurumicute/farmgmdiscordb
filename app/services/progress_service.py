"""玩家長期進度：每日簽到、圖鑑、成就與稱號。

繼承鏈：WorldBossService → ProgressService → AdminService（Bot 與網站都用 AdminService）。
- 成就進度由既有資料即時計算（歷史進度也算），只有「已領取」存在 farm_achievements。
- 簽到與成就的獎勵都經 _grant_reward：道具放不進倉庫時改發等值金幣，所有資產變動都寫交易紀錄。
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import TAIPEI_TZ
from app.db.models import (
    DailyCheckin,
    ExplorationLog,
    Farm,
    FarmAchievement,
    FarmFish,
    FarmStats,
    WorldBossRewardClaim,
)
from app.game import achievements, balance, checkin, exploration
from app.game.errors import NotFoundError, ValidationError
from app.game.rules import EventType
from app.repositories import animals as animals_repo
from app.repositories import codex as codex_repo
from app.repositories import crops as crops_repo
from app.repositories import farms as farms_repo
from app.repositories import fishing as fishing_repo
from app.repositories import inventory as inventory_repo
from app.repositories import titles as titles_repo
from app.services.dto import ActionResult
from app.services.world_boss_service import WorldBossService

# 圖鑑分類：(代碼, 圖示, 名稱)
CODEX_CATEGORIES = (
    ("crop", "🌾", "作物"),
    ("animal", "🐔", "動物產品"),
    ("goods", "🏭", "加工品"),
    ("material", "⛏️", "山洞素材"),
    ("fish", "🐟", "魚類"),
)
QUALITY_NAMES = list(balance.QUALITIES.values())  # 依 0–3 排列：普通、優良、稀有、傳說
TITLE_SOURCES = {"world_boss": "世界 Boss"}


def _pct(value: int, total: int) -> int:
    return 0 if total <= 0 else value * 100 // total


class ProgressService(WorldBossService):
    # ---------- 共用：發放獎勵 ----------

    def _grant_reward(self, session: Session, farm: Farm, event: EventType, label: str, gold: int,
                      items: dict) -> tuple[int, dict, bool]:
        """發放金幣與道具（呼叫端已鎖農場）；道具放不進倉庫時改發等值金幣（道具商店價格）。

        回傳 (實際金幣, 實際道具, 是否有道具改發金幣)。"""
        given, converted = {}, False
        space = farm.warehouse_capacity - inventory_repo.total_quantity(session, farm.id)
        for code, qty in items.items():
            if qty <= space:
                given[code] = qty
                space -= qty
            else:
                converted = True
                gold += balance.ITEMS[code][1] * qty
        for code, qty in given.items():
            inventory_repo.add(session, farm.id, code, qty)
            self._record(session, farm, event, f"{label}：獲得{balance.ITEMS[code][0]} ×{qty}"[:200],
                         item_code=code, quantity=qty)
        if gold:
            farm.gold += gold
            note = "（倉庫已滿，道具改發金幣）" if converted else ""
            self._record(session, farm, event, f"{label}：金幣 +{gold}{note}"[:200], gold_delta=gold, quantity=0)
        return gold, given, converted

    # ---------- 每日簽到 ----------

    def _taipei_today(self):
        return self._clock.now().astimezone(TAIPEI_TZ).date()

    def _next_checkin_at(self, today) -> datetime:
        """下一次可以簽到的時間（台灣時間隔天 00:00）。"""
        return datetime.combine(today + timedelta(days=1), datetime.min.time(), TAIPEI_TZ)

    @staticmethod
    def _last_checkin(session: Session, farm_id: int) -> DailyCheckin | None:
        stmt = select(DailyCheckin).where(DailyCheckin.farm_id == farm_id).order_by(DailyCheckin.day.desc()).limit(1)
        return session.scalars(stmt).first()

    def daily_checkin(self, player_id: int) -> dict | None:
        """今天還沒簽到就自動簽到並發放獎勵；還沒有農場時回傳 None。今天已簽到時不鎖農場。

        回傳 {new: 這次是否剛簽到, next_at: 下一次可簽到時間}；剛簽到時另有 streak、reward_text、converted。"""
        with self._transaction() as session:
            today = self._taipei_today()
            farm_id = session.scalar(select(Farm.id).where(Farm.player_id == player_id))
            if farm_id is None:
                return None
            already = {"new": False, "next_at": self._next_checkin_at(today)}
            done = select(DailyCheckin.id).where(DailyCheckin.farm_id == farm_id, DailyCheckin.day == today)
            if session.scalar(done) is not None:
                return already
            farm = self._locked_farm(session, player_id)  # 鎖定後再確認一次，同時開兩個面板也只會簽到一次
            if session.scalar(done) is not None:
                return already
            last = self._last_checkin(session, farm.id)
            streak = checkin.next_streak(last.day if last else None, last.streak if last else 0, today)
            reward = checkin.reward_for(streak)
            gold, items, converted = self._grant_reward(
                session, farm, EventType.CHECKIN_REWARD, f"每日簽到（連續第 {streak} 天）", reward["gold"],
                reward["items"],
            )
            session.add(DailyCheckin(farm_id=farm.id, day=today, streak=streak, gold=gold, items=items,
                                     created_at=self._clock.now()))
            session.flush()  # (farm_id, day) 唯一：萬一重複在這裡失敗並整筆回滾
            return {
                "new": True,
                "streak": streak,
                "reward_text": checkin.reward_text({"gold": gold, "items": items}),
                "converted": converted,
                "next_at": self._next_checkin_at(today),
            }

    def checkin_status(self, player_id: int) -> dict:
        """今天是否已簽到、目前連續天數、下一次簽到的獎勵。只讀取不鎖。"""
        with self._transaction() as session:
            farm_id = session.scalar(select(Farm.id).where(Farm.player_id == player_id))
            return self._checkin_status(session, farm_id)

    def _checkin_status(self, session: Session, farm_id: int | None) -> dict:
        today = self._taipei_today()
        last = self._last_checkin(session, farm_id) if farm_id is not None else None
        checked = last is not None and last.day == today
        # 連續天數：今天或昨天簽到過才算延續中
        alive = last is not None and last.day >= today - timedelta(days=1)
        streak = last.streak if alive else 0
        nxt = streak + 1 if alive else 1
        return {
            "checked_today": checked,
            "streak": streak,
            "next_reward_text": checkin.reward_text(checkin.reward_for(nxt)),
            "next_at": self._next_checkin_at(today),
        }

    # ---------- 圖鑑 ----------

    @staticmethod
    def _codex_categories(session: Session, farm_id: int) -> list[dict]:
        """各分類的圖鑑項目；停用的作物／動物／魚只在已取得時列出（與魚類圖鑑相同）。"""
        got = codex_repo.entries(session, farm_id)

        def entry(code: str, name: str, **extra) -> dict:
            row = got.get(code)
            return {"code": code, "name": name, "got": row is not None, "quantity": row.quantity if row else 0,
                    "best_quality": QUALITY_NAMES[row.best_quality] if row else None, **extra}

        crops = []
        for c in crops_repo.list_all(session):
            for code, name, variant in ((c.code, c.name, False), (c.variant_code, c.variant_name, True)):
                if c.active or code in got:
                    crops.append(entry(code, name, variant=variant))
        animals = [entry(a.product_code, a.product_name) for a in animals_repo.list_all(session)
                   if a.active or a.product_code in got]
        goods = [entry(code, name) for code, (name, _price, _aliases) in balance.GOODS.items()]
        materials = [entry(code, name) for code, (name, _rarity, _use) in exploration.MATERIALS.items()]
        caught = fishing_repo.codex(session, farm_id)
        fish = [
            {"code": t.code, "name": t.name, "got": t.code in caught,
             "quantity": caught[t.code]["count"] if t.code in caught else 0, "best_quality": None}
            for t in fishing_repo.list_fish_types(session) if t.active or t.code in caught
        ]
        lists = {"crop": crops, "animal": animals, "goods": goods, "material": materials, "fish": fish}
        return [
            {"key": key, "emoji": emoji, "name": name, "entries": lists[key],
             "discovered": sum(1 for e in lists[key] if e["got"]), "total": len(lists[key])}
            for key, emoji, name in CODEX_CATEGORIES
        ]

    @staticmethod
    def _codex_view(cats: list[dict]) -> dict:
        return {"categories": cats, "discovered": sum(c["discovered"] for c in cats),
                "total": sum(c["total"] for c in cats)}

    def codex(self, player_id: int) -> dict:
        """圖鑑：各分類已收集／總數與每個項目（未取得的名稱由介面隱藏）。只讀取不鎖。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            return self._codex_view(self._codex_categories(session, farm.id))

    # ---------- 成就 ----------

    def _achievement_metrics(self, session: Session, farm: Farm, categories: list[dict] | None = None) -> dict:
        """成就指標（全部從既有資料計算，含成就上線前的歷史）。

        每個查詢都以農場（或玩家）的索引取少量資料；次數類指標讀 farm_stats 的累計欄位，不掃描交易紀錄。"""
        cats = {c["key"]: c for c in (categories or self._codex_categories(session, farm.id))}
        crops = cats["crop"]["entries"]
        stats = session.get(FarmStats, farm.id)
        fish_total, fish_legendary = session.execute(
            select(func.count(), func.count().filter(FarmFish.rarity == "legendary")).where(FarmFish.farm_id == farm.id)
        ).one()
        boss_joined, boss_won = session.execute(
            select(func.count(), func.count().filter(WorldBossRewardClaim.outcome == "victory"))
            .where(WorldBossRewardClaim.player_id == farm.player_id)
        ).one()
        all_found = sum(c["discovered"] for c in cats.values())
        all_total = sum(c["total"] for c in cats.values())
        return {
            "crops": sum(e["quantity"] for e in crops),
            "crop_sales": stats.crop_sales_income if stats else 0,
            "level": farm.level,
            "plots": farms_repo.count_plots(session, farm.id),
            "gold": farm.gold,
            "legendary_crop": int(any(e["best_quality"] == QUALITY_NAMES[-1] for e in crops)),
            "variant_crop": sum(1 for e in crops if e["got"] and e["variant"]),
            "animal_products": sum(e["quantity"] for e in cats["animal"]["entries"]),
            "goods": sum(e["quantity"] for e in cats["goods"]["entries"]),
            "explorations": session.scalar(
                select(func.count()).select_from(ExplorationLog)
                .where(ExplorationLog.farm_id == farm.id, ExplorationLog.claimed_at.is_not(None))
            ),
            "fish": fish_total,
            "legendary_fish": fish_legendary,
            "steals": stats.steal_count if stats else 0,
            "defended": stats.defended_count if stats else 0,
            "gambles": stats.gamble_count if stats else 0,
            "boss_joined": boss_joined,
            "boss_won": boss_won,
            "best_streak": session.scalar(
                select(func.coalesce(func.max(DailyCheckin.streak), 0)).where(DailyCheckin.farm_id == farm.id)
            ),
            "codex_crop_pct": _pct(cats["crop"]["discovered"], cats["crop"]["total"]),
            "codex_fish_pct": _pct(cats["fish"]["discovered"], cats["fish"]["total"]),
            "codex_pct": _pct(all_found, all_total),
        }

    @staticmethod
    def _claimed(session: Session, farm_id: int) -> dict[str, FarmAchievement]:
        rows = session.scalars(select(FarmAchievement).where(FarmAchievement.farm_id == farm_id))
        return {r.code: r for r in rows}

    def achievements(self, player_id: int) -> dict:
        """成就清單與進度：state = claimed（已領取）／ready（可領取）／progress（進行中）。只讀取不鎖。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            return self._achievement_view(session, farm)

    def progress_overview(self, player_id: int) -> dict:
        """「成就圖鑑」總覽頁：圖鑑、成就、簽到與稱號一次讀取（圖鑑只計算一次）。只讀取不鎖。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            cats = self._codex_categories(session, farm.id)
            return {"codex": self._codex_view(cats), "achievements": self._achievement_view(session, farm, cats),
                    "checkin": self._checkin_status(session, farm.id), "titles": self._titles(session, farm)}

    def _achievement_view(self, session: Session, farm: Farm, categories: list[dict] | None = None) -> dict:
        metrics = self._achievement_metrics(session, farm, categories)
        claimed = self._claimed(session, farm.id)
        rows = []
        for a in achievements.ACHIEVEMENTS:
            value = metrics.get(a.metric, 0)
            state = "claimed" if a.code in claimed else ("ready" if value >= a.target else "progress")
            rows.append({
                "code": a.code, "category": a.category, "emoji": a.emoji, "name": a.name,
                "description": a.description, "value": min(value, a.target), "target": a.target,
                "unit": achievements.METRICS[a.metric][1], "state": state,
                "reward_text": achievements.reward_text(a), "title": a.title,
                "claimed_at": claimed[a.code].claimed_at if a.code in claimed else None,
            })
        return {
            "rows": rows,
            "claimed": sum(1 for r in rows if r["state"] == "claimed"),
            "ready": sum(1 for r in rows if r["state"] == "ready"),
            "total": len(rows),
        }

    def claim_achievements(self, player_id: int) -> ActionResult:
        """一次領取所有已達成但還沒領的成就獎勵（金幣、道具、稱號）。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            now = self._clock.now()
            metrics = self._achievement_metrics(session, farm)
            claimed = self._claimed(session, farm.id)
            ready = [a for a in achievements.ACHIEVEMENTS if a.code not in claimed and achievements.reached(a, metrics)]
            if not ready:
                raise ValidationError("目前沒有可以領取的成就獎勵。")
            lines, titles, converted_any = [], [], False
            for a in ready:
                gold, items, converted = self._grant_reward(
                    session, farm, EventType.ACHIEVEMENT_REWARD, f"成就「{a.name}」", a.gold, a.items
                )
                converted_any |= converted
                if a.title:
                    titles_repo.grant(session, farm.id, a.title, f"achievement:{a.code}", now)
                    titles.append(a.title)
                session.add(FarmAchievement(farm_id=farm.id, code=a.code, gold=gold, items=items, claimed_at=now))
                parts = [p for p in (checkin.reward_text({"gold": gold, "items": items}),) if p != "無"]
                parts += [f"稱號「{a.title}」"] if a.title else []
                lines.append(f"{a.emoji} {a.name}：{'、'.join(parts) or '—'}")
            session.flush()  # (farm_id, code) 為主鍵：重複領取在這裡失敗並整筆回滾
            message = f"🏅 領取 {len(ready)} 個成就獎勵：\n" + "\n".join(lines)
            if converted_any:
                message += "\n（倉庫已滿，部分道具改發等值金幣）"
            if titles:
                message += "\n🎖️ 新稱號可以在下方「稱號」設定顯示。"
            return ActionResult(message, farm.gold)

    # ---------- 稱號 ----------

    def titles(self, player_id: int) -> dict:
        """擁有的稱號與目前選用的稱號。只讀取不鎖。"""
        with self._transaction() as session:
            return self._titles(session, self._farm_for_read(session, player_id))

    @staticmethod
    def _titles(session: Session, farm: Farm) -> dict:
        owned = [
            {"title": t.title, "obtained_at": t.obtained_at,
             "source": (achievements.BY_CODE[t.source.split(":", 1)[1]].name
                        if t.source.startswith("achievement:") and t.source.split(":", 1)[1] in achievements.BY_CODE
                        else TITLE_SOURCES.get(t.source, t.source))}
            for t in titles_repo.owned(session, farm.id)
        ]
        return {"current": farm.title, "owned": owned}

    def set_title(self, player_id: int, title: str | None) -> ActionResult:
        """選用一個已擁有的稱號（None 或空字串 = 不顯示）。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            title = (title or "").strip() or None
            if title is not None and title not in {t.title for t in titles_repo.owned(session, farm.id)}:
                raise NotFoundError(f"你還沒有「{title}」這個稱號。")
            farm.title = title
            return ActionResult(f"已設定稱號「{title}」。" if title else "已取消顯示稱號。", farm.gold)

    def player_titles(self, player_ids: list[int]) -> dict[int, str]:
        """排行榜顯示用：玩家 → 目前選用的稱號。"""
        with self._transaction() as session:
            return titles_repo.selected_by_player(session, list(player_ids))
