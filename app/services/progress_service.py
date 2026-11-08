"""玩家長期進度：每日簽到、圖鑑、成就與稱號，以及商店的「特賣與升級」（溫室、每週特賣、稱號商店）。

繼承鏈：WorldBossService → ProgressService → AdminService（Bot 與網站都用 AdminService）。
- 成就進度由既有資料即時計算（歷史進度也算），只有「已領取」存在 farm_achievements。
- 簽到與成就的獎勵都經 _grant_reward：道具放不進倉庫時改發等值金幣，所有資產變動都寫交易紀錄。
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.clock import TAIPEI_TZ
from app.db.models import (
    DailyCheckin,
    ExplorationLog,
    Farm,
    FarmAchievement,
    FarmFish,
    FarmStats,
    WeeklyShopPurchase,
    WorldBossRewardClaim,
)
from app.game import achievements, balance, checkin, exploration, pets, rules, special_shop
from app.game.errors import AlreadyExistsError, InsufficientGoldError, NotFoundError, ValidationError
from app.game.rules import EventType, PlotState
from app.repositories import animals as animals_repo
from app.repositories import codex as codex_repo
from app.repositories import crops as crops_repo
from app.repositories import farms as farms_repo
from app.repositories import feedback as feedback_repo
from app.repositories import fishing as fishing_repo
from app.repositories import inventory as inventory_repo
from app.repositories import titles as titles_repo
from app.services.dto import ActionResult
from app.services.game_service import long_duration
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
TITLE_SOURCES = {"world_boss": "世界 Boss", "shop": "稱號商店", "admin": "管理員"}

assert not {a.title for a in achievements.BASE_ACHIEVEMENTS} & {t for t, _p in special_shop.title_shop()}, \
    "商店稱號與成就稱號重複"


def title_source(source: str) -> str:
    """稱號來源的顯示名稱：成就名稱、世界 Boss、稱號商店或管理員。"""
    if source.startswith("achievement:"):
        a = achievements.BASE_BY_CODE.get(source.split(":", 1)[1])
        return a.name if a else source
    return TITLE_SOURCES.get(source, source)


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
        for a in achievements.active():
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

    # ---------- 操作回饋：圖鑑與成就標記、歡迎回來摘要 ----------

    def _progress_marks(self, session: Session, farm: Farm) -> dict:
        """已登記的圖鑑項目與可領取的成就（面板比較操作前後，顯示「新圖鑑」「成就達成」）。"""
        cats = self._codex_categories(session, farm.id)
        view = self._achievement_view(session, farm, cats)
        codex = {f"{c['key']}:{e['code']}": e["name"] for c in cats for e in c["entries"] if e["got"]}
        return {
            "codex": codex, "codex_total": sum(c["total"] for c in cats),
            "ready": {r["code"]: f"{r['emoji']} {r['name']}" for r in view["rows"] if r["state"] == "ready"},
        }

    def progress_marks(self, player_id: int) -> dict:
        """只讀取不鎖。"""
        with self._transaction() as session:
            return self._progress_marks(session, self._farm_for_read(session, player_id))

    AWAY_MIN_SECONDS = 1800  # 離開超過 30 分鐘才顯示「你不在的時候」

    def welcome_back(self, player_id: int) -> dict:
        """打開面板時呼叫：記錄這次的時間，離開夠久時回傳期間發生的事（lines），並附上圖鑑與成就標記。"""
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            prev = feedback_repo.touch_seen(session, farm.id, now)
            marks = self._progress_marks(session, farm)
            lines = []
            if prev is not None and (now - prev).total_seconds() >= self.AWAY_MIN_SECONDS:
                lines = self._away_lines(session, farm, prev, now, marks)
            return {"lines": lines, "marks": marks}

    def _away_lines(self, session: Session, farm: Farm, since: datetime, now: datetime, marks: dict) -> list[str]:
        s = feedback_repo.steals_against(session, farm.player_id, since)
        lines = []
        if s["stolen_times"]:
            who = "、".join(s["thieves"])
            lines.append(f"🥷 被偷了 {s['stolen_times']} 次，共 {s['stolen_quantity']} 個作物（{who}）")
        dog = s["blocked"].pop(pets.DOG_BLOCKER, 0)
        if dog:
            lines.append(f"🐶 狗狗擋下了 {dog} 次偷菜")
        if s["blocked"]:
            lines.append(f"🛡️ 防守設施擋下了 {sum(s['blocked'].values())} 次偷菜")
        states = [rules.plot_state(p.crop_type_id, p.watered_at, p.ready_at, now)
                  for p in farms_repo.list_plots(session, farm.id)]
        ready = states.count(PlotState.READY)
        if ready:
            lines.append(f"🌾 {ready} 格作物成熟了，可以收成")
        withered = states.count(PlotState.WITHERED)
        if withered:
            lines.append(f"🥀 {withered} 格作物乾枯了，記得清除")
        back = session.scalar(
            select(func.count()).select_from(ExplorationLog)
            .where(ExplorationLog.farm_id == farm.id, ExplorationLog.claimed_at.is_(None),
                   ExplorationLog.next_available_at <= now)
        )
        if back:
            lines.append("⛰️ 探險隊回來了，到「山洞」領取戰利品")
        if marks["ready"]:
            lines.append(f"🏅 有 {len(marks['ready'])} 個成就可以領取")
        if not lines:
            return []
        away = long_duration(int((now - since).total_seconds()))
        return [f"🌙 歡迎回來！你離開了 {away}，這段時間："] + [f"・{x}" for x in lines]

    def claim_achievements(self, player_id: int) -> ActionResult:
        """一次領取所有已達成但還沒領的成就獎勵（金幣、道具、稱號）。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            now = self._clock.now()
            metrics = self._achievement_metrics(session, farm)
            claimed = self._claimed(session, farm.id)
            ready = [a for a in achievements.active() if a.code not in claimed and achievements.reached(a, metrics)]
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
            {"title": t.title, "obtained_at": t.obtained_at, "source": title_source(t.source)}
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

    # ---------- 特賣與升級：溫室、每週特賣、稱號商店 ----------

    @staticmethod
    def _weekly_offers(session: Session, start) -> list[special_shop.Offer]:
        crops = [special_shop.CropRef(c.code, c.name, c.seed_price, c.shop_buyable)
                 for c in crops_repo.list_all(session, active_only=True)]
        return special_shop.weekly_offers(start, crops)

    @staticmethod
    def _weekly_bought(session: Session, farm_id: int, start) -> dict[str, int]:
        rows = session.execute(select(WeeklyShopPurchase.item_code, WeeklyShopPurchase.quantity)
                               .where(WeeklyShopPurchase.farm_id == farm_id, WeeklyShopPurchase.week_start == start))
        return dict(rows.all())

    def special_shop(self, player_id: int) -> dict:
        """商店「特賣與升級」分頁：溫室等級與價格、本週特賣與已購買量、稱號商店。只讀取不鎖。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            start = special_shop.week_start(self._clock.now())
            bought = self._weekly_bought(session, farm.id, start)
            owned = {t.title for t in titles_repo.owned(session, farm.id)}
            level = farm.greenhouse_level
            return {
                "gold": farm.gold,
                "greenhouse": {
                    "level": level, "max": special_shop.greenhouse_max_level(),
                    "pct": special_shop.greenhouse_pct(level), "next_price": special_shop.greenhouse_price(level),
                    "next_pct": special_shop.greenhouse_pct(level + 1),
                    "per_level": special_shop.greenhouse_pct_per_level(),
                },
                "weekly": {
                    "week_start": start, "ends_at": special_shop.week_ends_at(start),
                    "offers": [
                        {"crop_code": o.crop_code, "name": f"{o.crop_name}種子", "price": o.price,
                         "original_price": o.original_price, "limit": o.limit, "rare": o.rare,
                         "bought": bought.get(rules.seed_item_code(o.crop_code), 0)}
                        for o in self._weekly_offers(session, start)
                    ],
                },
                "titles": [{"title": t, "price": price, "owned": t in owned} for t, price in special_shop.title_shop()],
            }

    def buy_weekly_deal(self, player_id: int, crop_code: str, quantity: int) -> ActionResult:
        """購買本週特賣的種子：每人每週每項限購；稀有種子放素材背包。"""
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValidationError("購買數量必須是正整數。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            start = special_shop.week_start(self._clock.now())
            offer = next((o for o in self._weekly_offers(session, start) if o.crop_code == crop_code), None)
            if offer is None:
                raise NotFoundError("這項商品不在本週特賣中（每週一 00:00 換一批），請重新整理。")
            item = rules.seed_item_code(offer.crop_code)
            bought = self._weekly_bought(session, farm.id, start).get(item, 0)
            if bought + quantity > offer.limit:
                left = max(0, offer.limit - bought)
                raise ValidationError(f"{offer.crop_name}種子本週每人限購 {offer.limit} 顆，你還可以買 {left} 顆。")
            cost = offer.price * quantity
            if farm.gold < cost:
                raise InsufficientGoldError(f"金幣不足：需要 {cost}，目前只有 {farm.gold}。")
            self._ensure_item_space(session, farm, item, quantity)
            farm.gold -= cost
            inventory_repo.add(session, farm.id, item, quantity)
            stmt = insert(WeeklyShopPurchase).values(farm_id=farm.id, week_start=start, item_code=item,
                                                     quantity=quantity)
            session.execute(stmt.on_conflict_do_update(
                index_elements=[WeeklyShopPurchase.farm_id, WeeklyShopPurchase.week_start,
                                WeeklyShopPurchase.item_code],
                set_={"quantity": WeeklyShopPurchase.quantity + stmt.excluded.quantity},
            ))
            self._record(session, farm, EventType.BUY_SEED,
                         f"每週特賣：購買 {quantity} 顆{offer.crop_name}種子（每顆 {offer.price}），金幣 -{cost}",
                         item_code=item, quantity=quantity, gold_delta=-cost)
            left = offer.limit - bought - quantity
            return ActionResult(
                f"🎁 買到 {quantity} 顆{offer.crop_name}種子，花費 {cost:,} 金幣（本週還能買 {left} 顆）。",
                farm.gold, -cost, item, quantity,
            )

    def buy_title(self, player_id: int, title: str) -> ActionResult:
        """稱號商店：用金幣買稱號，買到後可以在「🎖️ 稱號」選用。每個稱號只能買一次。"""
        price = special_shop.title_price(title)
        if price is None:
            raise NotFoundError(f"稱號商店沒有「{title}」。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if title in {t.title for t in titles_repo.owned(session, farm.id)}:
                raise AlreadyExistsError(f"你已經擁有稱號「{title}」。")
            if farm.gold < price:
                raise InsufficientGoldError(f"金幣不足：稱號「{title}」需要 {price:,}，目前只有 {farm.gold:,}。")
            farm.gold -= price
            titles_repo.grant(session, farm.id, title, "shop", self._clock.now())
            self._record(session, farm, EventType.BUY_ITEM, f"稱號商店：購買稱號「{title}」，金幣 -{price}",
                         item_code=special_shop.TITLE_SHOP_CODE, quantity=1, gold_delta=-price)
            return ActionResult(f"🎖️ 買到稱號「{title}」！到「🏅 成就圖鑑 → 🎖️ 稱號」選用。", farm.gold, -price)
