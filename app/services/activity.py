"""把成功的遊戲操作計入群組任務與競賽。

由 GameService._record 在寫入交易紀錄後、於同一筆資料庫交易中呼叫，因此：
- 只有成功提交的操作才會計分；操作失敗回滾時，貢獻也一起回滾。
- 每筆貢獻都以 (任務或競賽, 交易紀錄 ID) 唯一，同一筆操作不會重複計分，重啟後也不會重算。

歸屬規則：操作發生時的群組（context_group_id）必須等於農場的主要所屬群組（home_group_id），
且玩家為該群組成員（_record 只在玩家是成員時才記錄 context）。因此每筆操作最多計入一個群組。
"""

from sqlalchemy.orm import Session

from app.db.models import Farm, GameTransaction
from app.game import rules
from app.repositories import community as community_repo

# 可能影響任務或競賽的事件類型；其他事件直接略過，不做額外查詢
SCORING_EVENTS = {"harvest", "sell", "upgrade", "build", "collect_animal", "defended"}


def scoring_group(farm: Farm, tx: GameTransaction) -> int | None:
    if tx.context_group_id is None or farm.home_group_id != tx.context_group_id:
        return None
    return tx.context_group_id


def on_event(session: Session, farm: Farm, tx: GameTransaction) -> None:
    if tx.event_type not in SCORING_EVENTS:
        return
    group_id = scoring_group(farm, tx)
    if group_id is None:
        return
    now = tx.created_at

    for quest in community_repo.open_quests_for_event(session, group_id, now):
        amount = rules.quest_amount(
            quest.quest_type, quest.metric, quest.item_filter, tx.event_type, tx.item_code, tx.quantity, tx.gold_delta
        )
        if amount <= 0:
            continue
        if community_repo.add_quest_contribution(session, quest.id, farm.player_id, tx.id, amount, now):
            quest.progress += amount
            if quest.progress >= quest.target:
                # 達標即完成；之後的操作不再計入此任務
                quest.status = "completed"
                quest.completed_at = now

    for comp in community_repo.open_competitions_for_event(session, now):
        points = rules.competition_points(comp.comp_type, tx.event_type, tx.quantity, tx.gold_delta)
        if points > 0:
            community_repo.add_competition_contribution(session, comp.id, group_id, farm.player_id, tx.id, points, now)
