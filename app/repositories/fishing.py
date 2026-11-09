"""釣魚資料存取：魚種、釣具、玩家釣具、設定版本、釣魚 Session、魚簍、分享與統計。全部使用 SQLAlchemy（參數化查詢）。"""

from datetime import datetime

from sqlalchemy import and_, case, distinct, func, or_, select, update
from sqlalchemy.orm import Session

from app.db import catalog_cache
from app.db.models import (
    Farm,
    FarmFish,
    FarmFishingGear,
    FishingGear,
    FishingGuard,
    FishingSession,
    FishingSetting,
    FishShare,
    FishType,
    GameTransaction,
    GroupMember,
    Player,
)
from app.game import fishing

# ---------- 設定 ----------


def current_settings(session: Session) -> tuple[int, dict]:
    """回傳 (版本, 設定)；資料庫沒有設定時使用預設值（版本 0）。"""
    row = session.scalars(select(FishingSetting).order_by(FishingSetting.version.desc()).limit(1)).first()
    if row is None:
        return 0, fishing.DEFAULT_SETTINGS
    return row.version, row.settings


def add_settings_version(session: Session, settings: dict, actor: str, now: datetime) -> int:
    latest = session.scalar(select(func.coalesce(func.max(FishingSetting.version), 0)))
    version = latest + 1
    session.add(FishingSetting(version=version, settings=settings, created_by=actor, created_at=now))
    session.flush()
    return version


def settings_history(session: Session, limit: int = 10) -> list[FishingSetting]:
    return list(session.scalars(select(FishingSetting).order_by(FishingSetting.version.desc()).limit(limit)))


# ---------- 魚種與釣具目錄 ----------


def list_fish_types(session: Session, *, active_only: bool = False) -> list[FishType]:
    stmt = select(FishType).order_by(FishType.sort_order, FishType.id)
    if active_only:
        stmt = stmt.where(FishType.active)
    return catalog_cache.rows(session, ("fish", active_only), lambda: list(session.scalars(stmt)))


def get_guard(session: Session, farm_id: int) -> FishingGuard | None:
    """呼叫端必須已鎖定農場列（同一農場的防腳本狀態依序更新）。"""
    return session.get(FishingGuard, farm_id)


def get_fish_type(session: Session, code: str, *, for_update: bool = False) -> FishType | None:
    stmt = select(FishType).where(FishType.code == code)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def fish_type_names(session: Session) -> set[str]:
    return set(session.scalars(select(FishType.name)))


def list_gear(session: Session, *, active_only: bool = False) -> list[FishingGear]:
    kind_order = case({"rod": 1, "line": 2, "bait": 3}, value=FishingGear.kind)
    stmt = select(FishingGear).order_by(kind_order, FishingGear.sort_order, FishingGear.id)
    if active_only:
        stmt = stmt.where(FishingGear.active)
    return list(session.scalars(stmt))


def get_gear(session: Session, code: str, *, for_update: bool = False) -> FishingGear | None:
    stmt = select(FishingGear).where(FishingGear.code == code)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


# ---------- 玩家釣具 ----------


def owned_gear(session: Session, farm_id: int) -> list[FarmFishingGear]:
    return list(session.scalars(select(FarmFishingGear).where(FarmFishingGear.farm_id == farm_id)))


def get_owned(session: Session, farm_id: int, code: str, *, for_update: bool = False) -> FarmFishingGear | None:
    stmt = select(FarmFishingGear).where(FarmFishingGear.farm_id == farm_id, FarmFishingGear.gear_code == code)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def add_owned(session: Session, farm_id: int, gear: FishingGear, quantity: int, now: datetime,
              equipped: bool) -> FarmFishingGear:
    row = FarmFishingGear(farm_id=farm_id, gear_code=gear.code, kind=gear.kind, quantity=quantity,
                          equipped=equipped, acquired_at=now)
    session.add(row)
    session.flush()
    return row


def unequip_kind(session: Session, farm_id: int, kind: str) -> None:
    session.execute(
        update(FarmFishingGear)
        .where(FarmFishingGear.farm_id == farm_id, FarmFishingGear.kind == kind, FarmFishingGear.equipped)
        .values(equipped=False)
    )
    session.flush()


def equipped_gear(session: Session, farm_id: int) -> dict[str, tuple[FarmFishingGear, FishingGear]]:
    stmt = (
        select(FarmFishingGear, FishingGear)
        .join(FishingGear, FishingGear.code == FarmFishingGear.gear_code)
        .where(FarmFishingGear.farm_id == farm_id, FarmFishingGear.equipped)
    )
    return {owned.kind: (owned, gear) for owned, gear in session.execute(stmt)}


# ---------- 釣魚 Session ----------


def active_session(session: Session, farm_id: int, *, for_update: bool = False) -> FishingSession | None:
    stmt = select(FishingSession).where(
        FishingSession.farm_id == farm_id, FishingSession.status.in_(fishing.ACTIVE_STATUSES)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def last_session(session: Session, farm_id: int) -> FishingSession | None:
    stmt = (
        select(FishingSession).where(FishingSession.farm_id == farm_id)
        .order_by(FishingSession.started_at.desc(), FishingSession.id.desc()).limit(1)
    )
    return session.scalars(stmt).first()


def stale_active_sessions(session: Session, before: datetime, limit: int = 500) -> list[FishingSession]:
    """已超過期限仍是進行中狀態的 Session（Bot 重啟後清理用）。"""
    stmt = (
        select(FishingSession)
        .where(
            FishingSession.status.in_(fishing.ACTIVE_STATUSES),
            or_(
                and_(FishingSession.status == "casting", FishingSession.react_deadline < before),
                and_(FishingSession.status == "fighting", FishingSession.action_deadline < before),
            ),
        )
        .order_by(FishingSession.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def session_fish(session: Session, session_id: int) -> FarmFish | None:
    return session.scalars(select(FarmFish).where(FarmFish.session_id == session_id)).first()


def player_session_stats(session: Session, farm_id: int) -> dict:
    row = session.execute(
        select(
            func.count(),
            func.count().filter(FishingSession.status == "landed"),
            func.count().filter(FishingSession.status.in_(fishing.FAIL_STATUSES)),
            func.count().filter(and_(FishingSession.status == "landed", FishingSession.rarity == "legendary")),
        ).where(FishingSession.farm_id == farm_id)
    ).one()
    return {"sessions": row[0], "landed": row[1], "failed": row[2], "legendary": row[3]}


# ---------- 魚簍 ----------


def in_basket() -> tuple:
    """SQL 條件：還在魚簍中的魚（沒有賣掉，也沒有捐給世界 Boss）。"""
    return FarmFish.sold_at.is_(None), FarmFish.donated_at.is_(None)


def basket(session: Session, farm_id: int) -> list[tuple[FarmFish, FishType]]:
    stmt = (
        select(FarmFish, FishType)
        .join(FishType, FishType.code == FarmFish.fish_code)
        .where(FarmFish.farm_id == farm_id, *in_basket())
        .order_by(FarmFish.price.desc(), FarmFish.weight_g.desc(), FarmFish.id)
    )
    return [(fish, ftype) for fish, ftype in session.execute(stmt)]


def basket_count(session: Session, farm_id: int) -> int:
    return session.scalar(
        select(func.count()).select_from(FarmFish).where(FarmFish.farm_id == farm_id, *in_basket())
    )


def get_farm_fish(session: Session, farm_id: int, fish_id: int) -> tuple[FarmFish, FishType] | None:
    """只回傳這座農場自己的魚（防止查詢或操作別人的魚）。"""
    stmt = (
        select(FarmFish, FishType)
        .join(FishType, FishType.code == FarmFish.fish_code)
        .where(FarmFish.id == fish_id, FarmFish.farm_id == farm_id)
    )
    row = session.execute(stmt).first()
    return (row[0], row[1]) if row else None


def lock_unsold(session: Session, farm_id: int, fish_ids: list[int] | None) -> list[FarmFish]:
    """鎖定這座農場尚未販售的魚；fish_ids 為 None 表示全部。"""
    stmt = select(FarmFish).where(FarmFish.farm_id == farm_id, *in_basket())
    if fish_ids is not None:
        stmt = stmt.where(FarmFish.id.in_(fish_ids))
    return list(session.scalars(stmt.order_by(FarmFish.id).with_for_update()))


def best_weights(session: Session, farm_id: int) -> dict[str, int]:
    """這座農場每種魚的最大捕獲重量（含已販售的魚）。"""
    stmt = select(FarmFish.fish_code, func.max(FarmFish.weight_g)).where(FarmFish.farm_id == farm_id).group_by(
        FarmFish.fish_code
    )
    return {code: int(w) for code, w in session.execute(stmt)}


def codex(session: Session, farm_id: int) -> dict[str, dict]:
    stmt = (
        select(FarmFish.fish_code, func.count(), func.max(FarmFish.weight_g), func.max(FarmFish.price),
               func.min(FarmFish.caught_at))
        .where(FarmFish.farm_id == farm_id)
        .group_by(FarmFish.fish_code)
    )
    return {
        code: {"count": n, "max_weight_g": int(w), "max_price": int(p), "first_caught_at": first}
        for code, n, w, p, first in session.execute(stmt)
    }


def add_share(session: Session, fish_id: int, farm_id: int, group_id: int | None, now: datetime) -> FishShare:
    share = FishShare(fish_id=fish_id, farm_id=farm_id, group_id=group_id, shared_at=now)
    session.add(share)
    session.flush()
    return share


def last_share_at(session: Session, fish_id: int) -> datetime | None:
    return session.scalar(select(func.max(FishShare.shared_at)).where(FishShare.fish_id == fish_id))


# ---------- 排行榜 ----------


def leaderboard(session: Session, group_id: int | None, limit: int = 10) -> dict:
    """依捕獲數與最高價值的魚排名；group_id 有值時只列該群組成員。"""

    def scoped(stmt):
        if group_id is None:
            return stmt
        return stmt.join(GroupMember, and_(GroupMember.player_id == Farm.player_id, GroupMember.group_id == group_id))

    catches = scoped(
        select(Player.display_name, func.count(FarmFish.id),
               func.count(FarmFish.id).filter(FarmFish.rarity == "legendary"))
        .select_from(FarmFish)
        .join(Farm, Farm.id == FarmFish.farm_id)
        .join(Player, Player.id == Farm.player_id)
    ).group_by(Player.id, Player.display_name).order_by(func.count(FarmFish.id).desc(), Player.id).limit(limit)
    ranked = (
        select(FarmFish.farm_id, FarmFish.fish_code, FarmFish.weight_g, FarmFish.price, FarmFish.rarity,
               func.row_number().over(partition_by=FarmFish.farm_id,
                                      order_by=(FarmFish.price.desc(), FarmFish.weight_g.desc(), FarmFish.id))
               .label("rn"))
        .subquery()
    )
    best = scoped(
        select(Player.display_name, FishType.name, FishType.emoji, ranked.c.rarity, ranked.c.weight_g,
               ranked.c.price)
        .select_from(ranked)
        .join(Farm, Farm.id == ranked.c.farm_id)
        .join(Player, Player.id == Farm.player_id)
        .join(FishType, FishType.code == ranked.c.fish_code)
        .where(ranked.c.rn == 1)
    ).order_by(ranked.c.price.desc(), ranked.c.weight_g.desc()).limit(limit)
    return {
        "catches": [{"player_name": n, "count": c, "legendary": lg} for n, c, lg in session.execute(catches)],
        "best": [
            {"player_name": n, "fish_name": f, "emoji": e, "rarity": r, "weight_g": w, "price": p}
            for n, f, e, r, w, p in session.execute(best)
        ],
    }


# ---------- 監控統計 ----------


def overview(session: Session, now: datetime) -> dict:
    s = session.execute(
        select(
            func.count(),
            func.count().filter(FishingSession.status == "landed"),
            func.count().filter(FishingSession.status.in_(fishing.FAIL_STATUSES)),
            func.count().filter(FishingSession.status.in_(fishing.ACTIVE_STATUSES)),
            func.count(distinct(FishingSession.farm_id)),
        )
    ).one()
    by_rarity = {
        r: {"sessions": n, "landed": landed}
        for r, n, landed in session.execute(
            select(FishingSession.rarity, func.count(), func.count().filter(FishingSession.status == "landed"))
            .where(FishingSession.status.not_in(fishing.ACTIVE_STATUSES))
            .group_by(FishingSession.rarity)
        )
    }
    by_status = {st: n for st, n in session.execute(
        select(FishingSession.status, func.count()).group_by(FishingSession.status))}
    sold = session.execute(
        select(func.count(), func.coalesce(func.sum(GameTransaction.gold_delta), 0), func.coalesce(
            func.sum(GameTransaction.quantity), 0))
        .where(GameTransaction.event_type == "fish_sell")
    ).one()
    gear = session.execute(
        select(func.count(), func.coalesce(func.sum(-GameTransaction.gold_delta), 0))
        .where(GameTransaction.event_type == "fishing_buy")
    ).one()
    legendary = session.scalar(select(func.count()).select_from(FarmFish).where(FarmFish.rarity == "legendary"))
    shares = session.scalar(select(func.count()).select_from(FishShare))
    in_baskets = session.scalar(select(func.count()).select_from(FarmFish).where(*in_basket()))
    return {
        "sessions": s[0], "landed": s[1], "failed": s[2], "active": s[3], "players": s[4],
        "success_rate": round(s[1] * 100 / (s[0] - s[3]), 1) if s[0] - s[3] else None,
        "legendary_caught": legendary, "fish_sold": int(sold[2]), "sell_transactions": sold[0],
        "sell_gold": int(sold[1]), "gear_spent": int(gear[1]), "shares": shares, "in_baskets": in_baskets,
        "by_rarity": by_rarity, "by_status": by_status,
    }


def player_rows(session: Session, q: str | None, limit: int, offset: int) -> tuple[int, list[dict]]:
    """玩家釣魚統計（有釣魚紀錄或持有釣具的玩家），支援名稱／ID 搜尋與分頁。"""
    sess = (
        select(
            FishingSession.farm_id.label("farm_id"),
            func.count().label("sessions"),
            func.count().filter(FishingSession.status == "landed").label("landed"),
            func.count().filter(FishingSession.status.in_(fishing.FAIL_STATUSES)).label("failed"),
            func.max(FishingSession.started_at).label("last_at"),
        ).group_by(FishingSession.farm_id).subquery()
    )
    fish = (
        select(
            FarmFish.farm_id.label("farm_id"),
            func.count().filter(FarmFish.rarity == "legendary").label("legendary"),
            func.count().filter(FarmFish.sold_at.is_not(None)).label("sold"),
            func.coalesce(func.sum(FarmFish.sold_price), 0).label("sold_gold"),
            func.count().filter(*in_basket()).label("basket"),
        ).group_by(FarmFish.farm_id).subquery()
    )
    shares = select(FishShare.farm_id.label("farm_id"), func.count().label("shares")).group_by(
        FishShare.farm_id).subquery()
    gear_farms = select(FarmFishingGear.farm_id).distinct().subquery()
    base = (
        select(
            Player.id, Player.display_name, Player.external_platform, Player.external_user_id, Farm.id,
            func.coalesce(sess.c.sessions, 0), func.coalesce(sess.c.landed, 0), func.coalesce(sess.c.failed, 0),
            func.coalesce(fish.c.legendary, 0), func.coalesce(fish.c.sold, 0), func.coalesce(fish.c.sold_gold, 0),
            func.coalesce(fish.c.basket, 0), func.coalesce(shares.c.shares, 0), sess.c.last_at,
        )
        .select_from(Farm)
        .join(Player, Player.id == Farm.player_id)
        .outerjoin(sess, sess.c.farm_id == Farm.id)
        .outerjoin(fish, fish.c.farm_id == Farm.id)
        .outerjoin(shares, shares.c.farm_id == Farm.id)
        .where(or_(sess.c.farm_id.is_not(None), Farm.id.in_(select(gear_farms.c.farm_id))))
    )
    if q:
        cond = Player.display_name.ilike(f"%{_escape_like(q)}%", escape="\\")
        if q.lstrip("#").isdigit() and len(q.lstrip("#")) <= 18:
            cond = or_(cond, Player.id == int(q.lstrip("#")), Player.external_user_id == q.lstrip("#"))
        base = base.where(cond)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.execute(
        base.order_by(func.coalesce(sess.c.sessions, 0).desc(), Player.id).limit(limit).offset(offset)
    ).all()
    farm_ids = [r[4] for r in rows]
    equipped: dict[int, dict[str, str]] = {}
    if farm_ids:
        for farm_id, kind, name, qty in session.execute(
            select(FarmFishingGear.farm_id, FarmFishingGear.kind, FishingGear.name, FarmFishingGear.quantity)
            .join(FishingGear, FishingGear.code == FarmFishingGear.gear_code)
            .where(FarmFishingGear.farm_id.in_(farm_ids), FarmFishingGear.equipped)
        ):
            equipped.setdefault(farm_id, {})[kind] = f"{name}×{qty}" if kind == "bait" else name
    out = []
    for r in rows:
        out.append({
            "player_id": r[0], "player_name": r[1],
            "discord_user_id": r[3] if r[2] == "discord" else None, "farm_id": r[4],
            "sessions": r[5], "landed": r[6], "failed": r[7], "legendary": r[8], "sold": r[9],
            "sold_gold": int(r[10]), "basket": r[11], "shares": r[12], "last_at": r[13],
            "equipped": equipped.get(r[4], {}),
        })
    return total, out


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def player_detail(session: Session, farm_id: int, limit: int = 30) -> dict:
    catches = session.execute(
        select(FarmFish, FishType.name, FishType.emoji)
        .join(FishType, FishType.code == FarmFish.fish_code)
        .where(FarmFish.farm_id == farm_id)
        .order_by(FarmFish.caught_at.desc(), FarmFish.id.desc())
        .limit(limit)
    ).all()
    sessions = list(session.scalars(
        select(FishingSession).where(FishingSession.farm_id == farm_id)
        .order_by(FishingSession.started_at.desc(), FishingSession.id.desc()).limit(limit)
    ))
    sales = list(session.scalars(
        select(GameTransaction)
        .where(GameTransaction.farm_id == farm_id, GameTransaction.event_type.in_(("fish_sell", "fishing_buy")))
        .order_by(GameTransaction.created_at.desc(), GameTransaction.id.desc()).limit(limit)
    ))
    shares = session.execute(
        select(FishShare.shared_at, FishShare.group_id, FishType.name, FarmFish.weight_g, FarmFish.rarity)
        .join(FarmFish, FarmFish.id == FishShare.fish_id)
        .join(FishType, FishType.code == FarmFish.fish_code)
        .where(FishShare.farm_id == farm_id)
        .order_by(FishShare.shared_at.desc()).limit(limit)
    ).all()
    gear = session.execute(
        select(FarmFishingGear, FishingGear.name)
        .join(FishingGear, FishingGear.code == FarmFishingGear.gear_code)
        .where(FarmFishingGear.farm_id == farm_id)
        .order_by(FarmFishingGear.kind, FarmFishingGear.id)
    ).all()
    return {
        "catches": [
            {"id": f.id, "fish_code": f.fish_code, "name": name, "emoji": emoji, "rarity": f.rarity,
             "weight_g": f.weight_g, "price": f.price, "caught_at": f.caught_at, "sold_at": f.sold_at,
             "sold_price": f.sold_price}
            for f, name, emoji in catches
        ],
        "sessions": [
            {"id": s.id, "status": s.status, "status_name": fishing.STATUS_NAMES.get(s.status, s.status),
             "rarity": s.rarity, "fish_code": s.fish_code, "turns": s.turns, "started_at": s.started_at,
             "ended_at": s.ended_at, "settings_version": s.settings_version,
             "rod": (s.gear.get("rod") or {}).get("name"), "line": (s.gear.get("line") or {}).get("name"),
             "bait": (s.gear.get("bait") or {}).get("name")}
            for s in sessions
        ],
        "transactions": [
            {"event_type": t.event_type, "description": t.description, "gold_delta": t.gold_delta,
             "quantity": t.quantity, "created_at": t.created_at}
            for t in sales
        ],
        "shares": [
            {"shared_at": at, "group_id": gid, "fish_name": name, "weight_g": w, "rarity": r}
            for at, gid, name, w, r in shares
        ],
        "gear": [
            {"code": g.gear_code, "name": name, "kind": g.kind, "quantity": g.quantity, "equipped": g.equipped}
            for g, name in gear
        ],
    }


def gear_usage(session: Session) -> dict[str, int]:
    """每種釣具被多少座農場持有（管理網站顯示用）。"""
    return {code: n for code, n in session.execute(
        select(FarmFishingGear.gear_code, func.count()).group_by(FarmFishingGear.gear_code))}


def fish_counts(session: Session) -> dict[str, int]:
    return {code: n for code, n in session.execute(select(FarmFish.fish_code, func.count()).group_by(
        FarmFish.fish_code))}
