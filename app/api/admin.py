"""管理 API（寫入）：群組任務與競賽、作物品質設定、作物／動物種類、限時事件，
以及調整玩家的金幣、等級、背包、土地、建築、動物。

所有寫入都會記錄在管理操作紀錄（admin_actions）；調整玩家資料另外寫入玩家的交易紀錄（admin_adjust）。
群組建立與成員管理改由 Discord 端處理，網站不提供。

防護：
- 必須以管理員帳號登入（HttpOnly session cookie）。
- 僅接受本機 Host（由 TrustedHostMiddleware 檢查，防 DNS rebinding）。
- 必須帶自訂標頭 X-Farmgm-Admin，且 Origin（若有）須與 Host 相同（防 CSRF）。
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.api.auth import require_admin_session, require_csrf_header
from app.clock import TAIPEI_TZ
from app.game import balance
from app.game.errors import GameError, NotFoundError
from app.services.admin_service import AdminService

MAX_BIGINT = 2**63 - 1
CODE_PATTERN = balance.CATALOG_CODE_PATTERN

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(require_csrf_header), Depends(require_admin_session)],
)


def _social(request: Request) -> AdminService:
    return request.app.state.social_service


async def _call(fn, *args):
    try:
        return await run_in_threadpool(fn, *args)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except GameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


PlayerId = Annotated[int, Path(ge=1, le=MAX_BIGINT)]
CatalogCode = Annotated[str, Path(min_length=2, max_length=32, pattern=CODE_PATTERN)]


# ---------- 群組任務與競賽 ----------


def _aware(dt: datetime) -> datetime:
    # 網頁 datetime-local 沒有時區，視為台灣時間
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=TAIPEI_TZ)


class QuestCreate(BaseModel):
    group_id: int = Field(ge=1, le=MAX_BIGINT)
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    quest_type: Literal["harvest", "sell", "build", "animal", "defense"]
    metric: Literal["quantity", "amount"] = "quantity"
    item_filter: str | None = Field(default=None, pattern=CODE_PATTERN)  # 動物產品代碼（服務層確認存在）
    target: int = Field(ge=1, le=1_000_000)
    reward_mode: Literal["fixed", "proportional"]
    reward_type: Literal["gold", "fertilizer", "earthworm", "patrol_ticket"]
    reward_amount: int = Field(ge=1, le=1_000_000)
    starts_at: datetime
    ends_at: datetime


class CompetitionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    comp_type: Literal["harvest", "sales", "development", "defense"]
    rewards: list[int] = Field(min_length=1, max_length=3)
    starts_at: datetime
    ends_at: datetime


class CancelBody(BaseModel):
    reason: str = Field(min_length=2, max_length=200)


Actor = Annotated[str, Depends(require_admin_session)]
EventId = Annotated[int, Path(ge=1, le=MAX_BIGINT)]


@router.post("/quests", status_code=201)
async def create_quest(request: Request, body: QuestCreate, actor: Actor) -> dict:
    data = body.model_dump()
    data["starts_at"], data["ends_at"] = _aware(body.starts_at), _aware(body.ends_at)
    return await _call(lambda: _social(request).create_quest(actor, **data))


@router.post("/quests/{quest_id}/cancel")
async def cancel_quest(request: Request, quest_id: EventId, body: CancelBody, actor: Actor) -> dict:
    return await _call(_social(request).cancel_quest, actor, quest_id, body.reason)


@router.post("/competitions", status_code=201)
async def create_competition(request: Request, body: CompetitionCreate, actor: Actor) -> dict:
    data = body.model_dump()
    data["starts_at"], data["ends_at"] = _aware(body.starts_at), _aware(body.ends_at)
    return await _call(lambda: _social(request).create_competition(actor, **data))


@router.post("/competitions/{competition_id}/cancel")
async def cancel_competition(request: Request, competition_id: EventId, body: CancelBody, actor: Actor) -> dict:
    return await _call(_social(request).cancel_competition, actor, competition_id, body.reason)


@router.post("/competitions/settle-due")
async def settle_due(request: Request, actor: Actor) -> dict:
    settled = await _call(_social(request).settle_due_competitions, actor)
    return {"settled": settled}


# ---------- 作物品質、突變與生長時間 ----------


class GeneticsUpdate(BaseModel):
    # 範圍與總和的完整檢查在服務層（app.game.genetics.validate_settings）統一處理
    quality_bp: dict[str, int]
    quality_price_pct: dict[str, int]
    mutation_bp: dict[str, int]
    giant_multiplier: int
    golden_price_pct: int
    fast_reduction_seconds: int
    variant_price_pct: int
    level_bonus_bp: int
    level_bonus_max_bp: int


class CropGrowUpdate(BaseModel):
    grow_seconds: int


@router.put("/genetics")
async def update_genetics(request: Request, body: GeneticsUpdate, actor: Actor) -> dict:
    version = await _call(_social(request).update_genetics, actor, body.model_dump())
    return {"version": version}


@router.put("/crops/{crop_code}/grow-seconds")
async def update_crop_grow_seconds(
    request: Request,
    crop_code: Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[a-z_]+$")],
    body: CropGrowUpdate,
    actor: Actor,
) -> dict:
    return await _call(_social(request).update_crop_grow_seconds, actor, crop_code, body.grow_seconds)


# ---------- 作物種類 ----------
# 數值範圍與名稱、代碼重複的完整檢查在服務層（AdminService）統一處理


class CropUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    variant_name: str = Field(min_length=1, max_length=32)
    seed_price: int
    grow_seconds: int
    harvest_quantity: int
    sell_price: int


class CropCreate(CropUpdate):
    code: str = Field(pattern=CODE_PATTERN)
    variant_code: str = Field(pattern=CODE_PATTERN)


class ActiveBody(BaseModel):
    active: bool


@router.post("/crops", status_code=201)
async def create_crop(request: Request, body: CropCreate, actor: Actor) -> dict:
    return await _call(_social(request).create_crop, actor, body.model_dump())


@router.put("/crops/{crop_code}")
async def update_crop(request: Request, crop_code: CatalogCode, body: CropUpdate, actor: Actor) -> dict:
    return await _call(_social(request).update_crop, actor, crop_code, body.model_dump())


@router.put("/crops/{crop_code}/active")
async def set_crop_active(request: Request, crop_code: CatalogCode, body: ActiveBody, actor: Actor) -> dict:
    return await _call(_social(request).set_crop_active, actor, crop_code, body.active)


@router.delete("/crops/{crop_code}")
async def delete_crop(request: Request, crop_code: CatalogCode, actor: Actor) -> dict:
    return await _call(_social(request).delete_crop, actor, crop_code)


# ---------- 動物種類 ----------


class AnimalUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    product_name: str = Field(min_length=1, max_length=32)
    price: int
    feed_cost: int
    cycle_seconds: int
    product_quantity: int
    product_price: int


class AnimalCreate(AnimalUpdate):
    code: str = Field(pattern=CODE_PATTERN)
    product_code: str = Field(pattern=CODE_PATTERN)


@router.post("/animal-types", status_code=201)
async def create_animal_type(request: Request, body: AnimalCreate, actor: Actor) -> dict:
    return await _call(_social(request).create_animal_type, actor, body.model_dump())


@router.put("/animal-types/{animal_code}")
async def update_animal_type(request: Request, animal_code: CatalogCode, body: AnimalUpdate, actor: Actor) -> dict:
    return await _call(_social(request).update_animal_type, actor, animal_code, body.model_dump())


@router.put("/animal-types/{animal_code}/active")
async def set_animal_type_active(request: Request, animal_code: CatalogCode, body: ActiveBody, actor: Actor) -> dict:
    return await _call(_social(request).set_animal_type_active, actor, animal_code, body.active)


@router.delete("/animal-types/{animal_code}")
async def delete_animal_type(request: Request, animal_code: CatalogCode, actor: Actor) -> dict:
    return await _call(_social(request).delete_animal_type, actor, animal_code)


# ---------- 限時事件 ----------

TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
END_TIME_PATTERN = r"^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$"


class EventBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    crop_code: str | None = Field(default=None, pattern=CODE_PATTERN)  # 不指定 = 全部作物
    sell_price_pct: int = 100
    grow_reduction_pct: int = 0
    quality_bonus_bp: int = 0
    mutation_bonus_bp: int = 0
    harvest_bonus_pct: int = 0
    free_water: int = 0  # 1 = 免澆水
    harvest_loss: int = 0  # 大風颳走：收成時每格少 N 個
    schedule_type: Literal["once", "daily", "weekly"]
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_time: str | None = Field(default=None, pattern=TIME_PATTERN)  # 台灣時間 HH:MM
    end_time: str | None = Field(default=None, pattern=END_TIME_PATTERN)
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(default=[], max_length=7)  # 0 = 週一
    enabled: bool = True


def _minutes(value: str | None) -> int | None:
    if value is None:
        return None
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _event_data(body: EventBody) -> dict:
    data = body.model_dump(exclude={"start_time", "end_time", "weekdays"})
    data["starts_at"] = _aware(body.starts_at) if body.starts_at else None
    data["ends_at"] = _aware(body.ends_at) if body.ends_at else None
    data["start_minute"], data["end_minute"] = _minutes(body.start_time), _minutes(body.end_time)
    data["weekdays"] = sum(1 << d for d in set(body.weekdays)) or None
    return data


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/events", status_code=201)
async def create_event(request: Request, body: EventBody, actor: Actor) -> dict:
    return await _call(_social(request).create_event, actor, _event_data(body))


@router.put("/events/{event_id}")
async def update_event(request: Request, event_id: EventId, body: EventBody, actor: Actor) -> dict:
    return await _call(_social(request).update_event, actor, event_id, _event_data(body))


@router.put("/events/{event_id}/enabled")
async def set_event_enabled(request: Request, event_id: EventId, body: EnabledBody, actor: Actor) -> dict:
    return await _call(_social(request).set_event_enabled, actor, event_id, body.enabled)


@router.delete("/events/{event_id}")
async def delete_event(request: Request, event_id: EventId, actor: Actor) -> dict:
    return await _call(_social(request).delete_event, actor, event_id)


# ---------- 調整玩家：背包、土地、建築、動物 ----------

PlotNo = Annotated[int, Path(ge=1, le=10_000)]
Quality = Literal["normal", "good", "rare", "legendary"]
Mutation = Literal["none", "giant", "golden", "fast", "variant"]


class InventoryBody(BaseModel):
    item_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    quality: Quality = "normal"
    mutation: Mutation = "none"
    quantity: int = Field(ge=0, le=balance.ADMIN_MAX_ITEM_QUANTITY)  # 0 = 刪除這筆庫存


class FarmValuesBody(BaseModel):
    gold: int | None = Field(default=None, ge=0, le=balance.ADMIN_MAX_GOLD)
    level: int | None = Field(default=None, ge=1, le=balance.ADMIN_MAX_LEVEL)


class PlotsBody(BaseModel):
    count: int = Field(ge=1, le=balance.ADMIN_MAX_PLOTS)


class PlantBody(BaseModel):
    crop_code: str = Field(pattern=CODE_PATTERN)
    quality: Quality = "normal"
    mutation: Mutation = "none"
    watered: bool = False
    ready_now: bool = False


class BuildingBody(BaseModel):
    building_code: str = Field(pattern=CODE_PATTERN)


class AnimalsBody(BaseModel):
    animal_code: str = Field(pattern=CODE_PATTERN)
    quantity: int = Field(ge=1, le=balance.ANIMAL_MAX_BUY)


@router.put("/players/{player_id}/inventory")
async def set_inventory(request: Request, player_id: PlayerId, body: InventoryBody, actor: Actor) -> dict:
    return await _call(
        _social(request).set_inventory, actor, player_id, body.item_code, body.quality, body.mutation, body.quantity
    )


@router.put("/players/{player_id}/farm")
async def set_farm_values(request: Request, player_id: PlayerId, body: FarmValuesBody, actor: Actor) -> dict:
    return await _call(
        lambda: _social(request).set_farm_values(actor, player_id, gold=body.gold, level=body.level)
    )


class TransferGroupBody(BaseModel):
    group_id: int = Field(ge=1, le=MAX_BIGINT)
    reason: str = Field(min_length=1, max_length=balance.GROUP_TRANSFER_REASON_MAX_LEN)
    skip_cooldown: bool = False


@router.post("/players/{player_id}/transfer-group")
async def transfer_group(request: Request, player_id: PlayerId, body: TransferGroupBody, actor: Actor) -> dict:
    return await _call(
        lambda: _social(request).admin_transfer_farm(
            actor, player_id, body.group_id, body.reason, skip_cooldown=body.skip_cooldown
        )
    )


@router.post("/players/{player_id}/plots", status_code=201)
async def add_plots(request: Request, player_id: PlayerId, body: PlotsBody, actor: Actor) -> dict:
    return await _call(_social(request).add_plots, actor, player_id, body.count)


@router.delete("/players/{player_id}/plots/{plot_number}")
async def delete_plot(request: Request, player_id: PlayerId, plot_number: PlotNo, actor: Actor) -> dict:
    return await _call(_social(request).delete_plot, actor, player_id, plot_number)


@router.put("/players/{player_id}/plots/{plot_number}/crop")
async def plant_for_player(
    request: Request, player_id: PlayerId, plot_number: PlotNo, body: PlantBody, actor: Actor
) -> dict:
    return await _call(
        lambda: _social(request).plant_for_player(
            actor,
            player_id,
            plot_number,
            body.crop_code,
            body.quality,
            body.mutation,
            watered=body.watered,
            ready_now=body.ready_now,
        )
    )


@router.delete("/players/{player_id}/plots/{plot_number}/crop")
async def clear_plot(request: Request, player_id: PlayerId, plot_number: PlotNo, actor: Actor) -> dict:
    return await _call(_social(request).clear_plot_for_player, actor, player_id, plot_number)


@router.post("/players/{player_id}/buildings", status_code=201)
async def add_building(request: Request, player_id: PlayerId, body: BuildingBody, actor: Actor) -> dict:
    return await _call(_social(request).add_building, actor, player_id, body.building_code)


@router.delete("/players/{player_id}/buildings/{building_code}/{slot}")
async def delete_building(
    request: Request,
    player_id: PlayerId,
    building_code: CatalogCode,
    slot: Annotated[int, Path(ge=1, le=100)],
    actor: Actor,
) -> dict:
    return await _call(_social(request).delete_building, actor, player_id, building_code, slot)


@router.post("/players/{player_id}/animals", status_code=201)
async def add_animals(request: Request, player_id: PlayerId, body: AnimalsBody, actor: Actor) -> dict:
    return await _call(_social(request).add_animals_for_player, actor, player_id, body.animal_code, body.quantity)


@router.delete("/players/{player_id}/animals/{animal_no}")
async def delete_animal(
    request: Request, player_id: PlayerId, animal_no: Annotated[int, Path(ge=1, le=1_000_000)], actor: Actor
) -> dict:
    return await _call(_social(request).delete_animal_for_player, actor, player_id, animal_no)


# ---------- 排名獎勵規則 ----------


class RankingReward(BaseModel):
    reward_type: Literal["gold", "fertilizer", "earthworm", "patrol_ticket"]
    amount: int  # 範圍由服務層依獎勵類型檢查


class RankingRuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scope: Literal["group", "individual"]
    metric: Literal["money", "sales", "goods"]
    period: Literal["daily", "weekly", "monthly"]
    rewards: list[RankingReward] = Field(min_length=1, max_length=balance.RANKING_MAX_RANKS)
    enabled: bool = True


@router.post("/ranking-rules", status_code=201)
async def create_ranking_rule(request: Request, body: RankingRuleBody, actor: Actor) -> dict:
    return await _call(_social(request).create_ranking_rule, actor, body.model_dump())


@router.post("/ranking-rules/settle-due")
async def settle_due_rankings(request: Request, actor: Actor) -> dict:
    return {"settled": await _call(_social(request).settle_due_rankings, actor)}


@router.put("/ranking-rules/{rule_id}")
async def update_ranking_rule(request: Request, rule_id: EventId, body: RankingRuleBody, actor: Actor) -> dict:
    return await _call(_social(request).update_ranking_rule, actor, rule_id, body.model_dump())


@router.put("/ranking-rules/{rule_id}/enabled")
async def set_ranking_rule_enabled(request: Request, rule_id: EventId, body: EnabledBody, actor: Actor) -> dict:
    return await _call(_social(request).set_ranking_rule_enabled, actor, rule_id, body.enabled)


@router.delete("/ranking-rules/{rule_id}")
async def delete_ranking_rule(request: Request, rule_id: EventId, actor: Actor) -> dict:
    return await _call(_social(request).delete_ranking_rule, actor, rule_id)
