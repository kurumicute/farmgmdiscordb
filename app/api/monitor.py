"""唯讀監控 API 與 WebSocket。不提供任何修改遊戲資料的端點（寫入請見 app.api.admin）。"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Path, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from sqlalchemy.exc import SQLAlchemyError

from app.clock import TAIPEI_TZ
from app.services.monitor_service import MonitorService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])
ws_router = APIRouter()

DB_ERROR_MESSAGE = "資料庫暫時無法連線，無法取得監控資料。"
MAX_BIGINT = 2**63 - 1


def _service(request: Request) -> MonitorService:
    return request.app.state.monitor_service


@router.get("/summary")
async def get_summary(request: Request) -> dict:
    return await run_in_threadpool(_service(request).summary)


@router.get("/farms")
async def list_farms(
    request: Request,
    q: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict:
    q = q.strip() if q else None
    return await run_in_threadpool(_service(request).farms, q or None, limit, offset)


@router.get("/farms/{farm_id}")
async def get_farm(request: Request, farm_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]) -> dict:
    detail = await run_in_threadpool(_service(request).farm_detail, farm_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="找不到這座農場。")
    return detail


@router.get("/transactions")
async def list_transactions(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[dict]:
    return await run_in_threadpool(_service(request).transactions, limit)


@router.get("/charts")
async def get_charts(request: Request) -> dict:
    return await run_in_threadpool(_service(request).charts)


def _same_origin(websocket: WebSocket) -> bool:
    # 瀏覽器的 WebSocket 不受同源政策限制，因此需檢查 Origin，防止其他網站讀取監控資料
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # 非瀏覽器用戶端（例如測試或命令列工具）
    return urlsplit(origin).netloc.lower() == (websocket.headers.get("host") or "").lower()


@ws_router.websocket("/ws/monitor")
async def monitor_ws(websocket: WebSocket) -> None:
    if not _same_origin(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    service: MonitorService = websocket.app.state.monitor_service
    refresh = websocket.app.state.refresh_seconds
    try:
        while True:
            try:
                snapshot = await run_in_threadpool(service.snapshot)
                await websocket.send_json({"type": "snapshot", "data": jsonable_encoder(snapshot)})
            except SQLAlchemyError as exc:
                logger.warning("監控快照讀取失敗：%s", type(exc).__name__)
                await websocket.send_json({"type": "error", "message": DB_ERROR_MESSAGE})
            # 等待下一輪；期間若用戶端送出任何訊息（例如 "refresh"）則立即重新整理
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=refresh)
            except TimeoutError:
                pass
    except WebSocketDisconnect:
        pass


# ---------- 群組、偷菜、排行榜、背包（唯讀） ----------


@router.get("/players")
async def list_players(request: Request) -> list[dict]:
    return await run_in_threadpool(_service(request).players)


@router.get("/groups")
async def list_groups(request: Request) -> list[dict]:
    return await run_in_threadpool(_service(request).groups)


@router.get("/groups/{group_id}")
async def get_group(request: Request, group_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]) -> dict:
    detail = await run_in_threadpool(_service(request).group_detail, group_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="找不到這個群組。")
    return detail


@router.get("/steals")
async def list_steals(
    request: Request,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_BIGINT)] = None,
    player_id: Annotated[int | None, Query(ge=1, le=MAX_BIGINT)] = None,
    outcome: Annotated[Literal["success", "blocked"] | None, Query()] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    # 日期以台灣時間解讀；date_to 包含當天
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="開始日期不可晚於結束日期。")
    since = datetime.combine(date_from, time.min, TAIPEI_TZ) if date_from else None
    until = datetime.combine(date_to + timedelta(days=1), time.min, TAIPEI_TZ) if date_to else None
    return await run_in_threadpool(
        lambda: _service(request).steals(
            limit=limit, group_id=group_id, player_id=player_id, outcome=outcome, since=since, until=until
        )
    )


@router.get("/defenses")
async def list_defenses(request: Request) -> list[dict]:
    return await run_in_threadpool(_service(request).defenses)


@router.get("/leaderboard")
async def get_leaderboard(
    request: Request,
    scope: Annotated[Literal["global", "group", "groups"], Query()] = "global",
    kind: Annotated[Literal["money", "sales", "goods"], Query()] = "money",
    group_id: Annotated[int | None, Query(ge=1, le=MAX_BIGINT)] = None,
    period: Annotated[Literal["all", "day", "week", "month"], Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """scope=groups 為群組總排行（各群組成員加總）；period 為台灣時間的本日／本週／本月（money 改為期間淨賺金幣）。"""
    if scope == "group" and group_id is None:
        raise HTTPException(status_code=422, detail="群組排行榜必須指定群組。")
    if scope != "group":
        group_id = None
    result = await run_in_threadpool(
        lambda: _service(request).leaderboard(kind, group_id, limit, scope=scope, period=period)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="找不到這個群組。")
    return result


@router.get("/players/{player_id}/backpack")
async def get_backpack(request: Request, player_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]) -> dict:
    result = await run_in_threadpool(_service(request).backpack, player_id)
    if result is None:
        raise HTTPException(status_code=404, detail="找不到這位玩家的農場。")
    return result


@router.get("/ranch")
async def list_ranch(request: Request) -> list[dict]:
    return await run_in_threadpool(_service(request).ranch)


@router.get("/craft-orders")
async def list_craft_orders(
    request: Request,
    include_collected: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    return await run_in_threadpool(
        lambda: _service(request).craft_orders(include_collected=include_collected, limit=limit)
    )


# ---------- 群組任務與競賽（唯讀） ----------


@router.get("/quests")
async def list_quests(
    request: Request,
    group_id: Annotated[int | None, Query(ge=1, le=MAX_BIGINT)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    return await run_in_threadpool(_service(request).quests, group_id, limit)


@router.get("/quests/{quest_id}")
async def get_quest(request: Request, quest_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]) -> dict:
    detail = await run_in_threadpool(_service(request).quest_detail, quest_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="找不到這個任務。")
    return detail


@router.get("/competitions")
async def list_competitions(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    return await run_in_threadpool(_service(request).competitions, limit)


@router.get("/competitions/{competition_id}")
async def get_competition(request: Request, competition_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]) -> dict:
    detail = await run_in_threadpool(_service(request).competition_detail, competition_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="找不到這場競賽。")
    return detail


@router.get("/admin-actions")
async def list_admin_actions(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[dict]:
    return await run_in_threadpool(_service(request).admin_actions, limit)


# ---------- 作物品質與突變（唯讀） ----------


@router.get("/genetics")
async def get_genetics(request: Request) -> dict:
    return await run_in_threadpool(_service(request).genetics)


@router.get("/genetics/stats")
async def get_genetics_stats(request: Request) -> dict:
    return await run_in_threadpool(_service(request).genetics_stats)


# ---------- 作物、動物、事件目錄與單一玩家的管理資料（唯讀） ----------


@router.get("/catalog")
async def get_catalog(request: Request) -> dict:
    return await run_in_threadpool(_service(request).catalog)


@router.get("/players/{player_id}/admin-farm")
async def get_admin_farm(request: Request, player_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]) -> dict:
    result = await run_in_threadpool(_service(request).admin_farm, player_id)
    if result is None:
        raise HTTPException(status_code=404, detail="找不到這位玩家的農場。")
    return result


# ---------- 排名獎勵（唯讀） ----------


@router.get("/ranking-rewards")
async def get_ranking_rewards(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 30) -> dict:
    return await run_in_threadpool(_service(request).ranking_rewards, limit)


@router.get("/ranking-settlements/{settlement_id}")
async def get_ranking_settlement(
    request: Request, settlement_id: Annotated[int, Path(ge=1, le=MAX_BIGINT)]
) -> dict:
    result = await run_in_threadpool(_service(request).ranking_settlement, settlement_id)
    if result is None:
        raise HTTPException(status_code=404, detail="找不到這筆結算。")
    return result
