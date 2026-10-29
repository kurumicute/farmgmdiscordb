"""群組任務、競賽與管理紀錄的資料存取。"""

from datetime import datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (
    AdminAction,
    Competition,
    CompetitionAward,
    CompetitionContribution,
    CompetitionResult,
    Farm,
    Group,
    GroupMember,
    Player,
    Quest,
    QuestClaim,
    QuestContribution,
)

# ---------- 管理紀錄 ----------


def add_admin_action(
    session: Session, actor: str, action: str, target_type: str, target_id: int | None, details: dict, now: datetime
) -> None:
    session.add(
        AdminAction(
            actor=actor, action=action, target_type=target_type, target_id=target_id, details=details, created_at=now
        )
    )
    session.flush()


def list_admin_actions(session: Session, limit: int = 50) -> list[AdminAction]:
    stmt = select(AdminAction).order_by(AdminAction.created_at.desc(), AdminAction.id.desc()).limit(limit)
    return list(session.scalars(stmt))


# ---------- 任務 ----------


def get_quest(session: Session, quest_id: int, *, for_update: bool = False) -> Quest | None:
    stmt = select(Quest).where(Quest.id == quest_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def open_quests_for_event(session: Session, group_id: int, now: datetime) -> list[Quest]:
    """進行中、且事件時間落在期間內的任務；鎖定以序列化進度與完成判定。"""
    stmt = (
        select(Quest)
        .where(
            Quest.group_id == group_id,
            Quest.status == "active",
            Quest.starts_at <= now,
            Quest.ends_at > now,
        )
        .order_by(Quest.id)
        .with_for_update()
    )
    return list(session.scalars(stmt))


def add_quest_contribution(
    session: Session, quest_id: int, player_id: int, transaction_id: int, amount: int, now: datetime
) -> bool:
    """同一筆操作對同一任務只會寫入一次；回傳是否新增。"""
    stmt = (
        insert(QuestContribution)
        .values(quest_id=quest_id, player_id=player_id, transaction_id=transaction_id, amount=amount, created_at=now)
        .on_conflict_do_nothing(constraint="uq_quest_contributions_once")
        .returning(QuestContribution.id)
    )
    return session.execute(stmt).first() is not None


def list_quests(
    session: Session, *, group_ids: list[int] | None = None, statuses: tuple[str, ...] | None = None, limit: int = 50
) -> list[tuple[Quest, str]]:
    stmt = select(Quest, Group.name).join(Group, Group.id == Quest.group_id).order_by(Quest.id.desc()).limit(limit)
    if group_ids is not None:
        stmt = stmt.where(Quest.group_id.in_(group_ids))
    if statuses is not None:
        stmt = stmt.where(Quest.status.in_(statuses))
    return [(q, name) for q, name in session.execute(stmt)]


def quest_contributors(session: Session, quest_id: int) -> list[tuple[int, str, int]]:
    """(玩家ID, 名稱, 貢獻總量)，依貢獻由高至低。"""
    total = func.sum(QuestContribution.amount)
    stmt = (
        select(Player.id, Player.display_name, total)
        .join(Player, Player.id == QuestContribution.player_id)
        .where(QuestContribution.quest_id == quest_id)
        .group_by(Player.id, Player.display_name)
        .order_by(total.desc(), Player.id)
    )
    return [(pid, name, int(t)) for pid, name, t in session.execute(stmt)]


def player_quest_contribution(session: Session, quest_id: int, player_id: int) -> int:
    stmt = select(func.coalesce(func.sum(QuestContribution.amount), 0)).where(
        QuestContribution.quest_id == quest_id, QuestContribution.player_id == player_id
    )
    return int(session.scalar(stmt) or 0)


def quest_total_contribution(session: Session, quest_id: int) -> int:
    stmt = select(func.coalesce(func.sum(QuestContribution.amount), 0)).where(QuestContribution.quest_id == quest_id)
    return int(session.scalar(stmt) or 0)


def get_claim(session: Session, quest_id: int, player_id: int) -> QuestClaim | None:
    stmt = select(QuestClaim).where(QuestClaim.quest_id == quest_id, QuestClaim.player_id == player_id)
    return session.scalars(stmt).first()


def quest_claims(session: Session, quest_id: int) -> list[tuple[QuestClaim, str]]:
    stmt = (
        select(QuestClaim, Player.display_name)
        .join(Player, Player.id == QuestClaim.player_id)
        .where(QuestClaim.quest_id == quest_id)
        .order_by(QuestClaim.claimed_at, QuestClaim.id)
    )
    return [(c, name) for c, name in session.execute(stmt)]


# ---------- 競賽 ----------


def get_competition(session: Session, competition_id: int, *, for_update: bool = False) -> Competition | None:
    stmt = select(Competition).where(Competition.id == competition_id)
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalars(stmt).first()


def open_competitions_for_event(session: Session, now: datetime) -> list[Competition]:
    stmt = select(Competition).where(
        Competition.status == "active", Competition.starts_at <= now, Competition.ends_at > now
    )
    return list(session.scalars(stmt))


def add_competition_contribution(
    session: Session, competition_id: int, group_id: int, player_id: int, transaction_id: int, points: int, now
) -> bool:
    stmt = (
        insert(CompetitionContribution)
        .values(
            competition_id=competition_id,
            group_id=group_id,
            player_id=player_id,
            transaction_id=transaction_id,
            points=points,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_competition_contributions_once")
        .returning(CompetitionContribution.id)
    )
    return session.execute(stmt).first() is not None


def list_competitions(
    session: Session, *, statuses: tuple[str, ...] | None = None, limit: int = 50
) -> list[Competition]:
    stmt = select(Competition).order_by(Competition.id.desc()).limit(limit)
    if statuses is not None:
        stmt = stmt.where(Competition.status.in_(statuses))
    return list(session.scalars(stmt))


def due_competitions(session: Session, cutoff: datetime) -> list[int]:
    """已截止且超過結算緩衝時間、尚未結算的競賽 ID。"""
    stmt = select(Competition.id).where(Competition.status == "active", Competition.ends_at <= cutoff)
    return list(session.scalars(stmt))


def live_ranking(session: Session, competition_id: int) -> list[dict]:
    """依貢獻計算的即時群組排名（同分依群組 ID）。"""
    score = func.sum(CompetitionContribution.points)
    stmt = (
        select(
            Group.id,
            Group.name,
            score,
            func.count(distinct(CompetitionContribution.player_id)),
        )
        .join(Group, Group.id == CompetitionContribution.group_id)
        .where(CompetitionContribution.competition_id == competition_id)
        .group_by(Group.id, Group.name)
        .order_by(score.desc(), Group.id)
    )
    return [
        {"rank": i, "group_id": gid, "group_name": name, "score": int(s), "contributors": c}
        for i, (gid, name, s, c) in enumerate(session.execute(stmt), start=1)
    ]


def final_ranking(session: Session, competition_id: int) -> list[dict]:
    stmt = (
        select(CompetitionResult)
        .where(CompetitionResult.competition_id == competition_id)
        .order_by(CompetitionResult.rank)
    )
    return [
        {
            "rank": r.rank,
            "group_id": r.group_id,
            "group_name": r.group_name,
            "score": r.score,
            "contributors": r.contributors,
        }
        for r in session.scalars(stmt)
    ]


def competition_player_points(session: Session, competition_id: int, limit: int = 20) -> list[dict]:
    total = func.sum(CompetitionContribution.points)
    stmt = (
        select(Player.id, Player.display_name, Group.id, Group.name, total)
        .join(Player, Player.id == CompetitionContribution.player_id)
        .join(Group, Group.id == CompetitionContribution.group_id)
        .where(CompetitionContribution.competition_id == competition_id)
        .group_by(Player.id, Player.display_name, Group.id, Group.name)
        .order_by(total.desc(), Player.id)
        .limit(limit)
    )
    return [
        {"player_id": pid, "player_name": pname, "group_id": gid, "group_name": gname, "points": int(t)}
        for pid, pname, gid, gname, t in session.execute(stmt)
    ]


def player_competition_points(session: Session, competition_id: int, player_id: int) -> int:
    stmt = select(func.coalesce(func.sum(CompetitionContribution.points), 0)).where(
        CompetitionContribution.competition_id == competition_id, CompetitionContribution.player_id == player_id
    )
    return int(session.scalar(stmt) or 0)


def award_recipients(session: Session, group_id: int) -> list[tuple[Farm, str]]:
    """結算當下的群組成員中，農場主要所屬群組就是該群組的玩家（每人只屬於一個主要群組，不會重複領獎）。"""
    stmt = (
        select(Farm, Player.display_name)
        .join(Player, Player.id == Farm.player_id)
        .join(GroupMember, GroupMember.player_id == Player.id)
        .where(GroupMember.group_id == group_id, Farm.home_group_id == group_id)
        .order_by(Farm.id)
        .with_for_update(of=Farm)
    )
    return [(f, name) for f, name in session.execute(stmt)]


def add_results(session: Session, competition_id: int, ranking: list[dict]) -> None:
    session.add_all(
        CompetitionResult(
            competition_id=competition_id,
            group_id=r["group_id"],
            group_name=r["group_name"],
            rank=r["rank"],
            score=r["score"],
            contributors=r["contributors"],
        )
        for r in ranking
    )
    session.flush()


def add_award(session: Session, **values) -> bool:
    stmt = (
        insert(CompetitionAward)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["competition_id", "player_id"])
        .returning(CompetitionAward.player_id)
    )
    return session.execute(stmt).first() is not None


def competition_awards(session: Session, competition_id: int) -> list[CompetitionAward]:
    stmt = (
        select(CompetitionAward)
        .where(CompetitionAward.competition_id == competition_id)
        .order_by(CompetitionAward.rank, CompetitionAward.player_id)
    )
    return list(session.scalars(stmt))


def player_award(session: Session, competition_id: int, player_id: int) -> CompetitionAward | None:
    return session.get(CompetitionAward, (competition_id, player_id))
