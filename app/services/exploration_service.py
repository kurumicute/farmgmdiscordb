"""山洞冒險：區域探索、機器製作、假幣兌換、種子製造機、稀有作物培育器與自動收割／加工。

探險：出發時決定獎勵，探險時間結束後領取才放進背包，一次只能去一個區域。
規則與數值在 game/exploration.py；這裡只負責驗證與交易。所有會改變資產的操作都先鎖定農場資料列，
等級、進行中的探險、素材與倉庫空間都在同一筆交易中檢查，重複點擊或並行請求不會重複發獎或重複扣料。
"""

import logging
from datetime import timedelta

from sqlalchemy import select

from app.db.models import Farm
from app.game import balance, exploration, genetics, ruins, rules
from app.game.errors import (
    AlreadyExistsError,
    GameError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import CraftState, EventType, PlotState
from app.repositories import catalog as catalog_repo
from app.repositories import codex as codex_repo
from app.repositories import exploration as exploration_repo
from app.repositories import farms as farms_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.repositories import items as items_repo
from app.repositories import players as players_repo
from app.repositories import ranch as ranch_repo
from app.repositories import ruins as ruins_repo
from app.repositories import titles as titles_repo
from app.services.dto import ActionResult
from app.services.game_service import harvest_highlight
from app.services.ranch_service import RanchService

INCUBATOR = "rare_incubator"



logger = logging.getLogger(__name__)

def _names(session, codes) -> dict[str, str]:
    names = catalog_repo.item_names(session)
    return {c: names.get(c, exploration.material_name(c)) for c in codes}


def _join(items: dict[str, int], names: dict[str, str]) -> str:
    return "、".join(f"{names.get(code, code)} ×{qty}" for code, qty in items.items())


def find_machine(key: str) -> str | None:
    key = (key or "").strip()
    for code, machine in exploration.MACHINES.items():
        if key.lower() == code or key == machine.name:
            return code
    return None


def find_incubator_recipe(key: str) -> str | None:
    key = (key or "").strip()
    for code, (name, *_rest, aliases) in exploration.INCUBATOR_RECIPES.items():
        if key.lower() == code or key == name or key in aliases:
            return code
    return None


class ExplorationService(RanchService):
    # ---------- 冒險總覽與探險 ----------

    def _trip_view(self, log, now) -> dict | None:
        if log is None:
            return None
        area = exploration.AREAS[log.area_code]
        remaining = rules.remaining_seconds(log.next_available_at, now)
        return {"area": log.area_code, "area_name": area.name, "emoji": area.emoji, "started_at": log.explored_at,
                "ends_at": log.next_available_at, "remaining_seconds": remaining, "finished": remaining == 0}

    def exploration_overview(self, player_id: int) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            player = players_repo.get(session, farm.player_id)
            counts = exploration_repo.list_cooldowns(session, farm.id)
            trip = self._trip_view(exploration_repo.get_active(session, farm.id), now)
            codes = {d.item for a in exploration.AREAS.values() for pool in a.pools.values() for d in pool}
            names = _names(session, codes | {exploration.FAKE_COIN})
            self._settle_guardian(session, farm.home_group_id)
            ruins_open = ruins_repo.is_completed(session, farm.home_group_id)
            areas = []
            for code in exploration.AREA_ORDER:
                area = exploration.AREAS[code]
                row = counts.get(code)
                unlocked = farm.level >= area.unlock_level and (ruins_open or not area.needs_ruins)

                def pool(kind, area=area):
                    return list(dict.fromkeys(names[d.item] for d in area.pools.get(kind, ())))

                areas.append({
                    "code": code, "emoji": area.emoji, "name": area.name, "description": area.description,
                    "unlock_level": area.unlock_level, "unlocked": unlocked,
                    "needs_ruins": area.needs_ruins, "ruins_open": ruins_open,
                    "duration_seconds": area.duration_seconds,
                    # 一次只能進行一趟探險：有進行中或待領取的探險時，三個一般區域都不能出發；
                    # 古代遺跡是額外的探索，只看自己的冷卻
                    "ready": unlocked and (self._ruins_ready(row, now) if area.needs_ruins else trip is None),
                    "exploring": trip is not None and trip["area"] == code,
                    "next_at": row.next_available_at if area.needs_ruins and row else None,
                    "advanced": exploration.is_advanced(area, farm.level),
                    "advanced_level": area.advanced_level, "rolls": area.rolls,
                    "explore_count": row.explore_count if row else 0,
                    "common": pool("common"), "rare": pool("rare"), "seeds": pool("seed"),
                    "events": [(e.name, e.min_level) for e in area.events], "level_notes": list(area.level_notes),
                })
            return {"player_name": player.display_name, "level": farm.level, "areas": areas, "trip": trip,
                    "now": now}

    @staticmethod
    def _ruins_ready(row, now) -> bool:
        return row is None or now >= row.next_available_at

    def explore(self, player_id: int, area_key: str) -> dict:
        """出發探險：獎勵在出發時決定並記錄，探險時間結束後用 claim_exploration 領取。一次只能去一個區域。
        古代遺跡另外處理（explore_ruins）：不佔名額、立即拿到獎勵，回傳的是領取結果。"""
        code = exploration.find_area(area_key)
        if code is None:
            names = "、".join(a.name for a in exploration.AREAS.values())
            raise NotFoundError(f"沒有「{area_key}」這個探索區域。可探索區域：{names}")
        area = exploration.AREAS[code]
        if area.needs_ruins:
            return self.explore_ruins(player_id)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if farm.level < area.unlock_level:
                raise ValidationError(f"{area.name}需要農場 Lv.{area.unlock_level} 才能探索（目前 Lv.{farm.level}）。")
            now = self._clock.now()
            active = exploration_repo.get_active(session, farm.id, for_update=True)
            if active is not None:
                busy = exploration.AREAS[active.area_code].name
                if now >= active.next_available_at:
                    raise ValidationError(f"你從{busy}探險回來了，請先領取上一次的探險獎勵再出發。")
                left = rules.format_duration(rules.remaining_seconds(active.next_available_at, now))
                raise ValidationError(f"你正在{busy}探險，還需要 {left}才會回來；一次只能去一個區域。")
            result = exploration.explore(code, farm.level, self._explore_rng)
            until = now + timedelta(seconds=area.duration_seconds)
            row = exploration_repo.get_cooldown(session, farm.id, code, for_update=True)
            exploration_repo.set_cooldown(session, row, farm.id, code, now, until)
            # (farm_id) WHERE claimed_at IS NULL 唯一：並行出發時第二筆在這裡失敗並回滾
            log = exploration_repo.add_log(
                session, farm_id=farm.id, area_code=code, farm_level=farm.level, advanced=result.advanced,
                rewards=dict(result.items), events=list(result.events), explored_at=now, next_available_at=until,
            )
            return {"log_id": log.id, "area": code, "area_name": area.name, "emoji": area.emoji,
                    "advanced": result.advanced, "level": farm.level, "started_at": now, "ends_at": until,
                    "duration_seconds": area.duration_seconds}

    def explore_ruins(self, player_id: int) -> dict:
        """探索古代遺跡（所屬群組修好後的額外探索）：不受一次一趟限制，立即拿到獎勵，冷卻 1 天。
        掉落用古老森林模板再多抽 1–2 份；本週守護者已被打倒時再多抽 DEFEAT_BONUS_ROLLS 份，並對守護者造成傷害。"""
        area = exploration.AREAS["ruins"]
        with self._transaction() as session:
            found = session.execute(select(Farm.home_group_id).where(Farm.player_id == player_id)).first()
            self._settle_guardian(session, found[0] if found else None)  # 會鎖遺跡列，必須在鎖農場列之前
            farm = self._locked_farm(session, player_id)
            if not ruins_repo.is_completed(session, farm.home_group_id):
                raise ValidationError(f"{area.name}還沒修好：要所屬群組的成員一起捐素材修復完成後才能進入"
                                      "（「⛰️ 山洞」→「🏛️ 修復遺跡」）。")
            if farm.level < area.unlock_level:
                raise ValidationError(f"{area.name}需要農場 Lv.{area.unlock_level} 才能探索（目前 Lv.{farm.level}）。")
            now = self._clock.now()
            row = exploration_repo.get_cooldown(session, farm.id, "ruins", for_update=True)
            if not self._ruins_ready(row, now):
                left = rules.format_duration(rules.remaining_seconds(row.next_available_at, now))
                raise ValidationError(f"今天已經探索過{area.name}了，{left}後才能再探索。")
            g = ruins_repo.get_guardian(session, farm.home_group_id, ruins.week_start(now))
            bonus = ruins.DEFEAT_BONUS_ROLLS if g is not None and g.defeated_at is not None else 0
            low, high = exploration.RUINS_EXTRA_ROLLS
            extra = low + min(high - low, int(self._explore_rng() * (high - low + 1)))
            result = exploration.explore("ruins", farm.level, self._explore_rng, extra_rolls=extra + bonus)
            until = now + timedelta(seconds=area.duration_seconds)
            exploration_repo.set_cooldown(session, row, farm.id, "ruins", now, until)
            # 建立時就標記已領取：不會和一般探險「每座農場最多一筆未領取」的唯一索引衝突
            log = exploration_repo.add_log(
                session, farm_id=farm.id, area_code="ruins", farm_level=farm.level, advanced=result.advanced,
                rewards=dict(result.items), events=list(result.events), explored_at=now, next_available_at=until,
                claimed_at=now,
            )
            out = self._grant_log(session, farm, log, now)
            return {**out, "instant": True, "next_at": until, "extra_rolls": extra, "bonus_rolls": bonus}

    def claim_exploration(self, player_id: int) -> dict:
        """領取已回來的探險獎勵：素材、假幣與稀有種子都放進素材背包（不佔倉庫）；放不下時不領取。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            now = self._clock.now()
            log = exploration_repo.get_active(session, farm.id, for_update=True)
            if log is None:
                raise ValidationError("目前沒有可以領取的探險，先到山洞選一個區域出發吧。")
            area = exploration.AREAS[log.area_code]
            if now < log.next_available_at:
                left = rules.format_duration(rules.remaining_seconds(log.next_available_at, now))
                raise ValidationError(f"還在{area.name}探險中，{left}後才會回來。")
            return self._grant_log(session, farm, log, now)

    def _grant_log(self, session, farm, log, now) -> dict:
        """把探險紀錄的獎勵放進背包、寫交易紀錄並回傳結果（呼叫端已鎖農場列）；放不下時整筆不領取。"""
        area = exploration.AREAS[log.area_code]
        items = dict(log.rewards)
        total = sum(items.values())
        materials = sum(q for c, q in items.items() if inventory_repo.is_material(c))
        try:
            self._ensure_space(session, farm, total - materials)  # 一般物品放倉庫（目前掉落表沒有）
            self._ensure_material_space(session, farm, materials)  # 素材、假幣與稀有種子放素材背包
        except WarehouseFullError as exc:
            raise WarehouseFullError(f"{exc} 探險帶回來的物品放不下，清出空間後再領取。") from None
        for item, qty in items.items():
            inventory_repo.add(session, farm.id, item, qty)
            codex_repo.register(session, farm.id, item, qty, None, now)  # 山洞素材圖鑑
        log.claimed_at = now
        names = _names(session, items)
        text = f"探索{area.name}：{_join(items, names) or '一無所獲'}"
        if log.events:
            text += f"（{'、'.join(log.events)}）"
        self._record(session, farm, EventType.EXPLORE, text[:200], quantity=total)
        guardian_note = ""
        if area.needs_ruins and farm.home_group_id is not None:
            hit = self._hit_guardian(session, farm.home_group_id, farm.id, ruins.EXPEDITION_DAMAGE)
            if hit and hit[0]:
                guardian_note = f"⚔️ 在遺跡裡和{ruins.GUARDIAN_NAME}交手，造成 {hit[0]} 點傷害！"
                if hit[1]:
                    guardian_note += f"🎉 {ruins.GUARDIAN_NAME}倒下了！"
        seeds = {exploration.AMBER_SEED, exploration.MOON_SEED}
        return {
            "log_id": log.id, "area": log.area_code, "area_name": area.name, "emoji": area.emoji,
            "advanced": log.advanced, "level": log.farm_level,
            "items": [
                {"code": c, "name": names[c], "quantity": q,
                 "rarity": exploration.MATERIALS[c][1] if c in exploration.MATERIALS else "稀有種子"}
                for c, q in items.items()
            ],
            "events": list(log.events),
            "seeds": {names[c]: q for c, q in items.items() if c in seeds},
            "fake_coins": items.get(exploration.FAKE_COIN, 0),
            "started_at": log.explored_at, "ends_at": log.next_available_at, "guardian_note": guardian_note,
        }

    def exploration_logs(self, player_id: int, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            logs = exploration_repo.list_logs(session, farm.id, limit)
            names = _names(session, {c for log in logs for c in log.rewards})
            return [
                {"area": log.area_code, "area_name": exploration.AREAS[log.area_code].name,
                 "emoji": exploration.AREAS[log.area_code].emoji, "advanced": log.advanced, "level": log.farm_level,
                 "rewards": {names[c]: q for c, q in log.rewards.items()}, "events": list(log.events),
                 "explored_at": log.explored_at, "next_available_at": log.next_available_at,
                 "claimed_at": log.claimed_at}
                for log in logs
            ]

    # ---------- 古代遺跡（群組修復） ----------

    def ruins_status(self, player_id: int) -> dict:
        """所屬群組的遺跡修復進度、目前階段還缺的素材、我的背包數量、捐獻排行與本週守護者。
        讀取前先結算已結束的守護者週（沒打倒時遺跡崩壞）。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            group_id = farm.home_group_id
            self._settle_guardian(session, group_id)
            group = groups_repo.get(session, group_id) if group_id is not None else None
            row = ruins_repo.get(session, group_id) if group_id is not None else None
            stage = row.stage if row else 0
            progress = dict(row.progress) if row else {}
            info = ruins.stage_info(stage)
            codes = set(info.materials) if info else set()
            names = _names(session, codes)
            return {
                "group_id": group_id, "group_name": group.name if group else None,
                "stage": stage, "stages": len(ruins.STAGES), "completed": info is None,
                "completed_at": row.completed_at if row else None,
                "stage_name": info.name if info else None, "stage_description": info.description if info else None,
                "percent": ruins.percent(stage, progress),
                "materials": [
                    {"code": c, "name": names[c], "need": n, "donated": min(progress.get(c, 0), n),
                     "have": inventory_repo.get_quantity(session, farm.id, c)}
                    for c, n in (info.materials.items() if info else ())
                ],
                "mine": ruins_repo.contribution(session, group_id, farm.id) if group_id is not None else 0,
                "top": ruins_repo.top_contributors(session, group_id) if group_id is not None else [],
                "title": ruins.TITLE,
                "stage_names": [st.name for st in ruins.STAGES],
                "collapsed_at": row.collapsed_at if row else None,
                "guardian": self._guardian_view(session, row, farm) if row and row.completed_at else None,
            }

    def _guardian_view(self, session, row, farm) -> dict:
        now = self._clock.now()
        week = ruins.week_start(now)
        active = ruins.guardian_active(row.completed_at, now)
        g = ruins_repo.get_guardian(session, row.group_id, week) if active else None
        hp_max = g.hp_max if g else ruins.guardian_hp(ruins_repo.member_farms(session, row.group_id))
        bag = [(c, inventory_repo.get_quantity(session, farm.id, c)) for c in exploration.MATERIALS
               if ruins.material_damage(c)]
        names = _names(session, [c for c, _q in bag])
        first = ruins.first_guardian_week(row.completed_at)
        return {
            "name": ruins.GUARDIAN_NAME, "active": active,
            "appears_at": ruins.week_end(first - timedelta(days=7)), "ends_at": ruins.week_end(week),
            "hp_max": hp_max, "damage": min(g.damage, hp_max) if g else 0, "defeated": bool(g and g.defeated_at),
            "mine": ruins_repo.my_hits(session, row.group_id, week, farm.id) if active else 0,
            "top": ruins_repo.top_hits(session, row.group_id, week) if active else [],
            "weapons": [{"code": c, "name": names[c], "have": q, "damage": ruins.material_damage(c)}
                        for c, q in bag if q > 0],
            "expedition_damage": ruins.EXPEDITION_DAMAGE, "bonus_rolls": ruins.DEFEAT_BONUS_ROLLS,
        }

    def _settle_guardian(self, session, group_id: int | None) -> None:
        """結算已經結束的守護者週：該週沒打倒（或根本沒人攻擊）時遺跡崩壞回第三階段。
        沒有要結算的週時只讀一列、不加鎖；呼叫端不可已持有農場列的鎖（順序：遺跡列 → 農場列 → 守護者列）。"""
        if group_id is None:
            return
        now = self._clock.now()
        row = ruins_repo.get(session, group_id)
        if row is None or row.completed_at is None or not ruins.weeks_to_settle(row.completed_at,
                                                                                 row.guardian_settled, now):
            return
        row = ruins_repo.get(session, group_id, for_update=True)
        if row.completed_at is None:
            return
        for week in ruins.weeks_to_settle(row.completed_at, row.guardian_settled, now):
            guardian = ruins_repo.get_guardian(session, group_id, week)
            row.guardian_settled = week
            if guardian is None or guardian.defeated_at is None:
                row.stage, row.progress, row.completed_at = ruins.COMPLETED - 1, {}, None
                row.collapsed_at, row.updated_at = ruins.week_end(week), now
                logger.info("群組 #%s 的古代遺跡因 %s 那週的守護者沒被打倒而崩壞", group_id, week)
                break

    def _hit_guardian(self, session, group_id: int, farm_id: int, damage: int) -> tuple[int, bool] | None:
        """對本週守護者造成傷害（呼叫端已鎖農場列）。回傳 (實際傷害, 這次打倒)；守護者還沒出現時 None。"""
        now = self._clock.now()
        row = ruins_repo.get(session, group_id)
        if row is None or not ruins.guardian_active(row.completed_at, now):
            return None
        week = ruins.week_start(now)
        g = ruins_repo.ensure_guardian(session, group_id, week,
                                       ruins.guardian_hp(ruins_repo.member_farms(session, group_id)))
        if g.defeated_at is not None:
            return 0, False
        dealt = min(damage, g.hp_max - g.damage)
        g.damage += dealt
        ruins_repo.add_hit(session, group_id, week, farm_id, dealt)
        if g.damage >= g.hp_max:
            g.defeated_at = now
            return dealt, True
        return dealt, False

    def attack_guardian(self, player_id: int, item_key: str, quantity: int) -> ActionResult:
        """捐素材攻擊所屬群組本週的遺跡守護者；超過剩餘 HP 所需的數量不會扣。"""
        quantity = rules.validate_quantity(quantity)
        if quantity > ruins.ATTACK_MAX:
            raise ValidationError(f"一次最多用 {ruins.ATTACK_MAX} 個素材。")
        code = (item_key or "").strip()
        per = ruins.material_damage(code)
        if per is None:
            raise ValidationError("只能用山洞素材攻擊守護者（假幣不行）。")
        with self._transaction() as session:
            now = self._clock.now()
            found = session.execute(select(Farm.home_group_id).where(Farm.player_id == player_id)).first()
            if found is None:
                self._locked_farm(session, player_id)
            group_id = found[0]
            self._settle_guardian(session, group_id)  # 先結算（鎖遺跡列），再鎖農場列
            row = ruins_repo.get(session, group_id) if group_id is not None else None
            if row is None or row.completed_at is None:
                raise ValidationError("古代遺跡還沒修好（或已經崩壞），現在沒有守護者。")
            if not ruins.guardian_active(row.completed_at, now):
                raise ValidationError("守護者還沒出現：遺跡修好後的下一個週一（台灣時間）才會出現。")
            farm = self._locked_farm(session, player_id)
            if farm.home_group_id != group_id:
                raise ValidationError("農場的所屬群組剛剛變更了，請重新開啟遺跡頁面。")
            week = ruins.week_start(now)
            g = ruins_repo.ensure_guardian(session, group_id, week,
                                           ruins.guardian_hp(ruins_repo.member_farms(session, group_id)))
            if g.defeated_at is not None:
                raise ValidationError(f"本週的{ruins.GUARDIAN_NAME}已經被打倒了，下週一再來！")
            qty = min(quantity, -(-(g.hp_max - g.damage) // per))  # 剩餘 HP 需要幾個（無條件進位）
            name = _names(session, [code])[code]
            if not inventory_repo.remove(session, farm.id, code, qty):
                have = inventory_repo.get_quantity(session, farm.id, code)
                raise InsufficientItemsError(f"{name}不足：想用 {qty} 個，背包只有 {have} 個。")
            dealt, defeated = self._hit_guardian(session, group_id, farm.id, qty * per)
            self._record(session, farm, EventType.RUINS_GUARDIAN,
                         f"用 {name} ×{qty} 攻擊{ruins.GUARDIAN_NAME}，造成 {dealt} 傷害",
                         item_code=code, quantity=qty)
            text = f"⚔️ 用 {name} ×{qty} 攻擊{ruins.GUARDIAN_NAME}，造成 **{dealt}** 點傷害！"
            if qty < quantity:
                text += "（剩餘 HP 只需要這些，其餘留在背包）"
            if defeated:
                text += (f"\n🎉 {ruins.GUARDIAN_NAME}倒下了！本週古代遺跡探索每次多 {ruins.DEFEAT_BONUS_ROLLS} 份獎勵，"
                         "遺跡也安全度過這一週。")
            return ActionResult(text, farm.gold, item_code=code, quantity=qty)

    def donate_ruins(self, player_id: int, item_key: str, quantity: int) -> ActionResult:
        """捐素材修復所屬群組的遺跡；超過目前階段還缺的數量時只捐需要的部分。階段湊齊就進入下一階段，
        三個階段都完成時，所有捐過素材的成員獲得稱號。先鎖遺跡列再鎖農場列（和世界 Boss 相同順序）。"""
        quantity = rules.validate_quantity(quantity)
        if quantity > ruins.DONATE_MAX:
            raise ValidationError(f"一次最多捐 {ruins.DONATE_MAX} 個。")
        code = (item_key or "").strip()
        with self._transaction() as session:
            now = self._clock.now()
            # 只讀欄位、不載入 Farm 物件：之後 FOR UPDATE 載入時才是鎖定後的最新值（避免 identity map 留著舊值）
            found = session.execute(select(Farm.home_group_id).where(Farm.player_id == player_id)).first()
            if found is None:
                self._locked_farm(session, player_id)  # 產生正確的錯誤訊息
            group_id = found[0]
            if group_id is None:
                raise ValidationError("你的農場還沒有所屬群組，無法修復遺跡。")
            self._settle_guardian(session, group_id)  # 遺跡可能剛因守護者沒被打倒而崩壞
            row = ruins_repo.get_or_create(session, group_id, now)
            farm = self._locked_farm(session, player_id)
            if farm.home_group_id != group_id:  # 讀取後到鎖定前剛好轉移了所屬群組
                raise ValidationError("農場的所屬群組剛剛變更了，請重新開啟遺跡頁面。")
            info = ruins.stage_info(row.stage)
            if info is None:
                raise ValidationError("古代遺跡已經修復完成了，直接到山洞出發探險吧！")
            need = ruins.remaining(row.stage, row.progress)
            if code not in info.materials:
                names = _names(session, info.materials)
                raise ValidationError(f"「{info.name}」階段只需要：{'、'.join(names.values())}。")
            if code not in need:
                raise ValidationError(f"{_names(session, [code])[code]}已經夠了，捐其他還缺的素材吧。")
            qty = min(quantity, need[code])
            name = _names(session, [code])[code]
            if not inventory_repo.remove(session, farm.id, code, qty):
                have = inventory_repo.get_quantity(session, farm.id, code)
                raise InsufficientItemsError(f"{name}不足：想捐 {qty} 個，背包只有 {have} 個。")
            progress = dict(row.progress)
            progress[code] = progress.get(code, 0) + qty
            row.progress = progress  # 指定新的 dict，SQLAlchemy 才會偵測到 JSON 變更
            row.updated_at = now
            ruins_repo.add_contribution(session, group_id, farm.id, qty, now)
            self._record(session, farm, EventType.RUINS_DONATE, f"捐 {name} ×{qty} 修復古代遺跡（{info.name}）",
                         item_code=code, quantity=qty)
            text = f"🏛️ 捐出 {name} ×{qty}，幫忙{info.name}。"
            if qty < quantity:
                text += f"（這個階段只差 {qty} 個，其餘留在背包）"
            if ruins.stage_done(row.stage, row.progress):
                row.stage += 1
                row.progress = {}
                if row.stage >= ruins.COMPLETED:
                    row.completed_at = now
                    for farm_id in ruins_repo.contributor_farms(session, group_id):
                        titles_repo.grant(session, farm_id, ruins.TITLE, ruins.TITLE_SOURCE, now)
                    text += ("\n🎉 古代遺跡修復完成！整個群組都能到山洞探索「🏛️ 古代遺跡」，"
                             f"所有出過力的成員獲得稱號「{ruins.TITLE}」。")
                else:
                    text += f"\n✨ 「{info.name}」完成！下一階段：「{ruins.STAGES[row.stage].name}」。"
            return ActionResult(text, farm.gold, item_code=code, quantity=qty)

    # ---------- 假幣 ----------

    def exchange_fake_coins(self, player_id: int, quantity: int) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        name = exploration.MATERIALS[exploration.FAKE_COIN][0]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if not inventory_repo.remove(session, farm.id, exploration.FAKE_COIN, quantity):
                have = inventory_repo.get_quantity(session, farm.id, exploration.FAKE_COIN)
                raise InsufficientItemsError(f"{name}不足：想兌換 {quantity} 個，背包只有 {have} 個。")
            gold = quantity * exploration.FAKE_COIN_VALUE
            farm.gold += gold
            self._record(
                session, farm, EventType.EXCHANGE, f"兌換 {quantity} 個{name}，金幣 +{gold}",
                item_code=exploration.FAKE_COIN, quantity=quantity, gold_delta=gold,
            )
            return ActionResult(f"已兌換 {quantity} 個{name}，獲得 {gold} 金幣。", farm.gold, gold,
                                exploration.FAKE_COIN, quantity)

    # ---------- 機器 ----------

    def machines_status(self, player_id: int) -> dict:
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            tools = {t.tool_code: t for t in items_repo.list_tools(session, farm.id)}
            codes = {c for m in exploration.MACHINES.values() for c in m.materials}
            have = {c: inventory_repo.get_quantity(session, farm.id, c) for c in codes}
            names = _names(session, codes)
            machines = []
            for code in exploration.MACHINE_ORDER:
                m = exploration.MACHINES[code]
                tool = tools.get(code)
                machines.append({
                    "code": code, "emoji": m.emoji, "name": m.name, "description": m.description,
                    "unlock_level": m.unlock_level, "unlocked": farm.level >= m.unlock_level,
                    "owned": tool is not None, "enabled": bool(tool and tool.enabled),
                    "settings": dict(tool.settings or {}) if tool else {},
                    "materials": [(names[c], n, have.get(c, 0)) for c, n in m.materials.items()],
                    "missing": {names[c]: n for c, n in exploration.missing_materials(m, have).items()},
                })
            return {"level": farm.level, "machines": machines}

    def build_machine(self, player_id: int, key: str) -> ActionResult:
        code = find_machine(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這種機器。")
        machine = exploration.MACHINES[code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            # 農場已鎖定，加上 farm_tools 唯一限制，重複點擊或並行請求都不會重複扣料或重複建立
            if items_repo.get_tool(session, farm.id, code) is not None:
                raise AlreadyExistsError(f"你已經有{machine.name}了。")
            if farm.level < machine.unlock_level:
                raise ValidationError(f"{machine.name}需要農場 Lv.{machine.unlock_level}（目前 Lv.{farm.level}）。")
            have = {c: inventory_repo.get_quantity(session, farm.id, c) for c in machine.materials}
            missing = exploration.missing_materials(machine, have)
            names = _names(session, machine.materials)
            if missing:
                raise InsufficientItemsError(f"材料不足，還缺：{_join(missing, names)}。")
            for item, qty in machine.materials.items():
                if not inventory_repo.remove(session, farm.id, item, qty):
                    raise InsufficientItemsError(f"材料不足：{names[item]}。")
            items_repo.add_tool(session, farm.id, code, self._clock.now())
            used = _join(machine.materials, names)
            self._record(session, farm, EventType.BUILD_MACHINE, f"製作{machine.name}，消耗 {used}",
                         item_code=code, quantity=1)
            return ActionResult(f"{machine.emoji} {machine.name}製作完成！消耗 {used}。", farm.gold,
                                item_code=code, quantity=1)

    def _machine(self, session, farm, code: str, *, require_enabled: bool = True):
        machine = exploration.MACHINES[code]
        tool = items_repo.get_tool(session, farm.id, code)
        if tool is None:
            raise ValidationError(f"需要先製作{machine.name}（山洞探索取得素材，在「🔧 機器」製作）。")
        if require_enabled and not tool.enabled:
            raise ValidationError(f"{machine.name}已停用，請先啟用。")
        return tool

    def set_machine_enabled(self, player_id: int, key: str, enabled: bool) -> ActionResult:
        code = find_machine(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這種機器。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            tool = self._machine(session, farm, code, require_enabled=False)
            tool.enabled = enabled
            name = exploration.MACHINES[code].name
            return ActionResult(f"{name}已{'啟用' if enabled else '停用'}。", farm.gold)

    def set_processor_recipe(self, player_id: int, recipe_key: str | None) -> ActionResult:
        """設定自動加工機的加工項目；None 表示不自動開單（仍會自動領取完成的訂單）。"""
        code = None
        if recipe_key:
            code = self.find_recipe(recipe_key)
            if code is None:
                raise NotFoundError(f"沒有「{recipe_key}」這個加工配方。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            tool = self._machine(session, farm, "auto_processor", require_enabled=False)
            tool.settings = {"recipe": code} if code else {}
            label = balance.RECIPES[code][0] if code else "不自動開單"
            return ActionResult(f"自動加工機的加工項目設為：{label}。", farm.gold)

    # ---------- 種子製造機 ----------

    def make_seeds(
        self, player_id: int, crop_key: str, quantity: int, quality: str = "normal", mutation: str = "none"
    ) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        if quantity > exploration.SEED_MAKER_MAX:
            raise ValidationError(f"一次最多使用 {exploration.SEED_MAKER_MAX} 個作物。")
        quality, mutation = genetics.parse_quality(quality), genetics.parse_mutation(mutation)
        if quality not in exploration.SEED_MAKER_QUALITIES or mutation not in exploration.SEED_MAKER_MUTATIONS:
            raise ValidationError("種子製造機只能使用普通品質、沒有突變的作物。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            self._machine(session, farm, "seed_maker")
            crop = self._crop(session, crop_key, require_active=True)
            if not crop.shop_buyable and not exploration.SEED_MAKER_RARE_CROPS:
                raise ValidationError(f"{crop.name}是稀有作物，種子製造機無法製造它的種子。")
            if not inventory_repo.remove(session, farm.id, crop.code, quantity, quality, mutation):
                have = inventory_repo.get_quantity(session, farm.id, crop.code, quality, mutation)
                raise InsufficientItemsError(f"{crop.name}不足：需要 {quantity} 個，背包只有 {have} 個。")
            seeds = quantity * exploration.SEED_MAKER_RATIO
            item = rules.seed_item_code(crop.code)
            # 扣作物後再檢查空間（稀有種子放素材背包）
            self._ensure_item_space(session, farm, item, seeds)
            inventory_repo.add(session, farm.id, item, seeds)
            self._record(
                session, farm, EventType.MAKE_SEED, f"種子製造機：{quantity} 個{crop.name} → {seeds} 顆{crop.name}種子",
                item_code=item, quantity=seeds,
            )
            auto = self._auto_plant(session, farm)
            return ActionResult(f"🌱 用 {quantity} 個{crop.name}做出 {seeds} 顆{crop.name}種子。{auto}", farm.gold,
                                item_code=item, quantity=seeds)

    # ---------- 稀有作物培育器（以加工訂單執行） ----------

    def incubator_recipes(self) -> list[dict]:
        with self._transaction() as session:
            codes = {c for r in exploration.INCUBATOR_RECIPES.values() for c in (*r[1], r[2])}
            names = _names(session, codes)
        return [
            {"code": code, "name": name, "inputs": {names[c]: n for c, n in inputs.items()},
             "output_name": names[output], "output_quantity": out_qty, "seconds": seconds}
            for code, (name, inputs, output, out_qty, seconds, _aliases) in exploration.INCUBATOR_RECIPES.items()
        ]

    def start_incubation(self, player_id: int, key: str, quantity: int = 1) -> ActionResult:
        code = find_incubator_recipe(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這個培育配方。")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= (
            exploration.INCUBATOR_MAX_QUANTITY
        ):
            raise ValidationError(f"培育份數必須是 1–{exploration.INCUBATOR_MAX_QUANTITY}。")
        name, inputs, output, out_qty, seconds, _aliases = exploration.INCUBATOR_RECIPES[code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            self._machine(session, farm, INCUBATOR)
            return self._start_order(session, farm, code, name, INCUBATOR, inputs, output, out_qty, seconds, quantity)

    # ---------- 自動收割機與自動加工機 ----------

    def run_machines(self, player_id: int) -> str:
        """執行已啟用的自動收割機與自動加工機（開啟面板時呼叫）；沒有機器時不鎖定農場、不寫入。回傳結果說明。"""
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                return ""
            tools = {t.tool_code: t for t in items_repo.list_tools(session, farm.id) if t.enabled}
            if "auto_harvester" not in tools and "auto_processor" not in tools:
                return ""
            if not self.in_batch():
                return self._run_machine_tools(session, player_id, tools)
            # 在面板讀取整頁的共用交易中：機器在 SAVEPOINT 裡執行，失敗只回滾機器的部分，頁面照常讀取
            # （與原本「機器是獨立交易、失敗不影響面板」相同；下次開啟或換頁時再試）
            try:
                with session.begin_nested():
                    return self._run_machine_tools(session, player_id, tools)
            except GameError:
                return ""
            except Exception:
                logger.exception("自動機器執行失敗（玩家 #%s）", player_id)
                return ""

    def _run_machine_tools(self, session, player_id: int, tools: dict) -> str:
        farm = self._locked_farm(session, player_id)
        notes = []
        if "auto_harvester" in tools:
            notes.append(self._auto_harvest(session, farm, tools["auto_harvester"]))
        if "auto_processor" in tools:
            notes.append(self._auto_process(session, farm, tools["auto_processor"]))
        return " ".join(n for n in notes if n)

    def _auto_harvest(self, session, farm, tool) -> str:
        """收成成熟的作物；擁有收割機時才成熟的作物，即使已過乾枯時間也照樣收成（收割機會在成熟時處理）。"""
        now = self._clock.now()
        targets = []
        # 一次查詢並鎖定所有土地（原本每格各查一次）
        for p in farms_repo.list_plots(session, farm.id, for_update=True):
            state = rules.plot_state(p.crop_type_id, p.watered_at, p.ready_at, now)
            if state == PlotState.READY or (state == PlotState.WITHERED and p.ready_at >= tool.acquired_at):
                targets.append(p)
        if not targets:
            return ""
        done, total, stopped = 0, 0, ""
        details: list = []
        for plot in targets:
            try:
                _msg, qty, _item = self._harvest_locked(session, farm, plot, plot.crop_type, details)
            except WarehouseFullError:
                stopped = f"倉庫已滿，還有 {len(targets) - done} 格沒收。"
                break
            done += 1
            total += qty
        text = harvest_highlight(details) + f"🚜 自動收割機收成 {done} 格，共 {total} 個作物。" if done else ""
        text += stopped
        if done:
            text += self._auto_plant(session, farm)
        return text.strip()

    def _auto_process(self, session, farm, tool) -> str:
        """領取所有完成的加工訂單，再以背包原料開始設定的加工項目（能做幾份就做幾份，最多單張上限）。"""
        now = self._clock.now()
        notes = []
        collected = 0
        for order, *_rest in ranch_repo.list_orders(session, farm.id, include_collected=False, limit=50):
            if rules.craft_state(order.status, order.ready_at, now) != CraftState.READY:
                continue
            locked = ranch_repo.get_order(session, farm.id, order.id, for_update=True)
            try:
                self._collect_order(session, farm, locked, source="自動加工機：")
            except WarehouseFullError:
                notes.append("倉庫已滿，部分加工品還沒領取。")
                break
            collected += 1
        if collected:
            notes.insert(0, f"🏭 自動加工機領取 {collected} 張訂單。")
        code = (tool.settings or {}).get("recipe")
        if code in balance.RECIPES:
            name, building, inputs, output, out_qty, seconds, _aliases = balance.RECIPES[code]
            if ranch_repo.count_buildings(session, farm.id, building) == 0:
                notes.append(f"自動加工機：需要先建造{balance.BUILDINGS[building][0]}。")
            elif ranch_repo.busy_order(session, farm.id, building, now) is None:
                quantity = min(
                    [balance.CRAFT_MAX_QUANTITY]
                    + [inventory_repo.get_quantity(session, farm.id, item) // per for item, per in inputs.items()]
                )
                if quantity >= 1:
                    self._start_order(session, farm, code, name, building, inputs, output, out_qty, seconds, quantity,
                                      source="自動加工機：")
                    notes.append(f"🏭 自動加工機開始加工 {out_qty * quantity} 份{name}。")
        return " ".join(notes)
