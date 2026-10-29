"""管理員操作：作物／動物種類、限時事件，以及調整單一玩家的金幣、等級、背包、土地、建築、動物與作物。

- 每個方法都是一筆資料庫交易，並寫入管理操作紀錄（admin_actions）。
- 調整玩家資料時另外寫入一筆 admin_adjust 交易紀錄，玩家查詢紀錄時可看到；不計入任務或競賽。
- 代碼建立後不可修改（背包、交易紀錄與動物都以代碼參照）。
- 已被使用過的作物／動物種類只能停用；從未使用的才能刪除。
"""

import re
from datetime import timedelta

from sqlalchemy import delete, func, select, text

from app.db.models import (
    AnimalType,
    CraftOrder,
    CropType,
    FarmAnimal,
    FarmBuilding,
    FarmPlot,
    GameEvent,
    GameTransaction,
    InventoryItem,
    Quest,
    StealRecord,
)
from app.game import balance, genetics, rules
from app.game import events as event_rules
from app.game.errors import AlreadyExistsError, NotFoundError, ValidationError, WarehouseFullError
from app.game.rules import EventType
from app.repositories import animals as animals_repo
from app.repositories import catalog as catalog_repo
from app.repositories import community as community_repo
from app.repositories import crops as crops_repo
from app.repositories import events as events_repo
from app.repositories import farms as farms_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.repositories import ranch as ranch_repo
from app.services.game_service import genetics_label
from app.services.ranch_service import animal_type_view
from app.services.ranking_service import RankingService

CODE_RE = re.compile(balance.CATALOG_CODE_PATTERN)
PRODUCE_MUTATIONS = ("none", "giant", "golden", "fast")


def crop_view(c: CropType) -> dict:
    return {
        "id": c.id,
        "code": c.code,
        "name": c.name,
        "seed_price": c.seed_price,
        "grow_seconds": c.grow_seconds,
        "harvest_quantity": c.harvest_quantity,
        "sell_price": c.sell_price,
        "variant_code": c.variant_code,
        "variant_name": c.variant_name,
        "active": c.active,
    }


def _int(value, label: str, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValidationError(f"{label}必須是 {low}–{high} 之間的整數。")
    return value


def _name(value, label: str) -> str:
    value = (value or "").strip() if isinstance(value, str) else ""
    if not 1 <= len(value) <= balance.CATALOG_NAME_MAX_LEN:
        raise ValidationError(f"{label}長度必須為 1–{balance.CATALOG_NAME_MAX_LEN} 字。")
    return value


def _code(value, label: str) -> str:
    if not isinstance(value, str) or not CODE_RE.match(value) or value.startswith("seed_"):
        raise ValidationError(f"{label}只能使用小寫英文、數字與底線，2–32 字，以英文字母開頭，且不可用 seed_ 開頭。")
    return value


def _snapshot(obj, fields) -> dict:
    return {f: getattr(obj, f) for f in fields}


CROP_FIELDS = ("name", "seed_price", "grow_seconds", "harvest_quantity", "sell_price", "variant_name", "active")
ANIMAL_FIELDS = (
    "name", "price", "feed_cost", "cycle_seconds", "product_name", "product_quantity", "product_price", "active",
)


class AdminService(RankingService):
    # ---------- 共用 ----------

    def _audit(self, session, actor: str, action: str, target_type: str, target_id, details: dict) -> None:
        community_repo.add_admin_action(session, actor, action, target_type, target_id, details, self._clock.now())

    @staticmethod
    def _used_names(session, *, exclude_crop: str | None = None, exclude_animal: str | None = None) -> set[str]:
        """作物、變種、動物、產品、商品與道具的名稱（玩家以名稱下指令，名稱不可重複）。"""
        names = set()
        for c in crops_repo.list_all(session):
            if c.code != exclude_crop:
                names.update((c.name, c.variant_name))
        for a in animals_repo.list_all(session):
            if a.code != exclude_animal:
                names.update((a.name, a.product_name))
        names.update(v[0] for v in balance.GOODS.values())
        names.update(v[0] for v in balance.ITEMS.values())
        return names

    # ---------- 作物種類 ----------

    def _crop_values(self, data: dict) -> dict:
        return {
            "name": _name(data.get("name"), "作物名稱"),
            "variant_name": _name(data.get("variant_name"), "珍稀變種名稱"),
            "seed_price": _int(data.get("seed_price"), "種子價格", *balance.CROP_SEED_PRICE_RANGE),
            "grow_seconds": _int(data.get("grow_seconds"), "生長秒數", *balance.CROP_GROW_SECONDS_RANGE),
            "harvest_quantity": _int(
                data.get("harvest_quantity"), "收成數量", *balance.CROP_HARVEST_QUANTITY_RANGE
            ),
            "sell_price": _int(data.get("sell_price"), "售價", *balance.CROP_SELL_PRICE_RANGE),
        }

    @staticmethod
    def _check_names(used: set[str], *names: str) -> None:
        if len(set(names)) != len(names):
            raise ValidationError("名稱不可彼此相同。")
        for n in names:
            if n in used:
                raise AlreadyExistsError(f"名稱「{n}」已被其他作物、動物、商品或道具使用。")

    def create_crop(self, actor: str, data: dict) -> dict:
        code = _code(data.get("code"), "作物代碼")
        variant_code = _code(data.get("variant_code"), "珍稀變種代碼")
        values = self._crop_values(data)
        if code == variant_code:
            raise ValidationError("作物代碼與珍稀變種代碼不可相同。")
        with self._transaction() as session:
            reserved = catalog_repo.reserved_codes(session)
            for c in (code, variant_code):
                if c in reserved:
                    raise AlreadyExistsError(f"代碼「{c}」已被使用。")
            self._check_names(self._used_names(session), values["name"], values["variant_name"])
            crop = CropType(code=code, variant_code=variant_code, active=True, **values)
            session.add(crop)
            session.flush()
            self._audit(session, actor, "create_crop", "crop", crop.id, crop_view(crop))
            return crop_view(crop)

    def _crop_for_update(self, session, code: str) -> CropType:
        crop = crops_repo.by_code(session, code, for_update=True)
        if crop is None:
            raise NotFoundError(f"找不到作物「{code}」。")
        return crop

    def update_crop(self, actor: str, code: str, data: dict) -> dict:
        """修改名稱、價格、生長時間與收成量；已澆水的作物成熟時間不變，已種下的收成量不變。"""
        values = self._crop_values(data)
        with self._transaction() as session:
            crop = self._crop_for_update(session, code)
            self._check_names(
                self._used_names(session, exclude_crop=code), values["name"], values["variant_name"]
            )
            before = _snapshot(crop, CROP_FIELDS)
            for k, v in values.items():
                setattr(crop, k, v)
            self._audit(
                session, actor, "update_crop", "crop", crop.id,
                {"crop": code, "before": before, "after": _snapshot(crop, CROP_FIELDS)},
            )
            return crop_view(crop)

    def set_crop_active(self, actor: str, code: str, active: bool) -> dict:
        with self._transaction() as session:
            crop = self._crop_for_update(session, code)
            if crop.active != active:
                crop.active = active
                action = "enable_crop" if active else "disable_crop"
                self._audit(session, actor, action, "crop", crop.id, {"crop": code})
            return crop_view(crop)

    @staticmethod
    def _crop_in_use(session, crop: CropType) -> str | None:
        codes = (crop.code, crop.variant_code, rules.seed_item_code(crop.code))
        checks = (
            ("土地", select(FarmPlot.id).where(FarmPlot.crop_type_id == crop.id)),
            ("背包", select(InventoryItem.id).where(InventoryItem.item_code.in_(codes))),
            ("交易紀錄", select(GameTransaction.id).where(GameTransaction.item_code.in_(codes))),
            ("偷菜紀錄", select(StealRecord.id).where(StealRecord.crop_type_id == crop.id)),
            ("限時事件", select(GameEvent.id).where(GameEvent.crop_type_id == crop.id)),
        )
        for label, stmt in checks:
            if session.scalar(stmt.limit(1)) is not None:
                return label
        for recipe in balance.RECIPES.values():
            if crop.code in recipe[2] or crop.code == recipe[3]:
                return "加工配方"
        return None

    def delete_crop(self, actor: str, code: str) -> dict:
        with self._transaction() as session:
            crop = self._crop_for_update(session, code)
            used = self._crop_in_use(session, crop)
            if used:
                raise ValidationError(f"{crop.name}已有{used}使用，不能刪除，請改為停用（保留歷史資料）。")
            snapshot = crop_view(crop)
            session.delete(crop)
            self._audit(session, actor, "delete_crop", "crop", snapshot["id"], snapshot)
            return {"deleted": code}

    # ---------- 動物種類 ----------

    def _animal_values(self, data: dict) -> dict:
        return {
            "name": _name(data.get("name"), "動物名稱"),
            "product_name": _name(data.get("product_name"), "產品名稱"),
            "price": _int(data.get("price"), "購買價格", *balance.ANIMAL_PRICE_RANGE),
            "feed_cost": _int(data.get("feed_cost"), "餵食費用", *balance.ANIMAL_FEED_RANGE),
            "cycle_seconds": _int(data.get("cycle_seconds"), "生產週期秒數", *balance.ANIMAL_CYCLE_RANGE),
            "product_quantity": _int(
                data.get("product_quantity"), "每次產量", *balance.ANIMAL_PRODUCT_QUANTITY_RANGE
            ),
            "product_price": _int(data.get("product_price"), "產品售價", *balance.ANIMAL_PRODUCT_PRICE_RANGE),
        }

    def create_animal_type(self, actor: str, data: dict) -> dict:
        code = _code(data.get("code"), "動物代碼")
        product_code = _code(data.get("product_code"), "產品代碼")
        values = self._animal_values(data)
        if code == product_code:
            raise ValidationError("動物代碼與產品代碼不可相同。")
        with self._transaction() as session:
            reserved = catalog_repo.reserved_codes(session)
            for c in (code, product_code):
                if c in reserved:
                    raise AlreadyExistsError(f"代碼「{c}」已被使用。")
            self._check_names(self._used_names(session), values["name"], values["product_name"])
            animal = AnimalType(code=code, product_code=product_code, active=True, **values)
            session.add(animal)
            session.flush()
            self._audit(session, actor, "create_animal_type", "animal_type", animal.id, animal_type_view(animal))
            return animal_type_view(animal)

    def _animal_for_update(self, session, code: str) -> AnimalType:
        animal = animals_repo.get(session, code, for_update=True)
        if animal is None:
            raise NotFoundError(f"找不到動物「{code}」。")
        return animal

    def update_animal_type(self, actor: str, code: str, data: dict) -> dict:
        """修改後立即生效；已在生產中的動物完成時間不變，下次餵食起使用新週期。"""
        values = self._animal_values(data)
        with self._transaction() as session:
            animal = self._animal_for_update(session, code)
            self._check_names(
                self._used_names(session, exclude_animal=code), values["name"], values["product_name"]
            )
            before = _snapshot(animal, ANIMAL_FIELDS)
            for k, v in values.items():
                setattr(animal, k, v)
            self._audit(
                session, actor, "update_animal_type", "animal_type", animal.id,
                {"animal": code, "before": before, "after": _snapshot(animal, ANIMAL_FIELDS)},
            )
            return animal_type_view(animal)

    def set_animal_type_active(self, actor: str, code: str, active: bool) -> dict:
        with self._transaction() as session:
            animal = self._animal_for_update(session, code)
            if animal.active != active:
                animal.active = active
                action = "enable_animal_type" if active else "disable_animal_type"
                self._audit(session, actor, action, "animal_type", animal.id, {"animal": code})
            return animal_type_view(animal)

    def delete_animal_type(self, actor: str, code: str) -> dict:
        with self._transaction() as session:
            animal = self._animal_for_update(session, code)
            codes = (animal.code, animal.product_code)
            checks = (
                ("農場動物", select(FarmAnimal.id).where(FarmAnimal.animal_code == animal.code)),
                ("背包", select(InventoryItem.id).where(InventoryItem.item_code == animal.product_code)),
                ("交易紀錄", select(GameTransaction.id).where(GameTransaction.item_code.in_(codes))),
                ("群組任務", select(Quest.id).where(Quest.item_filter == animal.product_code)),
            )
            for label, stmt in checks:
                if session.scalar(stmt.limit(1)) is not None:
                    raise ValidationError(f"{animal.name}已有{label}使用，不能刪除，請改為停用（保留歷史資料）。")
            snapshot = animal_type_view(animal)
            session.delete(animal)
            self._audit(session, actor, "delete_animal_type", "animal_type", animal.id, snapshot)
            return {"deleted": code}

    # ---------- 限時事件 ----------

    def _event_values(self, session, raw: dict) -> dict:
        values = event_rules.validate(raw)
        crop_code = values.pop("crop_code")
        values["crop_type_id"] = None
        if crop_code:
            crop = crops_repo.by_code(session, crop_code)
            if crop is None:
                raise NotFoundError(f"找不到作物「{crop_code}」。")
            values["crop_type_id"] = crop.id
        return values

    @staticmethod
    def _event_audit(e: GameEvent) -> dict:
        data = {k: getattr(e, k) for k in ("name", "description", "crop_type_id", *event_rules.EFFECTS)}
        data.update(
            schedule=event_rules.schedule_text(e), schedule_type=e.schedule_type, enabled=e.enabled
        )
        return data

    def create_event(self, actor: str, raw: dict) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            event = GameEvent(**self._event_values(session, raw), created_by=actor, created_at=now, updated_at=now)
            session.add(event)
            session.flush()
            self._audit(session, actor, "create_event", "event", event.id, self._event_audit(event))
            return event_rules.view(event, now)

    def _event_for_update(self, session, event_id: int) -> GameEvent:
        event = events_repo.get(session, event_id, for_update=True)
        if event is None:
            raise NotFoundError(f"找不到事件 #{event_id}。")
        return event

    def update_event(self, actor: str, event_id: int, raw: dict) -> dict:
        """修改只影響之後的操作；已播種、已澆水或已販售的結果不會被重算。"""
        now = self._clock.now()
        with self._transaction() as session:
            event = self._event_for_update(session, event_id)
            before = self._event_audit(event)
            for k, v in self._event_values(session, raw).items():
                setattr(event, k, v)
            event.updated_at = now
            self._audit(
                session, actor, "update_event", "event", event.id, {"before": before, "after": self._event_audit(event)}
            )
            return event_rules.view(event, now)

    def set_event_enabled(self, actor: str, event_id: int, enabled: bool) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            event = self._event_for_update(session, event_id)
            if event.enabled != enabled:
                event.enabled = enabled
                event.updated_at = now
                action = "enable_event" if enabled else "disable_event"
                self._audit(session, actor, action, "event", event.id, {"name": event.name})
            return event_rules.view(event, now)

    def delete_event(self, actor: str, event_id: int) -> dict:
        """交易紀錄中保存的是事件名稱快照，刪除事件不影響歷史紀錄。"""
        with self._transaction() as session:
            event = self._event_for_update(session, event_id)
            snapshot = self._event_audit(event)
            session.delete(event)
            self._audit(session, actor, "delete_event", "event", event_id, snapshot)
            return {"deleted": event_id}

    # ---------- 玩家：背包 ----------

    def _adjust(self, session, farm, actor: str, action: str, description: str, details: dict, **tx) -> None:
        self._record(session, farm, EventType.ADMIN_ADJUST, f"管理員調整：{description}"[:200], **tx)
        self._audit(session, actor, action, "player", farm.player_id, {"farm_id": farm.id, **details})

    def set_inventory(
        self, actor: str, player_id: int, item_code: str, quality: str, mutation: str, quantity: int
    ) -> dict:
        """把某一筆庫存（物品＋品質＋突變）設為指定數量；0 表示刪除。增加時不可超過倉庫容量。"""
        quantity = _int(quantity, "數量", 0, balance.ADMIN_MAX_ITEM_QUANTITY)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            known = catalog_repo.items(session)
            if item_code not in known:
                raise NotFoundError(f"沒有「{item_code}」這種物品。")
            kind, name = known[item_code]
            if quality not in balance.QUALITIES or mutation not in balance.MUTATIONS:
                raise ValidationError("品質或突變不正確。")
            if kind == catalog_repo.PRODUCE:
                if mutation not in PRODUCE_MUTATIONS:
                    raise ValidationError("一般農產品的突變只能是 無／巨型／黃金／快速生長；珍稀變種請選變種物品。")
            elif kind == catalog_repo.VARIANT:
                if mutation != "variant":
                    raise ValidationError("珍稀變種物品的突變固定為「珍稀變種」。")
            elif quality != "normal" or mutation != "none":
                raise ValidationError(f"{name}沒有品質或突變。")
            before = inventory_repo.get_quantity(session, farm.id, item_code, quality, mutation)
            delta = quantity - before
            if delta == 0:
                return {"item_code": item_code, "quantity": quantity, "changed": False}
            if delta > 0:
                used = inventory_repo.total_quantity(session, farm.id)
                if used + delta > farm.warehouse_capacity:
                    raise WarehouseFullError(
                        f"倉庫空間不足：目前 {used}/{farm.warehouse_capacity}，無法再放入 {delta} 個。"
                    )
                inventory_repo.add(session, farm.id, item_code, delta, quality, mutation)
            else:
                inventory_repo.remove(session, farm.id, item_code, -delta, quality, mutation)
                if quantity == 0:
                    session.execute(
                        delete(InventoryItem).where(
                            InventoryItem.farm_id == farm.id,
                            InventoryItem.item_code == item_code,
                            InventoryItem.quality == quality,
                            InventoryItem.mutation == mutation,
                        )
                    )
            label = name + genetics_label(quality, mutation)
            self._adjust(
                session, farm, actor, "set_inventory", f"背包 {label} {before} → {quantity}",
                {"item_code": item_code, "quality": quality, "mutation": mutation, "before": before, "after": quantity},
                item_code=item_code, quantity=abs(delta), quality=quality, mutation=mutation,
            )
            return {"item_code": item_code, "quantity": quantity, "changed": True}

    # ---------- 玩家：金幣與農場等級 ----------

    def set_farm_values(self, actor: str, player_id: int, *, gold: int | None = None, level: int | None = None) -> dict:
        """直接設定金幣與農場等級（不增減土地）。金幣差額記入交易紀錄，維持「gold_delta 加總 = 全服金幣」。"""
        if gold is None and level is None:
            raise ValidationError("請至少修改金幣或農場等級其中一項。")
        if gold is not None:
            gold = _int(gold, "金幣", 0, balance.ADMIN_MAX_GOLD)
        if level is not None:
            level = _int(level, "農場等級", 1, balance.ADMIN_MAX_LEVEL)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            before = {"gold": farm.gold, "level": farm.level}
            parts, delta = [], 0
            if gold is not None and gold != farm.gold:
                delta = gold - farm.gold
                parts.append(f"金幣 {farm.gold} → {gold}")
                farm.gold = gold
            if level is not None and level != farm.level:
                parts.append(f"農場等級 {farm.level} → {level}")
                farm.level = level
            if not parts:
                return {**before, "changed": False}
            after = {"gold": farm.gold, "level": farm.level}
            self._adjust(
                session, farm, actor, "set_farm_values", "、".join(parts),
                {"before": before, "after": after}, gold_delta=delta,
            )
            return {**after, "changed": True}

    # ---------- 玩家：轉移農場所屬群組 ----------

    def admin_transfer_farm(
        self, actor: str, player_id: int, group_id: int, reason: str, *, skip_cooldown: bool = False
    ) -> dict:
        """管理員轉移農場所屬群組：必填原因並寫入 admin_actions；可選擇略過冷卻（同樣留下紀錄）。

        轉移後會更新 last_group_transfer_at（玩家的下一次轉移重新計算 7 天冷卻），並確保玩家是目標群組成員。
        """
        reason = (reason or "").strip() if isinstance(reason, str) else ""
        if not 1 <= len(reason) <= balance.GROUP_TRANSFER_REASON_MAX_LEN:
            raise ValidationError(f"轉移原因必填，長度 1–{balance.GROUP_TRANSFER_REASON_MAX_LEN} 字。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if farm.home_group_id == group_id:
                raise ValidationError("農場已經屬於這個群組。")
            group = groups_repo.get(session, group_id)
            if group is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            view = self._transfer_view(session, farm, group_id)
            if not view["can_transfer"] and not skip_cooldown:
                raise self._cooldown_error(view)
            if skip_cooldown:
                # 只在本交易內允許資料庫觸發器略過冷卻檢查；交易結束後自動失效
                session.execute(text("SET LOCAL farmgm.admin_group_transfer = 'on'"))
            groups_repo.add_member(session, group_id, player_id)
            before = {
                "home_group_id": farm.home_group_id,
                "last_group_transfer_at": _iso(farm.last_group_transfer_at),
            }
            now = self._clock.now()
            bypass = "（略過冷卻）" if skip_cooldown and not view["can_transfer"] else ""
            self._apply_transfer(
                session, farm, group, now,
                f"管理員轉移所屬群組：「{view['home_group_name'] or '未設定'}」→「{group.name}」{bypass}"
                f"；原因：{reason}",
            )
            after = {"home_group_id": group.id, "last_group_transfer_at": _iso(now)}
            self._audit(
                session, actor, "transfer_farm_group", "player", player_id,
                {
                    "farm_id": farm.id, "before": before, "after": after, "reason": reason,
                    "skip_cooldown": bool(skip_cooldown), "cooldown_bypassed": bool(bypass),
                },
            )
            return {**after, "changed": True, "cooldown_bypassed": bool(bypass)}

    # ---------- 玩家：土地與作物 ----------

    def add_plots(self, actor: str, player_id: int, count: int) -> dict:
        count = _int(count, "新增土地數量", 1, balance.ADMIN_MAX_PLOTS)
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            total = farms_repo.count_plots(session, farm.id)
            if total + count > balance.ADMIN_MAX_PLOTS:
                raise ValidationError(f"每座農場最多 {balance.ADMIN_MAX_PLOTS} 格土地（目前 {total} 格）。")
            numbers = farms_repo.add_plots(session, farm.id, count)
            self._adjust(
                session, farm, actor, "add_plots", f"新增土地第 {numbers[0]}–{numbers[-1]} 格",
                {"plots": numbers}, quantity=count,
            )
            return {"added": numbers, "total": total + count}

    def _empty_check_plot(self, session, farm, plot_number: int):
        plot = farms_repo.get_plot(session, farm.id, plot_number, for_update=True)
        if plot is None:
            raise NotFoundError(f"沒有第 {plot_number} 格土地。")
        return plot

    def delete_plot(self, actor: str, player_id: int, plot_number: int) -> dict:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plot = self._empty_check_plot(session, farm, plot_number)
            if plot.crop_type_id is not None:
                raise ValidationError(f"第 {plot_number} 格還有作物，請先清除作物再刪除土地。")
            if farms_repo.count_plots(session, farm.id) <= 1:
                raise ValidationError("農場至少要保留 1 格土地。")
            session.delete(plot)  # 偷菜紀錄的 plot_id 會設為 NULL，保留歷史
            self._adjust(session, farm, actor, "delete_plot", f"刪除第 {plot_number} 格土地", {"plot": plot_number})
            return {"deleted": plot_number}

    def plant_for_player(
        self,
        actor: str,
        player_id: int,
        plot_number: int,
        crop_code: str,
        quality: str = "normal",
        mutation: str = "none",
        *,
        watered: bool = False,
        ready_now: bool = False,
    ) -> dict:
        """在空地直接種下指定作物（不扣種子、不抽品質）。ready_now 表示立即成熟可收成。"""
        if quality not in balance.QUALITIES or mutation not in balance.MUTATIONS:
            raise ValidationError("品質或突變不正確。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            crop = crops_repo.by_code(session, crop_code)
            if crop is None:
                raise NotFoundError(f"找不到作物「{crop_code}」。")
            if not crop.active:
                raise ValidationError(f"{crop.name}已停用，不能種植。")
            plot = self._empty_check_plot(session, farm, plot_number)
            if plot.crop_type_id is not None:
                raise ValidationError(f"第 {plot_number} 格已經有作物，請先清除。")
            now = self._clock.now()
            settings = self._genetics(session)
            quantity = genetics.harvest_quantity(crop.harvest_quantity, mutation, settings)
            farms_repo.plant(plot, crop.id, quantity, now, quality, mutation)
            if ready_now:
                # 種植與澆水時間設在 1 秒前、成熟時間為現在：狀態立即為「可收成」
                start = now - timedelta(seconds=1)
                plot.planted_at = start
                farms_repo.water(plot, start, now)
                state = "可收成"
            elif watered:
                seconds = genetics.grow_seconds(crop.grow_seconds, mutation, False, settings)
                farms_repo.water(plot, now, rules.ready_time(now, seconds))
                state = f"已澆水，{rules.format_duration(seconds)}後成熟"
            else:
                state = "等待澆水"
            label = genetics.produce_name(crop, mutation) + genetics_label(quality, mutation)
            self._adjust(
                session, farm, actor, "plant_for_player", f"在第 {plot_number} 格種下{label}（{state}）",
                {"plot": plot_number, "crop": crop.code, "quality": quality, "mutation": mutation,
                 "watered": watered, "ready_now": ready_now},
                item_code=crop.code, quantity=quantity, quality=quality, mutation=mutation,
            )
            return {"plot_number": plot_number, "crop": crop.code, "quantity": quantity, "state": state}

    def clear_plot_for_player(self, actor: str, player_id: int, plot_number: int) -> dict:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            plot = self._empty_check_plot(session, farm, plot_number)
            if plot.crop_type_id is None:
                raise ValidationError(f"第 {plot_number} 格沒有作物。")
            crop = crops_repo.get(session, plot.crop_type_id)
            before = {"crop": crop.code, "quality": plot.quality, "mutation": plot.mutation,
                      "remaining_quantity": plot.remaining_quantity}
            farms_repo.clear(plot)
            self._adjust(
                session, farm, actor, "clear_plot", f"清除第 {plot_number} 格的{crop.name}",
                {"plot": plot_number, "before": before}, item_code=crop.code,
            )
            return {"cleared": plot_number}

    # ---------- 玩家：建築與動物 ----------

    def add_building(self, actor: str, player_id: int, building_code: str) -> dict:
        if building_code not in balance.BUILDINGS:
            raise NotFoundError(f"沒有「{building_code}」這種設施。")
        name, _price, max_count, _aliases = balance.BUILDINGS[building_code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            owned = {b.slot for b in ranch_repo.list_buildings(session, farm.id) if b.building_code == building_code}
            if len(owned) >= max_count:
                raise AlreadyExistsError(f"{name}每座農場最多 {max_count} 座。")
            slot = min(s for s in range(1, max_count + 1) if s not in owned)
            ranch_repo.add_building(session, farm.id, building_code, slot, self._clock.now())
            self._adjust(
                session, farm, actor, "add_building", f"新增{name}（第 {slot} 座）",
                {"building": building_code, "slot": slot}, item_code=building_code, quantity=1,
            )
            return {"building": building_code, "slot": slot}

    def delete_building(self, actor: str, player_id: int, building_code: str, slot: int) -> dict:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            building = session.scalars(
                select(FarmBuilding).where(
                    FarmBuilding.farm_id == farm.id,
                    FarmBuilding.building_code == building_code,
                    FarmBuilding.slot == slot,
                )
            ).first()
            if building is None:
                raise NotFoundError("找不到這座設施。")
            name = balance.BUILDINGS[building_code][0]
            remaining = ranch_repo.count_buildings(session, farm.id, building_code) - 1
            if building_code == "barn":
                animals = ranch_repo.count_animals(session, farm.id)
                if animals > remaining * balance.BARN_SLOTS:
                    raise ValidationError(
                        f"刪除後養殖容量只剩 {remaining * balance.BARN_SLOTS}，"
                        f"但目前有 {animals} 隻動物，請先刪除動物。"
                    )
            elif remaining == 0:
                pending = session.scalar(
                    select(func.count()).where(
                        CraftOrder.farm_id == farm.id,
                        CraftOrder.building_code == building_code,
                        CraftOrder.status == "in_progress",
                    )
                )
                if pending:
                    raise ValidationError(f"{name}還有未領取的加工訂單，請等玩家領取後再刪除。")
            session.delete(building)
            self._adjust(
                session, farm, actor, "delete_building", f"刪除{name}（第 {slot} 座）",
                {"building": building_code, "slot": slot}, item_code=building_code,
            )
            return {"deleted": {"building": building_code, "slot": slot}}

    def add_animals_for_player(self, actor: str, player_id: int, animal_code: str, quantity: int) -> dict:
        quantity = _int(quantity, "數量", 1, balance.ANIMAL_MAX_BUY)
        with self._transaction() as session:
            animal = animals_repo.get(session, animal_code)
            if animal is None:
                raise NotFoundError(f"找不到動物「{animal_code}」。")
            if not animal.active:
                raise ValidationError(f"{animal.name}已停用，不能新增。")
            farm = self._locked_farm(session, player_id)
            capacity = ranch_repo.count_buildings(session, farm.id, "barn") * balance.BARN_SLOTS
            used = ranch_repo.count_animals(session, farm.id)
            if used + quantity > capacity:
                raise ValidationError(f"養殖容量不足：目前 {used}/{capacity}，請先新增畜舍。")
            numbers = ranch_repo.add_animals(session, farm.id, animal.code, quantity, self._clock.now())
            self._adjust(
                session, farm, actor, "add_animals",
                f"新增 {quantity} 隻{animal.name}（編號 {numbers[0]}–{numbers[-1]}）",
                {"animal": animal.code, "numbers": numbers}, item_code=animal.code, quantity=quantity,
            )
            return {"added": numbers}

    def delete_animal_for_player(self, actor: str, player_id: int, animal_no: int) -> dict:
        """刪除動物；尚未收集的產品會一併消失。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            animal = ranch_repo.get_animal(session, farm.id, animal_no, for_update=True)
            if animal is None:
                raise NotFoundError(f"沒有編號 {animal_no} 的動物。")
            animal_type = animals_repo.get(session, animal.animal_code)
            session.delete(animal)
            self._adjust(
                session, farm, actor, "delete_animal", f"刪除{animal_type.name}#{animal_no}",
                {"animal": animal.animal_code, "animal_no": animal_no}, item_code=animal.animal_code, quantity=1,
            )
            return {"deleted": animal_no}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None
