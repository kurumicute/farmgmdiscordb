"""seed lab: clone (育苗) jobs and records

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-15

Go 版新增（Python 版沒有對應的程式）。種子實驗室新增「育苗」：付金幣與藥草，在研究槽複製一顆同基因的特殊種子。
- lab_jobs.kind：新增 clone（與 mutate 一樣只有 mother_seed_id，father_seed_id 為 NULL）。
- lab_records.kind：新增 clone。

降版會刪除育苗的工作與紀錄（還沒領取的育苗工作，來源種子改回種子庫），再把 CHECK 改回原本的值。
"""

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_lab_jobs_kind_valid"), "lab_jobs", type_="check")
    op.create_check_constraint(op.f("ck_lab_jobs_kind_valid"), "lab_jobs", "kind IN ('cross', 'mutate', 'clone')")
    op.drop_constraint(op.f("ck_lab_records_kind_valid"), "lab_records", type_="check")
    op.create_check_constraint(op.f("ck_lab_records_kind_valid"), "lab_records", "kind IN ('identify', 'cross', 'mutate', 'clone')")


def downgrade() -> None:
    op.execute(
        "UPDATE lab_seeds SET status = 'stored' WHERE status = 'busy' AND id IN "
        "(SELECT mother_seed_id FROM lab_jobs WHERE kind = 'clone' AND collected_at IS NULL)"
    )
    op.execute("DELETE FROM lab_jobs WHERE kind = 'clone'")
    op.execute("DELETE FROM lab_records WHERE kind = 'clone'")
    op.drop_constraint(op.f("ck_lab_jobs_kind_valid"), "lab_jobs", type_="check")
    op.create_check_constraint(op.f("ck_lab_jobs_kind_valid"), "lab_jobs", "kind IN ('cross', 'mutate')")
    op.drop_constraint(op.f("ck_lab_records_kind_valid"), "lab_records", type_="check")
    op.create_check_constraint(op.f("ck_lab_records_kind_valid"), "lab_records", "kind IN ('identify', 'cross', 'mutate')")
