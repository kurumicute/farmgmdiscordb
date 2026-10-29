"""啟動檢查：確認資料庫可連線且 Migration 已套用到最新版本（只檢查，不修改資料）。"""

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class StartupCheckError(RuntimeError):
    pass


def _alembic_heads() -> set[str]:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return set(ScriptDirectory.from_config(cfg).get_heads())


def check_database(engine: Engine) -> None:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(conn).get_current_heads())
    except SQLAlchemyError as exc:
        # 只回報例外類型，避免把含帳密的連線字串印出
        raise StartupCheckError(
            "無法連線到 PostgreSQL，請確認資料庫已啟動且 .env 的 DATABASE_URL 正確"
            f"（{type(exc).__name__}）。"
        ) from None

    heads = _alembic_heads()
    if current != heads:
        raise StartupCheckError(
            "資料庫 Migration 尚未套用到最新版本"
            f"（目前：{', '.join(sorted(current)) or '無'}；最新：{', '.join(sorted(heads))}）。"
            "請執行：alembic upgrade head"
        )
