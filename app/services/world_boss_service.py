"""世界 Boss（群組後勤活動）服務：查看活動、捐獻物資、排行榜、領取獎勵，以及管理員建立與控制活動。

- 規則與數值在 app/game/world_boss.py；每場活動的設定與獎勵快照存在 world_boss_events（PostgreSQL），Bot 重啟不影響。
- 群組隔離：玩家操作都以 group_id（由 Discord 互動來源決定）比對活動所屬群組，並確認玩家是群組成員。
- 戰鬥由系統依時間自動推進（_advance）：每次查看、捐獻、領獎或背景結算時，把 HP 從 last_tick_at 推進到現在並寫回資料庫。
  監控網站（唯讀交易）用 event_view 依同一公式即時推算，不寫入。
- 併發：先鎖活動列再鎖農場列（順序固定，避免死結）。捐獻帶 request_id，(event, player, request_id) 唯一，
  重複點擊只會處理一次；領獎以 (event, player) 唯一，不會重複發放。
- 每次捐獻與發獎都經 _record 寫入交易紀錄（boss_donate／boss_reward），金幣獎勵不計入淨賺排行。
"""

import re
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import TAIPEI_TZ
from app.db.models import (
    MUTATION_CODES,
    QUALITY_CODES,
    FarmFish,
    FishType,
    Group,
    Player,
    WorldBossContribution,
    WorldBossEvent,
    WorldBossRewardClaim,
    WorldBossSchedule,
    WorldBossSupply,
)
from app.game import genetics, rules
from app.game import world_boss as wb
from app.game.errors import AlreadyExistsError, InsufficientItemsError, NotFoundError, ValidationError
from app.game.rules import EventType
from app.repositories import catalog as catalog_repo
from app.repositories import community as community_repo
from app.repositories import crops as crops_repo
from app.repositories import farms as farms_repo
from app.repositories import fishing as fishing_repo
from app.repositories import genetics as genetics_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.repositories import titles as titles_repo
from app.services.dto import ActionResult
from app.services.game_service import genetics_label
from app.services.ranking_service import RankingService

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
GLOBAL_LABEL = "🌏 全服"


def scope_label(group: Group | None) -> str:
    return GLOBAL_LABEL if group is None else f"🏠 {group.name}"
CONTRIBUTION_LOG_LIMIT = 200


def item_category(kind: str | None, code: str) -> str | None:
    """背包物品的捐獻類別；種子、道具、假幣等不能捐獻時回傳 None。"""
    if code in wb.NOT_DONATABLE:
        return None
    if kind in (catalog_repo.PRODUCE, catalog_repo.VARIANT):
        return "crop"
    if kind in (catalog_repo.ANIMAL_PRODUCT, catalog_repo.GOODS):
        return "goods"
    if kind == catalog_repo.MATERIAL:
        return "special" if code in wb.SPECIAL_MATERIALS else "material"
    return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=TAIPEI_TZ)


def reward_text(part: dict, names: dict[str, str]) -> str:
    bits = [f"{part['gold']:,} 金幣"] if part.get("gold") else []
    bits += [f"{names.get(code, code)} ×{qty}" for code, qty in (part.get("items") or {}).items()]
    return "、".join(bits) or "無"


def rewards_text(rewards: dict, names: dict) -> dict:
    return {
        "min_points": wb.min_points(rewards),
        "victory": reward_text(rewards["victory"], names),
        "participation": reward_text(rewards["participation"], names),
        "bonus_points": rewards["bonus_points"],
        "bonus": reward_text(rewards["bonus"], names),
        "rank": [reward_text(r, names) for r in rewards["rank"]],
        "title": rewards["title"], "title_top": rewards["title_top"],
    }


# ---------- 查詢（只讀，監控網站與服務共用） ----------


def supplies_of(session: Session, event_id: int) -> dict[str, WorldBossSupply]:
    rows = session.scalars(select(WorldBossSupply).where(WorldBossSupply.event_id == event_id))
    return {r.supply_code: r for r in rows}


def progress_of(supplies: dict[str, WorldBossSupply]) -> dict[str, int]:
    return {code: s.progress for code, s in supplies.items()}


def ranking(session: Session, event_id: int) -> list[dict]:
    """個人貢獻排行（積分高者在前；同分時先捐獻者在前），名次不並列。"""
    C = WorldBossContribution
    stmt = (
        select(C.player_id, Player.display_name, func.sum(C.points), func.sum(C.quantity),
               func.min(C.created_at), func.count())
        .join(Player, Player.id == C.player_id)
        .where(C.event_id == event_id)
        .group_by(C.player_id, Player.display_name)
        .order_by(func.sum(C.points).desc(), func.min(C.created_at), C.player_id)
    )
    return [
        {"rank": i, "player_id": pid, "player_name": name, "points": int(points), "quantity": int(qty),
         "first_at": first, "donations": n}
        for i, (pid, name, points, qty, first, n) in enumerate(session.execute(stmt), start=1)
    ]


def _projection(ev: WorldBossEvent, stats: dict, now: datetime) -> dict:
    """不寫入資料庫，推算目前的 HP、狀態與結束資訊（與 _advance 相同公式，寫回後顯示結果一致）。"""
    out = {"hp": ev.hp, "defeated_at": ev.defeated_at, "ended_at": ev.ended_at, "end_reason": ev.end_reason}
    if ev.status == "active":
        r = wb.simulate(ev.hp, ev.last_tick_at, ev.starts_at, ev.ends_at, stats["damage_per_hour"], now)
        out["hp"] = r["hp"]
        if r["defeated_at"] is not None:
            out.update(hp=0, defeated_at=r["defeated_at"], ended_at=r["defeated_at"], end_reason="defeated")
        elif r["expired"]:
            out.update(ended_at=ev.ends_at, end_reason="timeout")
    elif ev.status == "paused" and now >= ev.ends_at:
        out.update(ended_at=ev.ends_at, end_reason="timeout")
    if out["end_reason"] is not None:
        out["state"] = "defeated" if out["defeated_at"] is not None else "ended"
    else:
        out["state"] = wb.display_state(ev.status, False, ev.starts_at, now)
    return out


def event_view(session: Session, ev: WorldBossEvent, now: datetime, names: dict | None = None) -> dict:
    supplies = supplies_of(session, ev.id)
    stats = wb.combat_stats(progress_of(supplies), ev.settings)
    proj = _projection(ev, stats, now)
    state = proj["state"]
    C = WorldBossContribution
    totals = session.execute(
        select(func.count(func.distinct(C.player_id)), func.coalesce(func.sum(C.quantity), 0),
               func.coalesce(func.sum(C.points), 0), func.count())
        .where(C.event_id == ev.id)
    ).one()
    claims = session.scalar(select(func.count()).select_from(WorldBossRewardClaim)
                            .where(WorldBossRewardClaim.event_id == ev.id))
    group = session.get(Group, ev.group_id) if ev.group_id is not None else None
    finished = state in ("ended", "defeated")
    return {
        "event_id": ev.id, "group_id": ev.group_id, "group_name": group.name if group else "全服",
        "scope_label": scope_label(group), "schedule_id": ev.schedule_id, "slot_at": ev.slot_at,
        "name": ev.name, "max_hp": ev.max_hp, "hp": proj["hp"],
        "hp_pct": round(proj["hp"] * 100 / ev.max_hp, 1),
        "status": ev.status, "state": state, "state_label": wb.STATE_LABELS[state],
        "starts_at": ev.starts_at, "ends_at": ev.ends_at, "ended_at": proj["ended_at"],
        "end_reason": proj["end_reason"], "defeated_at": proj["defeated_at"], "settled_at": ev.settled_at,
        "paused_at": ev.paused_at,
        "remaining_seconds": 0 if finished else max(0, int((ev.ends_at - now).total_seconds())),
        "supplies": [
            {"code": code, "emoji": wb.SUPPLY_INFO[code][0], "name": wb.SUPPLY_INFO[code][1],
             "effect": wb.SUPPLY_INFO[code][2], "description": wb.SUPPLY_INFO[code][3],
             "progress": supplies[code].progress if code in supplies else 0,
             "target": supplies[code].target if code in supplies else ev.settings["supply_targets"][code],
             "ratio_pct": round(stats["ratios"][code] * 100, 1)}
            for code in wb.SUPPLIES
        ],
        "attack_mult": round(stats["attack_mult"], 2),
        "uptime_pct": round(stats["uptime"] * 100, 1),
        "damage_per_hour": int(stats["damage_per_hour"]),
        "participants": totals[0], "total_quantity": int(totals[1]), "total_points": int(totals[2]),
        "donations": totals[3], "claims": claims,
        "settings": ev.settings, "rewards": ev.rewards,
        "rewards_text": rewards_text(ev.rewards, names) if names is not None else None,
        "created_by": ev.created_by, "created_at": ev.created_at,
    }


def detail_view(session: Session, ev: WorldBossEvent, now: datetime) -> dict:
    """管理網站用：活動資訊、完整排行、捐獻紀錄與獎勵發放紀錄。"""
    names = catalog_repo.item_names(session)
    names.update({f.code: f.name for f in fishing_repo.list_fish_types(session)})
    view = event_view(session, ev, now, names)
    view["ranking"] = ranking(session, ev.id)
    C = WorldBossContribution
    view["contributions"] = [
        {"id": c.id, "player_name": pname, "category": c.category,
         "category_name": wb.CATEGORY_INFO[c.category][1], "supply": wb.SUPPLY_INFO[c.supply_code][1],
         "item_code": c.item_code,
         "item_name": names.get(c.item_code, c.item_code) + genetics_label(c.quality, c.mutation),
         "quantity": c.quantity, "points": c.points, "damage": c.damage, "created_at": c.created_at}
        for c, pname in session.execute(
            select(C, Player.display_name).join(Player, Player.id == C.player_id)
            .where(C.event_id == ev.id).order_by(C.created_at.desc(), C.id.desc()).limit(CONTRIBUTION_LOG_LIMIT))
    ]
    view["claim_rows"] = [
        {"player_name": r.player_name, "rank": r.rank, "points": r.points, "outcome": r.outcome,
         "gold": r.gold, "items_text": reward_text({"gold": 0, "items": r.items}, names),
         "title": r.title, "claimed_at": r.claimed_at}
        for r in session.scalars(select(WorldBossRewardClaim).where(WorldBossRewardClaim.event_id == ev.id)
                                 .order_by(WorldBossRewardClaim.claimed_at.desc()))
    ]
    return view


def schedule_values(s: WorldBossSchedule) -> dict:
    return {"anchor_date": s.anchor_date, "interval_days": s.interval_days, "spawn_hour": s.spawn_hour,
            "spawn_minute": s.spawn_minute, "duration_minutes": s.duration_minutes, "max_hp": s.max_hp}


def last_slot(session: Session, schedule_id: int) -> datetime | None:
    return session.scalar(select(func.max(WorldBossEvent.slot_at)).where(WorldBossEvent.schedule_id == schedule_id))


def schedule_view(session: Session, s: WorldBossSchedule, now: datetime, names: dict) -> dict:
    """排程：時間規則、下一場（已建立的活動）與之後幾場的預計時間。"""
    group = session.get(Group, s.group_id) if s.group_id is not None else None
    E = WorldBossEvent
    upcoming = session.scalar(select(E).where(E.schedule_id == s.id, E.status != "ended").order_by(E.slot_at))
    after = last_slot(session, s.id)
    hours, minutes = divmod(s.duration_minutes, 60)
    return {
        "schedule_id": s.id, "name": s.name, "group_id": s.group_id, "group_name": group.name if group else "全服",
        "scope_label": scope_label(group), **schedule_values(s), "enabled": s.enabled,
        "time_text": f"每 {s.interval_days} 天 {s.spawn_hour:02d}:{s.spawn_minute:02d}（台灣時間）出現，"
                     f"持續 {hours} 小時" + (f" {minutes} 分" if minutes else ""),
        "settings": s.settings, "rewards": s.rewards, "rewards_text": rewards_text(s.rewards, names),
        "next_event": ({"event_id": upcoming.id, "name": upcoming.name, "starts_at": upcoming.starts_at,
                        "ends_at": upcoming.ends_at, "slot_at": upcoming.slot_at} if upcoming else None),
        "upcoming_slots": wb.upcoming_slots(schedule_values(s), now, after) if s.enabled else [],
        "created_by": s.created_by, "updated_at": s.updated_at,
    }


def defaults_view(session: Session) -> dict:
    """管理網站表單用：預設值、補給與類別說明、可捐獻物品與可發放的獎勵物品（只有規則與目錄）。"""
    kinds = catalog_repo.items(session)
    donatable = sorted(
        ({"code": code, "name": name, "category": item_category(kind, code)}
         for code, (kind, name) in kinds.items() if item_category(kind, code)),
        key=lambda x: (wb.CATEGORIES.index(x["category"]), x["code"]),
    )
    fish = [{"code": f.code, "name": f.name, "category": "fish"} for f in fishing_repo.list_fish_types(session)]
    return {
        "name": wb.DEFAULT_NAME, "max_hp": wb.DEFAULT_MAX_HP, "schedule": {
            k: (v.isoformat() if k == "anchor_date" else v) for k, v in wb.abyss_dragon().items()},
        "duration_hours": wb.DEFAULT_DURATION_HOURS, "settings": wb.DEFAULT_SETTINGS,
        "rewards": wb.DEFAULT_REWARDS,
        "supplies": {c: {"emoji": e, "name": n, "effect": f, "description": d}
                     for c, (e, n, f, d) in wb.SUPPLY_INFO.items()},
        "categories": {c: {"emoji": e, "name": n} for c, (e, n) in wb.CATEGORY_INFO.items()},
        "donatable": donatable + fish,
        "reward_items": [{"code": c, "name": n} for c, (_k, n) in kinds.items()],
    }


class WorldBossService(RankingService):
    # ---------- 共用 ----------

    @staticmethod
    def _boss_for_update(session: Session, event_id: int) -> WorldBossEvent:
        ev = session.scalar(select(WorldBossEvent).where(WorldBossEvent.id == event_id).with_for_update())
        if ev is None:
            raise NotFoundError(f"找不到世界 Boss 活動 #{event_id}。")
        return ev

    @staticmethod
    def _boss_finish(ev: WorldBossEvent, reason: str, at: datetime, now: datetime) -> None:
        ev.status, ev.end_reason, ev.ended_at, ev.paused_at = "ended", reason, at, None
        if reason == "defeated":
            ev.defeated_at, ev.hp = at, 0
        ev.settled_at = now
        ev.updated_at = now

    def _advance(self, session: Session, ev: WorldBossEvent, now: datetime) -> None:
        """把戰鬥推進到現在並寫回（只有進行中的活動會扣 HP）；到期或擊敗時結束活動。"""
        if ev.status == "ended":
            return
        if ev.status == "paused":
            if now >= ev.ends_at:
                self._boss_finish(ev, "timeout", ev.ends_at, now)
            return
        stats = wb.combat_stats(progress_of(supplies_of(session, ev.id)), ev.settings)
        r = wb.simulate(ev.hp, ev.last_tick_at, ev.starts_at, ev.ends_at, stats["damage_per_hour"], now)
        if r["hp"] != ev.hp or r["last_tick_at"] != ev.last_tick_at:
            ev.hp, ev.last_tick_at, ev.updated_at = r["hp"], r["last_tick_at"], now
        if r["defeated_at"] is not None:
            self._boss_finish(ev, "defeated", r["defeated_at"], now)
        elif r["expired"]:
            self._boss_finish(ev, "timeout", ev.ends_at, now)

    @staticmethod
    def _claim(session: Session, event_id: int, player_id: int) -> WorldBossRewardClaim | None:
        return session.scalar(select(WorldBossRewardClaim).where(
            WorldBossRewardClaim.event_id == event_id, WorldBossRewardClaim.player_id == player_id))

    def _member_event(self, session: Session, group_id: int | None, player_id: int, event_id: int,
                      *, lock: bool = True) -> WorldBossEvent:
        """確認玩家可以參加（並預設鎖定活動列）：全服活動所有玩家都可以；群組活動必須在該群組中操作且是成員
        （其他群組的活動一律視為不存在）。只讀取的查詢用 lock=False，不會和捐獻搶同一列的鎖。"""
        if group_id is not None:
            self._require_member(session, group_id, player_id)
        ev = self._boss_for_update(session, event_id) if lock else session.get(WorldBossEvent, event_id)
        if ev is None:
            raise NotFoundError(f"找不到世界 Boss 活動 #{event_id}。")
        if ev.group_id is not None and ev.group_id != group_id:
            raise NotFoundError(f"找不到世界 Boss 活動 #{event_id}。")
        return ev

    @staticmethod
    def _scope(group_id: int | None):
        """SQL 條件：玩家在目前位置看得到的活動（全服活動，加上所在群組的活動）。"""
        if group_id is None:
            return WorldBossEvent.group_id.is_(None)
        return or_(WorldBossEvent.group_id.is_(None), WorldBossEvent.group_id == group_id)

    @staticmethod
    def _crops(session: Session) -> dict:
        """作物產品代碼（含珍稀變種）→ 作物種類。"""
        return {code: crop for code, (crop, _variant) in crops_repo.produce_map(session).items()}

    @staticmethod
    def _points_per_unit(session: Session, settings: dict, category: str, code: str, quality: str,
                         mutation: str, crop=None) -> int:
        """每個物品的積分：指定物品用固定積分；作物依售價（含品質與突變）換算；其他物品不能捐（0）。"""
        fixed = settings["items"].get(code)
        if fixed is not None:
            return fixed["points"]
        if category == "crop" and crop is not None:
            price = genetics.unit_price(crop.sell_price, quality, mutation, genetics_repo.current(session)[1])
            return wb.price_points(price, settings["gold_per_point"])
        return 0

    @staticmethod
    def _supply_of(settings: dict, category: str, code: str) -> str:
        fixed = settings["items"].get(code)
        if fixed is not None:
            return fixed["supply"]
        return settings["fish_supply"] if category == "fish" else settings["crop_supply"]

    @staticmethod
    def _donated_by_item(session: Session, event_id: int, player_id: int) -> dict[str, int]:
        C = WorldBossContribution
        rows = session.execute(
            select(C.item_code, func.sum(C.quantity)).where(C.event_id == event_id, C.player_id == player_id)
            .group_by(C.item_code)
        )
        return {code: int(q) for code, q in rows}

    # ---------- 玩家 ----------

    def boss_current(self, group_id: int | None, player_id: int, event_id: int | None = None) -> dict | None:
        """玩家看得到的世界 Boss（全服活動與所在群組的活動）：指定 event_id 時顯示那一場，否則顯示進行中的活動
        （沒有時為即將開始、再沒有時為最近結束的一場），並附上自己的貢獻與可切換的其他活動；沒有任何活動時回傳 None。"""
        now = self._clock.now()
        with self._transaction() as session:
            self._spawn_due(session, now)
            if group_id is not None:
                self._require_member(session, group_id, player_id)
            E = WorldBossEvent
            scope = self._scope(group_id)
            open_ids = list(session.scalars(
                select(E.id).where(scope, E.status != "ended").order_by(E.starts_at > now, E.starts_at, E.id)))
            if event_id is not None and session.scalar(select(E.id).where(scope, E.id == event_id)) is None:
                event_id = None  # 看不到的活動（其他群組）一律忽略
            if event_id is None:
                event_id = open_ids[0] if open_ids else session.scalar(
                    select(E.id).where(scope).order_by(E.ended_at.desc().nulls_last(), E.id.desc()).limit(1))
            if event_id is None:
                return None
            # 只讀取：HP 與狀態依公式推算（event_view），不鎖活動列，大量玩家同時查看不會互相等待；
            # 實際寫回在捐獻、領獎、管理操作與背景結算時進行
            ev = session.get(E, event_id)
            view = event_view(session, ev, now, catalog_repo.item_names(session))
            rows = ranking(session, ev.id)
            view["me"] = self._my_status(session, ev, player_id, rows, view["state"])
            view["top"] = rows[:3]
            view["claimable"] = self._claimable(session, group_id, player_id, exclude=ev.id)
            view["choices"] = []
            for c in (session.get(E, i) for i in open_ids):
                group = session.get(Group, c.group_id) if c.group_id is not None else None
                state = wb.display_state(c.status, False, c.starts_at, now)
                view["choices"].append({"event_id": c.id, "name": c.name, "scope_label": scope_label(group),
                                        "state_label": wb.STATE_LABELS[state], "starts_at": c.starts_at})
            return view

    def _my_status(self, session: Session, ev: WorldBossEvent, player_id: int, rows: list[dict],
                   state: str) -> dict:
        mine = next((r for r in rows if r["player_id"] == player_id), None)
        claim = self._claim(session, ev.id, player_id)
        if claim is not None:
            claim_state = "claimed"
        elif state not in ("ended", "defeated"):
            claim_state = "pending"  # 活動結束後才能領
        elif mine is None:
            claim_state = "none"  # 沒有貢獻
        elif mine["points"] < wb.min_points(ev.rewards):
            claim_state = "below_min"  # 積分未達參與門檻
        else:
            claim_state = "ready"
        player = session.get(Player, player_id)
        return {
            "player_name": player.display_name if player else "",
            "points": mine["points"] if mine else 0, "quantity": mine["quantity"] if mine else 0,
            "rank": mine["rank"] if mine else None, "participants": len(rows),
            "claim_state": claim_state, "title": claim.title if claim else None,
        }

    @classmethod
    def _claimable(cls, session: Session, group_id: int | None, player_id: int,
                   exclude: int | None = None) -> list[dict]:
        """看得到的活動中已結束、有貢獻（達參與門檻）但還沒領獎的其他活動。"""
        C, R, E = WorldBossContribution, WorldBossRewardClaim, WorldBossEvent
        mine = (select(func.sum(C.points)).where(C.event_id == E.id, C.player_id == player_id)
                .correlate(E).scalar_subquery())
        stmt = (
            select(E.id, E.name, E.ended_at, E.defeated_at, E.rewards, mine)
            .where(cls._scope(group_id), E.status == "ended", E.id != (exclude or 0),
                   select(C.id).where(C.event_id == E.id, C.player_id == player_id).exists(),
                   ~select(R.id).where(R.event_id == E.id, R.player_id == player_id).exists())
            .order_by(E.ended_at.desc()).limit(10)
        )
        return [{"event_id": i, "name": n, "ended_at": e, "defeated": d is not None}
                for i, n, e, d, rewards, points in session.execute(stmt) if points >= wb.min_points(rewards)]

    def boss_donation_options(self, group_id: int, player_id: int, event_id: int, category: str) -> list[dict]:
        """玩家背包中這個類別可以捐獻的物品：名稱、持有數量、每個積分、補給類型與本場剩餘可捐數量。"""
        if category not in wb.CATEGORIES:
            raise ValidationError("捐獻類別不正確。")
        with self._transaction() as session:
            ev = self._member_event(session, group_id, player_id, event_id, lock=False)  # 只讀取，不鎖
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)  # 產生「找不到玩家／還沒有農場」的錯誤
            settings = ev.settings
            donated = self._donated_by_item(session, ev.id, player_id)
            cap = settings["limits"]["per_item_max"]
            options = []
            if category == "fish":
                rows = session.execute(
                    select(FarmFish.fish_code, FishType.name, FishType.emoji, func.count(), func.min(FarmFish.price),
                           func.max(FarmFish.price))
                    .join(FishType, FishType.code == FarmFish.fish_code)
                    .where(FarmFish.farm_id == farm.id, *fishing_repo.in_basket())
                    .group_by(FarmFish.fish_code, FishType.name, FishType.emoji, FishType.sort_order)
                    .order_by(FishType.sort_order, FarmFish.fish_code)
                )
                for code, name, emoji, count, low, high in rows:
                    fixed = settings["items"].get(code)
                    if fixed is not None and fixed["points"] <= 0:
                        continue
                    gpp = settings["gold_per_point"]
                    pts = (fixed["points"], fixed["points"]) if fixed else (wb.price_points(low, gpp),
                                                                            wb.price_points(high, gpp))
                    options.append({
                        "category": "fish", "code": code, "quality": "normal", "mutation": "none",
                        "name": f"{emoji} {name}", "held": count, "points_min": pts[0], "points_max": pts[1],
                        "supply": self._supply_of(settings, "fish", code),
                        "remaining": max(0, cap - donated.get(code, 0)),
                    })
                return options
            kinds = catalog_repo.items(session)
            crops = self._crops(session)
            names = catalog_repo.item_names(session)
            for stack in inventory_repo.list_items(session, farm.id):
                kind = kinds.get(stack.item_code, (None, None))[0]
                if item_category(kind, stack.item_code) != category:
                    continue
                crop = crops.get(stack.item_code)
                pts = self._points_per_unit(session, settings, category, stack.item_code, stack.quality,
                                            stack.mutation, crop)
                if pts <= 0:
                    continue
                name = (genetics.produce_name(crop, stack.mutation) if crop is not None
                        else names.get(stack.item_code, stack.item_code))
                options.append({
                    "category": category, "code": stack.item_code, "quality": stack.quality,
                    "mutation": stack.mutation, "name": name + genetics_label(stack.quality, stack.mutation),
                    "held": stack.quantity, "points_min": pts, "points_max": pts,
                    "supply": self._supply_of(settings, category, stack.item_code),
                    "remaining": max(0, cap - donated.get(stack.item_code, 0)),
                })
            return options

    def boss_donate(
        self, group_id: int, player_id: int, event_id: int, category: str, item_code: str, quantity: int,
        request_id: str, quality: str = "normal", mutation: str = "none",
    ) -> dict:
        """捐獻物資：扣除背包物品（或魚簍的魚）、增加補給進度與個人積分，並立即造成一次傷害。

        request_id 相同的請求只處理一次（重複點擊回傳 duplicate=True，不再扣物品）。"""
        if category not in wb.CATEGORIES:
            raise ValidationError("捐獻類別不正確。")
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.match(request_id):
            raise ValidationError("捐獻請求編號不正確，請重新開啟捐獻畫面。")
        if not isinstance(item_code, str) or not 1 <= len(item_code) <= 64:
            raise ValidationError("物品代碼不正確。")
        if quality not in QUALITY_CODES or mutation not in MUTATION_CODES:
            raise ValidationError("物品品質不正確。")
        quantity = rules.validate_quantity(quantity)
        now = self._clock.now()
        with self._transaction() as session:
            ev = self._member_event(session, group_id, player_id, event_id)
            farm = self._locked_farm(session, player_id)
            C = WorldBossContribution
            done = session.scalar(select(C).where(C.event_id == ev.id, C.player_id == player_id,
                                                  C.request_id == request_id))
            if done is not None:
                return {"message": "這筆捐獻已經完成，不會重複扣除物品。", "duplicate": True,
                        "points": done.points, "quantity": done.quantity, "damage": 0, "defeated": False}
            self._advance(session, ev, now)
            state = wb.display_state(ev.status, ev.defeated_at is not None, ev.starts_at, now)
            if state != "active":
                raise ValidationError(f"世界 Boss 活動{wb.STATE_LABELS[state]}，現在不能捐獻。")
            settings = ev.settings
            limits = settings["limits"]
            if quantity > limits["per_action_max"]:
                raise ValidationError(f"單次最多捐獻 {limits['per_action_max']} 個。")
            already = self._donated_by_item(session, ev.id, player_id).get(item_code, 0)
            if already + quantity > limits["per_item_max"]:
                left = max(0, limits["per_item_max"] - already)
                raise ValidationError(
                    f"每種物品每場活動最多捐獻 {limits['per_item_max']} 個，你這項還可以捐 {left} 個。")
            if category == "fish":
                points, name = self._take_fish(session, farm.id, item_code, quantity, settings, now)
                quality = mutation = None
            else:
                kind = catalog_repo.items(session).get(item_code, (None, None))[0]
                if item_category(kind, item_code) != category:
                    raise ValidationError("這個物品不能用這個類別捐獻。")
                crop = self._crops(session).get(item_code)
                per = self._points_per_unit(session, settings, category, item_code, quality, mutation, crop)
                if per <= 0:
                    raise ValidationError("這個物品不在本次活動的捐獻清單中。")
                if not inventory_repo.remove(session, farm.id, item_code, quantity, quality, mutation):
                    held = inventory_repo.get_quantity(session, farm.id, item_code, quality, mutation)
                    raise InsufficientItemsError(f"背包數量不足：只有 {held} 個，不能捐獻 {quantity} 個。")
                points = per * quantity
                base = (genetics.produce_name(crop, mutation) if crop is not None
                        else catalog_repo.item_names(session).get(item_code, item_code))
                name = base + genetics_label(quality, mutation)
            supply_code = self._supply_of(settings, category, item_code)
            supplies = supplies_of(session, ev.id)
            supplies[supply_code].progress += points
            stats = wb.combat_stats(progress_of(supplies), settings)
            damage = min(ev.hp, wb.instant_damage(points, settings, stats["attack_mult"]))
            ev.hp -= damage
            ev.updated_at = now
            try:
                with session.begin_nested():
                    session.add(C(event_id=ev.id, player_id=player_id, farm_id=farm.id, request_id=request_id,
                                  category=category, supply_code=supply_code, item_code=item_code, quality=quality,
                                  mutation=mutation, quantity=quantity, points=points, damage=damage,
                                  created_at=now))
            except IntegrityError:  # 已被活動列鎖擋住；保險起見由唯一限制再擋一次
                raise ValidationError("這筆捐獻已經處理過了。") from None
            supply_name = wb.SUPPLY_INFO[supply_code][1]
            defeated = ev.hp <= 0
            if defeated:
                self._boss_finish(ev, "defeated", now, now)
            self._record(
                session, farm, EventType.BOSS_DONATE,
                f"世界 Boss「{ev.name}」捐獻 {name} ×{quantity}（{supply_name} +{points} 積分）"[:200],
                item_code=item_code, quantity=quantity, quality=quality, mutation=mutation,
            )
            message = (f"📦 捐獻 {name} ×{quantity}，獲得 {points} 積分（{supply_name} +{points}），"
                       f"前線立即造成 {damage:,} 點傷害。")
            if defeated:
                message += f"\n🎉 {ev.name} 被擊敗了！可以領取勝利獎勵。"
            return {"message": message, "duplicate": False, "points": points, "quantity": quantity,
                    "damage": damage, "defeated": defeated}

    @staticmethod
    def _take_fish(session: Session, farm_id: int, fish_code: str, quantity: int, settings: dict,
                   now: datetime) -> tuple[int, str]:
        """捐出魚簍中這種魚價值最低的 quantity 隻（保留高價值的魚）。"""
        fixed = settings["items"].get(fish_code)
        if fixed is not None and fixed["points"] <= 0:
            raise ValidationError("這種魚不在本次活動的捐獻清單中。")
        fish = list(session.scalars(
            select(FarmFish).where(FarmFish.farm_id == farm_id, FarmFish.fish_code == fish_code,
                                   *fishing_repo.in_basket())
            .order_by(FarmFish.price, FarmFish.weight_g, FarmFish.id).limit(quantity).with_for_update()
        ))
        if len(fish) < quantity:
            raise InsufficientItemsError(f"魚簍裡這種魚只有 {len(fish)} 隻，不能捐獻 {quantity} 隻。")
        points = 0
        for f in fish:
            f.donated_at = now
            points += fixed["points"] if fixed else wb.price_points(f.price, settings["gold_per_point"])
        ftype = session.scalar(select(FishType).where(FishType.code == fish_code))
        return points, (f"{ftype.emoji} {ftype.name}" if ftype else fish_code)

    def boss_leaderboard(self, group_id: int, player_id: int, event_id: int,
                         limit: int = wb.LEADERBOARD_SIZE) -> dict:
        with self._transaction() as session:
            ev = self._member_event(session, group_id, player_id, event_id, lock=False)  # 排行與 HP 無關，只讀取
            rows = ranking(session, ev.id)
            mine = next((r for r in rows if r["player_id"] == player_id), None)
            return {"event_id": ev.id, "name": ev.name, "rows": rows[:limit], "me": mine, "participants": len(rows)}

    def boss_claim(self, group_id: int, player_id: int, event_id: int) -> ActionResult:
        """領取活動獎勵（活動結束後）：依結果、個人積分與名次發放；每人每場只能領一次。"""
        now = self._clock.now()
        with self._transaction() as session:
            ev = self._member_event(session, group_id, player_id, event_id)
            self._advance(session, ev, now)
            if ev.status != "ended":
                raise ValidationError("活動還沒結束，結束後才能領取獎勵。")
            if self._claim(session, ev.id, player_id) is not None:
                raise AlreadyExistsError("你已經領過這場活動的獎勵了。")
            rows = ranking(session, ev.id)
            mine = next((r for r in rows if r["player_id"] == player_id), None)
            if mine is None:
                raise ValidationError("你沒有參與這場活動的捐獻，沒有獎勵可以領。")
            need = wb.min_points(ev.rewards)
            if mine["points"] < need:
                raise ValidationError(f"你的貢獻積分 {mine['points']:,} 未達參與門檻 {need:,} 分，沒有獎勵可以領。")
            farm = self._locked_farm(session, player_id)
            defeated = ev.defeated_at is not None
            reward = wb.compose_rewards(ev.rewards, defeated=defeated, points=mine["points"], rank=mine["rank"])
            names = catalog_repo.item_names(session)
            for code, qty in reward["items"].items():
                self._ensure_item_space(session, farm, code, qty)
            try:
                with session.begin_nested():
                    session.add(WorldBossRewardClaim(
                        event_id=ev.id, player_id=player_id, player_name=mine["player_name"], rank=mine["rank"],
                        points=mine["points"], outcome="victory" if defeated else "participation",
                        gold=reward["gold"], items=reward["items"], title=reward["title"], claimed_at=now,
                    ))
            except IntegrityError:
                raise AlreadyExistsError("你已經領過這場活動的獎勵了。") from None
            label = "勝利" if defeated else "參與"
            if reward["gold"]:
                farm.gold += reward["gold"]
                self._record(session, farm, EventType.BOSS_REWARD,
                             f"世界 Boss「{ev.name}」{label}獎勵（第 {mine['rank']} 名）{reward['gold']} 金幣"[:200],
                             gold_delta=reward["gold"])
            for code, qty in reward["items"].items():
                inventory_repo.add(session, farm.id, code, qty)
                self._record(session, farm, EventType.BOSS_REWARD,
                             f"世界 Boss「{ev.name}」{label}獎勵：{names.get(code, code)} ×{qty}"[:200],
                             item_code=code, quantity=qty)
            got = reward_text({"gold": reward["gold"], "items": reward["items"]}, names)
            message = f"🎁 領取「{ev.name}」{'、'.join(reward['parts']) or label + '獎勵'}：{got}。"
            if reward["title"]:
                titles_repo.grant(session, farm.id, reward["title"], "world_boss", now)  # 可在「成就圖鑑」選用
                message += f"\n🏅 獲得稱號「{reward['title']}」！可到「🏅 成就圖鑑」設定顯示。"
            return ActionResult(message, farm.gold, reward["gold"], None, sum(reward["items"].values()))

    # ---------- 背景結算 ----------

    def finalize_due_world_bosses(self) -> list[int]:
        """推進所有未結束的活動；已過結束時間或 HP 歸零的活動在此結束（開放領獎）。回傳本次結束的活動 ID。"""
        now = self._clock.now()
        with self._transaction() as session:
            ids = list(session.scalars(select(WorldBossEvent.id).where(WorldBossEvent.status != "ended")
                                       .order_by(WorldBossEvent.id)))
        done = []
        for event_id in ids:
            with self._transaction() as session:
                ev = self._boss_for_update(session, event_id)
                self._advance(session, ev, now)
                if ev.status == "ended":
                    done.append(ev.id)
        return done

    def spawn_due_world_bosses(self) -> list[int]:
        """依啟用中的排程建立下一場世界 Boss（上一場結束後立即建立，出現前管理員可個別修改）；回傳新建立的活動 ID。"""
        with self._transaction() as session:
            return self._spawn_due(session, self._clock.now())

    def _spawn_due(self, session: Session, now: datetime, schedule_id: int | None = None) -> list[int]:
        S, E = WorldBossSchedule, WorldBossEvent
        stmt = select(S).where(S.enabled.is_(True),
                               ~select(E.id).where(E.schedule_id == S.id, E.status != "ended").exists())
        if schedule_id is not None:
            stmt = stmt.where(S.id == schedule_id)
        created = []
        for sched in session.scalars(stmt.order_by(S.id)):
            slot = wb.slot_after(sched.anchor_date, sched.spawn_hour, sched.spawn_minute, sched.interval_days,
                                 sched.duration_minutes, now, last_slot(session, sched.id))
            ev = WorldBossEvent(
                group_id=sched.group_id, schedule_id=sched.id, slot_at=slot, name=sched.name, level=1,
                max_hp=sched.max_hp, hp=sched.max_hp, status="active", starts_at=slot,
                ends_at=slot + timedelta(minutes=sched.duration_minutes), last_tick_at=max(slot, now),
                settings=sched.settings, rewards=sched.rewards, created_by=f"schedule#{sched.id}",
                created_at=now, updated_at=now,
            )
            try:  # 多個程序同時建立同一場時由唯一限制擋住，略過即可
                with session.begin_nested():
                    session.add(ev)
                    session.flush()
                    for code in wb.SUPPLIES:
                        session.add(WorldBossSupply(event_id=ev.id, supply_code=code,
                                                    target=sched.settings["supply_targets"][code], progress=0))
                    session.flush()
            except IntegrityError:
                continue
            created.append(ev.id)
        return created

    def run_background_settlement(self, actor: str = "system") -> dict | None:
        result = super().run_background_settlement(actor)
        if result is not None:
            result["world_boss"] = self.finalize_due_world_bosses()
            result["world_boss_spawned"] = self.spawn_due_world_bosses()
        return result

    # ---------- 管理員 ----------

    def _boss_audit(self, session, actor: str, action: str, event_id: int, details: dict) -> None:
        community_repo.add_admin_action(session, actor, action, "world_boss", event_id, details, self._clock.now())

    @staticmethod
    def _validators(session: Session):
        kinds = catalog_repo.items(session)
        fish_codes = set(session.scalars(select(FishType.code)))

        def category(code: str) -> str | None:
            if code in fish_codes:
                return "fish"
            return item_category(kinds.get(code, (None, None))[0], code)

        return category, (lambda code: code in kinds)

    @staticmethod
    def _check_times(starts_at: datetime, ends_at: datetime, now: datetime) -> None:
        if ends_at <= starts_at:
            raise ValidationError("結束時間必須晚於開始時間。")
        seconds = (ends_at - starts_at).total_seconds()
        low, high = wb.DURATION_RANGE_SECONDS
        if not low <= seconds <= high:
            raise ValidationError(f"活動期間必須介於 {low // 60} 分鐘到 {high // 86400} 天。")
        if ends_at <= now:
            raise ValidationError("結束時間必須晚於現在。")

    @staticmethod
    def _basic_values(data: dict) -> dict:
        return {
            "name": rules.validate_name(str(data.get("name", "")), label="Boss 名稱", max_len=wb.NAME_MAX_LEN),
            "level": wb.int_value(data.get("level") or 1, "Boss 等級", *wb.LEVEL_RANGE),  # 介面不顯示，預設 1
            "max_hp": wb.int_value(data.get("max_hp"), "Boss HP", *wb.MAX_HP_RANGE),
        }

    def create_world_boss(self, actor: str, data: dict) -> dict:
        now = self._clock.now()
        values = self._basic_values(data)
        starts_at, ends_at = _aware(data["starts_at"]), _aware(data["ends_at"])
        self._check_times(starts_at, ends_at, now)
        with self._transaction() as session:
            group_id = self._boss_group_id(session, data.get("group_id"))
            category, valid_item = self._validators(session)
            settings = wb.validate_settings(data.get("settings") or wb.DEFAULT_SETTINGS, category)
            rewards = wb.validate_rewards(data.get("rewards") or wb.DEFAULT_REWARDS, valid_item)
            ev = WorldBossEvent(
                group_id=group_id, **values, hp=values["max_hp"], status="active", starts_at=starts_at,
                ends_at=ends_at, last_tick_at=starts_at, settings=settings, rewards=rewards, created_by=actor,
                created_at=now, updated_at=now,
            )
            try:
                with session.begin_nested():
                    session.add(ev)
                    session.flush()
            except IntegrityError:
                where = "全服" if group_id is None else "這個群組"
                raise AlreadyExistsError(f"{where}已經有尚未結束的手動世界 Boss 活動，請先結束它。") from None
            for code in wb.SUPPLIES:
                session.add(WorldBossSupply(event_id=ev.id, supply_code=code,
                                            target=settings["supply_targets"][code], progress=0))
            session.flush()
            self._boss_audit(session, actor, "world_boss_create", ev.id, {
                "name": ev.name, "group_id": group_id, "max_hp": ev.max_hp, "level": ev.level,
                "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()})
            return event_view(session, ev, now)

    @staticmethod
    def _boss_group_id(session: Session, group_id) -> int | None:
        """None 表示全服；其他值必須是存在的群組。"""
        if group_id is None:
            return None
        if isinstance(group_id, bool) or not isinstance(group_id, int) or groups_repo.get(session, group_id) is None:
            raise NotFoundError(f"找不到群組 #{group_id}。")
        return group_id

    def _open_event(self, session: Session, event_id: int, now: datetime) -> WorldBossEvent:
        ev = self._boss_for_update(session, event_id)
        self._advance(session, ev, now)
        if ev.status == "ended":
            raise ValidationError("活動已經結束，不能再修改（歷史紀錄會保留）。")
        return ev

    def update_world_boss(self, actor: str, event_id: int, data: dict) -> dict:
        """修改名稱、等級、HP 與期間（活動結束前）。已開始的活動不能改開始時間；HP 上限改變時剩餘 HP 依差額調整。"""
        now = self._clock.now()
        values = self._basic_values(data)
        with self._transaction() as session:
            ev = self._open_event(session, event_id, now)
            before = {"name": ev.name, "level": ev.level, "max_hp": ev.max_hp, "hp": ev.hp,
                      "starts_at": ev.starts_at.isoformat(), "ends_at": ev.ends_at.isoformat()}
            starts_at = _aware(data["starts_at"]) if data.get("starts_at") else ev.starts_at
            ends_at = _aware(data["ends_at"]) if data.get("ends_at") else ev.ends_at
            started = now >= ev.starts_at
            if started and starts_at != ev.starts_at:
                raise ValidationError("活動已經開始，不能修改開始時間。")
            self._check_times(starts_at, ends_at, now)
            if values["max_hp"] != ev.max_hp:
                ev.hp = (max(1, min(values["max_hp"], ev.hp + values["max_hp"] - ev.max_hp)) if started
                         else values["max_hp"])
            ev.name, ev.level, ev.max_hp = values["name"], values["level"], values["max_hp"]
            if not started:
                ev.last_tick_at = starts_at
            ev.starts_at, ev.ends_at, ev.updated_at = starts_at, ends_at, now
            self._boss_audit(session, actor, "world_boss_update", ev.id, {"before": before, "after": {
                "name": ev.name, "level": ev.level, "max_hp": ev.max_hp, "hp": ev.hp,
                "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}})
            return event_view(session, ev, now)

    def update_world_boss_settings(self, actor: str, event_id: int, raw: dict) -> dict:
        """修改補給目標、效果、捐獻限制與可捐獻物品（活動結束前）；先以舊設定把傷害結算到現在。"""
        now = self._clock.now()
        with self._transaction() as session:
            ev = self._open_event(session, event_id, now)
            category, _valid = self._validators(session)
            settings = wb.validate_settings(raw, category)
            for code, row in supplies_of(session, ev.id).items():
                row.target = settings["supply_targets"][code]
            ev.settings, ev.updated_at = settings, now
            self._boss_audit(session, actor, "world_boss_settings", ev.id, {"name": ev.name, "settings": settings})
            return event_view(session, ev, now)

    def update_world_boss_rewards(self, actor: str, event_id: int, raw: dict) -> dict:
        """修改獎勵設定；已經有人領獎後不能再改（避免同一場活動獎勵不一致）。"""
        now = self._clock.now()
        with self._transaction() as session:
            ev = self._boss_for_update(session, event_id)
            claimed = session.scalar(select(func.count()).select_from(WorldBossRewardClaim)
                                     .where(WorldBossRewardClaim.event_id == ev.id))
            if claimed:
                raise ValidationError("已經有玩家領取獎勵，不能再修改這場活動的獎勵。")
            _category, valid_item = self._validators(session)
            ev.rewards, ev.updated_at = wb.validate_rewards(raw, valid_item), now
            self._boss_audit(session, actor, "world_boss_rewards", ev.id, {"name": ev.name, "rewards": ev.rewards})
            return event_view(session, ev, now)

    def world_boss_action(self, actor: str, event_id: int, action: str) -> dict:
        """start：立即開始（尚未開始的活動）；pause／resume：暫停與繼續（暫停期間不扣 HP、不能捐獻）；end：提前結束。"""
        if action not in ("start", "pause", "resume", "end"):
            raise ValidationError("不支援的操作。")
        now = self._clock.now()
        with self._transaction() as session:
            ev = self._open_event(session, event_id, now)
            if action == "start":
                if ev.status != "active" or now >= ev.starts_at:
                    raise ValidationError("只有尚未開始的活動可以立即開始。")
                ev.starts_at = ev.last_tick_at = now
            elif action == "pause":
                if ev.status != "active" or now < ev.starts_at:
                    raise ValidationError("只有進行中的活動可以暫停。")
                ev.status, ev.paused_at = "paused", now
            elif action == "resume":
                if ev.status != "paused":
                    raise ValidationError("活動沒有暫停。")
                ev.status, ev.paused_at, ev.last_tick_at = "active", None, max(now, ev.starts_at)
            else:
                self._boss_finish(ev, "admin", now, now)
            ev.updated_at = now
            self._boss_audit(session, actor, f"world_boss_{action}", ev.id, {"name": ev.name})
            return event_view(session, ev, now)

    # ---------- 固定週期的世界 Boss（排程） ----------

    def _schedule_values(self, session: Session, data: dict) -> dict:
        values = wb.validate_schedule(data)
        category, valid_item = self._validators(session)
        base = wb.abyss_dragon()
        return {
            **values,
            "name": rules.validate_name(str(data.get("name", "")), label="Boss 名稱", max_len=wb.NAME_MAX_LEN),
            "group_id": self._boss_group_id(session, data.get("group_id")),
            "settings": wb.validate_settings(data.get("settings") or base["settings"], category),
            "rewards": wb.validate_rewards(data.get("rewards") or base["rewards"], valid_item),
            "enabled": bool(data.get("enabled", True)),
        }

    @staticmethod
    def _schedule_for_update(session: Session, schedule_id: int) -> WorldBossSchedule:
        sched = session.scalar(select(WorldBossSchedule).where(WorldBossSchedule.id == schedule_id).with_for_update())
        if sched is None:
            raise NotFoundError(f"找不到世界 Boss 排程 #{schedule_id}。")
        return sched

    def _schedule_audit(self, session, actor: str, action: str, sched: WorldBossSchedule) -> None:
        community_repo.add_admin_action(session, actor, action, "world_boss_schedule", sched.id, {
            "name": sched.name, "group_id": sched.group_id, "interval_days": sched.interval_days,
            "time": f"{sched.spawn_hour:02d}:{sched.spawn_minute:02d}", "anchor_date": sched.anchor_date.isoformat(),
            "duration_minutes": sched.duration_minutes, "max_hp": sched.max_hp, "enabled": sched.enabled,
        }, self._clock.now())

    def create_world_boss_schedule(self, actor: str, data: dict) -> dict:
        """新增固定週期的世界 Boss；建立後立即產生下一場（出現前可在活動列表個別修改時間與獎勵）。"""
        now = self._clock.now()
        with self._transaction() as session:
            sched = WorldBossSchedule(**self._schedule_values(session, data), created_by=actor, created_at=now,
                                      updated_at=now)
            session.add(sched)
            session.flush()
            self._schedule_audit(session, actor, "world_boss_schedule_create", sched)
            self._spawn_due(session, now, sched.id)
            return schedule_view(session, sched, now, catalog_repo.item_names(session))

    def update_world_boss_schedule(self, actor: str, schedule_id: int, data: dict) -> dict:
        """修改排程（之後產生的每一場都套用）；已經建立的下一場不受影響，要改那一場請在活動列表修改。"""
        now = self._clock.now()
        with self._transaction() as session:
            sched = self._schedule_for_update(session, schedule_id)
            for key, value in self._schedule_values(session, data).items():
                setattr(sched, key, value)
            sched.updated_at = now
            self._schedule_audit(session, actor, "world_boss_schedule_update", sched)
            self._spawn_due(session, now, sched.id)
            return schedule_view(session, sched, now, catalog_repo.item_names(session))

    def set_world_boss_schedule_enabled(self, actor: str, schedule_id: int, enabled: bool) -> dict:
        """停用後不再產生新的一場（已建立的下一場保留，可在活動列表結束）。"""
        now = self._clock.now()
        with self._transaction() as session:
            sched = self._schedule_for_update(session, schedule_id)
            sched.enabled, sched.updated_at = bool(enabled), now
            self._schedule_audit(session, actor, "world_boss_schedule_enable" if enabled
                                 else "world_boss_schedule_disable", sched)
            if enabled:
                self._spawn_due(session, now, sched.id)
            return schedule_view(session, sched, now, catalog_repo.item_names(session))

    def delete_world_boss_schedule(self, actor: str, schedule_id: int) -> dict:
        """刪除排程；尚未結束的那一場一併結束（管理員結束），已產生的活動與紀錄保留（schedule_id 設為 NULL）。"""
        now = self._clock.now()
        with self._transaction() as session:
            sched = self._schedule_for_update(session, schedule_id)
            for ev in session.scalars(select(WorldBossEvent).where(
                    WorldBossEvent.schedule_id == sched.id, WorldBossEvent.status != "ended").with_for_update()):
                self._advance(session, ev, now)
                if ev.status != "ended":
                    self._boss_finish(ev, "admin", now, now)
            session.flush()
            self._schedule_audit(session, actor, "world_boss_schedule_delete", sched)
            session.delete(sched)
            return {"deleted": schedule_id}
