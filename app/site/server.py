"""介紹網站（公開）：Bot 功能介紹、邀請連結與彙總統計。預設 Port 24444。

- 只提供彙總數字（玩家數、農場數、伺服器數、今日操作數等），不輸出任何玩家名稱、ID 或個別資料。
- /api/guide 提供操作手冊用的遊戲設定（作物、動物、道具、品質與突變、啟用中的活動），
  數值即時讀自資料庫與 balance，管理員在後台調整後網站會跟著更新；同樣不含玩家資料。
- 查詢都在唯讀交易中執行並快取（統計 STATS_CACHE_SECONDS、手冊 GUIDE_CACHE_SECONDS 秒），訪客再多也不會增加資料庫負擔。
- 沒有任何寫入端點；頁面為靜態檔，前端只用 textContent 填入資料。
"""

import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Farm, FarmPlot, GameTransaction, Group, Player
from app.game import balance
from app.game import events as event_rules
from app.repositories import animals as animals_repo
from app.repositories import crops as crops_repo
from app.repositories import events as events_repo
from app.repositories import genetics as genetics_repo

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
STATS_CACHE_SECONDS = 30
GUIDE_CACHE_SECONDS = 60
# 邀請 Bot 需要的權限：檢視頻道、傳送訊息、嵌入連結、附加檔案、讀取訊息紀錄、連接語音、說話
INVITE_PERMISSIONS = 1024 + 2048 + 16384 + 32768 + 65536 + 1048576 + 2097152

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


def invite_url(application_id: int | None) -> str | None:
    if not application_id:
        return None
    return (
        f"https://discord.com/oauth2/authorize?client_id={application_id}"
        f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}"
    )


class StatsCache:
    def __init__(self, factory: sessionmaker[Session] | None, ttl: float = STATS_CACHE_SECONDS) -> None:
        self.factory = factory
        self.ttl = ttl
        self._value: dict | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict | None:
        with self._lock:
            if self._value is not None and time.monotonic() - self._at < self.ttl:
                return self._value
            if self.factory is None:
                return None
            with self.factory() as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                since = session.scalar(text("SELECT date_trunc('day', now() AT TIME ZONE 'Asia/Taipei') "
                                            "AT TIME ZONE 'Asia/Taipei'"))
                self._value = {
                    "players": session.scalar(select(func.count()).select_from(Player)) or 0,
                    "farms": session.scalar(select(func.count()).select_from(Farm)) or 0,
                    "servers": session.scalar(
                        select(func.count()).select_from(Group).where(Group.discord_guild_id.is_not(None))
                    ) or 0,
                    "growing": session.scalar(
                        select(func.count()).select_from(FarmPlot).where(FarmPlot.crop_type_id.is_not(None))
                    ) or 0,
                    "actions_today": session.scalar(
                        select(func.count()).select_from(GameTransaction).where(GameTransaction.created_at >= since)
                    ) or 0,
                    "last_activity": session.scalar(select(func.max(GameTransaction.created_at))),
                }
            self._at = time.monotonic()
            return self._value


def _pct(bp: int) -> float:
    """萬分比 → 百分比（最多兩位小數）。"""
    return round(bp / 100, 2)


def _duration(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    parts = [f"{hours} 小時" if hours else "", f"{minutes} 分鐘" if minutes else ""]
    return " ".join(p for p in parts if p) or f"{seconds} 秒"


def build_guide(session: Session, now: datetime) -> dict:
    """操作手冊需要的遊戲設定。只輸出規則與數值，不含任何玩家、農場或群組資料。"""
    crops = [c for c in crops_repo.list_all(session) if c.active]
    animals = [a for a in animals_repo.list_all(session) if a.active]
    version, g = genetics_repo.current(session)
    names = {c.code: c.name for c in crops_repo.list_all(session)}
    names.update({code: v[0] for code, v in balance.GOODS.items()})
    mutation_effects = {
        "giant": f"收成數量 ×{g['giant_multiplier']}",
        "golden": f"售價 ×{g['golden_price_pct'] / 100:g}",
        "fast": (f"生長時間減少 {_duration(g['fast_reduction_seconds'])}"
                 f"（最短 {_duration(balance.MIN_GROW_SECONDS)}，仍需澆水）"),
        "variant": f"長成獨立的稀有作物，售價 ×{g['variant_price_pct'] / 100:g}",
    }
    return {
        "genetics_version": version,
        "start": {
            "gold": balance.INITIAL_GOLD,
            "plots": balance.INITIAL_PLOTS,
            "warehouse": balance.INITIAL_WAREHOUSE_CAPACITY,
            "upgrade_cost": balance.UPGRADE_COST,
            "upgrade_plots": balance.UPGRADE_PLOTS_ADDED,
            "max_plots": balance.MAX_PLOTS,
            "wither_minutes": balance.WITHER_SECONDS // 60,
            "steal_max": balance.STEAL_MAX_PER_ACTION,
            "transfer_cooldown_days": balance.GROUP_TRANSFER_COOLDOWN_SECONDS // 86400,
            "max_block_pct": round(balance.DEFENSE_MAX_BLOCK_CHANCE * 100),
            "patrol_days": balance.PATROL_DAYS,
            "barn_slots": balance.BARN_SLOTS,
            "craft_max": balance.CRAFT_MAX_QUANTITY,
            "warehouse_step": balance.WAREHOUSE_EXPANSION_STEP,
            "warehouse_prices": list(balance.WAREHOUSE_EXPANSION_PRICES),
            "warehouse_max": balance.WAREHOUSE_MAX_CAPACITY,
        },
        "crops": [
            {"name": c.name, "seed_price": c.seed_price, "grow_seconds": c.grow_seconds,
             "harvest_quantity": c.harvest_quantity, "sell_price": c.sell_price, "variant_name": c.variant_name}
            for c in crops
        ],
        "qualities": [
            {"name": label, "chance_pct": _pct(g["quality_bp"][code]), "price_mul": g["quality_price_pct"][code] / 100}
            for code, label in balance.QUALITIES.items()
        ],
        "level_bonus_pct": _pct(g["level_bonus_bp"]),
        "level_bonus_max_pct": _pct(g["level_bonus_max_bp"]),
        "mutations": [
            {"name": balance.MUTATIONS[code], "chance_pct": _pct(g["mutation_bp"][code]), "effect": effect}
            for code, effect in mutation_effects.items()
        ],
        "items": [
            {"name": name, "price": price, "kind": kind, "description": desc}
            for name, price, kind, desc, _aliases in balance.ITEMS.values()
        ],
        "defenses": [
            {"name": name, "price": price, "max": max_count, "chance_pct": round(chance * 100), "range": cells}
            for name, price, max_count, chance, cells, _aliases in (balance.DEFENSES[c] for c in balance.DEFENSE_ORDER)
        ],
        "buildings": [
            {"name": name, "price": price, "max": max_count}
            for name, price, max_count, _aliases in balance.BUILDINGS.values()
        ],
        "animals": [
            {"name": a.name, "price": a.price, "feed_cost": a.feed_cost, "cycle_seconds": a.cycle_seconds,
             "product_name": a.product_name, "product_quantity": a.product_quantity, "product_price": a.product_price}
            for a in animals
        ],
        "recipes": [
            {"name": name, "building": balance.BUILDINGS[building][0], "building_price": balance.BUILDINGS[building][1],
             "inputs": [{"name": names.get(code, code), "quantity": qty} for code, qty in inputs.items()],
             "output_quantity": out_qty, "seconds": seconds, "sell_price": balance.GOODS[output][1]}
            for name, building, inputs, output, out_qty, seconds, _aliases in balance.RECIPES.values()
        ],
        "gamble": {
            "min": balance.GAMBLE_MIN, "max": balance.GAMBLE_MAX, "big_from": balance.GAMBLE_BIG_FROM,
            "hilo_multiplier": balance.GAMBLE_HILO_MULTIPLIER, "exact_multiplier": balance.GAMBLE_EXACT_MULTIPLIER,
            "qualities": [balance.QUALITIES[q] for q in balance.GAMBLE_QUALITIES],
            "mutations": [balance.MUTATIONS[m] for m in balance.GAMBLE_MUTATIONS],
        },
        # 只列啟用中的活動（名稱、效果與時段本來就會在 Discord 面板公開顯示）
        "events": [
            {"name": e.name, "description": e.description, "effects_text": event_rules.effects_text(e),
             "schedule_text": event_rules.schedule_text(e), "active_now": event_rules.is_active(e, now)}
            for e in sorted(events_repo.list_all(session), key=lambda e: e.id)
            if e.enabled
        ],
    }


class GuideCache:
    def __init__(self, factory: sessionmaker[Session] | None, ttl: float = GUIDE_CACHE_SECONDS) -> None:
        self.factory = factory
        self.ttl = ttl
        self._value: dict | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict | None:
        with self._lock:
            if self._value is not None and time.monotonic() - self._at < self.ttl:
                return self._value
            if self.factory is None:
                return None
            with self.factory() as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                self._value = build_guide(session, datetime.now(UTC))
            self._at = time.monotonic()
            return self._value


def create_site_app(
    session_factory: sessionmaker[Session] | None = None,
    *,
    site_name: str = "油祥農場",
    application_id: int | None = None,
    support_url: str | None = None,
    music_enabled: bool = True,
    music_application_id: int | None = None,
    music_prefix_commands: bool = False,
    music_prefix: str = "!",
    music_playlist_limit: int = 20,
    music_idle_timeout: int = 300,
    music_volume: float = 0.1,
) -> FastAPI:
    app = FastAPI(title=site_name, docs_url=None, redoc_url=None, openapi_url=None)
    stats = StatsCache(session_factory)
    guide = GuideCache(session_factory)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        # 網站更新後訪客立即看到新版：瀏覽器可快取，但每次先向伺服器確認（ETag）
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/api/info")
    async def info() -> dict:
        return {
            "name": site_name,
            "invite_url": invite_url(application_id),
            # 音樂改用獨立 Bot 時，另外提供音樂 Bot 的邀請連結
            "music_invite_url": invite_url(music_application_id),
            "support_url": support_url,
            "music_enabled": music_enabled,
            # 音樂說明用的設定值（不含任何 Token 或機密）
            "music": {
                "prefix_commands": music_prefix_commands,
                "prefix": music_prefix,
                "playlist_limit": music_playlist_limit,
                "idle_minutes": round(music_idle_timeout / 60, 1),
                "default_volume_pct": round(music_volume * 100),
            },
        }

    @app.get("/api/guide")
    async def get_guide():
        try:
            value = await run_in_threadpool(guide.get)
        except SQLAlchemyError as exc:
            logger.warning("介紹網站手冊資料查詢失敗：%s", type(exc).__name__)
            return JSONResponse(status_code=503, content={"detail": "遊戲設定暫時無法取得"})
        if value is None:
            return JSONResponse(status_code=503, content={"detail": "遊戲設定暫時無法取得"})
        return {**value, "cached_seconds": GUIDE_CACHE_SECONDS}

    @app.get("/api/stats")
    async def get_stats():
        try:
            value = await run_in_threadpool(stats.get)
        except SQLAlchemyError as exc:
            logger.warning("介紹網站統計查詢失敗：%s", type(exc).__name__)
            return JSONResponse(status_code=503, content={"detail": "統計資料暫時無法取得"})
        if value is None:
            return JSONResponse(status_code=503, content={"detail": "統計資料暫時無法取得"})
        return {**value, "cached_seconds": STATS_CACHE_SECONDS}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
