"""遊戲操作服務。

每個公開方法都是一個完整的資料庫交易：成功時資料與交易紀錄一起提交，
失敗時（包含遊戲規則錯誤）全部回滾。方法只接受基本型別參數，
不依賴 CMD、FastAPI 或 Discord，未來的介面可直接呼叫。
"""

import copy
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.clock import TAIPEI_TZ, Clock, SystemClock
from app.db.models import CropType, Farm, FarmEffect
from app.game import balance, exploration, genetics, rules
from app.game import events as event_rules
from app.game.errors import (
    AlreadyExistsError,
    HomeGroupLockedError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    NotGroupMemberError,
    PlotEmptyError,
    PlotNotReadyError,
    PlotNotWateredError,
    PlotOccupiedError,
    PlotWitheredError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import EventType, PlotState
from app.repositories import animals as animals_repo
from app.repositories import catalog as catalog_repo
from app.repositories import community as community_repo
from app.repositories import crops as crops_repo
from app.repositories import events as events_repo
from app.repositories import farm_stats as farm_stats_repo
from app.repositories import farms as farms_repo
from app.repositories import genetics as genetics_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.repositories import items as items_repo
from app.repositories import players as players_repo
from app.repositories import steals as steals_repo
from app.repositories import transactions as tx_repo
from app.services import activity
from app.services.dto import (
    ActionResult,
    Backpack,
    BackpackLine,
    CropInfo,
    EffectStatus,
    FarmStatus,
    Inventory,
    InventoryEntry,
    PlayerInfo,
    PlotInfo,
    ToolStatus,
    TransactionInfo,
)


def _crop_info(crop: CropType) -> CropInfo:
    return CropInfo(
        code=crop.code,
        name=crop.name,
        seed_price=crop.seed_price,
        grow_seconds=crop.grow_seconds,
        harvest_quantity=crop.harvest_quantity,
        sell_price=crop.sell_price,
        shop_buyable=crop.shop_buyable,
    )


class GameService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock | None = None,
        rng: Callable[[], float] | None = None,
        genetics_rng: Callable[[], float] | None = None,
        dice: Callable[[], int] | None = None,
        explore_rng: Callable[[], float] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()
        # 防守判定用的亂數來源，回傳 [0, 1) 的浮點數；測試可注入固定值
        self._rng = rng or random.SystemRandom().random
        # 播種時品質與突變判定用的亂數來源（與防守分開，互不影響）
        self._genetics_rng = genetics_rng or random.SystemRandom().random
        # 自動播種機挑選種子用
        self._pick = random.SystemRandom().choice
        # 賭場擲數字（1–10）；測試可注入固定值
        self._dice = dice or (lambda: random.SystemRandom().randint(balance.GAMBLE_MIN, balance.GAMBLE_MAX))
        # 山洞探索掉落判定（回傳 [0, 1)）；測試可注入固定值
        self._explore_rng = explore_rng or random.SystemRandom().random
        self._context_group_id: int | None = None

    def in_group_context(self, group_id: int | None) -> "GameService":
        """回傳記錄操作所在群組（context_group_id）的服務副本；只做紀錄，不影響農場歸屬或排行。"""
        clone = copy.copy(self)
        clone._context_group_id = group_id
        return clone

    @contextmanager
    def _transaction(self) -> Iterator[Session]:
        with self._session_factory.begin() as session:
            yield session

    # ---------- 共用的內部方法 ----------

    @staticmethod
    def _locked_farm(session: Session, player_id: int) -> Farm:
        farm = farms_repo.get_by_player(session, player_id, for_update=True)
        if farm is None:
            if players_repo.get(session, player_id) is None:
                raise NotFoundError(f"找不到玩家 #{player_id}。")
            raise NotFoundError("這位玩家還沒有農場，請先建立農場（farm start）。")
        return farm

    @staticmethod
    def _crop(session: Session, key: str, *, require_active: bool = False) -> CropType:
        crop = crops_repo.find(session, key)
        if crop is None:
            names = "、".join(f"{c.code}（{c.name}）" for c in crops_repo.list_all(session, active_only=True))
            raise NotFoundError(f"沒有「{key}」這種作物。可用作物：{names}")
        if require_active and not crop.active:
            raise ValidationError(f"{crop.name}已停用，不能購買種子或種植（已持有的作物仍可收成、販售）。")
        return crop

    def _active_events(self, session: Session) -> list:
        return events_repo.active(session, self._clock.now())

    @staticmethod
    def _genetics(session: Session) -> dict:
        """目前生效的品質與突變設定（最新版本）。"""
        _version, settings = genetics_repo.current(session)
        return settings

    @staticmethod
    def _ensure_space(session: Session, farm: Farm, incoming: int) -> None:
        used = inventory_repo.total_quantity(session, farm.id)
        if used + incoming > farm.warehouse_capacity:
            raise WarehouseFullError(f"倉庫空間不足：目前 {used}/{farm.warehouse_capacity}，需要再放入 {incoming} 個。")

    def _record(self, session: Session, farm: Farm, event: EventType, description: str, **kw) -> None:
        now = self._clock.now()
        farm.updated_at = now
        if "context_group_id" not in kw:
            ctx = self._context_group_id
            # 只有玩家確實是該群組成員時才記錄上下文，避免把操作歸到無關的群組
            if ctx is not None and not groups_repo.is_member(session, ctx, farm.player_id):
                ctx = None
            kw["context_group_id"] = ctx
        tx = tx_repo.add(
            session,
            farm_id=farm.id,
            event_type=event.value,
            description=description,
            created_at=now,
            **kw,
        )
        # 同一筆交易中更新排行用的累積統計，並計入群組任務與競賽（只有成功提交的操作才會計分）
        farm_stats_repo.on_transaction(session, farm, tx)
        activity.on_event(session, farm, tx)

    # ---------- 玩家 ----------

    def create_player(
        self,
        display_name: str,
        external_platform: str | None = None,
        external_user_id: str | None = None,
    ) -> PlayerInfo:
        name = rules.validate_name(display_name, label="玩家名稱", max_len=balance.PLAYER_NAME_MAX_LEN)
        if (external_platform is None) != (external_user_id is None):
            raise ValidationError("外部平台與外部使用者 ID 必須同時提供。")
        try:
            with self._transaction() as session:
                player = players_repo.create(session, name, external_platform, external_user_id)
                return PlayerInfo(player.id, player.display_name, False, external_platform, external_user_id)
        except IntegrityError:
            raise AlreadyExistsError("此外部帳號已綁定其他玩家。") from None

    def get_player_by_external(self, platform: str, user_id: str) -> PlayerInfo | None:
        """供未來 Discord 等平台依外部使用者 ID 找到對應玩家。"""
        with self._transaction() as session:
            player = players_repo.get_by_external(session, platform, user_id)
            if player is None:
                return None
            has_farm = farms_repo.get_by_player(session, player.id) is not None
            return PlayerInfo(player.id, player.display_name, has_farm, platform, user_id)

    def get_player(self, player_id: int) -> PlayerInfo:
        with self._transaction() as session:
            player = players_repo.get(session, player_id)
            if player is None:
                raise NotFoundError(f"找不到玩家 #{player_id}。")
            has_farm = farms_repo.get_by_player(session, player.id) is not None
            return PlayerInfo(
                player.id, player.display_name, has_farm, player.external_platform, player.external_user_id
            )

    def list_players(self, limit: int = 100) -> list[PlayerInfo]:
        with self._transaction() as session:
            players = players_repo.list_all(session, limit)
            return [
                PlayerInfo(
                    p.id,
                    p.display_name,
                    farms_repo.get_by_player(session, p.id) is not None,
                    p.external_platform,
                    p.external_user_id,
                )
                for p in players
            ]

    def search_players(
        self, keyword: str | None = None, page: int = 1, page_size: int = 20
    ) -> tuple[list[PlayerInfo], int]:
        """依名稱關鍵字或 ID 搜尋玩家並分頁；回傳 (本頁玩家, 符合總數)。"""
        page_size = max(1, min(page_size, 100))
        page = max(1, page)
        keyword = (keyword or "").strip()[: balance.PLAYER_NAME_MAX_LEN] or None
        with self._transaction() as session:
            rows, total = players_repo.search(session, keyword, page_size, (page - 1) * page_size)
            return [
                PlayerInfo(p.id, p.display_name, has_farm, p.external_platform, p.external_user_id)
                for p, has_farm in rows
            ], total

    # ---------- 農場 ----------

    def create_farm(self, player_id: int, farm_name: str | None = None, *, home_group_id: int | None) -> FarmStatus:
        """建立全域農場並固定主要所屬群組；玩家若不是該群組成員會自動加入。"""
        if home_group_id is None:
            raise ValidationError("建立農場必須指定所屬群組：farm start <群組ID>，或先用 group use <群組ID> 選擇群組。")
        try:
            with self._transaction() as session:
                player = players_repo.get(session, player_id, for_update=True)
                if player is None:
                    raise NotFoundError(f"找不到玩家 #{player_id}。")
                if farms_repo.get_by_player(session, player_id) is not None:
                    raise AlreadyExistsError("這位玩家已經擁有農場。")
                group = groups_repo.get(session, home_group_id)
                if group is None:
                    raise NotFoundError(f"找不到群組 #{home_group_id}。")
                name = rules.validate_name(
                    farm_name or f"{player.display_name}的農場",
                    label="農場名稱",
                    max_len=balance.FARM_NAME_MAX_LEN,
                )
                farm = farms_repo.create(
                    session,
                    player_id=player_id,
                    name=name,
                    level=balance.INITIAL_LEVEL,
                    gold=balance.INITIAL_GOLD,
                    warehouse_capacity=balance.INITIAL_WAREHOUSE_CAPACITY,
                    home_group_id=home_group_id,
                )
                groups_repo.add_member(session, home_group_id, player_id)
                farms_repo.add_plots(session, farm.id, balance.INITIAL_PLOTS)
                farm_stats_repo.ensure_row(session, farm)
                # 初始金幣也記為一筆入帳，使所有交易的 gold_delta 加總等於農場金幣
                self._record(
                    session,
                    farm,
                    EventType.CREATE_FARM,
                    f"在群組「{group.name}」建立農場「{name}」，獲得初始金幣 {balance.INITIAL_GOLD}",
                    gold_delta=balance.INITIAL_GOLD,
                    context_group_id=home_group_id,
                )
                return self._status(session, farm, player.display_name)
        except IntegrityError:
            raise AlreadyExistsError("這位玩家已經擁有農場。") from None

    def get_home_group(self, player_id: int) -> tuple[int, str] | None:
        """回傳農場主要所屬群組 (群組ID, 名稱)；舊農場尚未設定時回傳 None。"""
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            if farm.home_group_id is None:
                return None
            group = groups_repo.get(session, farm.home_group_id)
            return group.id, group.name

    def set_home_group(self, player_id: int, group_id: int) -> ActionResult:
        """為尚未綁定群組的舊農場做一次性設定；已綁定的農場一律拒絕（資料庫觸發器另有保護）。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if farm.home_group_id is not None:
                current = groups_repo.get(session, farm.home_group_id)
                raise HomeGroupLockedError(
                    f"農場已屬於群組 #{current.id}「{current.name}」，"
                    "不可更改一次性設定（只適用於尚未綁定群組的舊農場）。"
                    f"為避免頻繁切換群組操縱排行，變更所屬群組請使用群組轉移（每 {_cooldown_text()} 一次）。"
                )
            group = groups_repo.get(session, group_id)
            if group is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            farm.home_group_id = group_id
            groups_repo.add_member(session, group_id, player_id)
            self._record(
                session,
                farm,
                EventType.BIND_GROUP,
                f"農場設定主要所屬群組為「{group.name}」（一次性設定）",
                context_group_id=group_id,
            )
            return ActionResult(
                f"農場已設定為屬於群組 #{group.id}「{group.name}」；"
                f"之後只能透過群組轉移變更（每 {_cooldown_text()} 一次）。",
                farm.gold,
            )

    # ---------- 轉移所屬群組（7 天冷卻） ----------

    def _transfer_view(self, session: Session, farm: Farm, target_group_id: int | None) -> dict:
        now = self._clock.now()
        cooldown = timedelta(seconds=balance.GROUP_TRANSFER_COOLDOWN_SECONDS)
        last = farm.last_group_transfer_at
        next_at = last + cooldown if last is not None else None
        remaining = rules.remaining_seconds(next_at, now) if next_at is not None else 0
        home = groups_repo.get(session, farm.home_group_id) if farm.home_group_id is not None else None
        target = groups_repo.get(session, target_group_id) if target_group_id is not None else None
        return {
            "farm_id": farm.id,
            "player_id": farm.player_id,
            "home_group_id": home.id if home else None,
            "home_group_name": home.name if home else None,
            "target_group_id": target.id if target else None,
            "target_group_name": target.name if target else None,
            "is_member": bool(target and groups_repo.is_member(session, target.id, farm.player_id)),
            "last_transfer_at": last,
            "next_transfer_at": next_at if remaining > 0 else None,
            "remaining_seconds": remaining,
            "can_transfer": remaining <= 0,
            "now": now,
        }

    def group_transfer_status(self, player_id: int, target_group_id: int | None = None) -> dict:
        """目前所屬群組、冷卻狀態與（指定時）目標群組；只查詢，不修改。"""
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            return self._transfer_view(session, farm, target_group_id)

    @staticmethod
    def _cooldown_error(view: dict) -> HomeGroupLockedError:
        left = long_duration(view["remaining_seconds"])
        until = view["next_transfer_at"].astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")
        return HomeGroupLockedError(
            f"群組轉移冷卻中：每次轉移後需等待 {_cooldown_text()}，還需要 {left}，下次可轉移時間 {until}（台灣時間）。"
        )

    def _apply_transfer(self, session: Session, farm: Farm, group, now: datetime, description: str) -> str | None:
        old = groups_repo.get(session, farm.home_group_id) if farm.home_group_id is not None else None
        farm.home_group_id = group.id
        farm.last_group_transfer_at = now
        self._record(session, farm, EventType.TRANSFER_GROUP, description[:200], context_group_id=group.id)
        try:
            # 立即送出 UPDATE，讓資料庫觸發器的冷卻檢查在本交易內生效
            session.flush()
        except IntegrityError:
            raise HomeGroupLockedError("群組轉移被資料庫保護拒絕（冷卻中或資料不一致），請稍後再試。") from None
        return old.name if old else None

    def transfer_home_group(self, player_id: int, group_id: int) -> ActionResult:
        """把農場的主要所屬群組轉移到 group_id；只更新同一座農場，不複製或刪除任何資產。

        在鎖定農場列的交易中檢查冷卻，並由資料庫觸發器再次檢查；兩個伺服器同時提交時只有一筆成功。
        歷史交易、任務、競賽與排名獎勵紀錄不改寫；之後的群組排行立即依新群組計算。
        """
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if farm.home_group_id is None:
                raise ValidationError("這座農場尚未設定所屬群組，請先做一次性設定。")
            if farm.home_group_id == group_id:
                raise ValidationError("農場已經屬於這個群組，不需要轉移。")
            group = groups_repo.get(session, group_id)
            if group is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            if not groups_repo.is_member(session, group_id, player_id):
                raise NotGroupMemberError("你不是目標群組的成員，無法把農場轉移過去。")
            view = self._transfer_view(session, farm, group_id)
            if not view["can_transfer"]:
                raise self._cooldown_error(view)
            now = self._clock.now()
            old_name = self._apply_transfer(
                session, farm, group, now, f"農場所屬群組由「{view['home_group_name']}」轉移到「{group.name}」"
            )
            next_at = (now + timedelta(seconds=balance.GROUP_TRANSFER_COOLDOWN_SECONDS)).astimezone(TAIPEI_TZ)
            return ActionResult(
                f"農場已從「{old_name}」轉移到「{group.name}」。之後的群組排行與任務、競賽計分都以新群組計算；"
                f"下次可轉移時間 {next_at.strftime('%Y-%m-%d %H:%M')}（台灣時間）。",
                farm.gold,
            )

    def _status(self, session: Session, farm: Farm, player_name: str) -> FarmStatus:
        now = self._clock.now()
        plots = []
        for plot in farms_repo.list_plots(session, farm.id):
            crop = plot.crop_type
            plots.append(
                PlotInfo(
                    plot_number=plot.plot_number,
                    state=rules.plot_state(plot.crop_type_id, plot.watered_at, plot.ready_at, now),
                    crop_code=crop.code if crop else None,
                    crop_name=crop.name if crop else None,
                    planted_at=plot.planted_at,
                    ready_at=plot.ready_at,
                    remaining_seconds=rules.remaining_seconds(plot.ready_at, now),
                    watered_at=plot.watered_at,
                    remaining_quantity=plot.remaining_quantity,
                    fertilized=plot.fertilized,
                    quality=plot.quality,
                    mutation=plot.mutation,
                )
            )
        return FarmStatus(
            farm_id=farm.id,
            player_id=farm.player_id,
            home_group_id=farm.home_group_id,
            player_name=player_name,
            name=farm.name,
            level=farm.level,
            gold=farm.gold,
            warehouse_used=inventory_repo.total_quantity(session, farm.id),
            warehouse_capacity=farm.warehouse_capacity,
            plots=plots,
            now=now,
        )

    def get_farm_status(self, player_id: int) -> FarmStatus:
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)  # 產生正確的錯誤訊息
            return self._status(session, farm, farm.player.display_name)

    # ---------- 商店與倉庫 ----------

    def get_market_prices(self) -> list[CropInfo]:
        with self._transaction() as session:
            return [_crop_info(c) for c in crops_repo.list_all(session, active_only=True)]

    def get_inventory(self, player_id: int) -> Inventory:
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            names = catalog_repo.item_names(session)
            items = [
                InventoryEntry(
                    i.item_code,
                    names.get(i.item_code, i.item_code) + genetics_label(i.quality, i.mutation),
                    i.quantity,
                )
                for i in inventory_repo.list_items(session, farm.id)
            ]
            return Inventory(items, sum(i.quantity for i in items), farm.warehouse_capacity)

    def buy_seeds(self, player_id: int, crop_key: str, quantity: int) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            crop = self._crop(session, crop_key, require_active=True)
            if not crop.shop_buyable:
                raise ValidationError(f"商店不販售{crop.name}種子，只能從山洞探索或稀有作物培育器取得。")
            cost = crop.seed_price * quantity
            if farm.gold < cost:
                raise InsufficientGoldError(f"金幣不足：需要 {cost}，目前只有 {farm.gold}。")
            self._ensure_space(session, farm, quantity)
            farm.gold -= cost
            item = rules.seed_item_code(crop.code)
            inventory_repo.add(session, farm.id, item, quantity)
            self._record(
                session,
                farm,
                EventType.BUY_SEED,
                f"購買 {quantity} 顆{crop.name}種子，金幣 -{cost}",
                item_code=item,
                quantity=quantity,
                gold_delta=-cost,
            )
            auto = self._auto_plant(session, farm)
            return ActionResult(
                f"已購買 {quantity} 顆{crop.name}種子，花費 {cost} 金幣。{auto}", farm.gold, -cost, item, quantity
            )

    # ---------- 種植與收成 ----------

    def plant_crop(self, player_id: int, plot_number: int, crop_key: str) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            crop = self._crop(session, crop_key, require_active=True)
            plot = farms_repo.get_plot(session, farm.id, plot_number, for_update=True)
            if plot is None:
                raise NotFoundError(f"沒有第 {plot_number} 格土地。")
            if plot.crop_type_id is not None:
                raise PlotOccupiedError(f"第 {plot_number} 格土地已經種植作物。")
            item = rules.seed_item_code(crop.code)
            if not inventory_repo.remove(session, farm.id, item, 1):
                raise InsufficientItemsError(f"沒有{crop.name}種子，請先購買（buy {crop.code} 1）。")
            label, seconds = self._plant(session, farm, plot, crop)
            if seconds is not None:
                tail = f"已自動澆水，{rules.format_duration(seconds)}後成熟。"
            else:
                tail = f"請記得澆水（water {plot_number}），澆水後開始生長。"
            return ActionResult(
                f"已在第 {plot_number} 格種下{crop.name}{label}，{tail}", farm.gold, item_code=item, quantity=1
            )

    def _plant(self, session: Session, farm: Farm, plot, crop: CropType, *, source: str = "") -> tuple[str, int | None]:
        """在已鎖定、空著的土地種下一顆（種子由呼叫端扣除）。回傳 (品質標籤, 自動澆水後的生長秒數或 None)。"""
        now = self._clock.now()
        # 播種時一次決定整批的品質與主要突變，之後固定不變（收成、偷菜、重啟都不重新抽取）
        settings = self._genetics(session)
        # 限時事件的品質、突變與收成加成在播種當下決定，之後事件結束也不會改變
        active = self._active_events(session)
        q_bonus, m_bonus, h_bonus = (event_rules.bonus(active, e, crop.id) for e in event_rules.PLANT_EFFECTS)
        quality = genetics.roll_quality(
            genetics.effective_quality_bp(settings, farm.level, q_bonus.value), self._genetics_rng()
        )
        mutation = genetics.roll_mutation(settings, self._genetics_rng(), m_bonus.value)
        quantity = genetics.harvest_quantity(crop.harvest_quantity, mutation, settings, h_bonus.value)
        farms_repo.plant(plot, crop.id, quantity, now, quality, mutation)
        label = genetics_label(quality, mutation)
        applied = event_rules.merge_applied(q_bonus, m_bonus, h_bonus)
        self._record(
            session,
            farm,
            EventType.PLANT,
            f"{source}在第 {plot.plot_number} 格土地種植{crop.name}{label}{_events_note(applied)}",
            item_code=rules.seed_item_code(crop.code),
            quantity=1,
            quality=quality,
            mutation=mutation,
            applied_events=applied,
        )
        # 免澆水活動期間，或持有並啟用自動澆水器時，種下後立即澆水
        free = event_rules.bonus(active, "free_water", crop.id)
        if free.value > 0:
            return label, self._water(session, farm, plot, crop, now, auto=True, reason=free)
        if items_repo.tool_enabled(session, farm.id, balance.TOOL_AUTO_WATERER):
            return label, self._water(session, farm, plot, crop, now, auto=True)
        return label, None

    def _auto_plant(self, session: Session, farm: Farm) -> str:
        """自動播種機：從背包隨機挑選種子把所有空地種滿。未持有或停用時不做任何事，回傳空字串。"""
        if not items_repo.tool_enabled(session, farm.id, balance.TOOL_AUTO_PLANTER):
            return ""
        empty = [p for p in farms_repo.list_plots(session, farm.id) if p.crop_type_id is None]
        if not empty:
            return ""
        stock = {
            c: inventory_repo.get_quantity(session, farm.id, rules.seed_item_code(c.code), "normal", "none")
            for c in crops_repo.list_all(session, active_only=True)
        }
        planted: dict[str, int] = {}
        for plot in empty:
            choices = [c for c, n in stock.items() if n > 0]
            if not choices:
                break
            crop = self._pick(choices)
            inventory_repo.remove(session, farm.id, rules.seed_item_code(crop.code), 1)
            stock[crop] -= 1
            plot = farms_repo.get_plot(session, farm.id, plot.plot_number, for_update=True)
            self._plant(session, farm, plot, crop, source="自動播種機：")
            planted[crop.name] = planted.get(crop.name, 0) + 1
        if not planted:
            return " 🤖 自動播種機：背包沒有種子可以種。"
        detail = "、".join(f"{name}×{n}" for name, n in planted.items())
        return f" 🤖 自動播種機種下 {sum(planted.values())} 格（{detail}）。"

    def _water(
        self, session: Session, farm: Farm, plot, crop: CropType, now, *, auto: bool = False, reason=None
    ) -> int:
        earthworm = items_repo.effect_active(session, farm.id, balance.EFFECT_EARTHWORM, now)
        grow = event_rules.bonus(self._active_events(session), "grow_reduction_pct", crop.id)
        # 快速生長突變也只在澆水後才開始計時，不會繞過澆水與乾枯規則
        seconds = genetics.grow_seconds(
            crop.grow_seconds, plot.mutation, earthworm, self._genetics(session), grow.value
        )
        farms_repo.water(plot, now, rules.ready_time(now, seconds))
        extra = ("（蚯蚓加速）" if earthworm else "") + ("（快速生長）" if plot.mutation == "fast" else "")
        applied = event_rules.merge_applied(grow, *([reason] if reason else []))
        source = "免澆水活動" if reason else ("自動澆水器" if auto else "澆水")
        self._record(
            session,
            farm,
            EventType.WATER,
            f"{source}：第 {plot.plot_number} 格{crop.name}開始生長{extra}{_events_note(applied)}",
            quantity=0,
            applied_events=applied,
        )
        return seconds

    def _plot_for_action(self, session: Session, farm: Farm, plot_number: int):
        plot = farms_repo.get_plot(session, farm.id, plot_number, for_update=True)
        if plot is None:
            raise NotFoundError(f"沒有第 {plot_number} 格土地。")
        now = self._clock.now()
        return plot, now, rules.plot_state(plot.crop_type_id, plot.watered_at, plot.ready_at, now)

    def water_crop(self, player_id: int, plot_number: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plot, now, state = self._plot_for_action(session, farm, plot_number)
            if state == PlotState.EMPTY:
                raise PlotEmptyError(f"第 {plot_number} 格土地沒有作物。")
            if state != PlotState.WAITING_WATER:
                raise ValidationError(f"第 {plot_number} 格已經澆過水了。")
            crop = crops_repo.get(session, plot.crop_type_id)
            seconds = self._water(session, farm, plot, crop, now)
            return ActionResult(
                f"已為第 {plot_number} 格的{crop.name}澆水，{rules.format_duration(seconds)}後成熟。", farm.gold
            )

    def harvest_crop(self, player_id: int, plot_number: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plot, now, state = self._plot_for_action(session, farm, plot_number)
            if state == PlotState.EMPTY:
                raise PlotEmptyError(f"第 {plot_number} 格土地沒有作物。")
            crop = crops_repo.get(session, plot.crop_type_id)
            if state == PlotState.WAITING_WATER:
                raise PlotNotWateredError(f"第 {plot_number} 格的{crop.name}還沒澆水（water {plot_number}）。")
            if state == PlotState.GROWING:
                left = rules.format_duration(rules.remaining_seconds(plot.ready_at, now))
                raise PlotNotReadyError(f"第 {plot_number} 格的{crop.name}尚未成熟，還需要 {left}。")
            if state == PlotState.WITHERED:
                raise PlotWitheredError(f"第 {plot_number} 格的{crop.name}已經乾枯，請用 clear {plot_number} 清除。")
            message, quantity, item = self._harvest_locked(session, farm, plot, crop)
            message += self._auto_plant(session, farm)
            return ActionResult(message, farm.gold, item_code=item, quantity=quantity)

    def _harvest_locked(self, session: Session, farm: Farm, plot, crop: CropType) -> tuple[str, int, str]:
        """收成一格已成熟、已鎖定的土地；倉庫不足時在修改任何資料前丟出 WarehouseFullError。"""
        # 大風颳走：收成時每格少 N 個，至少保留 1 個（在收成當下判斷）
        wind = event_rules.bonus(self._active_events(session), "harvest_loss", crop.id)
        lost = min(wind.value, max(0, plot.remaining_quantity - 1))
        quantity = plot.remaining_quantity - lost
        auto_sell = items_repo.tool_enabled(session, farm.id, balance.TOOL_AUTO_SELLER)
        if not auto_sell:
            # 倉庫空間不足時拒絕收成，作物保留在土地上，不會遺失（自動販售會立即賣出，不佔倉庫）
            self._ensure_space(session, farm, quantity)
        quality, mutation = plot.quality, plot.mutation
        item = genetics.produce_code(crop, mutation)
        name = genetics.produce_name(crop, mutation) + genetics_label(quality, mutation)
        stolen = steals_repo.stolen_quantity(session, plot.id, plot.planting_no)
        inventory_repo.add(session, farm.id, item, quantity, quality, mutation)
        plot_number = plot.plot_number
        farms_repo.clear(plot)  # 收成後土地清空，同一批作物不會再發放
        note = f"（已被偷走 {stolen} 個）" if stolen > 0 else ""
        if lost:
            note += f"（大風颳走 {lost} 個）"
        self._record(
            session,
            farm,
            EventType.HARVEST,
            f"收成第 {plot_number} 格，獲得 {quantity} 個{name}{note}",
            item_code=item,
            quantity=quantity,
            quality=quality,
            mutation=mutation,
            applied_events=event_rules.merge_applied(wind) if lost else None,
        )
        message = f"收成第 {plot_number} 格，獲得 {quantity} 個{name}{note}。"
        if auto_sell:
            price, income = self._sell_locked(session, farm, crop, quantity, quality, mutation, source="自動販售機：")
            message += f" 🏪 自動販售機賣出（單價 {price}），金幣 +{income}。"
        return message, quantity, item

    def clear_withered(self, player_id: int, plot_number: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plot, now, state = self._plot_for_action(session, farm, plot_number)
            if state != PlotState.WITHERED:
                raise ValidationError(f"第 {plot_number} 格沒有乾枯的作物可清除。")
            crop = crops_repo.get(session, plot.crop_type_id)
            farms_repo.clear(plot)
            self._record(session, farm, EventType.CLEAR, f"清除第 {plot_number} 格乾枯的{crop.name}")
            auto = self._auto_plant(session, farm)
            return ActionResult(f"已清除第 {plot_number} 格乾枯的{crop.name}，土地恢復為空地。{auto}", farm.gold)

    # ---------- 一鍵操作（整批在同一筆交易中完成，只鎖一次農場） ----------

    def _plots_in_state(self, session: Session, farm: Farm, state: PlotState) -> list:
        now = self._clock.now()
        return [
            farms_repo.get_plot(session, farm.id, p.plot_number, for_update=True)
            for p in farms_repo.list_plots(session, farm.id)
            if rules.plot_state(p.crop_type_id, p.watered_at, p.ready_at, now) == state
        ]

    def plant_all(self, player_id: int, crop_key: str) -> ActionResult:
        """把所有空地種滿同一種作物；種子不夠時用目前金幣自動補買（買到的種子立即種下，不佔倉庫）。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            crop = self._crop(session, crop_key, require_active=True)
            plots = self._plots_in_state(session, farm, PlotState.EMPTY)
            if not plots:
                raise ValidationError("沒有空地可以種植。")
            item = rules.seed_item_code(crop.code)
            have = inventory_repo.get_quantity(session, farm.id, item)
            bought = 0
            if len(plots) > have and not crop.shop_buyable:
                if have <= 0:
                    raise InsufficientItemsError(f"沒有{crop.name}種子（商店不販售，只能從山洞探索或培育取得）。")
            elif len(plots) > have:
                bought = min(len(plots) - have, farm.gold // crop.seed_price)
                if bought <= 0 and have <= 0:
                    raise InsufficientGoldError(f"金幣不足：{crop.name}種子每顆 {crop.seed_price} 金幣。")
                if bought > 0:
                    cost = crop.seed_price * bought
                    farm.gold -= cost
                    inventory_repo.add(session, farm.id, item, bought)
                    self._record(
                        session, farm, EventType.BUY_SEED, f"購買 {bought} 顆{crop.name}種子，金幣 -{cost}",
                        item_code=item, quantity=bought, gold_delta=-cost,
                    )
            count = min(len(plots), have + bought)
            watered = 0
            for plot in plots[:count]:
                inventory_repo.remove(session, farm.id, item, 1)
                _label, seconds = self._plant(session, farm, plot, crop)
                watered += seconds is not None
            text = f"在 {count} 格種下{crop.name}" + (f"（購買 {bought} 顆種子）" if bought else "")
            if count < len(plots):
                reason = "金幣不夠" if crop.shop_buyable else "種子不夠"
                text += f"；{reason}，還有 {len(plots) - count} 格空著"
            if watered == count:
                text += "，已自動澆水。"
            else:
                text += "。記得按「💧 全部澆水」。"
            return ActionResult(text, farm.gold, item_code=item, quantity=count)

    def water_all(self, player_id: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plots = self._plots_in_state(session, farm, PlotState.WAITING_WATER)
            if not plots:
                raise ValidationError("沒有需要澆水的土地。")
            now = self._clock.now()
            seconds = [self._water(session, farm, p, crops_repo.get(session, p.crop_type_id), now) for p in plots]
            return ActionResult(
                f"已為 {len(plots)} 格澆水，最快 {rules.format_duration(min(seconds))}後成熟。", farm.gold,
                quantity=len(plots),
            )

    def harvest_all(self, player_id: int) -> ActionResult:
        """收成所有成熟的土地；倉庫放不下時停在那一格（之前收成的保留，之後的留在土地上），不會遺失作物。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plots = self._plots_in_state(session, farm, PlotState.READY)
            if not plots:
                raise ValidationError("目前沒有可收成的作物。")
            done, total, stopped = 0, 0, ""
            for plot in plots:
                try:
                    _msg, quantity, _item = self._harvest_locked(
                        session, farm, plot, crops_repo.get(session, plot.crop_type_id)
                    )
                except WarehouseFullError as exc:
                    stopped = f" 還有 {len(plots) - done} 格沒收：{exc}"
                    break
                done += 1
                total += quantity
            if not done:
                raise WarehouseFullError(stopped.strip())
            auto = self._auto_plant(session, farm)
            return ActionResult(f"收成 {done} 格，共 {total} 個作物。{stopped}{auto}", farm.gold, quantity=total)

    def clear_all_withered(self, player_id: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plots = self._plots_in_state(session, farm, PlotState.WITHERED)
            if not plots:
                raise ValidationError("沒有乾枯的作物。")
            for plot in plots:
                crop = crops_repo.get(session, plot.crop_type_id)
                farms_repo.clear(plot)
                self._record(session, farm, EventType.CLEAR, f"清除第 {plot.plot_number} 格乾枯的{crop.name}")
            auto = self._auto_plant(session, farm)
            return ActionResult(f"已清除 {len(plots)} 格乾枯的作物。{auto}", farm.gold, quantity=len(plots))

    # ---------- 販售與擴建 ----------

    def sell_crop(
        self, player_id: int, crop_key: str, quantity: int, quality: str = "normal", mutation: str = "none"
    ) -> ActionResult:
        """販售指定品質／突變的農產品；單價依統一公式計算並保存於交易紀錄。"""
        quantity = rules.validate_quantity(quantity)
        quality, mutation = genetics.parse_quality(quality), genetics.parse_mutation(mutation)
        if quality is None:
            raise ValidationError("品質只能是 普通／優良／稀有／傳說。")
        if mutation is None:
            raise ValidationError("突變只能是 無／巨型作物／黃金作物／快速生長／珍稀變種。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            variant_of = crops_repo.find_variant(session, crop_key)
            if variant_of is not None:
                # 珍稀變種是獨立作物，突變固定為 variant
                crop, mutation = variant_of, "variant"
            else:
                crop = self._crop(session, crop_key)
                if mutation == "variant":
                    raise ValidationError(f"珍稀變種請使用變種名稱販售（{crop.variant_name}）。")
            item = genetics.produce_code(crop, mutation)
            name = genetics.produce_name(crop, mutation) + genetics_label(quality, mutation)
            price, income = self._sell_locked(session, farm, crop, quantity, quality, mutation)
            return ActionResult(
                f"已出售 {quantity} 個{name}（單價 {price}），獲得 {income} 金幣。", farm.gold, income, item, quantity
            )

    def _sell_locked(
        self, session: Session, farm: Farm, crop: CropType, quantity: int, quality: str, mutation: str, *,
        source: str = "",
    ) -> tuple[int, int]:
        """從背包扣除並以目前價目（含活動售價加成）賣出；回傳 (單價, 收入)。手動販售與自動販售機共用。"""
        item = genetics.produce_code(crop, mutation)
        name = genetics.produce_name(crop, mutation) + genetics_label(quality, mutation)
        if not inventory_repo.remove(session, farm.id, item, quantity, quality, mutation):
            have = inventory_repo.get_quantity(session, farm.id, item, quality, mutation)
            raise InsufficientItemsError(f"{name}數量不足：想賣 {quantity} 個，倉庫只有 {have} 個。")
        sell = event_rules.bonus(self._active_events(session), "sell_price_pct", crop.id)
        price = event_rules.apply_sell_bonus(
            genetics.unit_price(crop.sell_price, quality, mutation, self._genetics(session)), sell.value
        )
        income = price * quantity
        farm.gold += income
        applied = event_rules.merge_applied(sell)
        self._record(
            session,
            farm,
            EventType.SELL,
            f"{source}出售 {quantity} 個{name}（單價 {price}），金幣 +{income}{_events_note(applied)}",
            item_code=item,
            quantity=quantity,
            gold_delta=income,
            quality=quality,
            mutation=mutation,
            unit_price=price,
            applied_events=applied,
        )
        return price, income

    def upgrade_farm(self, player_id: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            cost = balance.upgrade_price(farms_repo.count_plots(session, farm.id))
            if cost is None:
                raise ValidationError(f"農田已達上限 {balance.MAX_PLOTS} 格，無法再擴建。")
            if farm.gold < cost:
                raise InsufficientGoldError(f"金幣不足：擴建需要 {cost}，目前只有 {farm.gold}。")
            farm.gold -= cost
            farm.level += 1
            numbers = farms_repo.add_plots(session, farm.id, balance.UPGRADE_PLOTS_ADDED)
            total = farms_repo.count_plots(session, farm.id)
            self._record(
                session,
                farm,
                EventType.UPGRADE,
                f"擴建土地 +{len(numbers)} 格（共 {total} 格），金幣 -{cost}",
                quantity=len(numbers),
                gold_delta=-cost,
            )
            auto = self._auto_plant(session, farm)
            nxt = balance.upgrade_price(total)
            tail = f"下一次擴建需要 {nxt} 金幣。" if nxt else "農田已達上限。"
            return ActionResult(
                f"擴建完成！農場升到 {farm.level} 級，新增第 {numbers[0]} 格，"
                f"共 {total} 格土地，花費 {cost} 金幣。{tail}{auto}",
                farm.gold,
                -cost,
                quantity=len(numbers),
            )

    def expand_warehouse(self, player_id: int) -> ActionResult:
        """倉庫擴大卷：立即 +50 格，價格依目前容量遞增，最大 300 格。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            price = balance.warehouse_expansion_price(farm.warehouse_capacity)
            if price is None:
                raise ValidationError(f"倉庫已經是最大容量 {balance.WAREHOUSE_MAX_CAPACITY} 格。")
            if farm.gold < price:
                raise InsufficientGoldError(f"金幣不足：倉庫擴大卷需要 {price}，目前只有 {farm.gold}。")
            before = farm.warehouse_capacity
            farm.gold -= price
            farm.warehouse_capacity = min(before + balance.WAREHOUSE_EXPANSION_STEP, balance.WAREHOUSE_MAX_CAPACITY)
            self._record(
                session,
                farm,
                EventType.BUY_ITEM,
                f"使用倉庫擴大卷：倉庫 {before} → {farm.warehouse_capacity} 格，金幣 -{price}",
                item_code=balance.WAREHOUSE_EXPANSION_CODE,
                quantity=1,
                gold_delta=-price,
            )
            nxt = balance.warehouse_expansion_price(farm.warehouse_capacity)
            tail = f"下一次擴大需要 {nxt} 金幣。" if nxt else "已達最大容量。"
            return ActionResult(
                f"倉庫擴大為 {farm.warehouse_capacity} 格，花費 {price} 金幣。{tail}", farm.gold, -price,
                balance.WAREHOUSE_EXPANSION_CODE, 1,
            )

    # ---------- 賭場 ----------

    @staticmethod
    def gamble_eligible(quality: str, mutation: str) -> bool:
        """只有稀有／傳說品質，或巨型作物／黃金作物／珍稀變種可以下注。"""
        return quality in balance.GAMBLE_QUALITIES or mutation in balance.GAMBLE_MUTATIONS

    def gamble_options(self, player_id: int) -> list[dict]:
        """背包中可以下注的作物（依品質、突變分開）。"""
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            produce = crops_repo.produce_map(session)
            settings = self._genetics(session)
            rows = []
            for i in inventory_repo.list_items(session, farm.id):
                entry = produce.get(i.item_code)
                if entry is None or not self.gamble_eligible(i.quality, i.mutation):
                    continue
                crop = entry[0]
                rows.append({
                    "item_code": i.item_code,
                    "crop_code": crop.code,
                    "quality": i.quality,
                    "mutation": i.mutation,
                    "quantity": i.quantity,
                    "name": genetics.produce_name(crop, i.mutation) + genetics_label(i.quality, i.mutation),
                    "unit_price": genetics.unit_price(crop.sell_price, i.quality, i.mutation, settings),
                })
            return rows

    def gamble(
        self,
        player_id: int,
        item_code: str,
        quality: str,
        mutation: str,
        quantity: int,
        mode: str,
        guess: int | None = None,
    ) -> dict:
        """下注：擲 1–10。比大（6–10）／比小（1–5）贏了價值 ×2，猜中數字 ×5，存成賭場獎品；輸了沒收下注的作物。"""
        quantity = rules.validate_quantity(quantity)
        if mode not in balance.GAMBLE_MODES:
            raise ValidationError("玩法只能是比大、比小或猜數字。")
        if mode == "exact" and (
            not isinstance(guess, int) or isinstance(guess, bool)
            or not balance.GAMBLE_MIN <= guess <= balance.GAMBLE_MAX
        ):
            raise ValidationError(f"請猜 {balance.GAMBLE_MIN}–{balance.GAMBLE_MAX} 的整數。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            entry = crops_repo.produce_map(session).get(item_code)
            if entry is None or not self.gamble_eligible(quality, mutation):
                raise ValidationError("只能用稀有、傳說品質，或巨型作物、黃金作物、珍稀變種的作物下注。")
            crop = entry[0]
            name = genetics.produce_name(crop, mutation) + genetics_label(quality, mutation)
            if not inventory_repo.remove(session, farm.id, item_code, quantity, quality, mutation):
                have = inventory_repo.get_quantity(session, farm.id, item_code, quality, mutation)
                raise InsufficientItemsError(f"{name}數量不足：想下注 {quantity} 個，倉庫只有 {have} 個。")
            roll = self._dice()
            if mode == "big":
                won = roll >= balance.GAMBLE_BIG_FROM
            elif mode == "small":
                won = roll < balance.GAMBLE_BIG_FROM
            else:
                won = roll == guess
            multiplier = balance.GAMBLE_EXACT_MULTIPLIER if mode == "exact" else balance.GAMBLE_HILO_MULTIPLIER
            choice = f"猜 {guess}" if mode == "exact" else balance.GAMBLE_MODES[mode]
            if won:
                # 贏得的作物從一般庫存移到獎品（總數不變，不會超過倉庫容量）
                inventory_repo.add_prize(session, farm.id, item_code, quality, mutation, multiplier, quantity)
                result = f"贏了，價值 ×{multiplier}"
            else:
                result = "輸了，作物沒收"
            self._record(
                session,
                farm,
                EventType.GAMBLE,
                f"賭場（{choice}）下注 {quantity} 個{name}：擲出 {roll}，{result}",
                item_code=item_code,
                quantity=quantity,
                quality=quality,
                mutation=mutation,
            )
            unit = genetics.unit_price(crop.sell_price, quality, mutation, self._genetics(session))
            if won:
                message = (
                    f"🎲 擲出 **{roll}**！🎉 你贏了！{quantity} 個{name}的價值變成 ×{multiplier}"
                    f"（每個 {unit * multiplier} 金），已存進倉庫的「🏆 賭場獎品」。"
                )
            else:
                message = f"🎲 擲出 **{roll}**… 💀 你輸了，失去 {quantity} 個{name}。"
            return {"roll": roll, "won": won, "multiplier": multiplier, "quantity": quantity, "name": name,
                    "message": message}

    def list_prizes(self, player_id: int) -> list[dict]:
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            return prize_views(session, farm)

    def sell_prize(self, player_id: int, prize_id: int, quantity: int) -> ActionResult:
        """賣出賭場獎品：單價 = 價目表單價 × 倍率（再套用活動售價加成）。只能賣自己農場的獎品。"""
        quantity = rules.validate_quantity(quantity)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            prize = next((p for p in inventory_repo.list_prizes(session, farm.id, for_update=True)
                          if p.id == prize_id), None)
            if prize is None:
                raise NotFoundError("找不到這個賭場獎品。")
            crop = crops_repo.produce_map(session)[prize.item_code][0]
            name = genetics.produce_name(crop, prize.mutation) + genetics_label(prize.quality, prize.mutation)
            if prize.quantity < quantity:
                raise InsufficientItemsError(f"{name}（×{prize.multiplier}）只有 {prize.quantity} 個。")
            base = genetics.unit_price(crop.sell_price, prize.quality, prize.mutation, self._genetics(session))
            sell = event_rules.bonus(self._active_events(session), "sell_price_pct", crop.id)
            price = event_rules.apply_sell_bonus(base * prize.multiplier, sell.value)
            income = price * quantity
            prize.quantity -= quantity
            farm.gold += income
            applied = event_rules.merge_applied(sell)
            self._record(
                session,
                farm,
                EventType.SELL,
                f"出售 {quantity} 個{name}（賭場 ×{prize.multiplier} 獎品，單價 {price}），金幣 +{income}"
                f"{_events_note(applied)}",
                item_code=prize.item_code,
                quantity=quantity,
                gold_delta=income,
                quality=prize.quality,
                mutation=prize.mutation,
                unit_price=price,
                applied_events=applied,
            )
            return ActionResult(
                f"已出售 {quantity} 個{name}（賭場 ×{prize.multiplier}，單價 {price}），獲得 {income} 金幣。",
                farm.gold, income, prize.item_code, quantity,
            )

    # ---------- 紀錄 ----------

    def get_recent_transactions(self, player_id: int, limit: int = 10) -> list[TransactionInfo]:
        limit = max(1, min(limit, 100))
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            return [
                TransactionInfo(t.created_at, t.event_type, t.item_code, t.quantity, t.gold_delta, t.description)
                for t in tx_repo.recent_for_farm(session, farm.id, limit)
            ]

    # ---------- 道具 ----------

    @staticmethod
    def find_item(key: str) -> str | None:
        """依代碼或中文名稱找道具代碼；找不到時回傳 None（可能是作物）。"""
        key = key.strip()
        for code, (name, _price, _kind, _desc, aliases) in balance.ITEMS.items():
            if key.lower() == code or key == name or key in aliases:
                return code
        return None

    def buy(self, player_id: int, key: str, quantity: int) -> ActionResult:
        """商店購買入口：道具或種子。"""
        code = self.find_item(key)
        if code is not None:
            return self.buy_item(player_id, code, quantity)
        return self.buy_seeds(player_id, key, quantity)

    def buy_item(self, player_id: int, item_code: str, quantity: int = 1) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        if item_code not in balance.ITEMS:
            raise NotFoundError(f"沒有「{item_code}」這種道具。")
        name, price, kind, _desc, _aliases = balance.ITEMS[item_code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            now = self._clock.now()
            if kind == "tool":
                if quantity != 1:
                    raise ValidationError(f"{name}是永久工具，一次只能購買 1 個。")
                # 農場已鎖定，加上唯一限制，重複指令或並行請求都不會重複扣款
                if items_repo.get_tool(session, farm.id, item_code) is not None:
                    raise AlreadyExistsError(f"你已經永久持有{name}，不需要再購買。")
            cost = price * quantity
            if farm.gold < cost:
                raise InsufficientGoldError(f"金幣不足：需要 {cost}，目前只有 {farm.gold}。")
            if kind == "tool":
                items_repo.add_tool(session, farm.id, item_code, now)
            else:
                self._ensure_space(session, farm, quantity)
                inventory_repo.add(session, farm.id, item_code, quantity)
            farm.gold -= cost
            self._record(
                session,
                farm,
                EventType.BUY_ITEM,
                f"購買 {quantity} 個{name}，金幣 -{cost}",
                item_code=item_code,
                quantity=quantity,
                gold_delta=-cost,
            )
            # 剛買下自動播種機時，立即把空地種滿
            auto = self._auto_plant(session, farm) if item_code == balance.TOOL_AUTO_PLANTER else ""
            return ActionResult(
                f"已購買 {quantity} 個{name}，花費 {cost} 金幣。{auto}", farm.gold, -cost, item_code, quantity
            )

    def use_fertilizer(self, player_id: int, plot_number: int) -> ActionResult:
        name = balance.ITEMS[balance.ITEM_FERTILIZER][0]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plot, now, state = self._plot_for_action(session, farm, plot_number)
            if state != PlotState.GROWING:
                label = rules.PLOT_STATE_LABELS[state]
                raise ValidationError(f"{name}只能用在生長中的土地（第 {plot_number} 格目前：{label}）。")
            if plot.fertilized:
                raise ValidationError(f"第 {plot_number} 格這次種植已經施過{name}了。")
            if not inventory_repo.remove(session, farm.id, balance.ITEM_FERTILIZER, 1):
                raise InsufficientItemsError(f"沒有{name}，請先購買（buy fertilizer 1）。")
            new_ready = now + (plot.ready_at - now) * balance.FERTILIZER_TIME_FACTOR
            if new_ready <= plot.watered_at:
                new_ready = plot.watered_at + timedelta(microseconds=1)
            plot.ready_at = new_ready
            plot.fertilized = True
            crop = crops_repo.get(session, plot.crop_type_id)
            self._record(
                session,
                farm,
                EventType.USE_ITEM,
                f"對第 {plot_number} 格{crop.name}施{name}，剩餘時間減少 {_fertilizer_pct()}%",
                item_code=balance.ITEM_FERTILIZER,
                quantity=1,
            )
            left = rules.format_duration(rules.remaining_seconds(new_ready, now))
            return ActionResult(
                f"已對第 {plot_number} 格施{name}，剩餘時間減少 {_fertilizer_pct()}%，還需要 {left}成熟。", farm.gold
            )

    def use_earthworm(self, player_id: int) -> ActionResult:
        name = balance.ITEMS[balance.ITEM_EARTHWORM][0]
        duration = timedelta(seconds=balance.EARTHWORM_DURATION_SECONDS)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if not inventory_repo.remove(session, farm.id, balance.ITEM_EARTHWORM, 1):
                raise InsufficientItemsError(f"沒有{name}，請先購買（buy earthworm 1）。")
            now = self._clock.now()
            effect = items_repo.get_effect(session, farm.id, balance.EFFECT_EARTHWORM)
            if effect is not None and effect.started_at <= now < effect.expires_at:
                effect.expires_at = effect.expires_at + duration  # 有效期間再次使用：延長
                action = "延長"
            elif effect is not None:
                effect.started_at, effect.expires_at = now, now + duration
                effect.multiplier = balance.EARTHWORM_GROW_MULTIPLIER
                action = "啟用"
            else:
                effect = FarmEffect(
                    farm_id=farm.id,
                    effect_code=balance.EFFECT_EARTHWORM,
                    multiplier=balance.EARTHWORM_GROW_MULTIPLIER,
                    started_at=now,
                    expires_at=now + duration,
                )
                session.add(effect)
                action = "啟用"
            until = effect.expires_at.astimezone(TAIPEI_TZ).strftime("%m-%d %H:%M")
            self._record(
                session,
                farm,
                EventType.USE_ITEM,
                f"使用{name}（{action}），生長加速至 {until}",
                item_code=balance.ITEM_EARTHWORM,
                quantity=1,
            )
            left = long_duration(rules.remaining_seconds(effect.expires_at, now))
            return ActionResult(
                f"已{action}{name}效果：澆水的作物生長時間 ×{balance.EARTHWORM_GROW_MULTIPLIER}，剩餘 {left}。",
                farm.gold,
            )

    def use_item(self, player_id: int, key: str, plot_number: int | None = None) -> ActionResult:
        code = self.find_item(key)
        if code == balance.ITEM_FERTILIZER:
            if plot_number is None:
                raise ValidationError("使用化肥需要指定土地：use fertilizer <土地編號>")
            return self.use_fertilizer(player_id, plot_number)
        if code == balance.ITEM_EARTHWORM:
            return self.use_earthworm(player_id)
        if code == balance.TOOL_AUTO_WATERER:
            raise ValidationError("自動澆水器是永久工具，不需要使用；可用 tool on/off auto_waterer 切換。")
        raise NotFoundError(f"沒有「{key}」這種道具。")

    def set_tool_enabled(self, player_id: int, tool_key: str, enabled: bool) -> ActionResult:
        code = self.find_item(tool_key)
        if code is None or balance.ITEMS[code][2] != "tool":
            raise NotFoundError(f"沒有「{tool_key}」這種工具。")
        name = balance.ITEMS[code][0]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            tool = items_repo.get_tool(session, farm.id, code)
            if tool is None:
                raise NotFoundError(f"你還沒有{name}。")
            tool.enabled = enabled
            return ActionResult(f"{name}已{'啟用' if enabled else '停用'}。", farm.gold)

    # ---------- 背包 ----------

    def get_backpack(self, player_id: int) -> Backpack:
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            return build_backpack(session, farm, self._clock.now())

    # ---------- 限時活動 ----------

    def active_events(self) -> list[dict]:
        """目前生效中的限時活動（含限定作物名稱）。"""
        now = self._clock.now()
        with self._transaction() as session:
            names = {c.id: c.name for c in crops_repo.list_all(session)}
            return [
                {**event_rules.view(e, now), "crop_name": names.get(e.crop_type_id)}
                for e in events_repo.active(session, now)
            ]

    # ---------- 作物品質與突變 ----------

    def genetics_settings(self) -> tuple[int, dict]:
        with self._transaction() as session:
            return genetics_repo.current(session)

    def crop_catalog(self) -> list[dict]:
        """各作物的基本資料、珍稀變種與各品質／突變的單價（依目前設定計算）。"""
        with self._transaction() as session:
            settings = self._genetics(session)
            rows = []
            for crop in crops_repo.list_all(session):
                rows.append(
                    {
                        **_crop_info(crop).__dict__,
                        "active": crop.active,
                        "variant_code": crop.variant_code,
                        "variant_name": crop.variant_name,
                        "prices": {
                            q: {
                                m: genetics.unit_price(crop.sell_price, q, m, settings)
                                for m in ("none", "golden", "variant")
                            }
                            for q in balance.QUALITIES
                        },
                        "fast_grow_seconds": genetics.grow_seconds(crop.grow_seconds, "fast", False, settings),
                        "giant_quantity": genetics.harvest_quantity(crop.harvest_quantity, "giant", settings),
                    }
                )
            return rows

    def find_crop_info(self, key: str) -> dict:
        """依作物或變種的代碼／名稱查詢。"""
        with self._transaction() as session:
            crop = crops_repo.find_variant(session, key) or self._crop(session, key)
            code = crop.code
        return next(c for c in self.crop_catalog() if c["code"] == code)

    def quality_table(self, player_id: int | None = None) -> dict:
        """基礎品質機率、售價倍率，以及（指定玩家時）依農場等級加成後的實際機率。"""
        with self._transaction() as session:
            settings = self._genetics(session)
            level = None
            if player_id is not None:
                farm = farms_repo.get_by_player(session, player_id)
                if farm is None:
                    self._locked_farm(session, player_id)
                level = farm.level
        return {
            "base_bp": settings["quality_bp"],
            "price_pct": settings["quality_price_pct"],
            "level": level,
            "effective_bp": genetics.effective_quality_bp(settings, level) if level else None,
            "level_bonus_bp": settings["level_bonus_bp"],
            "level_bonus_max_bp": settings["level_bonus_max_bp"],
        }

    def mutation_table(self) -> list[dict]:
        with self._transaction() as session:
            settings = self._genetics(session)
        effects = {
            "giant": f"收成數量 ×{settings['giant_multiplier']}",
            "golden": f"基本售價 ×{settings['golden_price_pct'] / 100:g}",
            "fast": f"生長時間減少 {rules.format_duration(settings['fast_reduction_seconds'])}"
            f"（最短 {rules.format_duration(balance.MIN_GROW_SECONDS)}，仍需澆水）",
            "variant": f"長成獨立的珍稀變種作物，基本售價 = 原作物 ×{settings['variant_price_pct'] / 100:g}",
        }
        return [
            {"code": m, "name": balance.MUTATIONS[m], "bp": settings["mutation_bp"][m], "effect": effects[m]}
            for m in genetics.MUTATION_ORDER
        ]

    def update_genetics(self, actor: str, raw: dict) -> int:
        """驗證後新增一個設定版本；無效設定一律拒絕。只影響之後播種的作物與之後的販售單價。"""
        settings = genetics.validate_settings(raw)
        now = self._clock.now()
        with self._transaction() as session:
            old_version, old = genetics_repo.current(session)
            version = genetics_repo.add_version(session, settings, actor, now)
            community_repo.add_admin_action(
                session,
                actor,
                "update_genetics",
                "crop_genetics",
                version,
                {"from_version": old_version, "before": old, "after": settings},
                now,
            )
            return version

    def update_crop_grow_seconds(self, actor: str, crop_code: str, seconds: int) -> dict:
        """調整作物生長時間；已澆水的作物成熟時間不變，之後澆水的作物使用新時間。"""
        low, high = balance.CROP_GROW_SECONDS_RANGE
        if not isinstance(seconds, int) or isinstance(seconds, bool) or not low <= seconds <= high:
            raise ValidationError(f"生長時間必須在 {low}–{high} 秒之間。")
        now = self._clock.now()
        with self._transaction() as session:
            crop = crops_repo.find(session, crop_code)
            if crop is None:
                raise NotFoundError(f"找不到作物「{crop_code}」。")
            before = crop.grow_seconds
            crops_repo.set_grow_seconds(session, crop.code, seconds)
            community_repo.add_admin_action(
                session,
                actor,
                "update_crop_grow_time",
                "crop",
                crop.id,
                {"crop": crop.code, "before": before, "after": seconds},
                now,
            )
            return _crop_info(crop).__dict__


def genetics_label(quality: str | None, mutation: str | None) -> str:
    """顯示用標籤，例如「【稀有・黃金作物】」；普通且無突變時為空字串。"""
    parts = []
    if quality and quality != "normal":
        parts.append(balance.QUALITIES[quality])
    if mutation and mutation not in ("none", "variant"):
        parts.append(balance.MUTATIONS[mutation])
    return f"【{'・'.join(parts)}】" if parts else ""


def long_duration(seconds: int) -> str:
    """較長時間的顯示，例如「6 天 23 小時 59 分」；不足 1 小時時沿用 format_duration。"""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return rules.format_duration(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    parts = ([f"{days} 天"] if days else []) + ([f"{hours} 小時"] if hours else []) + [f"{rest // 60} 分"]
    return " ".join(p for p in parts if p != "0 分" or len(parts) == 1)


def _fertilizer_pct() -> int:
    return round((1 - balance.FERTILIZER_TIME_FACTOR) * 100)


def _cooldown_text() -> str:
    return long_duration(balance.GROUP_TRANSFER_COOLDOWN_SECONDS)


def _events_note(applied: list[dict] | None) -> str:
    return f"（活動：{'、'.join(e['name'] for e in applied)}）" if applied else ""


def build_backpack(session: Session, farm: Farm, now: datetime) -> Backpack:
    """組合完整背包；CMD 與網頁監控共用，確保顯示一致。"""
    stacks = inventory_repo.list_items(session, farm.id)
    # 種子、道具、商品只有普通品質；農產品依 (代碼, 品質, 突變) 分開顯示
    quantities = {i.item_code: i.quantity for i in stacks if i.quality == "normal" and i.mutation == "none"}
    settings = genetics_repo.current(session)[1]
    crops = crops_repo.list_all(session)
    by_produce = {code: crop for code, (crop, _is_variant) in crops_repo.produce_map(session).items()}
    seeds, produce = [], []
    for crop in crops:
        seed_qty = quantities.get(rules.seed_item_code(crop.code), 0)
        if seed_qty:
            seeds.append(BackpackLine(crop.code, f"{crop.name}種子", seed_qty, crop.seed_price))
    for i in stacks:
        crop = by_produce.get(i.item_code)
        if crop is None:
            continue
        produce.append(
            BackpackLine(
                i.item_code,
                genetics.produce_name(crop, i.mutation),
                i.quantity,
                genetics.unit_price(crop.sell_price, i.quality, i.mutation, settings),
                quality=i.quality,
                mutation=i.mutation,
            )
        )
    consumables, tools = [], []
    owned = {t.tool_code: t for t in items_repo.list_tools(session, farm.id)}
    for code, (name, price, kind, desc, _aliases) in balance.ITEMS.items():
        if kind == "consumable" and quantities.get(code):
            consumables.append(BackpackLine(code, name, quantities[code], price, desc))
        elif kind == "tool":
            tool = owned.get(code)
            acquired = tool.acquired_at if tool else None
            tools.append(ToolStatus(code, name, tool is not None, bool(tool and tool.enabled), acquired, desc))
    effect = items_repo.get_effect(session, farm.id, balance.EFFECT_EARTHWORM)
    if effect is None:
        status = "inactive"
    elif effect.started_at <= now < effect.expires_at:
        status = "active"
    else:
        status = "expired"
    effects = [
        EffectStatus(
            code=balance.EFFECT_EARTHWORM,
            name=balance.ITEMS[balance.ITEM_EARTHWORM][0],
            status=status,
            started_at=effect.started_at if effect else None,
            expires_at=effect.expires_at if effect else None,
            remaining_seconds=rules.remaining_seconds(effect.expires_at, now) if status == "active" else 0,
            multiplier=effect.multiplier if effect else None,
            description=f"澆水的作物生長時間 ×{balance.EARTHWORM_GROW_MULTIPLIER}",
        )
    ]
    goods = [
        BackpackLine(a.product_code, a.product_name, quantities[a.product_code], a.product_price)
        for a in animals_repo.list_all(session)
        if quantities.get(a.product_code)
    ] + [
        BackpackLine(code, name, quantities[code], price)
        for code, (name, price, _aliases) in balance.GOODS.items()
        if quantities.get(code)
    ]
    produce.sort(key=lambda x: (x.code, list(balance.QUALITIES).index(x.quality), x.mutation))
    prizes = prize_views(session, farm, settings)
    materials = [
        BackpackLine(code, name, quantities[code],
                     exploration.FAKE_COIN_VALUE if code == exploration.FAKE_COIN else 0, f"{rarity}・{use}")
        for code, (name, rarity, use) in exploration.MATERIALS.items()
        if quantities.get(code)
    ]
    return Backpack(
        prizes=prizes,
        materials=materials,
        player_id=farm.player_id,
        goods=goods,
        seeds=seeds,
        produce=produce,
        consumables=consumables,
        tools=tools,
        effects=effects,
        # 賭場獎品也佔倉庫空間；山洞素材放在素材背包，不佔倉庫
        used=sum(i.quantity for i in stacks if not inventory_repo.is_material(i.item_code))
        + sum(p["quantity"] for p in prizes),
        material_used=sum(x.quantity for x in materials),
        material_capacity=exploration.MATERIAL_BAG_CAPACITY,
        capacity=farm.warehouse_capacity,
        now=now,
    )


def prize_views(session: Session, farm: Farm, settings: dict | None = None) -> list[dict]:
    """賭場獎品（顯示用）：名稱、數量、倍率與目前單價（價目表單價 × 倍率，不含活動加成）。"""
    settings = settings or genetics_repo.current(session)[1]
    produce = crops_repo.produce_map(session)
    rows = []
    for p in inventory_repo.list_prizes(session, farm.id):
        crop = produce[p.item_code][0]
        rows.append({
            "id": p.id,
            "item_code": p.item_code,
            "crop_code": crop.code,
            "quality": p.quality,
            "mutation": p.mutation,
            "multiplier": p.multiplier,
            "quantity": p.quantity,
            "name": genetics.produce_name(crop, p.mutation) + genetics_label(p.quality, p.mutation),
            "unit_price": genetics.unit_price(crop.sell_price, p.quality, p.mutation, settings) * p.multiplier,
        })
    return rows
