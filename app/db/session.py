"""資料庫連線與 Session 管理。"""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str, **kwargs) -> Engine:
    # 一律以 UTC 與資料庫溝通；pool_pre_ping 讓資料庫重啟後能自動換掉失效連線
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC", "connect_timeout": 5},
        **kwargs,
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
