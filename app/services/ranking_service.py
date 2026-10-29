"""群組總排行與排名獎勵（每日／每週／每月結算）。

結算規則：
- 只結算「已結束超過緩衝時間」且結束時間晚於規則啟用時間（active_since）的期間；停用期間或修改週期前的期間不回溯補發。
- 每個期間在單一交易中完成：鎖定規則、寫入結算快照與每筆獎勵、發放金幣或道具、寫入交易紀錄。
  (rule_id, period_start) 與 (settlement_id, player_id) 唯一，重複呼叫或並行結算都不會重複發獎。
- 群組排名的獎勵發給結算當下「主要所屬群組為該群組且仍是成員」的所有玩家。
- 只有分數大於 0 才能得獎。道具獎勵直接放入背包，不受倉庫容量限制（避免自動結算時遺失獎勵）。
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from app.db.models import Farm, Player, RankingAward, RankingRewardRule, RankingSettlement
from app.game import balance
from app.game import ranking as ranking_rules
from app.game.errors import NotFoundError, ValidationError
from app.game.rules import EventType
from app.repositories import community as community_repo
from app.repositories import inventory as inventory_repo
from app.repositories import leaderboard as leaderboard_repo
from app.services.community_service import CommunityService

LEADERBOARD_LIMIT = 100
# 背景結算的 PostgreSQL advisory lock 鍵（任意固定值；所有程序相同）
SETTLEMENT_LOCK_KEY = 0x4641524D_0001


def rule_view(rule: RankingRewardRule, now: datetime) -> dict:
    start, end = ranking_rules.period_bounds(rule.period, now)
    return {
        "id": rule.id,
        "name": rule.name,
        "scope": rule.scope,
        "scope_label": "群組" if rule.scope == "group" else "個人",
        "metric": rule.metric,
        "metric_label": ranking_rules.METRICS[rule.metric],
        "period": rule.period,
        "period_label": ranking_rules.PERIODS[rule.period],
        "rewards": rule.rewards,
        "rewards_text": "；".join(
            f"第 {i} 名 {ranking_rules.reward_text(r)}" for i, r in enumerate(rule.rewards, start=1)
        ),
        "enabled": rule.enabled,
        "active_since": rule.active_since,
        "current_period_start": start,
        "current_period_end": end,
        "next_settle_at": end + timedelta(seconds=balance.RANKING_SETTLE_GRACE_SECONDS),
        "created_by": rule.created_by,
        "updated_at": rule.updated_at,
    }


def settlement_view(s: RankingSettlement, award_count: int | None = None) -> dict:
    snap = s.rule_snapshot
    return {
        "id": s.id,
        "rule_id": s.rule_id,
        "rule_name": snap["name"],
        "scope": snap["scope"],
        "metric": snap["metric"],
        "period": snap["period"],
        "summary": f"{'群組' if snap['scope'] == 'group' else '個人'}｜{ranking_rules.METRICS[snap['metric']]}"
        f"｜{ranking_rules.period_label(snap['period'], s.period_start)}",
        "period_start": s.period_start,
        "period_end": s.period_end,
        "ranking": s.ranking,
        "settled_by": s.settled_by,
        "settled_at": s.settled_at,
        "award_count": award_count,
    }


def settlement_detail(session, s: RankingSettlement) -> dict:
    awards = session.scalars(
        select(RankingAward)
        .where(RankingAward.settlement_id == s.id)
        .order_by(RankingAward.rank, RankingAward.player_id)
    )
    view = settlement_view(s)
    view["awards"] = [
        {
            "player_id": a.player_id,
            "player_name": a.player_name,
            "group_id": a.group_id,
            "rank": a.rank,
            "reward_text": ranking_rules.reward_text({"reward_type": a.reward_type, "amount": a.amount}),
        }
        for a in awards
    ]
    view["award_count"] = len(view["awards"])
    return view


class RankingService(CommunityService):
    # ---------- 排行榜（含群組總排行與期間） ----------

    @staticmethod
    def view_bounds(view_period: str, now: datetime) -> tuple[datetime | None, datetime | None]:
        if view_period == "all":
            return None, None
        if view_period not in ranking_rules.VIEW_TO_PERIOD:
            raise ValidationError("期間只能是 all、day、week 或 month。")
        return ranking_rules.period_bounds(ranking_rules.VIEW_TO_PERIOD[view_period], now)

    def group_leaderboard(self, kind: str, view_period: str = "all", limit: int = 10) -> list[dict]:
        """群組總排行：各群組成員加總後排名（所有玩家都可查看）。"""
        if kind not in leaderboard_repo.KINDS:
            raise ValidationError("排行榜種類只能是 money、sales 或 goods。")
        since, until = self.view_bounds(view_period, self._clock.now())
        with self._transaction() as session:
            return leaderboard_repo.group_ranking(session, kind, max(1, min(limit, LEADERBOARD_LIMIT)), since, until)

    def period_leaderboard(self, kind: str, view_period: str, limit: int = 10) -> list[dict]:
        """個人總排行（可指定本日／本週／本月）。"""
        if kind not in leaderboard_repo.KINDS:
            raise ValidationError("排行榜種類只能是 money、sales 或 goods。")
        since, until = self.view_bounds(view_period, self._clock.now())
        with self._transaction() as session:
            return leaderboard_repo.ranking(session, kind, None, max(1, min(limit, LEADERBOARD_LIMIT)), since, until)

    # ---------- 規則管理（由管理 API 呼叫） ----------

    def _rule_for_update(self, session, rule_id: int) -> RankingRewardRule:
        rule = session.scalars(
            select(RankingRewardRule).where(RankingRewardRule.id == rule_id).with_for_update()
        ).first()
        if rule is None:
            raise NotFoundError(f"找不到排名獎勵規則 #{rule_id}。")
        return rule

    @staticmethod
    def _rule_audit(rule: RankingRewardRule) -> dict:
        return {k: getattr(rule, k) for k in ("name", "scope", "metric", "period", "rewards", "enabled")}

    def list_ranking_rules(self) -> list[dict]:
        now = self._clock.now()
        with self._transaction() as session:
            rules = session.scalars(select(RankingRewardRule).order_by(RankingRewardRule.id))
            return [rule_view(r, now) for r in rules]

    def create_ranking_rule(self, actor: str, raw: dict) -> dict:
        values = ranking_rules.validate_rule(raw)
        now = self._clock.now()
        with self._transaction() as session:
            rule = RankingRewardRule(**values, active_since=now, created_by=actor, created_at=now, updated_at=now)
            session.add(rule)
            session.flush()
            community_repo.add_admin_action(
                session, actor, "create_ranking_rule", "ranking_rule", rule.id, self._rule_audit(rule), now
            )
            return rule_view(rule, now)

    def update_ranking_rule(self, actor: str, rule_id: int, raw: dict) -> dict:
        """修改後的獎勵用於之後結算的期間；變更週期或從停用改為啟用時，從現在起算，不回溯補發。"""
        values = ranking_rules.validate_rule(raw)
        now = self._clock.now()
        with self._transaction() as session:
            rule = self._rule_for_update(session, rule_id)
            before = self._rule_audit(rule)
            if values["period"] != rule.period or (values["enabled"] and not rule.enabled):
                rule.active_since = now
            for k, v in values.items():
                setattr(rule, k, v)
            rule.updated_at = now
            community_repo.add_admin_action(
                session, actor, "update_ranking_rule", "ranking_rule", rule.id,
                {"before": before, "after": self._rule_audit(rule)}, now,
            )
            return rule_view(rule, now)

    def set_ranking_rule_enabled(self, actor: str, rule_id: int, enabled: bool) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            rule = self._rule_for_update(session, rule_id)
            if rule.enabled != enabled:
                rule.enabled = enabled
                rule.updated_at = now
                if enabled:
                    rule.active_since = now  # 停用期間的期間不補發
                action = "enable_ranking_rule" if enabled else "disable_ranking_rule"
                community_repo.add_admin_action(
                    session, actor, action, "ranking_rule", rule.id, {"name": rule.name}, now
                )
            return rule_view(rule, now)

    def delete_ranking_rule(self, actor: str, rule_id: int) -> dict:
        """刪除規則；已結算的歷史與發獎紀錄保留（以規則快照顯示）。"""
        now = self._clock.now()
        with self._transaction() as session:
            rule = self._rule_for_update(session, rule_id)
            snapshot = self._rule_audit(rule)
            session.delete(rule)
            community_repo.add_admin_action(
                session, actor, "delete_ranking_rule", "ranking_rule", rule_id, snapshot, now
            )
            return {"deleted": rule_id}

    # ---------- 結算 ----------

    def _due_periods(self, rule: RankingRewardRule, cutoff: datetime, settled: set) -> list[tuple]:
        """規則啟用後已結束（且過了緩衝時間）但尚未結算的期間，由舊到新。"""
        periods = []
        start, end = ranking_rules.period_bounds(rule.period, rule.active_since)
        for _ in range(balance.RANKING_MAX_CATCHUP_PERIODS):
            if end > cutoff:
                break
            if start not in settled and end > rule.active_since:
                periods.append((start, end))
            start, end = ranking_rules.period_bounds(rule.period, end)
        return periods

    def run_background_settlement(self, actor: str = "system") -> dict | None:
        """背景結算排名獎勵與競賽。以 session 層級 advisory lock 確保同一時間只有一個程序在結算；
        取不到鎖時回傳 None（另一個程序正在結算）。即使沒有鎖，唯一限制也保證不會重複發獎。"""
        # 使用獨立的 Connection 持有鎖（Session commit 會把連線還給連線池，不能用來持有 session 層級的鎖）；
        # 程序中斷或連線斷開時 PostgreSQL 會自動釋放鎖
        with self._session_factory.kw["bind"].connect() as lock_conn:
            if not lock_conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": SETTLEMENT_LOCK_KEY}):
                lock_conn.rollback()
                return None
            lock_conn.commit()
            try:
                return {
                    "rankings": self.settle_due_rankings(actor),
                    "competitions": self.settle_due_competitions(actor),
                }
            finally:
                lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SETTLEMENT_LOCK_KEY})
                lock_conn.commit()

    def settle_due_rankings(self, actor: str = "system") -> list[int]:
        """結算所有到期的期間，回傳新建立的結算 ID；可重複呼叫，不會重複發獎。"""
        now = self._clock.now()
        cutoff = now - timedelta(seconds=balance.RANKING_SETTLE_GRACE_SECONDS)
        with self._transaction() as session:
            rules = list(session.scalars(select(RankingRewardRule).where(RankingRewardRule.enabled.is_(True))))
            plan = []
            for rule in rules:
                # 有時區的 datetime 依實際時間點比較，資料庫回傳 UTC 也能與台灣時間的期間開始比對
                settled = set(
                    session.scalars(select(RankingSettlement.period_start).where(RankingSettlement.rule_id == rule.id))
                )
                plan += [(rule.id, start, end) for start, end in self._due_periods(rule, cutoff, settled)]
        created = []
        for rule_id, start, end in plan:
            settlement_id = self._settle_period(rule_id, start, end, actor)
            if settlement_id is not None:
                created.append(settlement_id)
        return created

    def _settle_period(self, rule_id: int, start: datetime, end: datetime, actor: str) -> int | None:
        now = self._clock.now()
        with self._transaction() as session:
            rule = session.scalars(
                select(RankingRewardRule).where(RankingRewardRule.id == rule_id).with_for_update()
            ).first()
            if rule is None or not rule.enabled:
                return None
            exists = session.scalar(
                select(RankingSettlement.id).where(
                    RankingSettlement.rule_id == rule_id, RankingSettlement.period_start == start
                )
            )
            if exists is not None:
                return None
            limit = max(len(rule.rewards), 10)
            if rule.scope == "group":
                ranking = leaderboard_repo.group_ranking(session, rule.metric, limit, start, end)
            else:
                ranking = leaderboard_repo.ranking(session, rule.metric, None, limit, start, end)
            settlement = RankingSettlement(
                rule_id=rule.id,
                rule_snapshot=self._rule_audit(rule),
                period_start=start,
                period_end=end,
                ranking=ranking,
                settled_by=actor,
                settled_at=now,
            )
            session.add(settlement)
            session.flush()
            label = f"{rule.name}（{ranking_rules.period_label(rule.period, start)}）"
            paid = 0
            # 分數由高到低排序，只有分數 > 0 的前幾名得獎
            for row in ranking[: len(rule.rewards)]:
                if row["score"] <= 0:
                    break
                reward = rule.rewards[row["rank"] - 1]
                if rule.scope == "group":
                    recipients = community_repo.award_recipients(session, row["group_id"])
                    group_id = row["group_id"]
                else:
                    farm = session.scalars(
                        select(Farm).where(Farm.player_id == row["player_id"]).with_for_update()
                    ).first()
                    recipients = [(farm, row["player_name"])] if farm else []
                    group_id = row["home_group_id"]
                context = group_id if rule.scope == "group" else None
                for farm, player_name in recipients:
                    self._pay(
                        session, settlement, farm, player_name, group_id, context, row["rank"], reward, label, now
                    )
                    paid += 1
            community_repo.add_admin_action(
                session, actor, "settle_ranking", "ranking_settlement", settlement.id,
                {"rule_id": rule.id, "period_start": start.isoformat(), "awards": paid}, now,
            )
            return settlement.id

    def _pay(self, session, settlement, farm, player_name, group_id, context, rank, reward, label, now) -> None:
        session.add(
            RankingAward(
                settlement_id=settlement.id,
                player_id=farm.player_id,
                group_id=group_id,
                player_name=player_name,
                rank=rank,
                reward_type=reward["reward_type"],
                amount=reward["amount"],
                created_at=now,
            )
        )
        session.flush()
        text = ranking_rules.reward_text(reward)
        if reward["reward_type"] == "gold":
            farm.gold += reward["amount"]
            tx = {"gold_delta": reward["amount"]}
        else:
            inventory_repo.add(session, farm.id, reward["reward_type"], reward["amount"])
            tx = {"item_code": reward["reward_type"], "quantity": reward["amount"]}
        self._record(
            session, farm, EventType.RANKING_REWARD, f"排名獎勵：{label}第 {rank} 名，獲得 {text}",
            context_group_id=context,
            **tx,
        )

    # ---------- 查詢結算紀錄 ----------

    def ranking_settlements(self, limit: int = 30) -> list[dict]:
        with self._transaction() as session:
            counts = dict(
                session.execute(
                    select(RankingAward.settlement_id, func.count()).group_by(RankingAward.settlement_id)
                ).all()
            )
            rows = session.scalars(
                select(RankingSettlement).order_by(RankingSettlement.settled_at.desc(), RankingSettlement.id.desc())
                .limit(max(1, min(limit, 200)))
            )
            return [settlement_view(s, counts.get(s.id, 0)) for s in rows]

    def ranking_settlement_detail(self, settlement_id: int) -> dict:
        with self._transaction() as session:
            s = session.get(RankingSettlement, settlement_id)
            if s is None:
                raise NotFoundError(f"找不到結算 #{settlement_id}。")
            return settlement_detail(session, s)

    def player_ranking_awards(self, player_id: int, limit: int = 10) -> list[dict]:
        with self._transaction() as session:
            if session.get(Player, player_id) is None:
                raise NotFoundError(f"找不到玩家 #{player_id}。")
            rows = session.execute(
                select(RankingAward, RankingSettlement)
                .join(RankingSettlement, RankingSettlement.id == RankingAward.settlement_id)
                .where(RankingAward.player_id == player_id)
                .order_by(RankingAward.created_at.desc(), RankingAward.id.desc())
                .limit(limit)
            )
            return [
                {
                    "created_at": a.created_at,
                    "rule_name": s.rule_snapshot["name"],
                    "period": ranking_rules.period_label(s.rule_snapshot["period"], s.period_start),
                    "rank": a.rank,
                    "reward_text": ranking_rules.reward_text({"reward_type": a.reward_type, "amount": a.amount}),
                }
                for a, s in rows
            ]
