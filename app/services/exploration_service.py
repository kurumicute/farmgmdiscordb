"""山洞冒險：區域探索、機器製作、假幣兌換、種子製造機、稀有作物培育器與自動收割／加工。

探險：出發時決定獎勵，探險時間結束後領取才放進背包，一次只能去一個區域。
規則與數值在 game/exploration.py；這裡只負責驗證與交易。所有會改變資產的操作都先鎖定農場資料列，
等級、進行中的探險、素材與倉庫空間都在同一筆交易中檢查，重複點擊或並行請求不會重複發獎或重複扣料。
"""

from datetime import timedelta

from app.game import balance, exploration, genetics, rules
from app.game.errors import (
    AlreadyExistsError,
    InsufficientItemsError,
    NotFoundError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import CraftState, EventType, PlotState
from app.repositories import catalog as catalog_repo
from app.repositories import exploration as exploration_repo
from app.repositories import farms as farms_repo
from app.repositories import inventory as inventory_repo
from app.repositories import items as items_repo
from app.repositories import players as players_repo
from app.repositories import ranch as ranch_repo
from app.services.dto import ActionResult
from app.services.ranch_service import RanchService

INCUBATOR = "rare_incubator"


def _names(session, codes) -> dict[str, str]:
    names = catalog_repo.item_names(session)
    return {c: names.get(c, exploration.material_name(c)) for c in codes}


def _join(items: dict[str, int], names: dict[str, str]) -> str:
    return "、".join(f"{names.get(code, code)} ×{qty}" for code, qty in items.items())


def find_machine(key: str) -> str | None:
    key = (key or "").strip()
    for code, machine in exploration.MACHINES.items():
        if key.lower() == code or key == machine.name:
            return code
    return None


def find_incubator_recipe(key: str) -> str | None:
    key = (key or "").strip()
    for code, (name, *_rest, aliases) in exploration.INCUBATOR_RECIPES.items():
        if key.lower() == code or key == name or key in aliases:
            return code
    return None


class ExplorationService(RanchService):
    # ---------- 冒險總覽與探險 ----------

    def _trip_view(self, log, now) -> dict | None:
        if log is None:
            return None
        area = exploration.AREAS[log.area_code]
        remaining = rules.remaining_seconds(log.next_available_at, now)
        return {"area": log.area_code, "area_name": area.name, "emoji": area.emoji, "started_at": log.explored_at,
                "ends_at": log.next_available_at, "remaining_seconds": remaining, "finished": remaining == 0}

    def exploration_overview(self, player_id: int) -> dict:
        now = self._clock.now()
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            player = players_repo.get(session, farm.player_id)
            counts = exploration_repo.list_cooldowns(session, farm.id)
            trip = self._trip_view(exploration_repo.get_active(session, farm.id), now)
            codes = {d.item for a in exploration.AREAS.values() for pool in a.pools.values() for d in pool}
            names = _names(session, codes | {exploration.FAKE_COIN})
            areas = []
            for code in exploration.AREA_ORDER:
                area = exploration.AREAS[code]
                row = counts.get(code)
                unlocked = farm.level >= area.unlock_level

                def pool(kind, area=area):
                    return list(dict.fromkeys(names[d.item] for d in area.pools.get(kind, ())))

                areas.append({
                    "code": code, "emoji": area.emoji, "name": area.name, "description": area.description,
                    "unlock_level": area.unlock_level, "unlocked": unlocked,
                    "duration_seconds": area.duration_seconds,
                    # 一次只能進行一趟探險：有進行中或待領取的探險時，所有區域都不能出發
                    "ready": unlocked and trip is None, "exploring": trip is not None and trip["area"] == code,
                    "advanced": exploration.is_advanced(area, farm.level),
                    "advanced_level": area.advanced_level, "rolls": area.rolls,
                    "explore_count": row.explore_count if row else 0,
                    "common": pool("common"), "rare": pool("rare"), "seeds": pool("seed"),
                    "events": [(e.name, e.min_level) for e in area.events], "level_notes": list(area.level_notes),
                })
            return {"player_name": player.display_name, "level": farm.level, "areas": areas, "trip": trip,
                    "now": now}

    def explore(self, player_id: int, area_key: str) -> dict:
        """出發探險：獎勵在出發時決定並記錄，探險時間結束後用 claim_exploration 領取。一次只能去一個區域。"""
        code = exploration.find_area(area_key)
        if code is None:
            names = "、".join(a.name for a in exploration.AREAS.values())
            raise NotFoundError(f"沒有「{area_key}」這個探索區域。可探索區域：{names}")
        area = exploration.AREAS[code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if farm.level < area.unlock_level:
                raise ValidationError(f"{area.name}需要農場 Lv.{area.unlock_level} 才能探索（目前 Lv.{farm.level}）。")
            now = self._clock.now()
            active = exploration_repo.get_active(session, farm.id, for_update=True)
            if active is not None:
                busy = exploration.AREAS[active.area_code].name
                if now >= active.next_available_at:
                    raise ValidationError(f"你從{busy}探險回來了，請先領取上一次的探險獎勵再出發。")
                left = rules.format_duration(rules.remaining_seconds(active.next_available_at, now))
                raise ValidationError(f"你正在{busy}探險，還需要 {left}才會回來；一次只能去一個區域。")
            result = exploration.explore(code, farm.level, self._explore_rng)
            until = now + timedelta(seconds=area.duration_seconds)
            row = exploration_repo.get_cooldown(session, farm.id, code, for_update=True)
            exploration_repo.set_cooldown(session, row, farm.id, code, now, until)
            # (farm_id) WHERE claimed_at IS NULL 唯一：並行出發時第二筆在這裡失敗並回滾
            log = exploration_repo.add_log(
                session, farm_id=farm.id, area_code=code, farm_level=farm.level, advanced=result.advanced,
                rewards=dict(result.items), events=list(result.events), explored_at=now, next_available_at=until,
            )
            return {"log_id": log.id, "area": code, "area_name": area.name, "emoji": area.emoji,
                    "advanced": result.advanced, "level": farm.level, "started_at": now, "ends_at": until,
                    "duration_seconds": area.duration_seconds}

    def claim_exploration(self, player_id: int) -> dict:
        """領取已回來的探險獎勵：素材、假幣與稀有種子都放進素材背包（不佔倉庫）；放不下時不領取。"""
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            now = self._clock.now()
            log = exploration_repo.get_active(session, farm.id, for_update=True)
            if log is None:
                raise ValidationError("目前沒有可以領取的探險，先到山洞選一個區域出發吧。")
            area = exploration.AREAS[log.area_code]
            if now < log.next_available_at:
                left = rules.format_duration(rules.remaining_seconds(log.next_available_at, now))
                raise ValidationError(f"還在{area.name}探險中，{left}後才會回來。")
            items = dict(log.rewards)
            total = sum(items.values())
            materials = sum(q for c, q in items.items() if inventory_repo.is_material(c))
            try:
                self._ensure_space(session, farm, total - materials)  # 一般物品放倉庫（目前掉落表沒有）
                self._ensure_material_space(session, farm, materials)  # 素材、假幣與稀有種子放素材背包
            except WarehouseFullError as exc:
                raise WarehouseFullError(f"{exc} 探險帶回來的物品放不下，清出空間後再領取。") from None
            for item, qty in items.items():
                inventory_repo.add(session, farm.id, item, qty)
            log.claimed_at = now
            names = _names(session, items)
            text = f"探索{area.name}：{_join(items, names) or '一無所獲'}"
            if log.events:
                text += f"（{'、'.join(log.events)}）"
            self._record(session, farm, EventType.EXPLORE, text[:200], quantity=total)
            seeds = {exploration.AMBER_SEED, exploration.MOON_SEED}
            return {
                "log_id": log.id, "area": log.area_code, "area_name": area.name, "emoji": area.emoji,
                "advanced": log.advanced, "level": log.farm_level,
                "items": [
                    {"code": c, "name": names[c], "quantity": q,
                     "rarity": exploration.MATERIALS[c][1] if c in exploration.MATERIALS else "稀有種子"}
                    for c, q in items.items()
                ],
                "events": list(log.events),
                "seeds": {names[c]: q for c, q in items.items() if c in seeds},
                "fake_coins": items.get(exploration.FAKE_COIN, 0),
                "started_at": log.explored_at, "ends_at": log.next_available_at,
            }

    def exploration_logs(self, player_id: int, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 50))
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            logs = exploration_repo.list_logs(session, farm.id, limit)
            names = _names(session, {c for log in logs for c in log.rewards})
            return [
                {"area": log.area_code, "area_name": exploration.AREAS[log.area_code].name,
                 "emoji": exploration.AREAS[log.area_code].emoji, "advanced": log.advanced, "level": log.farm_level,
                 "rewards": {names[c]: q for c, q in log.rewards.items()}, "events": list(log.events),
                 "explored_at": log.explored_at, "next_available_at": log.next_available_at,
                 "claimed_at": log.claimed_at}
                for log in logs
            ]

    # ---------- 假幣 ----------

    def exchange_fake_coins(self, player_id: int, quantity: int) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        name = exploration.MATERIALS[exploration.FAKE_COIN][0]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if not inventory_repo.remove(session, farm.id, exploration.FAKE_COIN, quantity):
                have = inventory_repo.get_quantity(session, farm.id, exploration.FAKE_COIN)
                raise InsufficientItemsError(f"{name}不足：想兌換 {quantity} 個，背包只有 {have} 個。")
            gold = quantity * exploration.FAKE_COIN_VALUE
            farm.gold += gold
            self._record(
                session, farm, EventType.EXCHANGE, f"兌換 {quantity} 個{name}，金幣 +{gold}",
                item_code=exploration.FAKE_COIN, quantity=quantity, gold_delta=gold,
            )
            return ActionResult(f"已兌換 {quantity} 個{name}，獲得 {gold} 金幣。", farm.gold, gold,
                                exploration.FAKE_COIN, quantity)

    # ---------- 機器 ----------

    def machines_status(self, player_id: int) -> dict:
        with self._transaction() as session:
            farm = self._farm_for_read(session, player_id)
            tools = {t.tool_code: t for t in items_repo.list_tools(session, farm.id)}
            codes = {c for m in exploration.MACHINES.values() for c in m.materials}
            have = {c: inventory_repo.get_quantity(session, farm.id, c) for c in codes}
            names = _names(session, codes)
            machines = []
            for code in exploration.MACHINE_ORDER:
                m = exploration.MACHINES[code]
                tool = tools.get(code)
                machines.append({
                    "code": code, "emoji": m.emoji, "name": m.name, "description": m.description,
                    "unlock_level": m.unlock_level, "unlocked": farm.level >= m.unlock_level,
                    "owned": tool is not None, "enabled": bool(tool and tool.enabled),
                    "settings": dict(tool.settings or {}) if tool else {},
                    "materials": [(names[c], n, have.get(c, 0)) for c, n in m.materials.items()],
                    "missing": {names[c]: n for c, n in exploration.missing_materials(m, have).items()},
                })
            return {"level": farm.level, "machines": machines}

    def build_machine(self, player_id: int, key: str) -> ActionResult:
        code = find_machine(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這種機器。")
        machine = exploration.MACHINES[code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            # 農場已鎖定，加上 farm_tools 唯一限制，重複點擊或並行請求都不會重複扣料或重複建立
            if items_repo.get_tool(session, farm.id, code) is not None:
                raise AlreadyExistsError(f"你已經有{machine.name}了。")
            if farm.level < machine.unlock_level:
                raise ValidationError(f"{machine.name}需要農場 Lv.{machine.unlock_level}（目前 Lv.{farm.level}）。")
            have = {c: inventory_repo.get_quantity(session, farm.id, c) for c in machine.materials}
            missing = exploration.missing_materials(machine, have)
            names = _names(session, machine.materials)
            if missing:
                raise InsufficientItemsError(f"材料不足，還缺：{_join(missing, names)}。")
            for item, qty in machine.materials.items():
                if not inventory_repo.remove(session, farm.id, item, qty):
                    raise InsufficientItemsError(f"材料不足：{names[item]}。")
            items_repo.add_tool(session, farm.id, code, self._clock.now())
            used = _join(machine.materials, names)
            self._record(session, farm, EventType.BUILD_MACHINE, f"製作{machine.name}，消耗 {used}",
                         item_code=code, quantity=1)
            return ActionResult(f"{machine.emoji} {machine.name}製作完成！消耗 {used}。", farm.gold,
                                item_code=code, quantity=1)

    def _machine(self, session, farm, code: str, *, require_enabled: bool = True):
        machine = exploration.MACHINES[code]
        tool = items_repo.get_tool(session, farm.id, code)
        if tool is None:
            raise ValidationError(f"需要先製作{machine.name}（山洞探索取得素材，在「🔧 機器」製作）。")
        if require_enabled and not tool.enabled:
            raise ValidationError(f"{machine.name}已停用，請先啟用。")
        return tool

    def set_machine_enabled(self, player_id: int, key: str, enabled: bool) -> ActionResult:
        code = find_machine(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這種機器。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            tool = self._machine(session, farm, code, require_enabled=False)
            tool.enabled = enabled
            name = exploration.MACHINES[code].name
            return ActionResult(f"{name}已{'啟用' if enabled else '停用'}。", farm.gold)

    def set_processor_recipe(self, player_id: int, recipe_key: str | None) -> ActionResult:
        """設定自動加工機的加工項目；None 表示不自動開單（仍會自動領取完成的訂單）。"""
        code = None
        if recipe_key:
            code = self.find_recipe(recipe_key)
            if code is None:
                raise NotFoundError(f"沒有「{recipe_key}」這個加工配方。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            tool = self._machine(session, farm, "auto_processor", require_enabled=False)
            tool.settings = {"recipe": code} if code else {}
            label = balance.RECIPES[code][0] if code else "不自動開單"
            return ActionResult(f"自動加工機的加工項目設為：{label}。", farm.gold)

    # ---------- 種子製造機 ----------

    def make_seeds(
        self, player_id: int, crop_key: str, quantity: int, quality: str = "normal", mutation: str = "none"
    ) -> ActionResult:
        quantity = rules.validate_quantity(quantity)
        if quantity > exploration.SEED_MAKER_MAX:
            raise ValidationError(f"一次最多使用 {exploration.SEED_MAKER_MAX} 個作物。")
        quality, mutation = genetics.parse_quality(quality), genetics.parse_mutation(mutation)
        if quality not in exploration.SEED_MAKER_QUALITIES or mutation not in exploration.SEED_MAKER_MUTATIONS:
            raise ValidationError("種子製造機只能使用普通品質、沒有突變的作物。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            self._machine(session, farm, "seed_maker")
            crop = self._crop(session, crop_key, require_active=True)
            if not crop.shop_buyable and not exploration.SEED_MAKER_RARE_CROPS:
                raise ValidationError(f"{crop.name}是稀有作物，種子製造機無法製造它的種子。")
            if not inventory_repo.remove(session, farm.id, crop.code, quantity, quality, mutation):
                have = inventory_repo.get_quantity(session, farm.id, crop.code, quality, mutation)
                raise InsufficientItemsError(f"{crop.name}不足：需要 {quantity} 個，背包只有 {have} 個。")
            seeds = quantity * exploration.SEED_MAKER_RATIO
            item = rules.seed_item_code(crop.code)
            # 扣作物後再檢查空間（稀有種子放素材背包）
            self._ensure_item_space(session, farm, item, seeds)
            inventory_repo.add(session, farm.id, item, seeds)
            self._record(
                session, farm, EventType.MAKE_SEED, f"種子製造機：{quantity} 個{crop.name} → {seeds} 顆{crop.name}種子",
                item_code=item, quantity=seeds,
            )
            auto = self._auto_plant(session, farm)
            return ActionResult(f"🌱 用 {quantity} 個{crop.name}做出 {seeds} 顆{crop.name}種子。{auto}", farm.gold,
                                item_code=item, quantity=seeds)

    # ---------- 稀有作物培育器（以加工訂單執行） ----------

    def incubator_recipes(self) -> list[dict]:
        with self._transaction() as session:
            codes = {c for r in exploration.INCUBATOR_RECIPES.values() for c in (*r[1], r[2])}
            names = _names(session, codes)
        return [
            {"code": code, "name": name, "inputs": {names[c]: n for c, n in inputs.items()},
             "output_name": names[output], "output_quantity": out_qty, "seconds": seconds}
            for code, (name, inputs, output, out_qty, seconds, _aliases) in exploration.INCUBATOR_RECIPES.items()
        ]

    def start_incubation(self, player_id: int, key: str, quantity: int = 1) -> ActionResult:
        code = find_incubator_recipe(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這個培育配方。")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= (
            exploration.INCUBATOR_MAX_QUANTITY
        ):
            raise ValidationError(f"培育份數必須是 1–{exploration.INCUBATOR_MAX_QUANTITY}。")
        name, inputs, output, out_qty, seconds, _aliases = exploration.INCUBATOR_RECIPES[code]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            self._machine(session, farm, INCUBATOR)
            return self._start_order(session, farm, code, name, INCUBATOR, inputs, output, out_qty, seconds, quantity)

    # ---------- 自動收割機與自動加工機 ----------

    def run_machines(self, player_id: int) -> str:
        """執行已啟用的自動收割機與自動加工機（開啟面板時呼叫）；沒有機器時不鎖定農場、不寫入。回傳結果說明。"""
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                return ""
            tools = {t.tool_code: t for t in items_repo.list_tools(session, farm.id) if t.enabled}
            if "auto_harvester" not in tools and "auto_processor" not in tools:
                return ""
            farm = self._locked_farm(session, player_id)
            notes = []
            if "auto_harvester" in tools:
                notes.append(self._auto_harvest(session, farm, tools["auto_harvester"]))
            if "auto_processor" in tools:
                notes.append(self._auto_process(session, farm, tools["auto_processor"]))
            return " ".join(n for n in notes if n)

    def _auto_harvest(self, session, farm, tool) -> str:
        """收成成熟的作物；擁有收割機時才成熟的作物，即使已過乾枯時間也照樣收成（收割機會在成熟時處理）。"""
        now = self._clock.now()
        targets = []
        # 一次查詢並鎖定所有土地（原本每格各查一次）
        for p in farms_repo.list_plots(session, farm.id, for_update=True):
            state = rules.plot_state(p.crop_type_id, p.watered_at, p.ready_at, now)
            if state == PlotState.READY or (state == PlotState.WITHERED and p.ready_at >= tool.acquired_at):
                targets.append(p)
        if not targets:
            return ""
        done, total, stopped = 0, 0, ""
        for plot in targets:
            try:
                _msg, qty, _item = self._harvest_locked(session, farm, plot, plot.crop_type)
            except WarehouseFullError:
                stopped = f"倉庫已滿，還有 {len(targets) - done} 格沒收。"
                break
            done += 1
            total += qty
        text = f"🚜 自動收割機收成 {done} 格，共 {total} 個作物。" if done else ""
        text += stopped
        if done:
            text += self._auto_plant(session, farm)
        return text.strip()

    def _auto_process(self, session, farm, tool) -> str:
        """領取所有完成的加工訂單，再以背包原料開始設定的加工項目（能做幾份就做幾份，最多單張上限）。"""
        now = self._clock.now()
        notes = []
        collected = 0
        for order, *_rest in ranch_repo.list_orders(session, farm.id, include_collected=False, limit=50):
            if rules.craft_state(order.status, order.ready_at, now) != CraftState.READY:
                continue
            locked = ranch_repo.get_order(session, farm.id, order.id, for_update=True)
            try:
                self._collect_order(session, farm, locked, source="自動加工機：")
            except WarehouseFullError:
                notes.append("倉庫已滿，部分加工品還沒領取。")
                break
            collected += 1
        if collected:
            notes.insert(0, f"🏭 自動加工機領取 {collected} 張訂單。")
        code = (tool.settings or {}).get("recipe")
        if code in balance.RECIPES:
            name, building, inputs, output, out_qty, seconds, _aliases = balance.RECIPES[code]
            if ranch_repo.count_buildings(session, farm.id, building) == 0:
                notes.append(f"自動加工機：需要先建造{balance.BUILDINGS[building][0]}。")
            elif ranch_repo.busy_order(session, farm.id, building, now) is None:
                quantity = min(
                    [balance.CRAFT_MAX_QUANTITY]
                    + [inventory_repo.get_quantity(session, farm.id, item) // per for item, per in inputs.items()]
                )
                if quantity >= 1:
                    self._start_order(session, farm, code, name, building, inputs, output, out_qty, seconds, quantity,
                                      source="自動加工機：")
                    notes.append(f"🏭 自動加工機開始加工 {out_qty * quantity} 份{name}。")
        return " ".join(notes)
