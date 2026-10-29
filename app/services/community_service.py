"""群組共同任務與群組競賽。

- 任務與競賽只能由管理員建立／取消／結算，所有管理操作都寫入 admin_actions。
- 玩家操作任務時必須明確傳入群組（CMD 為目前選擇的群組；未來 Discord 為事件發生時的 Guild），且必須是成員。
- 計分由 app.services.activity 在遊戲操作的同一筆交易中完成。
- 競賽在截止並超過緩衝時間後結算：保存群組排名快照並發放獎勵，結算只會發生一次。
"""

from datetime import datetime, timedelta

from app.db.models import Competition, Quest, QuestClaim
from app.game import balance, rules
from app.game.errors import (
    AlreadyExistsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import EventType
from app.repositories import animals as animals_repo
from app.repositories import catalog as catalog_repo
from app.repositories import community as community_repo
from app.repositories import farms as farms_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.services.dto import ActionResult
from app.services.ranch_service import item_name
from app.services.social_service import SocialService

NAME_MAX = 60
DESC_MAX = 300
REASON_MIN, REASON_MAX = 2, 200


def reward_label(reward_type: str, amount: int) -> str:
    return f"{amount} 金幣" if reward_type == "gold" else f"{amount} 個{item_name(reward_type)}"


def quest_view(quest, group_name: str, now: datetime, names: dict[str, str] | None = None) -> dict:
    state = rules.event_state(quest.status, quest.starts_at, quest.ends_at, now)
    type_name = balance.QUEST_TYPES[quest.quest_type][0]
    unit = "金幣" if quest.metric == "amount" else ("次" if quest.quest_type in ("build", "defense") else "個")
    goal = item_name(quest.item_filter, names) if quest.item_filter else ""
    mode = balance.QUEST_REWARD_MODES[quest.reward_mode]
    return {
        "quest_id": quest.id,
        "group_id": quest.group_id,
        "group_name": group_name,
        "name": quest.name,
        "description": quest.description,
        "quest_type": quest.quest_type,
        "type_name": type_name,
        "metric": quest.metric,
        "item_filter": quest.item_filter,
        "goal_text": f"{type_name}{('（' + goal + '）') if goal else ''} 達到 {quest.target} {unit}",
        "target": quest.target,
        "progress": quest.progress,
        "percent": min(100, quest.progress * 100 // quest.target),
        "state": state,
        "state_label": rules.EVENT_STATE_LABELS[state],
        "reward_mode": quest.reward_mode,
        "reward_type": quest.reward_type,
        "reward_amount": quest.reward_amount,
        "reward_text": f"{mode}：{reward_label(quest.reward_type, quest.reward_amount)}"
        + ("（獎池）" if quest.reward_mode == "proportional" else "／人"),
        "starts_at": quest.starts_at,
        "ends_at": quest.ends_at,
        "completed_at": quest.completed_at,
        "cancel_reason": quest.cancel_reason,
        "cancelled_at": quest.cancelled_at,
        "created_by": quest.created_by,
    }


def competition_view(comp, now: datetime) -> dict:
    state = rules.event_state(comp.status, comp.starts_at, comp.ends_at, now)
    name, rule = balance.COMPETITION_TYPES[comp.comp_type]
    return {
        "competition_id": comp.id,
        "name": comp.name,
        "description": comp.description,
        "comp_type": comp.comp_type,
        "type_name": name,
        "scoring_rule": rule,
        "rewards": list(comp.rewards),
        "starts_at": comp.starts_at,
        "ends_at": comp.ends_at,
        "state": state,
        "state_label": "待結算" if state == "expired" else rules.EVENT_STATE_LABELS[state],
        "settled_at": comp.settled_at,
        "cancel_reason": comp.cancel_reason,
        "cancelled_at": comp.cancelled_at,
        "created_by": comp.created_by,
    }


def _validate_text(value: str, label: str, max_len: int, *, required: bool = True) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValidationError(f"{label}不可為空白。")
    if len(text) > max_len:
        raise ValidationError(f"{label}最多 {max_len} 個字元。")
    if any(ord(ch) < 32 and ch not in "\n" for ch in text):
        raise ValidationError(f"{label}不可包含控制字元。")
    return text


def _validate_period(starts_at: datetime, ends_at: datetime, now: datetime) -> None:
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise ValidationError("開始與截止時間必須包含時區。")
    if ends_at <= starts_at:
        raise ValidationError("截止時間必須晚於開始時間。")
    if ends_at <= now:
        raise ValidationError("截止時間必須在未來。")
    if ends_at - starts_at > timedelta(days=balance.EVENT_MAX_DAYS):
        raise ValidationError(f"期間最長 {balance.EVENT_MAX_DAYS} 天。")


class CommunityService(SocialService):
    # ---------- 管理：任務 ----------

    def create_quest(
        self,
        actor: str,
        *,
        group_id: int,
        name: str,
        quest_type: str,
        target: int,
        reward_mode: str,
        reward_type: str,
        reward_amount: int,
        starts_at: datetime,
        ends_at: datetime,
        description: str = "",
        metric: str = "quantity",
        item_filter: str | None = None,
    ) -> dict:
        now = self._clock.now()
        name = _validate_text(name, "任務名稱", NAME_MAX)
        description = _validate_text(description, "任務說明", DESC_MAX, required=False)
        if quest_type not in balance.QUEST_TYPES:
            raise ValidationError("任務類型不正確。")
        if metric not in balance.QUEST_TYPES[quest_type][1]:
            raise ValidationError("此任務類型不支援該計量方式（只有共同販售可依金額計算）。")
        if item_filter is not None and quest_type != "animal":
            raise ValidationError("只有動物生產任務可指定產品。")
        if not 1 <= target <= balance.QUEST_MAX_TARGET:
            raise ValidationError(f"目標必須在 1–{balance.QUEST_MAX_TARGET} 之間。")
        if reward_mode not in balance.QUEST_REWARD_MODES:
            raise ValidationError("獎勵模式不正確。")
        if reward_type not in balance.QUEST_REWARD_TYPES:
            raise ValidationError("獎勵類型只能是金幣、化肥、蚯蚓或巡查券。")
        limit = balance.QUEST_MAX_GOLD_REWARD if reward_type == "gold" else balance.QUEST_MAX_ITEM_REWARD
        if not 1 <= reward_amount <= limit:
            raise ValidationError(f"獎勵數量必須在 1–{limit} 之間。")
        _validate_period(starts_at, ends_at, now)
        with self._transaction() as session:
            group = groups_repo.get(session, group_id)
            if group is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            if item_filter is not None:
                product = animals_repo.find_product(session, item_filter)
                if product is None:
                    raise ValidationError("指定的動物產品不存在。")
                item_filter = product.product_code
            quest = Quest(
                group_id=group_id,
                name=name,
                description=description,
                quest_type=quest_type,
                metric=metric,
                item_filter=item_filter,
                target=target,
                reward_mode=reward_mode,
                reward_type=reward_type,
                reward_amount=reward_amount,
                starts_at=starts_at,
                ends_at=ends_at,
                status="active",
                progress=0,
                created_by=actor,
                created_at=now,
            )
            session.add(quest)
            session.flush()
            view = quest_view(quest, group.name, now, catalog_repo.item_names(session))
            community_repo.add_admin_action(
                session,
                actor,
                "create_quest",
                "quest",
                quest.id,
                {k: str(v) if isinstance(v, datetime) else v for k, v in view.items()},
                now,
            )
            return view

    def cancel_quest(self, actor: str, quest_id: int, reason: str) -> dict:
        reason = _validate_text(reason, "取消原因", REASON_MAX)
        if len(reason) < REASON_MIN:
            raise ValidationError(f"取消原因至少 {REASON_MIN} 個字。")
        now = self._clock.now()
        with self._transaction() as session:
            quest = community_repo.get_quest(session, quest_id, for_update=True)
            if quest is None:
                raise NotFoundError(f"找不到任務 #{quest_id}。")
            if quest.status != "active":
                raise ValidationError(
                    f"任務 #{quest_id} 目前為「{rules.EVENT_STATE_LABELS[quest.status]}」，不能取消。"
                )
            quest.status = "cancelled"
            quest.cancelled_at = now
            quest.cancel_reason = reason
            community_repo.add_admin_action(
                session, actor, "cancel_quest", "quest", quest_id, {"reason": reason, "progress": quest.progress}, now
            )
            group_name = groups_repo.get(session, quest.group_id).name
            return quest_view(quest, group_name, now, catalog_repo.item_names(session))

    # ---------- 管理：競賽 ----------

    def create_competition(
        self,
        actor: str,
        *,
        name: str,
        comp_type: str,
        rewards: list[int],
        starts_at: datetime,
        ends_at: datetime,
        description: str = "",
    ) -> dict:
        now = self._clock.now()
        name = _validate_text(name, "競賽名稱", NAME_MAX)
        description = _validate_text(description, "競賽說明", DESC_MAX, required=False)
        if comp_type not in balance.COMPETITION_TYPES:
            raise ValidationError("競賽類型不正確。")
        if not 1 <= len(rewards) <= balance.COMPETITION_REWARD_RANKS:
            raise ValidationError(f"請設定 1–{balance.COMPETITION_REWARD_RANKS} 個名次的獎勵。")
        if any(
            not isinstance(r, int) or isinstance(r, bool) or not 0 <= r <= balance.COMPETITION_MAX_REWARD
            for r in rewards
        ):
            raise ValidationError(f"每個名次的獎勵必須在 0–{balance.COMPETITION_MAX_REWARD} 金幣之間。")
        _validate_period(starts_at, ends_at, now)
        with self._transaction() as session:
            comp = Competition(
                name=name,
                description=description,
                comp_type=comp_type,
                rewards=list(rewards),
                starts_at=starts_at,
                ends_at=ends_at,
                status="active",
                created_by=actor,
                created_at=now,
            )
            session.add(comp)
            session.flush()
            view = competition_view(comp, now)
            community_repo.add_admin_action(
                session,
                actor,
                "create_competition",
                "competition",
                comp.id,
                {k: str(v) if isinstance(v, datetime) else v for k, v in view.items()},
                now,
            )
            return view

    def cancel_competition(self, actor: str, competition_id: int, reason: str) -> dict:
        reason = _validate_text(reason, "取消原因", REASON_MAX)
        if len(reason) < REASON_MIN:
            raise ValidationError(f"取消原因至少 {REASON_MIN} 個字。")
        now = self._clock.now()
        with self._transaction() as session:
            comp = community_repo.get_competition(session, competition_id, for_update=True)
            if comp is None:
                raise NotFoundError(f"找不到競賽 #{competition_id}。")
            if comp.status != "active":
                raise ValidationError(
                    f"競賽 #{competition_id} 目前為「{rules.EVENT_STATE_LABELS[comp.status]}」，不能取消。"
                )
            comp.status = "cancelled"
            comp.cancelled_at = now
            comp.cancel_reason = reason
            community_repo.add_admin_action(
                session, actor, "cancel_competition", "competition", competition_id, {"reason": reason}, now
            )
            return competition_view(comp, now)

    def settle_due_competitions(self, actor: str = "system") -> list[int]:
        cutoff = self._clock.now() - timedelta(seconds=balance.COMPETITION_SETTLE_GRACE_SECONDS)
        with self._transaction() as session:
            due = community_repo.due_competitions(session, cutoff)
        return [cid for cid in due if self.settle_competition(cid, actor)]

    def settle_competition(self, competition_id: int, actor: str = "system") -> bool:
        """結算一場競賽；已結算或已取消時回傳 False（重複呼叫不會重複發獎）。"""
        now = self._clock.now()
        with self._transaction() as session:
            comp = community_repo.get_competition(session, competition_id, for_update=True)
            if comp is None:
                raise NotFoundError(f"找不到競賽 #{competition_id}。")
            if comp.status != "active":
                return False
            if now < comp.ends_at + timedelta(seconds=balance.COMPETITION_SETTLE_GRACE_SECONDS):
                raise ValidationError("競賽尚未截止（或仍在截止後的結算緩衝時間內），不能結算。")
            ranking = community_repo.live_ranking(session, competition_id)
            community_repo.add_results(session, competition_id, ranking)
            paid = 0
            for row in ranking[: len(comp.rewards)]:
                gold = comp.rewards[row["rank"] - 1]
                if gold <= 0:
                    continue
                for farm, player_name in community_repo.award_recipients(session, row["group_id"]):
                    if not community_repo.add_award(
                        session,
                        competition_id=competition_id,
                        player_id=farm.player_id,
                        group_id=row["group_id"],
                        player_name=player_name,
                        rank=row["rank"],
                        gold=gold,
                        created_at=now,
                    ):
                        continue
                    farm.gold += gold
                    paid += 1
                    self._record(
                        session,
                        farm,
                        EventType.COMPETITION_REWARD,
                        f"競賽「{comp.name}」群組第 {row['rank']} 名獎勵，金幣 +{gold}",
                        gold_delta=gold,
                        context_group_id=row["group_id"],
                    )
            comp.status = "settled"
            comp.settled_at = now
            community_repo.add_admin_action(
                session,
                actor,
                "settle_competition",
                "competition",
                competition_id,
                {"groups": len(ranking), "awards": paid},
                now,
            )
            return True

    # ---------- 玩家：任務 ----------

    def _member_quest(self, session, group_id: int, player_id: int, quest_id: int):
        self._require_member(session, group_id, player_id)
        quest = community_repo.get_quest(session, quest_id)
        if quest is None or quest.group_id != group_id:
            # 不透露其他群組的任務
            raise NotFoundError(f"目前群組沒有任務 #{quest_id}。")
        return quest

    def player_quests(self, player_id: int) -> list[dict]:
        """玩家所屬各群組中進行中的任務（只查詢，不需選擇群組）。"""
        now = self._clock.now()
        with self._transaction() as session:
            group_ids = [g.id for g, _c, member in groups_repo.list_with_counts(session, player_id) if member]
            if not group_ids:
                return []
            rows = community_repo.list_quests(session, group_ids=group_ids, statuses=("active", "completed"))
            names = catalog_repo.item_names(session)
            views = [quest_view(q, name, now, names) for q, name in rows]
            for v in views:
                v["my_contribution"] = community_repo.player_quest_contribution(session, v["quest_id"], player_id)
            return [v for v in views if v["state"] in ("active", "scheduled", "completed")]

    def group_quests(self, group_id: int, player_id: int) -> list[dict]:
        now = self._clock.now()
        with self._transaction() as session:
            self._require_member(session, group_id, player_id)
            names = catalog_repo.item_names(session)
            rows = community_repo.list_quests(session, group_ids=[group_id])
            return [quest_view(q, name, now, names) for q, name in rows]

    def quest_status(self, group_id: int, player_id: int, quest_id: int) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            quest = self._member_quest(session, group_id, player_id, quest_id)
            view = quest_view(quest, groups_repo.get(session, group_id).name, now, catalog_repo.item_names(session))
            view["contributors"] = community_repo.quest_contributors(session, quest_id)
            view["my_contribution"] = community_repo.player_quest_contribution(session, quest_id, player_id)
            claim = community_repo.get_claim(session, quest_id, player_id)
            view["my_claim"] = None if claim is None else {"amount": claim.amount, "claimed_at": claim.claimed_at}
            view["my_reward"] = self._reward_for(session, quest, player_id)
            return view

    @staticmethod
    def _reward_for(session, quest, player_id: int) -> int:
        mine = community_repo.player_quest_contribution(session, quest.id, player_id)
        if mine <= 0:
            return 0
        if quest.reward_mode == "fixed":
            return quest.reward_amount
        total = community_repo.quest_total_contribution(session, quest.id)
        return rules.proportional_share(quest.reward_amount, mine, total)

    def claim_quest(self, group_id: int, player_id: int, quest_id: int) -> ActionResult:
        now = self._clock.now()
        with self._transaction() as session:
            quest = self._member_quest(session, group_id, player_id, quest_id)
            state = rules.event_state(quest.status, quest.starts_at, quest.ends_at, now)
            if state == "cancelled":
                raise ValidationError(f"任務 #{quest_id} 已取消（原因：{quest.cancel_reason}），無法領取獎勵。")
            if state != "completed":
                hint = "已截止且未達成目標" if state == "expired" else "尚未完成"
                raise ValidationError(f"任務 #{quest_id} {hint}，無法領取獎勵。")
            # 鎖定農場後再檢查是否領過：重複指令或並行請求只會成功一次（另有唯一限制保護）
            farm = self._locked_farm(session, player_id)
            if community_repo.get_claim(session, quest_id, player_id) is not None:
                raise AlreadyExistsError(f"你已經領取過任務 #{quest_id} 的獎勵。")
            amount = self._reward_for(session, quest, player_id)
            if community_repo.player_quest_contribution(session, quest_id, player_id) <= 0:
                raise ValidationError("你沒有參與這個任務，無法領取獎勵。")
            if amount <= 0:
                raise ValidationError("依貢獻比例分配後你的獎勵為 0。")
            if quest.reward_type == "gold":
                farm.gold += amount
                tx_kw = {"gold_delta": amount}
            else:
                used = inventory_repo.total_quantity(session, farm.id)
                if used + amount > farm.warehouse_capacity:
                    raise WarehouseFullError(
                        f"背包空間不足：目前 {used}/{farm.warehouse_capacity}，需要 {amount} 格。"
                        "獎勵會保留，清出空間後再領取。"
                    )
                inventory_repo.add(session, farm.id, quest.reward_type, amount)
                tx_kw = {"item_code": quest.reward_type, "quantity": amount}
            session.add(
                QuestClaim(
                    quest_id=quest_id, player_id=player_id, reward_type=quest.reward_type, amount=amount, claimed_at=now
                )
            )
            session.flush()
            label = reward_label(quest.reward_type, amount)
            self._record(
                session,
                farm,
                EventType.QUEST_REWARD,
                f"領取任務「{quest.name}」獎勵：{label}",
                context_group_id=group_id,
                **tx_kw,
            )
            return ActionResult(f"已領取任務 #{quest_id}「{quest.name}」的獎勵：{label}。", farm.gold)

    # ---------- 玩家：競賽 ----------

    def competitions(self, *, include_finished: bool = True) -> list[dict]:
        self.settle_due_competitions()
        now = self._clock.now()
        with self._transaction() as session:
            statuses = None if include_finished else ("active",)
            return [competition_view(c, now) for c in community_repo.list_competitions(session, statuses=statuses)]

    def competition_detail(self, competition_id: int, player_id: int | None = None) -> dict:
        self.settle_due_competitions()
        now = self._clock.now()
        with self._transaction() as session:
            comp = community_repo.get_competition(session, competition_id)
            if comp is None:
                raise NotFoundError(f"找不到競賽 #{competition_id}。")
            view = competition_view(comp, now)
            settled = comp.status == "settled"
            view["final"] = settled
            view["ranking"] = (
                community_repo.final_ranking(session, competition_id)
                if settled
                else community_repo.live_ranking(session, competition_id)
            )
            view["top_players"] = community_repo.competition_player_points(session, competition_id)
            view["awards"] = [
                {
                    "player_id": a.player_id,
                    "player_name": a.player_name,
                    "group_id": a.group_id,
                    "rank": a.rank,
                    "gold": a.gold,
                }
                for a in community_repo.competition_awards(session, competition_id)
            ]
            if player_id is not None:
                farm = farms_repo.get_by_player(session, player_id)
                view["my_points"] = community_repo.player_competition_points(session, competition_id, player_id)
                view["my_home_group_id"] = farm.home_group_id if farm else None
                award = community_repo.player_award(session, competition_id, player_id)
                view["my_award"] = None if award is None else {"rank": award.rank, "gold": award.gold}
            return view

    def competition_history(self, player_id: int | None = None, limit: int = 10) -> list[dict]:
        self.settle_due_competitions()
        with self._transaction() as session:
            ids = [c.id for c in community_repo.list_competitions(session, statuses=("settled",), limit=limit)]
        return [self.competition_detail(cid, player_id) for cid in ids]
