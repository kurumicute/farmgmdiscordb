"""管理員：新功能（每日簽到、寵物、特賣與升級、成就、稱號、圖鑑）的規則設定與單一玩家資料調整。

由 AdminService 繼承（需要 _transaction、_locked_farm、_audit、_adjust 與 ProgressService 的讀取方法）。
- 規則設定存在 feature_settings（見 game/feature_config.py）；儲存後本程序立即生效，Bot 最晚 10 秒內套用。
- 調整玩家資料都不改金幣；每次調整寫一筆 admin_adjust 交易紀錄（金幣 0，玩家看得到）與管理操作紀錄。
"""

from datetime import timedelta

from sqlalchemy import delete

from app.db.models import DailyCheckin, FarmAchievement, FarmCodexEntry, FarmTitle, WeeklyShopPurchase
from app.game import achievements, balance, checkin, feature_config, pets, special_shop
from app.game.errors import AlreadyExistsError, NotFoundError, ValidationError
from app.repositories import feature_settings as feature_settings_repo
from app.repositories import pets as pets_repo
from app.repositories import titles as titles_repo
from app.services.progress_service import QUALITY_NAMES, title_source

FEATURE_KEYS = (checkin.CONFIG_KEY, pets.CONFIG_KEY, special_shop.CONFIG_KEY, achievements.CONFIG_KEY)
CODEX_EDITABLE = ("crop", "animal", "goods", "material")  # 魚類圖鑑來自 farm_fish，不能在這裡改
ADMIN_TITLE_SOURCE = "admin"
MAX_STREAK = 10_000


def _int(value, label: str, low: int, high: int) -> int:
    return feature_config.int_value(value, label, low, high)


class ProgressAdmin:
    # ======================================================================
    # 規則設定
    # ======================================================================

    def _check_titles(self, key: str, value: dict) -> None:
        """商店稱號不可和成就稱號重複（兩邊都可以改，存檔時以另一邊目前的設定檢查）。"""
        if key == special_shop.CONFIG_KEY:
            clash = {t["title"] for t in value["titles"]} & achievements.titles()
        elif key == achievements.CONFIG_KEY:
            shop = {t for t, _p in special_shop.title_shop()}
            clash = {a.title for a in achievements.apply_config(value) if a.title} & shop
        else:
            return
        if clash:
            raise ValidationError(f"商店稱號與成就稱號不可重複：{'、'.join(sorted(clash))}。")

    def feature_settings(self) -> dict:
        """各項規則目前的設定、預設值與最後修改者，以及編輯畫面需要的道具與成就清單。"""
        with self._transaction() as session:
            feature_settings_repo.refresh(session, force=True)
            stored = feature_settings_repo.rows(session)
        keys = []
        for key in FEATURE_KEYS:
            row = stored.get(key)
            keys.append({
                "key": key, "label": feature_config.label(key), "current": feature_config.get(key),
                "defaults": feature_config.defaults(key), "is_default": feature_config.is_default(key),
                "updated_at": row.updated_at if row else None, "updated_by": row.updated_by if row else None,
            })
        categories = {c: (emoji, name) for c, emoji, name in achievements.CATEGORIES}
        return {
            "keys": keys,
            "items": [{"code": code, "name": v[0], "price": v[1]} for code, v in balance.ITEMS.items()],
            "achievements": [
                {"code": a.code, "category": a.category, "category_name": categories[a.category][1],
                 "emoji": a.emoji, "name": a.name, "metric": achievements.METRICS[a.metric][0],
                 "unit": achievements.METRICS[a.metric][1], "max_target": 100 if a.metric in achievements.PCT_METRICS
                 else 1_000_000_000_000, "default": achievements.editable(a),
                 "current": achievements.editable(achievements.by_code()[a.code])}
                for a in achievements.BASE_ACHIEVEMENTS
            ],
            "limits": {"pet_levels": pets.LEVEL_LIMIT, "greenhouse_levels": special_shop.GREENHOUSE_LEVEL_LIMIT,
                       "checkin_days": checkin.MAX_CYCLE_DAYS, "title_max_len": special_shop.TITLE_MAX_LEN},
        }

    def update_feature_settings(self, actor: str, key: str, raw: dict) -> dict:
        if key not in FEATURE_KEYS:
            raise NotFoundError(f"沒有「{key}」這項設定。")
        value = feature_config.validate(key, raw)
        self._check_titles(key, value)
        with self._transaction() as session:
            before = feature_config.get(key)
            feature_settings_repo.save(session, key, value, actor, self._clock.now())
            self._audit(session, actor, "update_feature_settings", "feature_settings", None,
                        {"key": key, "before": before, "after": value})
        feature_config.set_current(key, value)  # 提交後才在本程序生效
        return {"key": key, "current": value}

    def reset_feature_settings(self, actor: str, key: str) -> dict:
        """還原成程式內的預設值（刪除資料列）。"""
        if key not in FEATURE_KEYS:
            raise NotFoundError(f"沒有「{key}」這項設定。")
        defaults = feature_config.defaults(key)
        self._check_titles(key, defaults)
        with self._transaction() as session:
            before = feature_config.get(key)
            feature_settings_repo.remove(session, key)
            self._audit(session, actor, "reset_feature_settings", "feature_settings", None,
                        {"key": key, "before": before})
        feature_config.set_current(key, None)
        return {"key": key, "current": defaults}

    # ======================================================================
    # 單一玩家：讀取
    # ======================================================================

    def admin_progress(self, player_id: int) -> dict:
        """管理網站「玩家調整」的新功能資料：溫室、寵物、稱號、簽到、成就、圖鑑與本週特賣購買量。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            cats = self._codex_categories(session, farm.id)
            today = self._taipei_today()
            last = self._last_checkin(session, farm.id)
            owned_pets = pets_repo.levels(session, farm.id)
            start = special_shop.week_start(self._clock.now())
            bought = self._weekly_bought(session, farm.id, start)
            item_names = self._item_names(session)
            return {
                "greenhouse": {"level": farm.greenhouse_level, "max": special_shop.greenhouse_max_level(),
                               "pct": special_shop.greenhouse_pct(farm.greenhouse_level)},
                "pets": [{"code": code, "name": p.name, "emoji": p.emoji, "level": owned_pets.get(code),
                          "max": pets.max_level(), "pct": pets.bonus_pct(owned_pets.get(code))}
                         for code, p in pets.PETS.items()],
                "titles": {
                    "current": farm.title,
                    "owned": [{"title": t.title, "source": title_source(t.source), "obtained_at": t.obtained_at}
                              for t in titles_repo.owned(session, farm.id)],
                    "known": sorted(achievements.titles() | {t for t, _p in special_shop.title_shop()}),
                },
                "checkin": {
                    **self._checkin_status(session, farm.id),
                    "last_day": last.day if last else None, "last_streak": last.streak if last else None,
                    "today": today,
                },
                "achievements": self._achievement_view(session, farm, cats)["rows"],
                "codex": [
                    {"key": c["key"], "name": c["name"], "editable": c["key"] in CODEX_EDITABLE,
                     "discovered": c["discovered"], "total": c["total"],
                     "entries": [{"code": e["code"], "name": e["name"], "got": e["got"], "quantity": e["quantity"],
                                  "best_quality": QUALITY_NAMES.index(e["best_quality"]) if e["best_quality"] else 0}
                                 for e in c["entries"]]}
                    for c in cats
                ],
                "quality_names": QUALITY_NAMES,
                "weekly": {"week_start": start,
                           "purchases": [{"item_code": code, "name": item_names.get(code, code), "quantity": qty}
                                         for code, qty in sorted(bought.items())]},
            }

    @staticmethod
    def _item_names(session) -> dict[str, str]:
        from app.repositories import catalog as catalog_repo

        return {code: name for code, (_kind, name) in catalog_repo.items(session).items()}

    # ======================================================================
    # 單一玩家：調整
    # ======================================================================

    def set_greenhouse_level(self, actor: str, player_id: int, level: int) -> dict:
        level = _int(level, "溫室等級", 0, special_shop.greenhouse_max_level())
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            before = farm.greenhouse_level
            if before == level:
                return {"level": level, "changed": False}
            farm.greenhouse_level = level
            self._adjust(session, farm, actor, "set_greenhouse_level", f"溫室等級 {before} → {level}",
                         {"before": before, "after": level})
            return {"level": level, "changed": True}

    def set_pet_level(self, actor: str, player_id: int, pet_code: str, level: int | None) -> dict:
        """設定寵物等級；level 為 None 表示移除這隻寵物（不退費）。"""
        pet = pets.PETS.get(pet_code)
        if pet is None:
            raise NotFoundError(f"沒有「{pet_code}」這種寵物。")
        if level is not None:
            level = _int(level, f"{pet.name}等級", 0, pets.max_level())
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            row = pets_repo.get(session, farm.id, pet_code)
            before = row.level if row else None
            if before == level:
                return {"level": level, "changed": False}
            if level is None:
                session.delete(row)
            elif row is None:
                from app.db.models import FarmPet

                session.add(FarmPet(farm_id=farm.id, pet_code=pet_code, level=level, bought_at=self._clock.now()))
            else:
                row.level = level
            text = lambda lv: "沒有" if lv is None else f"Lv{lv}"  # noqa: E731
            self._adjust(session, farm, actor, "set_pet_level", f"{pet.name} {text(before)} → {text(level)}",
                         {"pet": pet_code, "before": before, "after": level})
            return {"level": level, "changed": True}

    def grant_title(self, actor: str, player_id: int, title: str) -> dict:
        title = feature_config.title_value(title, "稱號", special_shop.TITLE_MAX_LEN, required=True)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if title in {t.title for t in titles_repo.owned(session, farm.id)}:
                raise AlreadyExistsError(f"玩家已經擁有稱號「{title}」。")
            titles_repo.grant(session, farm.id, title, ADMIN_TITLE_SOURCE, self._clock.now())
            self._adjust(session, farm, actor, "grant_title", f"給予稱號「{title}」", {"title": title})
            return {"title": title}

    def revoke_title(self, actor: str, player_id: int, title: str) -> dict:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            found = session.get(FarmTitle, (farm.id, title))
            if found is None:
                raise NotFoundError(f"玩家沒有稱號「{title}」。")
            session.delete(found)
            if farm.title == title:
                farm.title = None
            self._adjust(session, farm, actor, "revoke_title", f"移除稱號「{title}」",
                         {"title": title, "source": found.source})
            return {"title": title}

    def set_active_title(self, actor: str, player_id: int, title: str | None) -> dict:
        """設定玩家目前顯示的稱號（必須已擁有；None = 不顯示）。"""
        title = (title or "").strip() or None
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if title is not None and title not in {t.title for t in titles_repo.owned(session, farm.id)}:
                raise NotFoundError(f"玩家沒有稱號「{title}」。")
            before = farm.title
            if before == title:
                return {"title": title, "changed": False}
            farm.title = title
            self._adjust(session, farm, actor, "set_active_title", f"顯示稱號「{before or '無'}」→「{title or '無'}」",
                         {"before": before, "after": title})
            return {"title": title, "changed": True}

    def set_checkin_streak(self, actor: str, player_id: int, streak: int) -> dict:
        """設定目前的連續簽到天數（例如 Bot 停機讓玩家斷簽時補回）。

        最近一次簽到是今天或昨天：直接改那一筆；否則補一筆「昨天」的簽到（不發獎），玩家今天簽到時就會接續。"""
        streak = _int(streak, "連續天數", 1, MAX_STREAK)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            today = self._taipei_today()
            last = self._last_checkin(session, farm.id)
            if last is not None and last.day >= today - timedelta(days=1):
                before = last.streak
                last.streak = streak
                where = "今天" if last.day == today else "昨天"
            else:
                before = 0
                session.add(DailyCheckin(farm_id=farm.id, day=today - timedelta(days=1), streak=streak, gold=0,
                                         items={}, created_at=self._clock.now()))
                where = "補登昨天"
            self._adjust(session, farm, actor, "set_checkin_streak", f"連續簽到 {before} → {streak} 天（{where}）",
                         {"before": before, "after": streak})
            return {"streak": streak}

    def set_achievement_claimed(self, actor: str, player_id: int, code: str, claimed: bool) -> dict:
        """標記成就為已領取（不發獎）或重設為未領取（玩家可以再領一次獎勵；已給的稱號不會收回）。"""
        a = achievements.by_code().get(code)
        if a is None:
            raise NotFoundError(f"沒有「{code}」這個成就。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            row = session.get(FarmAchievement, (farm.id, code))
            if (row is not None) == claimed:
                return {"code": code, "claimed": claimed, "changed": False}
            if claimed:
                session.add(FarmAchievement(farm_id=farm.id, code=code, gold=0, items={}, claimed_at=self._clock.now()))
                text = f"成就「{a.name}」標記為已領取（不發獎）"
            else:
                session.delete(row)
                text = f"成就「{a.name}」重設為未領取"
            self._adjust(session, farm, actor, "set_achievement_claimed", text, {"code": code, "claimed": claimed})
            return {"code": code, "claimed": claimed, "changed": True}

    def set_codex_entry(self, actor: str, player_id: int, item_code: str, quantity: int, best_quality: int) -> dict:
        """設定圖鑑的累計數量與最高品質；數量 0 = 改回未取得。魚類圖鑑來自魚簍紀錄，不能在這裡改。"""
        quantity = _int(quantity, "累計數量", 0, 1_000_000_000_000)
        best_quality = _int(best_quality, "最高品質", 0, len(QUALITY_NAMES) - 1)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            names = {e["code"]: e["name"] for c in self._codex_categories(session, farm.id)
                     if c["key"] in CODEX_EDITABLE for e in c["entries"]}
            if item_code not in names:
                raise NotFoundError(f"「{item_code}」不是可調整的圖鑑項目。")
            row = session.get(FarmCodexEntry, (farm.id, item_code))
            before = (row.quantity, row.best_quality) if row else (0, 0)
            if before == (quantity, best_quality if quantity else 0):
                return {"item_code": item_code, "changed": False}
            if quantity == 0:
                if row is not None:
                    session.delete(row)
            elif row is None:
                session.add(FarmCodexEntry(farm_id=farm.id, item_code=item_code, quantity=quantity,
                                           best_quality=best_quality, first_at=self._clock.now()))
            else:
                row.quantity, row.best_quality = quantity, best_quality
            after = f"{quantity}（{QUALITY_NAMES[best_quality]}）" if quantity else "未取得"
            self._adjust(session, farm, actor, "set_codex_entry", f"圖鑑 {names[item_code]} → {after}",
                         {"item_code": item_code, "before": list(before), "after": [quantity, best_quality]})
            return {"item_code": item_code, "changed": True}

    def reset_weekly_purchases(self, actor: str, player_id: int) -> dict:
        """清除玩家本週的每週特賣購買量（可以重新買到限購數量）。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            start = special_shop.week_start(self._clock.now())
            bought = self._weekly_bought(session, farm.id, start)
            if not bought:
                return {"cleared": 0}
            session.execute(delete(WeeklyShopPurchase).where(WeeklyShopPurchase.farm_id == farm.id,
                                                             WeeklyShopPurchase.week_start == start))
            self._adjust(session, farm, actor, "reset_weekly_purchases", "重設本週特賣購買量",
                         {"week_start": start.isoformat(), "before": bought})
            return {"cleared": len(bought)}
