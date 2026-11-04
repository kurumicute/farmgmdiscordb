"""world boss rewards: participation threshold (min_points)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-04

世界 Boss 獎勵新增「參與門檻」min_points：個人積分未達門檻的玩家沒有任何獎勵（含勝利、參與、達標與排名），
避免只捐 1 個小麥就領到勝利獎勵。只改 JSON 資料，不改資料表結構：
- 排程（world_boss_schedules）沒有 min_points 的補上 30（之後產生的場次沿用）；
- 尚未結束且還沒有人領獎的場次補上 30；已結束的場次不動（程式把缺少的欄位視為 0，維持原本的領獎資格）。

降版只移除 min_points 欄位（回到沒有門檻）。
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

MIN_POINTS = 30


def upgrade() -> None:
    op.execute(
        "UPDATE world_boss_schedules SET rewards = jsonb_set(rewards::jsonb, '{min_points}', "
        f"'{MIN_POINTS}')::json WHERE NOT (rewards::jsonb ? 'min_points')"
    )
    op.execute(
        "UPDATE world_boss_events e SET rewards = jsonb_set(e.rewards::jsonb, '{min_points}', "
        f"'{MIN_POINTS}')::json WHERE e.status <> 'ended' AND NOT (e.rewards::jsonb ? 'min_points') "
        "AND NOT EXISTS (SELECT 1 FROM world_boss_reward_claims c WHERE c.event_id = e.id)"
    )


def downgrade() -> None:
    op.execute("UPDATE world_boss_schedules SET rewards = (rewards::jsonb - 'min_points')::json")
    op.execute("UPDATE world_boss_events SET rewards = (rewards::jsonb - 'min_points')::json")
