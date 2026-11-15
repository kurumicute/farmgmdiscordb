"""seed lab expansions and farm decorations

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-15

Go 版新增（Python 版沒有對應的程式）。
- seed_labs.library_level：種子庫擴充等級（每級容量增加的格數與價格在管理網站「種子實驗室」調整）。CHECK 0–10。
- seed_labs.extra_slots：額外購買的研究槽數。CHECK 0–5。
- farm_decorations：農場裝飾（買一次永久擁有）；slot 為 NULL 表示收起來，1–6 是庭院擺飾區的位置。
  同一座農場每種裝飾只有一個，同一個位置只能擺一個。

降版會刪除上述欄位與資料表（已花的金幣不退）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("seed_labs", sa.Column("library_level", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("seed_labs", sa.Column("extra_slots", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.create_check_constraint(op.f("ck_seed_labs_library_level_range"), "seed_labs", "library_level BETWEEN 0 AND 10")
    op.create_check_constraint(op.f("ck_seed_labs_extra_slots_range"), "seed_labs", "extra_slots BETWEEN 0 AND 5")
    op.create_table(
        "farm_decorations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("farm_id", sa.BigInteger(), nullable=False),
        sa.Column("decor_code", sa.String(32), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot BETWEEN 1 AND 6", name=op.f("ck_farm_decorations_slot_range")),
        sa.ForeignKeyConstraint(["farm_id"], ["farms.id"], name=op.f("fk_farm_decorations_farm_id_farms"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_farm_decorations")),
        sa.UniqueConstraint("farm_id", "decor_code", name=op.f("uq_farm_decorations_farm_code")),
    )
    op.create_index("uq_farm_decorations_farm_slot", "farm_decorations", ["farm_id", "slot"], unique=True,
                    postgresql_where=sa.text("slot IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_farm_decorations_farm_slot", table_name="farm_decorations")
    op.drop_table("farm_decorations")
    op.drop_constraint(op.f("ck_seed_labs_extra_slots_range"), "seed_labs", type_="check")
    op.drop_constraint(op.f("ck_seed_labs_library_level_range"), "seed_labs", type_="check")
    op.drop_column("seed_labs", "extra_slots")
    op.drop_column("seed_labs", "library_level")
