"""釣魚小遊戲：釣具商店、裝備、拋竿、咬鉤反應、拉扯博弈、魚簍、販售、分享、圖鑑與排行榜。

規則與數值在 game/fishing.py；這裡負責驗證與交易：
- 每個會改變資產或釣魚狀態的方法都先鎖定農場資料列（_locked_farm），同一位玩家的操作依序執行。
- 拋竿時在同一筆交易扣魚餌並建立 Session（每座農場最多一筆進行中的 Session，部分唯一索引），不會重複扣餌。
- 拉扯操作帶入畫面上的回合數（turn），重複點擊或過期畫面送出的操作不會被執行兩次。
- 釣起時在同一筆交易把 Session 改為 landed 並新增魚（farm_fish.session_id 唯一），不會重複發魚。
- 販售以「鎖定 + sold_at IS NULL」標記，金幣經 _record 寫入交易紀錄（fish_sell），不會重複發錢。
- 分享只寫分享紀錄，不轉移、不消耗魚；只能分享自己的魚（以農場比對，不接受他人的魚 ID）。
- 時間到期（沒有及時拉竿、拉扯操作逾時）在下一次讀取或操作時判定，不靠排程；Bot 啟動時另外清理殘留的 Session。
"""

import random
from collections.abc import Callable
from datetime import timedelta

from app.db.models import FarmFish, FishingSession
from app.game import fishing
from app.game.errors import (
    AlreadyExistsError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
)
from app.game.rules import EventType
from app.repositories import fishing as fishing_repo
from app.repositories import groups as groups_repo
from app.repositories import players as players_repo
from app.services.dto import ActionResult
from app.services.exploration_service import ExplorationService


def gear_view(g, owned=None, level: int | None = None) -> dict:
    return {
        "code": g.code, "kind": g.kind, "kind_name": fishing.GEAR_KIND_NAMES[g.kind], "name": g.name,
        "emoji": g.emoji, "description": g.description, "price": g.price, "required_level": g.required_level,
        "stock": g.stock, "effects": dict(g.effects), "effects_text": fishing.gear_effects_text(g.kind, g.effects),
        "sort_order": g.sort_order, "active": g.active,
        "owned": owned is not None and (g.kind != "bait" or owned.quantity > 0),
        "quantity": owned.quantity if owned is not None else 0,
        "equipped": bool(owned is not None and owned.equipped),
        "unlocked": level is None or level >= g.required_level,
    }


def fish_type_view(f) -> dict:
    return {
        "id": f.id, "code": f.code, "name": f.name, "emoji": f.emoji, "rarity": f.rarity,
        "rarity_name": fishing.RARITY_NAMES[f.rarity], "rarity_emoji": fishing.RARITY_EMOJI[f.rarity],
        "min_price": f.min_price, "max_price": f.max_price, "min_weight_g": f.min_weight_g,
        "max_weight_g": f.max_weight_g, "difficulty": f.difficulty, "description": f.description,
        "sort_order": f.sort_order, "active": f.active,
    }


def fish_view(fish: FarmFish, ftype) -> dict:
    return {
        "id": fish.id, "code": fish.fish_code, "name": ftype.name, "emoji": ftype.emoji, "rarity": fish.rarity,
        "rarity_name": fishing.RARITY_NAMES[fish.rarity], "rarity_emoji": fishing.RARITY_EMOJI[fish.rarity],
        "weight_g": fish.weight_g, "weight_text": fishing.weight_text(fish.weight_g), "price": fish.price,
        "caught_at": fish.caught_at, "sold_at": fish.sold_at,
    }


class FishingService(ExplorationService):
    def __init__(self, *args, fishing_rng: Callable[[], float] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # 釣魚的魚種、重量、脫鉤與魚的動作判定（回傳 [0, 1)）；測試可注入固定序列
        self._fishing_rng = fishing_rng or random.SystemRandom().random

    # ---------- 內部工具 ----------

    def _context_group(self, session, farm) -> int | None:
        ctx = self._context_group_id
        if ctx is not None and not groups_repo.is_member(session, ctx, farm.player_id):
            return None
        return ctx

    @staticmethod
    def _finish(row: FishingSession, status: str, at) -> None:
        row.status = status
        row.ended_at = at
        row.action_deadline = None

    def _expire(self, row: FishingSession | None, now) -> FishingSession | None:
        """依時間判定進行中的 Session 是否已失敗（沒有及時拉竿、操作逾時、超過最長時間）。回傳仍進行中的 Session。"""
        if row is None or row.status not in fishing.ACTIVE_STATUSES:
            return row
        grace = timedelta(seconds=fishing.REACTION_GRACE_SECONDS)
        if now > row.started_at + timedelta(seconds=fishing.SESSION_MAX_SECONDS):
            self._finish(row, "abandoned", now)
        elif row.status == "casting" and now > row.react_deadline + grace:
            state = dict(row.state)
            state["last"] = {"text": "🐟 你沒有及時拉竿，魚把餌吃掉跑了…"}
            row.state = state
            self._finish(row, "missed", row.react_deadline)
        elif row.status == "fighting" and row.action_deadline is not None and now > row.action_deadline + grace:
            state = dict(row.state)
            state["last"] = {"text": "⌛ 太久沒有操作，魚趁機掙脫逃走了…"}
            row.state = state
            self._finish(row, "timeout", row.action_deadline)
        return row if row.status in fishing.ACTIVE_STATUSES else None

    def _session_view(self, session, row: FishingSession, now, farm=None) -> dict:
        state = row.state or {}
        if row.status == "casting":
            phase = "bite" if now >= row.bite_at else "waiting"
        elif row.status == "fighting":
            phase = "fighting"
        else:
            phase = "ended"
        rod, line, bait = (row.gear.get(k) or {} for k in ("rod", "line", "bait"))
        view = {
            "id": row.id, "status": row.status, "status_name": fishing.STATUS_NAMES.get(row.status, row.status),
            "phase": phase, "turn": state.get("turn", 0), "started_at": row.started_at, "bite_at": row.bite_at,
            "react_deadline": row.react_deadline, "action_deadline": row.action_deadline, "ended_at": row.ended_at,
            "rarity": row.rarity, "rarity_name": fishing.RARITY_NAMES[row.rarity],
            "rarity_emoji": fishing.RARITY_EMOJI[row.rarity],
            "rod": rod.get("name"), "line": line.get("name"), "bait": bait.get("name"),
            "stamina": state.get("stamina", 0), "max_stamina": state.get("max_stamina", 0),
            "distance": state.get("distance", 0), "escape_distance": fishing.ESCAPE_DISTANCE,
            "tension": state.get("tension", 0), "max_tension": state.get("max_tension", fishing.BASE_MAX_TENSION),
            "intent": state.get("intent"), "intent_text": fishing.INTENT_TEXT.get(state.get("intent"), ""),
            "last_text": (state.get("last") or {}).get("text"), "fish": None, "record": None,
        }
        if row.status == "landed":
            got = fishing_repo.get_farm_fish(session, row.farm_id, fishing_repo.session_fish(session, row.id).id)
            fish, ftype = got
            view["fish"] = fish_view(fish, ftype)
            view["record"] = self._record_flags(session, row.farm_id, fish)
        return view

    @staticmethod
    def _record_flags(session, farm_id: int, fish: FarmFish) -> dict:
        best = fishing_repo.best_weights(session, farm_id)
        return {
            "species": best.get(fish.fish_code) == fish.weight_g,
            "overall": max(best.values(), default=0) == fish.weight_g,
        }

    def _gear_rows(self, session, farm):
        return fishing_repo.equipped_gear(session, farm.id)

    # ---------- 釣魚中心 ----------

    def fishing_overview(self, player_id: int) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            player = players_repo.get(session, farm.player_id)
            version, settings = fishing_repo.current_settings(session)
            equipped = self._gear_rows(session, farm)
            active = fishing_repo.active_session(session, farm.id)
            if active is not None:
                expired = now > active.react_deadline if active.status == "casting" else (
                    active.action_deadline is not None and now > active.action_deadline)
            else:
                expired = False
            bait = equipped.get("bait")
            return {
                "player_name": player.display_name, "level": farm.level, "gold": farm.gold,
                "rod": gear_view(equipped["rod"][1], equipped["rod"][0]) if "rod" in equipped else None,
                "line": gear_view(equipped["line"][1], equipped["line"][0]) if "line" in equipped else None,
                "bait": gear_view(bait[1], bait[0]) if bait else None,
                "bait_quantity": bait[0].quantity if bait else 0,
                "basket": fishing_repo.basket_count(session, farm.id),
                "basket_capacity": settings["basket_capacity"],
                "active": active is not None and not expired, "stats": fishing_repo.player_session_stats(session,
                                                                                                         farm.id),
                "settings_version": version, "base_success_pct": settings["base_success_pct"],
                "chances": fishing.rarity_chances(settings["rarity_bp"], dict(bait[1].effects) if bait else {}),
            }

    # ---------- 釣具商店與裝備 ----------

    def fishing_shop(self, player_id: int) -> dict:
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            owned = {o.gear_code: o for o in fishing_repo.owned_gear(session, farm.id)}
            items = [gear_view(g, owned.get(g.code), farm.level) for g in fishing_repo.list_gear(session)
                     if g.active or g.code in owned]
            return {"gold": farm.gold, "level": farm.level, "items": items}

    def buy_fishing_gear(self, player_id: int, code: str, quantity: int = 1) -> ActionResult:
        quantity = self._valid_int(quantity, "購買數量", 1, fishing.BAIT_MAX_BUY)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            gear = fishing_repo.get_gear(session, code, for_update=True)  # 鎖定商品列，庫存不會被超賣
            if gear is None or not gear.active:
                raise NotFoundError("釣具商店沒有這個商品。")
            if farm.level < gear.required_level:
                raise ValidationError(
                    f"{gear.name}需要農場 Lv.{gear.required_level} 才能購買（目前 Lv.{farm.level}）。"
                )
            owned = fishing_repo.get_owned(session, farm.id, gear.code, for_update=True)
            if gear.kind != "bait":
                if quantity != 1:
                    raise ValidationError(f"{gear.name}一次只能買 1 個。")
                if owned is not None:
                    raise AlreadyExistsError(f"你已經有{gear.name}了。")
            elif (owned.quantity if owned else 0) + quantity > fishing.BAIT_MAX_HOLD:
                raise ValidationError(f"魚餌最多持有 {fishing.BAIT_MAX_HOLD} 個。")
            if gear.stock is not None and gear.stock < quantity:
                raise InsufficientItemsError(f"{gear.name}庫存不足（剩 {gear.stock} 個）。")
            cost = gear.price * quantity
            if farm.gold < cost:
                raise InsufficientGoldError(f"金幣不足：需要 {cost}，目前只有 {farm.gold}。")
            farm.gold -= cost
            if gear.stock is not None:
                gear.stock -= quantity
            now = self._clock.now()
            equipped = fishing_repo.equipped_gear(session, farm.id)
            auto_equip = gear.kind not in equipped
            if owned is None:
                fishing_repo.add_owned(session, farm.id, gear, quantity, now, equipped=auto_equip)
            else:
                owned.quantity += quantity
                if auto_equip:
                    owned.equipped = True
            self._record(
                session, farm, EventType.FISHING_BUY, f"購買{gear.name} ×{quantity}，花費 {cost} 金幣",
                item_code=gear.code, quantity=quantity, gold_delta=-cost, unit_price=gear.price,
            )
            note = "，已自動裝備" if auto_equip else ""
            return ActionResult(f"{gear.emoji} 買了 {gear.name} ×{quantity}，花費 {cost} 金幣{note}。", farm.gold,
                                -cost, gear.code, quantity)

    def equip_fishing_gear(self, player_id: int, code: str) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            owned = fishing_repo.get_owned(session, farm.id, code, for_update=True)
            gear = fishing_repo.get_gear(session, code)
            if owned is None or gear is None:
                raise NotFoundError("你沒有這個釣具，請先到釣具商店購買。")
            if gear.kind == "bait" and owned.quantity <= 0:
                raise InsufficientItemsError(f"{gear.name}已經用完了，請先購買。")
            if owned.equipped:
                return ActionResult(f"{gear.name}已經裝備中。", farm.gold)
            fishing_repo.unequip_kind(session, farm.id, gear.kind)
            owned.equipped = True
            return ActionResult(f"{gear.emoji} 已裝備{fishing.GEAR_KIND_NAMES[gear.kind]}：{gear.name}。", farm.gold)

    # ---------- 釣魚 ----------

    def fishing_state(self, player_id: int) -> dict | None:
        """目前進行中的釣魚，或最近一次釣魚的結果；先依時間判定逾時。沒有任何紀錄時回傳 None。"""
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            active = fishing_repo.active_session(session, farm.id)
            if active is not None:
                if self._needs_expire(active, now):
                    farm = self._locked_farm(session, player_id)
                    active = fishing_repo.active_session(session, farm.id, for_update=True)
                    self._expire(active, now)
                return self._session_view(session, active, now)
            last = fishing_repo.last_session(session, farm.id)
            return self._session_view(session, last, now) if last else None

    @staticmethod
    def _needs_expire(row: FishingSession, now) -> bool:
        grace = timedelta(seconds=fishing.REACTION_GRACE_SECONDS)
        if now > row.started_at + timedelta(seconds=fishing.SESSION_MAX_SECONDS):
            return True
        if row.status == "casting":
            return now > row.react_deadline + grace
        return row.action_deadline is not None and now > row.action_deadline + grace

    def start_fishing(self, player_id: int) -> dict:
        """拋竿：檢查裝備、魚餌與魚簍空間，扣 1 個魚餌，決定魚種與上鉤時間。"""
        now = self._clock.now()
        rng = self._fishing_rng
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            active = self._expire(fishing_repo.active_session(session, farm.id, for_update=True), now)
            if active is not None:
                raise ValidationError("你已經在釣魚了，請先完成這一次。")
            version, settings = fishing_repo.current_settings(session)
            equipped = fishing_repo.equipped_gear(session, farm.id)
            missing = [fishing.GEAR_KIND_NAMES[k] for k in fishing.GEAR_KINDS if k not in equipped]
            if missing:
                raise ValidationError(f"還沒有裝備{'、'.join(missing)}，請先到「🛒 釣具商店」購買並裝備。")
            bait_row, bait = equipped["bait"]
            if bait_row.quantity <= 0:
                raise InsufficientItemsError(f"{bait.name}用完了，請到釣具商店補充魚餌。")
            capacity = settings["basket_capacity"]
            if fishing_repo.basket_count(session, farm.id) >= capacity:
                raise ValidationError(f"魚簍已滿（{capacity} 隻），請先賣掉一些魚再來釣。")
            fish_types = fishing_repo.list_fish_types(session, active_only=True)
            pools: dict[str, list] = {}
            for f in fish_types:
                pools.setdefault(f.rarity, []).append(f)
            rarity = fishing.roll_rarity(settings["rarity_bp"], dict(bait.effects), rng)
            ftype = fishing.pick_fish(pools, rarity, rng)
            if ftype is None:
                raise ValidationError("目前沒有可以釣的魚，請稍後再試。")
            weight = fishing.roll_weight(ftype.min_weight_g, ftype.max_weight_g, rng)
            price = fishing.fish_price(ftype.min_price, ftype.max_price, ftype.min_weight_g, ftype.max_weight_g,
                                       weight)
            rod, line = dict(equipped["rod"][1].effects), dict(equipped["line"][1].effects)
            state = fishing.new_fight(settings, ftype.rarity, ftype.difficulty, line, rng)
            bite_at = now + timedelta(seconds=fishing.bite_wait_seconds(settings, dict(bait.effects), rng))
            react_deadline = bite_at + timedelta(seconds=fishing.reaction_seconds(settings, rod))
            bait_row.quantity -= 1  # 扣魚餌與建立 Session 在同一筆交易
            gear = {k: {"code": g.code, "name": g.name, "effects": dict(g.effects)} for k, (_o, g) in equipped.items()}
            row = FishingSession(
                farm_id=farm.id, status="casting", settings_version=version, settings=settings, gear=gear,
                fish_code=ftype.code, rarity=ftype.rarity, weight_g=weight, price=price, state=state, turns=0,
                started_at=now, bite_at=bite_at, react_deadline=react_deadline,
                context_group_id=self._context_group(session, farm),
            )
            session.add(row)
            session.flush()  # 部分唯一索引：並行拋竿時第二筆在這裡失敗並回滾（魚餌也不會被扣）
            return self._session_view(session, row, now)

    def _active_for_action(self, session, farm, session_id: int, now) -> FishingSession | None:
        row = fishing_repo.active_session(session, farm.id, for_update=True)
        if row is None or row.id != session_id:
            last = fishing_repo.last_session(session, farm.id)
            if last is not None and last.id == session_id:
                return None  # 這次釣魚已經結束（例如重複點擊）：回傳最新狀態
            raise NotFoundError("找不到這次釣魚，請重新開始。")
        return row

    def hook_fish(self, player_id: int, session_id: int) -> dict:
        """拉竿：魚上鉤後、反應時間內拉竿才會進入拉扯；太早拉竿不會有任何效果。"""
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            row = self._active_for_action(session, farm, session_id, now)
            if row is not None and self._expire(row, now) is not None:
                if row.status != "casting":
                    pass  # 已經在拉扯中（重複點擊拉竿）
                elif now < row.bite_at:
                    raise ValidationError("魚還沒上鉤，再等一下！")
                else:
                    row.status = "fighting"
                    row.action_deadline = now + timedelta(seconds=row.settings["action_timeout_seconds"])
                    state = dict(row.state)
                    state["last"] = {"text": "‼️ 拉竿成功，魚上鉤了！開始拉扯！"}
                    row.state = state
            target = row or fishing_repo.last_session(session, farm.id)
            return self._session_view(session, target, now)

    def fishing_action(self, player_id: int, session_id: int, action: str, turn: int) -> dict:
        """拉扯操作（收線／放線／等待）。turn 必須等於畫面上的回合數，避免重複點擊或過期畫面重複執行。"""
        if action not in fishing.ACTIONS:
            raise ValidationError("沒有這個操作。")
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            row = self._active_for_action(session, farm, session_id, now)
            view_extra = {}
            if row is not None and self._expire(row, now) is not None:
                if row.status != "fighting":
                    raise ValidationError("請先拉竿讓魚上鉤。")
                if row.state.get("turn", 0) != turn:
                    view_extra["stale"] = True  # 畫面已經更新過（重複點擊），不重複執行
                else:
                    diff = row.settings["difficulty"][row.rarity]
                    rod = (row.gear.get("rod") or {}).get("effects", {})
                    state, result = fishing.step(row.state, action, diff, rod, self._fishing_rng)
                    row.state = state
                    row.turns = state["turn"]
                    if result is None:
                        row.action_deadline = now + timedelta(seconds=row.settings["action_timeout_seconds"])
                    else:
                        self._finish(row, result, now)
                        if result == "landed":
                            session.add(FarmFish(farm_id=farm.id, session_id=row.id, fish_code=row.fish_code,
                                                 rarity=row.rarity, weight_g=row.weight_g, price=row.price,
                                                 caught_at=now))
                            session.flush()  # session_id 唯一：同一次釣魚不會發兩隻魚
            target = row or fishing_repo.last_session(session, farm.id)
            return {**self._session_view(session, target, now), **view_extra}

    def give_up_fishing(self, player_id: int, session_id: int) -> dict:
        """收竿放棄（已扣的魚餌不退還）。"""
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            row = self._active_for_action(session, farm, session_id, now)
            if row is not None and self._expire(row, now) is not None:
                state = dict(row.state)
                state["last"] = {"text": "🎣 你收起魚竿，這次先不釣了。"}
                row.state = state
                self._finish(row, "abandoned", now)
            target = row or fishing_repo.last_session(session, farm.id)
            return self._session_view(session, target, now)

    def cleanup_fishing_sessions(self) -> int:
        """把已超過期限仍標記為進行中的 Session 判定為失敗（Bot 啟動時呼叫）；回傳處理筆數。"""
        now = self._clock.now()
        grace = timedelta(seconds=fishing.REACTION_GRACE_SECONDS)
        with self._transaction() as session:
            rows = fishing_repo.stale_active_sessions(session, now - grace)
            for row in rows:
                self._expire(row, now)
            return len(rows)

    # ---------- 魚簍與販售 ----------

    def fish_basket(self, player_id: int) -> dict:
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            _version, settings = fishing_repo.current_settings(session)
            best = fishing_repo.best_weights(session, farm.id)
            fish = []
            for f, t in fishing_repo.basket(session, farm.id):
                v = fish_view(f, t)
                v["record"] = best.get(f.fish_code) == f.weight_g
                fish.append(v)
            return {"fish": fish, "capacity": settings["basket_capacity"], "gold": farm.gold,
                    "total_value": sum(f["price"] for f in fish)}

    def sell_fish(self, player_id: int, fish_ids: list[int] | None) -> ActionResult:
        """販售指定的魚（fish_ids 為 None 表示全部）。只能賣自己魚簍中尚未賣出的魚。"""
        if fish_ids is not None:
            if not isinstance(fish_ids, list) or not fish_ids:
                raise ValidationError("請選擇要賣的魚。")
            if len(fish_ids) > fishing.SELL_MAX_PER_ACTION:
                raise ValidationError(f"一次最多賣 {fishing.SELL_MAX_PER_ACTION} 隻魚。")
            if any(not isinstance(i, int) or isinstance(i, bool) or i <= 0 for i in fish_ids):
                raise ValidationError("魚的編號不正確。")
            fish_ids = sorted(set(fish_ids))
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            rows = fishing_repo.lock_unsold(session, farm.id, fish_ids)
            if not rows:
                raise ValidationError("魚簍裡沒有可以賣的魚。")
            if fish_ids is not None and len(rows) != len(fish_ids):
                raise ValidationError("有些魚已經賣掉或不在你的魚簍裡，請重新整理後再試。")
            now = self._clock.now()
            total = 0
            for f in rows:
                f.sold_at, f.sold_price = now, f.price
                total += f.price
            farm.gold += total
            codes = {f.fish_code for f in rows}
            names = {t.code: t.name for t in fishing_repo.list_fish_types(session) if t.code in codes}
            detail = "、".join(sorted({names.get(f.fish_code, f.fish_code) for f in rows}))
            self._record(
                session, farm, EventType.FISH_SELL, f"販售 {len(rows)} 隻魚（{detail}），獲得 {total} 金幣"[:200],
                item_code=next(iter(codes)) if len(codes) == 1 else None, quantity=len(rows), gold_delta=total,
                unit_price=rows[0].price if len(rows) == 1 else None,
            )
            return ActionResult(f"💰 賣出 {len(rows)} 隻魚，獲得 {total} 金幣。", farm.gold, total, None, len(rows))

    # ---------- 分享與展示 ----------

    def share_fish(self, player_id: int, fish_id: int) -> dict:
        """記錄分享並回傳展示用資料。分享不消耗、不轉移魚，也沒有獎勵；只能分享自己魚簍中的魚。"""
        if not isinstance(fish_id, int) or isinstance(fish_id, bool) or fish_id <= 0:
            raise ValidationError("魚的編號不正確。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            group_id = self._context_group(session, farm)
            if group_id is None:
                raise ValidationError("分享魚類需要在你所在的 Discord 伺服器中操作。")
            got = fishing_repo.get_farm_fish(session, farm.id, fish_id)
            if got is None or got[0].sold_at is not None:
                raise NotFoundError("找不到這隻魚（可能已經賣掉了）。")
            fish, ftype = got
            now = self._clock.now()
            last = fishing_repo.last_share_at(session, fish.id)
            if last is not None and (now - last).total_seconds() < fishing.SHARE_COOLDOWN_SECONDS:
                raise ValidationError("剛剛已經分享過這隻魚了，稍等一下再分享。")
            fishing_repo.add_share(session, fish.id, farm.id, group_id, now)
            player = players_repo.get(session, farm.player_id)
            return {**fish_view(fish, ftype), "player_name": player.display_name,
                    "record": self._record_flags(session, farm.id, fish)}

    def best_fish(self, player_id: int) -> dict | None:
        """魚簍中最珍貴的魚（價值最高，同價取最重）。"""
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            rows = fishing_repo.basket(session, farm.id)
            if not rows:
                return None
            f, t = rows[0]
            return fish_view(f, t)

    # ---------- 圖鑑與排行榜 ----------

    def fish_codex(self, player_id: int) -> dict:
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            caught = fishing_repo.codex(session, farm.id)
            entries = []
            for t in fishing_repo.list_fish_types(session):
                c = caught.get(t.code)
                if not t.active and c is None:
                    continue
                entries.append({**fish_type_view(t), "caught": c is not None, "count": c["count"] if c else 0,
                                "max_weight_g": c["max_weight_g"] if c else None})
            return {"entries": entries, "discovered": sum(1 for e in entries if e["caught"]), "total": len(entries)}

    def fishing_leaderboard(self, group_id: int | None) -> dict:
        with self._transaction() as session:
            return fishing_repo.leaderboard(session, group_id)

    # ---------- 共用 ----------

    @staticmethod
    def _valid_int(value, label: str, low: int, high: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValidationError(f"{label}必須是 {low}–{high} 的整數。")
        return value
