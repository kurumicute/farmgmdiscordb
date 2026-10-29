"""FastAPI 監控網頁服務入口。

啟動：python -m app.main   （或 uvicorn app.main:app --host 127.0.0.1 --port 8000）
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.monitor import DB_ERROR_MESSAGE, router, ws_router
from app.auth import AdminAuth
from app.clock import Clock
from app.config import get_settings
from app.db.check import check_database
from app.db.session import make_engine, make_session_factory
from app.services.admin_service import AdminService
from app.services.monitor_service import MonitorService

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    clock: Clock | None = None,
    refresh_seconds: float | None = None,
    check: bool = True,
    allowed_hosts: list[str] | None = None,
    admin_auth: AdminAuth | None = None,
    settle_interval_seconds: float | None = None,
) -> FastAPI:
    """settle_interval_seconds：背景自動結算排名獎勵的間隔。

    None 時正式執行使用設定值；測試（注入 session_factory）不啟動背景結算。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = None
        factory = session_factory
        refresh = refresh_seconds
        interval = settle_interval_seconds
        if factory is None:
            settings = get_settings()
            engine = make_engine(settings.database_url, **settings.engine_options())
            factory = make_session_factory(engine)
            refresh = refresh or settings.monitor_refresh_seconds
            if settings.background_tasks_runner == "web":
                interval = interval or settings.ranking_settle_interval_seconds
        if check:
            check_database(factory.kw["bind"])
        app.state.monitor_service = MonitorService(factory, clock)
        app.state.social_service = AdminService(factory, clock)
        app.state.refresh_seconds = refresh or 3.0
        app.state.admin_auth = admin_auth if admin_auth is not None else _admin_auth_from_settings()
        if not app.state.admin_auth.enabled:
            logger.warning("未設定 ADMIN_USERNAME / ADMIN_PASSWORD_HASH，網頁管理功能停用。")
        task = asyncio.create_task(_auto_settle(app, interval)) if interval else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            if engine is not None:
                engine.dispose()

    # 監控服務僅供本機使用，關閉自動產生的 API 文件頁面以減少暴露面
    app = FastAPI(title="農場監控", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(SQLAlchemyError)
    async def db_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.warning("監控 API 資料庫錯誤：%s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": DB_ERROR_MESSAGE})

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # 只接受本機 Host，防止 DNS rebinding 讓外部網站讀取或操作本機服務
    hosts = allowed_hosts if allowed_hosts is not None else get_settings_safe_hosts()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(ws_router)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


async def _auto_settle(app: FastAPI, interval: float) -> None:
    """定期結算到期的排名獎勵（每日／每週／每月）與競賽（BACKGROUND_TASKS_RUNNER=web 時）。

    結算可重複執行：advisory lock 讓多個程序不會同時結算，唯一限制保證不會重複發獎。
    """
    while True:
        try:
            settled = await run_in_threadpool(app.state.social_service.run_background_settlement, "system")
            if settled and (settled["rankings"] or settled["competitions"]):
                logger.info("已自動結算：%s", settled)
        except Exception as exc:  # 資料庫暫時無法連線等錯誤：記錄後下次再試，不中斷服務
            logger.warning("自動結算排名獎勵失敗：%s", type(exc).__name__)
        await asyncio.sleep(interval)


def _admin_auth_from_settings() -> AdminAuth:
    try:
        s = get_settings()
    except Exception:  # 設定缺漏時管理功能停用（fail closed）
        return AdminAuth(None, None)
    return AdminAuth(s.admin_username, s.admin_password_hash, session_seconds=s.admin_session_hours * 3600)


def get_settings_safe_hosts() -> list[str]:
    try:
        return get_settings().allowed_host_list
    except Exception:  # 設定缺漏時仍以最嚴格的本機清單啟動，實際錯誤會在 lifespan 回報
        return ["127.0.0.1", "localhost"]


app = create_app()


def run() -> None:
    import uvicorn

    from app.db.check import StartupCheckError

    settings = get_settings()
    try:
        check_database(make_engine(settings.database_url))
    except StartupCheckError as exc:
        raise SystemExit(f"[啟動失敗] {exc}") from None
    print(f"監控網頁：http://{settings.app_host}:{settings.app_port}")
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    run()
