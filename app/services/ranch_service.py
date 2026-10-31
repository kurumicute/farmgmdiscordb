"""動物養殖、建築與食品加工。

所有會改變資產的操作都先鎖定農場資料列，並在單一資料庫交易中完成：
扣款與動物狀態、原料扣除與訂單建立、成品發放與訂單狀態都一起提交或一起回滾。
"""

from datetime import timedelta

from app.game import balance, exploration, genetics, rules
from app.game.errors import (
    AlreadyExistsError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import AnimalState, CraftState, EventType
from app.repositories import animals as animals_repo
from app.repositories import catalog as catalog_repo
from app.repositories import inventory as inventory_repo
from app.repositories import ranch as ranch_repo
from app.services.dto import ActionResult
from app.services.game_service import GameService


def _find(table: dict, key: str, alias_index: int) -> str | None:
    key = key.strip()
    for code, values in table.items():
        if key.lower() == code or key == values[0] or key in values[alias_index] or key.lower() in values[alias_index]:
            return code
    return None


_CROP_NAMES = {code: name for code, name, *_rest in balance.INITIAL_CROPS}


def item_name(code: str, names: dict[str, str] | None = None) -> str:
    """物品代碼的顯示名稱；names 為資料庫目錄（catalog.item_names），未提供時只用程式內建名稱。"""
    if names and code in names:
        return names[code]
    if code in balance.GOODS:
        return balance.GOODS[code][0]
    if code in balance.ITEMS:
        return balance.ITEMS[code][0]
    if code in exploration.MATERIALS:
        return exploration.MATERIALS[code][0]
    return _CROP_NAMES.get(code, code)


def animal_view(animal, now, animal_type) -> dict:
    """animal_type 為該動物的 AnimalType（呼叫端以 animals_repo.by_code 一次取得）。"""
    state = rules.animal_state(animal.fed_at, animal.ready_at, now)
    return {
        "animal_no": animal.animal_no,
        "code": animal.animal_code,
        "name": animal_type.name,
        "active": animal_type.active,
        "state": state.value,
        "state_label": rules.ANIMAL_STATE_LABELS[state],
        "fed_at": animal.fed_at,
        "ready_at": animal.ready_at,
        "remaining_seconds": rules.remaining_seconds(animal.ready_at, now) if state == AnimalState.PRODUCING else 0,
        "feed_cost": animal_type.feed_cost,
        "cycle_seconds": animal_type.cycle_seconds,
        "product_code": animal_type.product_code,
        "product_name": animal_type.product_name,
        "product_quantity": animal_type.product_quantity,
        "cycles_completed": animal.cycles_completed,
    }


def animal_type_view(a) -> dict:
    return {
        "code": a.code,
        "name": a.name,
        "price": a.price,
        "feed_cost": a.feed_cost,
        "cycle_seconds": a.cycle_seconds,
        "product_code": a.product_code,
        "product_name": a.product_name,
        "product_quantity": a.product_quantity,
        "product_price": a.product_price,
        "active": a.active,
    }


def facility_name(code: str) -> str:
    """加工設施名稱：建築（磨坊等）或機器（稀有作物培育器）。"""
    if code in balance.BUILDINGS:
        return balance.BUILDINGS[code][0]
    if code in exploration.MACHINES:
        return exploration.MACHINES[code].name
    return code


def recipe_name(code: str) -> str:
    for table in (balance.RECIPES, exploration.INCUBATOR_RECIPES):
        if code in table:
            return table[code][0]
    return code


def order_view(order, now, names: dict[str, str] | None = None) -> dict:
    state = rules.craft_state(order.status, order.ready_at, now)
    return {
        "order_id": order.id,
        "recipe_code": order.recipe_code,
        "recipe_name": recipe_name(order.recipe_code),
        "building_code": order.building_code,
        "building_name": facility_name(order.building_code),
        "quantity": order.quantity,
        "inputs": {item_name(k, names): v for k, v in order.inputs.items()},
        "input_codes": dict(order.inputs),
        "output_code": order.output_code,
        "output_name": item_name(order.output_code, names),
        "output_quantity": order.output_quantity,
        "status": order.status,
        "state": state.value,
        "state_label": rules.CRAFT_STATE_LABELS[state],
        "started_at": order.started_at,
        "ready_at": order.ready_at,
        "collected_at": order.collected_at,
        "remaining_seconds": rules.remaining_seconds(order.ready_at, now) if state == CraftState.IN_PROGRESS else 0,
    }


class RanchService(GameService):
    # ---------- 查詢名稱 ----------

    @staticmethod
    def find_building(key: str) -> str | None:
        return _find(balance.BUILDINGS, key, 3)

    @staticmethod
    def find_recipe(key: str) -> str | None:
        return _find(balance.RECIPES, key, 6)

    @staticmethod
    def _goods(session, key: str) -> tuple[str, str, int] | None:
        """可販售商品 (代碼, 名稱, 單價)：加工品（程式內建）或動物產品（資料庫）。"""
        code = _find(balance.GOODS, key, 2)
        if code is not None:
            name, price, _aliases = balance.GOODS[code]
            return code, name, price
        animal = animals_repo.find_product(session, key)
        if animal is not None:
            return animal.product_code, animal.product_name, animal.product_price
        return None

    def animal_types(self, *, active_only: bool = True) -> list[dict]:
        with self._transaction() as session:
            return [animal_type_view(a) for a in animals_repo.list_all(session, active_only=active_only)]

    # ---------- 建築 ----------

    def build(self, player_id: int, key: str) -> ActionResult:
        code = self.find_building(key)
        if code is None:
            names = "、".join(v[0] for v in balance.BUILDINGS.values())
            raise NotFoundError(f"沒有「{key}」這種設施。可建造：{names}")
        name, price, max_count, _aliases = balance.BUILDINGS[code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            owned = {b.slot for b in ranch_repo.list_buildings(session, farm.id) if b.building_code == code}
            if len(owned) >= max_count:
                raise AlreadyExistsError(f"{name}每座農場最多 {max_count} 座，你已經有 {len(owned)} 座。")
            if farm.gold < price:
                raise InsufficientGoldError(f"金幣不足：{name}需要 {price}，目前只有 {farm.gold}。")
            slot = min(s for s in range(1, max_count + 1) if s not in owned)
            ranch_repo.add_building(session, farm.id, code, slot, self._clock.now())
            farm.gold -= price
            self._record(
                session,
                farm,
                EventType.BUILD,
                f"建造{name}（第 {slot} 座），金幣 -{price}",
                item_code=code,
                quantity=1,
                gold_delta=-price,
            )
            extra = f"，養殖容量 +{balance.BARN_SLOTS}" if code == "barn" else ""
            return ActionResult(f"已建造{name}（第 {slot} 座），花費 {price} 金幣{extra}。", farm.gold, -price, code, 1)

    def _capacity(self, session, farm_id: int) -> int:
        return ranch_repo.count_buildings(session, farm_id, "barn") * balance.BARN_SLOTS

    def workshop_status(self, player_id: int) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            buildings = ranch_repo.list_buildings(session, farm.id)
            counts = {code: sum(1 for b in buildings if b.building_code == code) for code in balance.BUILDINGS}
            busy = {}
            names = catalog_repo.item_names(session)
            for code in balance.BUILDINGS:
                order = ranch_repo.busy_order(session, farm.id, code, now)
                busy[code] = order_view(order, now, names) if order else None
            return {
                "counts": counts,
                "busy": busy,
                "capacity": self._capacity(session, farm.id),
                "animals": ranch_repo.count_animals(session, farm.id),
                "gold": farm.gold,
            }

    def _farm_for_read(self, session, player_id: int):
        from app.repositories import farms as farms_repo

        farm = farms_repo.get_by_player(session, player_id)
        if farm is None:
            self._locked_farm(session, player_id)  # 產生正確的錯誤訊息
        return farm

    # ---------- 動物 ----------

    def buy_animals(self, player_id: int, key: str, quantity: int) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        if quantity > balance.ANIMAL_MAX_BUY:
            raise ValidationError(f"一次最多購買 {balance.ANIMAL_MAX_BUY} 隻。")
        with self._transaction() as session:
            animal_type = animals_repo.find(session, key)
            if animal_type is None or not animal_type.active:
                names = "、".join(a.name for a in animals_repo.list_all(session, active_only=True))
                reason = "已停用，不能購買" if animal_type else "這種動物不存在"
                raise NotFoundError(f"「{key}」{reason}。可購買：{names or '（目前沒有）'}")
            code, name, price = animal_type.code, animal_type.name, animal_type.price
            farm = self._locked_farm(session, player_id)
            capacity = self._capacity(session, farm.id)
            if capacity == 0:
                raise ValidationError("動物必須安置在畜舍中，請先建造畜舍（workshop build 畜舍）。")
            used = ranch_repo.count_animals(session, farm.id)
            # 先檢查容量與金幣，兩者都通過才扣款
            if used + quantity > capacity:
                raise ValidationError(f"養殖容量不足：目前 {used}/{capacity}，無法再安置 {quantity} 隻。")
            cost = price * quantity
            if farm.gold < cost:
                raise InsufficientGoldError(f"金幣不足：需要 {cost}，目前只有 {farm.gold}。")
            numbers = ranch_repo.add_animals(session, farm.id, code, quantity, self._clock.now())
            farm.gold -= cost
            self._record(
                session,
                farm,
                EventType.BUY_ANIMAL,
                f"購買 {quantity} 隻{name}（編號 {numbers[0]}–{numbers[-1]}），金幣 -{cost}",
                item_code=code,
                quantity=quantity,
                gold_delta=-cost,
            )
            return ActionResult(
                f"已購買 {quantity} 隻{name}（編號 {numbers[0]}–{numbers[-1]}），花費 {cost} 金幣。"
                f"記得餵食（animal feed <編號>）才會開始生產。",
                farm.gold,
                -cost,
                code,
                quantity,
            )

    def list_animals(self, player_id: int) -> list[dict]:
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            types = animals_repo.by_code(session)
            return [animal_view(a, now, types[a.animal_code]) for a in ranch_repo.list_animals(session, farm.id)]

    def animal_status(self, player_id: int) -> dict:
        animals = self.list_animals(player_id)
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            capacity = self._capacity(session, farm.id)
        hungry = [a for a in animals if a["state"] == AnimalState.HUNGRY]
        producing = [a for a in animals if a["state"] == AnimalState.PRODUCING]
        return {
            "capacity": capacity,
            "used": len(animals),
            "counts": {s.value: sum(1 for a in animals if a["state"] == s) for s in AnimalState},
            "feed_cost_all": sum(a["feed_cost"] for a in hungry),
            "next_ready_at": min((a["ready_at"] for a in producing), default=None),
            "gold": farm.gold,
        }

    def _feed(self, session, farm, animal, now, animal_type) -> int:
        name, feed_cost = animal_type.name, animal_type.feed_cost
        farm.gold -= feed_cost
        animal.fed_at = now
        animal.ready_at = now + timedelta(seconds=animal_type.cycle_seconds)
        self._record(
            session,
            farm,
            EventType.FEED_ANIMAL,
            f"餵食 {name}#{animal.animal_no}，金幣 -{feed_cost}",
            item_code=animal.animal_code,
            quantity=1,
            gold_delta=-feed_cost,
        )
        return feed_cost

    def feed_animal(self, player_id: int, animal_no: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            animal = ranch_repo.get_animal(session, farm.id, animal_no, for_update=True)
            if animal is None:
                raise NotFoundError(f"沒有編號 {animal_no} 的動物。")
            now = self._clock.now()
            animal_type = animals_repo.get(session, animal.animal_code)
            name, feed_cost, cycle = animal_type.name, animal_type.feed_cost, animal_type.cycle_seconds
            state = rules.animal_state(animal.fed_at, animal.ready_at, now)
            if state == AnimalState.PRODUCING:
                left = rules.format_duration(rules.remaining_seconds(animal.ready_at, now))
                raise ValidationError(f"{name}#{animal_no} 已經餵過，正在生產中（還需要 {left}）。")
            if state == AnimalState.READY:
                raise ValidationError(f"{name}#{animal_no} 的產品還沒收集，請先收集（animal collect {animal_no}）。")
            if farm.gold < feed_cost:
                raise InsufficientGoldError(f"金幣不足：餵食{name}需要 {feed_cost}，目前只有 {farm.gold}。")
            self._feed(session, farm, animal, now, animal_type)
            return ActionResult(
                f"已餵食 {name}#{animal_no}，花費 {feed_cost} 金幣，{rules.format_duration(cycle)}後可收集。",
                farm.gold,
                -feed_cost,
            )

    def feed_all(self, player_id: int) -> ActionResult:
        """依編號順序餵食所有待餵食的動物，餵到金幣不足為止；只對實際餵到的動物扣款。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            now = self._clock.now()
            hungry = [
                a
                for a in ranch_repo.list_animals(session, farm.id, for_update=True)
                if rules.animal_state(a.fed_at, a.ready_at, now) == AnimalState.HUNGRY
            ]
            if not hungry:
                return ActionResult("目前沒有需要餵食的動物。", farm.gold)
            types = animals_repo.by_code(session)
            fed, spent, skipped = [], 0, []
            for animal in hungry:
                if skipped or farm.gold < types[animal.animal_code].feed_cost:
                    skipped.append(animal)
                    continue
                spent += self._feed(session, farm, animal, now, types[animal.animal_code])
                fed.append(animal)
            label = lambda a: f"{types[a.animal_code].name}#{a.animal_no}"  # noqa: E731
            if not fed:
                raise InsufficientGoldError(
                    f"金幣不足，沒有餵食任何動物（下一隻{label(skipped[0])}需要 "
                    f"{types[skipped[0].animal_code].feed_cost}，目前只有 {farm.gold}）。"
                )
            msg = f"已餵食 {len(fed)} 隻（{'、'.join(label(a) for a in fed)}），花費 {spent} 金幣。"
            if skipped:
                msg += f" 金幣不足，未餵食：{'、'.join(label(a) for a in skipped)}。"
            return ActionResult(msg, farm.gold, -spent, quantity=len(fed))

    def collect_animal(self, player_id: int, animal_no: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            animal = ranch_repo.get_animal(session, farm.id, animal_no, for_update=True)
            if animal is None:
                raise NotFoundError(f"沒有編號 {animal_no} 的動物。")
            now = self._clock.now()
            animal_type = animals_repo.get(session, animal.animal_code)
            name, product, qty = animal_type.name, animal_type.product_code, animal_type.product_quantity
            product_name = animal_type.product_name
            state = rules.animal_state(animal.fed_at, animal.ready_at, now)
            if state == AnimalState.HUNGRY:
                raise ValidationError(f"{name}#{animal_no} 尚未餵食，沒有產品可收集（animal feed {animal_no}）。")
            if state == AnimalState.PRODUCING:
                left = rules.format_duration(rules.remaining_seconds(animal.ready_at, now))
                raise ValidationError(f"{name}#{animal_no} 還在生產中，還需要 {left}。")
            used = inventory_repo.total_quantity(session, farm.id)
            if used + qty > farm.warehouse_capacity:
                # 產品保留在動物身上，不會遺失
                raise WarehouseFullError(
                    f"背包空間不足：目前 {used}/{farm.warehouse_capacity}，需要 {qty} 格才能收集{product_name}。"
                    "請先販售或使用物品清出空間，產品會保留到你領取為止。"
                )
            inventory_repo.add(session, farm.id, product, qty)
            animal.fed_at = None
            animal.ready_at = None
            animal.cycles_completed += 1
            self._record(
                session,
                farm,
                EventType.COLLECT_ANIMAL,
                f"從{name}#{animal_no} 收集 {qty} 個{product_name}",
                item_code=product,
                quantity=qty,
            )
            return ActionResult(
                f"從{name}#{animal_no} 收集了 {qty} 個{product_name}。記得再次餵食以開始下一個週期。",
                farm.gold,
                item_code=product,
                quantity=qty,
            )

    # ---------- 加工 ----------

    def recipes(self, player_id: int | None = None) -> list[dict]:
        counts = {}
        with self._transaction() as session:
            names = catalog_repo.item_names(session)
            if player_id is not None:
                farm = self._farm_for_read(session, player_id)
                for b in ranch_repo.list_buildings(session, farm.id):
                    counts[b.building_code] = counts.get(b.building_code, 0) + 1
        rows = []
        for code, (name, building, inputs, output, out_qty, seconds, _aliases) in balance.RECIPES.items():
            rows.append(
                {
                    "code": code,
                    "name": name,
                    "building_code": building,
                    "building_name": balance.BUILDINGS[building][0],
                    "built": counts.get(building, 0) > 0,
                    "inputs": {item_name(k, names): v for k, v in inputs.items()},
                    "output_name": item_name(output, names),
                    "output_quantity": out_qty,
                    "seconds": seconds,
                    "sell_price": balance.GOODS[output][1] if output in balance.GOODS else None,
                }
            )
        return rows

    def start_craft(self, player_id: int, key: str, quantity: int) -> ActionResult:
        code = self.find_recipe(key)
        if code is None:
            names = "、".join(v[0] for v in balance.RECIPES.values())
            raise NotFoundError(f"沒有「{key}」這個配方。可用配方：{names}")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValidationError("份數必須是正整數。")
        if quantity > balance.CRAFT_MAX_QUANTITY:
            raise ValidationError(f"單張訂單最多 {balance.CRAFT_MAX_QUANTITY} 份。")
        name, building, inputs, output, out_qty, seconds, _aliases = balance.RECIPES[code]
        building_name = balance.BUILDINGS[building][0]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if ranch_repo.count_buildings(session, farm.id, building) == 0:
                raise ValidationError(f"需要先建造{building_name}才能加工{name}（workshop build {building_name}）。")
            return self._start_order(session, farm, code, name, building, inputs, output, out_qty, seconds, quantity)

    def _start_order(
        self, session, farm, code, name, building, inputs, output, out_qty, seconds, quantity, *, source: str = ""
    ) -> ActionResult:
        """在已鎖定的農場開一張加工訂單（設施檢查由呼叫端負責）；原料扣除與訂單建立在同一筆交易中。"""
        building_name = facility_name(building)
        now = self._clock.now()
        busy = ranch_repo.busy_order(session, farm.id, building, now)
        if busy is not None:
            left = rules.format_duration(rules.remaining_seconds(busy.ready_at, now))
            raise ValidationError(
                f"{building_name}正在處理訂單 #{busy.id}，還需要 {left}；每座設施一次只能加工一張訂單。"
            )
        names = catalog_repo.item_names(session)
        needed = {item: per * quantity for item, per in inputs.items()}
        lacking = []
        for item, amount in needed.items():
            have = inventory_repo.get_quantity(session, farm.id, item)
            if have < amount:
                lacking.append(f"{item_name(item, names)} 需要 {amount}、只有 {have}")
        if lacking:
            raise InsufficientItemsError(f"原料不足：{'；'.join(lacking)}。")
        # 扣除原料與建立訂單在同一筆交易中完成
        for item, amount in needed.items():
            if not inventory_repo.remove(session, farm.id, item, amount):
                raise InsufficientItemsError(f"原料不足：{item_name(item, names)}。")
        ready_at = now + timedelta(seconds=seconds * quantity)
        order = ranch_repo.add_order(
            session,
            farm_id=farm.id,
            recipe_code=code,
            building_code=building,
            quantity=quantity,
            inputs=needed,
            output_code=output,
            output_quantity=out_qty * quantity,
            status="in_progress",
            started_at=now,
            ready_at=ready_at,
        )
        used = "、".join(f"{item_name(k, names)} x{v}" for k, v in needed.items())
        self._record(
            session,
            farm,
            EventType.CRAFT_START,
            f"{source}訂單 #{order.id}：以 {used} 開始加工 {out_qty * quantity} 份{name}",
            item_code=output,
            quantity=out_qty * quantity,
        )
        return ActionResult(
            f"已建立加工訂單 #{order.id}：{building_name}加工 {out_qty * quantity} 份{name}，"
            f"扣除原料 {used}，{rules.format_duration(seconds * quantity)}後完成。",
            farm.gold,
            item_code=output,
            quantity=out_qty * quantity,
        )

    def craft_orders(self, player_id: int, *, include_collected: bool = False, limit: int = 20) -> list[dict]:
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            rows = ranch_repo.list_orders(session, farm.id, include_collected=include_collected, limit=limit)
            names = catalog_repo.item_names(session)
            return [order_view(o, now, names) for o, *_rest in rows]

    def collect_craft(self, player_id: int, order_id: int) -> ActionResult:
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            order = ranch_repo.get_order(session, farm.id, order_id, for_update=True)
            if order is None:
                raise NotFoundError(f"找不到你的加工訂單 #{order_id}。")
            return self._collect_order(session, farm, order)

    def _collect_order(self, session, farm, order, *, source: str = "") -> ActionResult:
        """領取已鎖定農場的一張訂單；倉庫不足時在修改任何資料前丟出 WarehouseFullError。"""
        order_id = order.id
        now = self._clock.now()
        state = rules.craft_state(order.status, order.ready_at, now)
        out_name = item_name(order.output_code, catalog_repo.item_names(session))
        if state == CraftState.COLLECTED:
            raise ValidationError(f"訂單 #{order_id} 的{out_name}已經領取過了。")
        if state == CraftState.IN_PROGRESS:
            left = rules.format_duration(rules.remaining_seconds(order.ready_at, now))
            raise ValidationError(f"訂單 #{order_id} 還在加工中，還需要 {left}。")
        try:  # 培育器的稀有種子放素材背包，其餘成品放倉庫
            self._ensure_item_space(session, farm, order.output_code, order.output_quantity)
        except WarehouseFullError as exc:
            raise WarehouseFullError(f"{exc}成品會保留在訂單 #{order_id}（待領取），清出空間後再領取。") from None
        inventory_repo.add(session, farm.id, order.output_code, order.output_quantity)
        order.status = "collected"
        order.collected_at = now
        self._record(
            session,
            farm,
            EventType.CRAFT_COLLECT,
            f"{source}領取訂單 #{order_id}：{order.output_quantity} 份{out_name}",
            item_code=order.output_code,
            quantity=order.output_quantity,
        )
        return ActionResult(
            f"已領取訂單 #{order_id}：{order.output_quantity} 份{out_name}。",
            farm.gold,
            item_code=order.output_code,
            quantity=order.output_quantity,
        )

    # ---------- 商品販售 ----------

    def sell_goods(self, player_id: int, key: str, quantity: int) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        with self._transaction() as session:
            goods = self._goods(session, key)
            if goods is None:
                raise NotFoundError(f"沒有「{key}」這種商品。")
            code, name, price = goods
            farm = self._locked_farm(session, player_id)
            if not inventory_repo.remove(session, farm.id, code, quantity):
                have = inventory_repo.get_quantity(session, farm.id, code)
                raise InsufficientItemsError(f"{name}數量不足：想賣 {quantity} 個，背包只有 {have} 個。")
            income = price * quantity
            farm.gold += income
            self._record(
                session,
                farm,
                EventType.SELL,
                f"出售 {quantity} 個{name}（單價 {price}），金幣 +{income}",
                item_code=code,
                quantity=quantity,
                gold_delta=income,
                unit_price=price,
            )
            return ActionResult(f"已出售 {quantity} 個{name}，獲得 {income} 金幣。", farm.gold, income, code, quantity)

    def sell(
        self, player_id: int, key: str, quantity: int, quality: str = "normal", mutation: str = "none"
    ) -> ActionResult:
        """販售入口：作物（可指定品質與突變）或動物產品／加工品（只有普通品質）。"""
        with self._transaction() as session:
            is_goods = self._goods(session, key) is not None
        if is_goods:
            if genetics.parse_quality(quality) != "normal" or genetics.parse_mutation(mutation) != "none":
                raise ValidationError("動物產品與加工品沒有品質或突變，不需要指定。")
            return self.sell_goods(player_id, key, quantity)
        return self.sell_crop(player_id, key, quantity, quality, mutation)
