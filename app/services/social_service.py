"""群組、偷菜與排行榜。

群組上下文一律由呼叫端以 group_id 明確傳入（CMD 傳目前選取的群組；
未來 Discord 以 get_group_by_guild(guild_id) 取得），服務本身不保存任何選取狀態。
"""

import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Group, Player
from app.game import balance, genetics, rules
from app.game.errors import (
    AlreadyExistsError,
    AlreadyStolenError,
    InsufficientGoldError,
    InsufficientItemsError,
    NotFoundError,
    NotGroupMemberError,
    PlotEmptyError,
    PlotNotReadyError,
    PlotNotWateredError,
    PlotWitheredError,
    ValidationError,
    WarehouseFullError,
)
from app.game.rules import EventType, PlotState
from app.repositories import crops as crops_repo
from app.repositories import defenses as defenses_repo
from app.repositories import farms as farms_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.repositories import leaderboard as leaderboard_repo
from app.repositories import players as players_repo
from app.repositories import steals as steals_repo
from app.services.dto import ActionResult, ExternalContext, FarmStatus, GroupInfo, StealTarget
from app.services.game_service import genetics_label
from app.services.ranch_service import RanchService

GUILD_ID_RE = re.compile(r"^[0-9]{1,20}$")
LEADERBOARD_MAX = 50


def _group_info(group, member_count: int, is_member: bool = False) -> GroupInfo:
    return GroupInfo(group.id, group.name, group.discord_guild_id, member_count, group.created_at, is_member)


def external_name(raw: str | None, max_len: int, fallback: str) -> str:
    """把外部平台的顯示名稱整理成合法名稱：去除控制字元與首尾空白、截斷長度，空白時使用 fallback。"""
    name = "".join(ch for ch in (raw or "") if ord(ch) >= 32).strip()[:max_len].strip()
    return name or fallback


class SocialService(RanchService):
    # ---------- 外部平台（Discord）對應 ----------

    def _ensure_guild_group(self, session: Session, guild_id: str, guild_name: str | None) -> Group:
        if not GUILD_ID_RE.match(guild_id):
            raise ValidationError("Discord Guild ID 必須是 1–20 位數字。")
        name = external_name(guild_name, balance.GROUP_NAME_MAX_LEN, f"伺服器 {guild_id}")
        # 並行的第一次互動只會建立一筆（discord_guild_id 唯一）
        session.execute(
            insert(Group).values(name=name, discord_guild_id=guild_id).on_conflict_do_nothing(
                index_elements=["discord_guild_id"]
            )
        )
        group = groups_repo.get_by_guild(session, guild_id)
        if group.name != name:
            group.name = name  # 伺服器改名時同步顯示名稱
        return group

    def ensure_guild_group(self, guild_id: str, guild_name: str | None) -> int:
        """Bot 加入伺服器時建立（或取得）對應群組，回傳群組 ID。"""
        with self._transaction() as session:
            return self._ensure_guild_group(session, guild_id, guild_name).id

    def external_context(
        self, platform: str, user_id: str, guild_id: str | None = None, guild_name: str | None = None
    ) -> ExternalContext:
        """取得（必要時建立）伺服器對應的群組；玩家已存在時確保其為該群組成員。不會建立玩家。"""
        with self._transaction() as session:
            group = self._ensure_guild_group(session, guild_id, guild_name) if guild_id is not None else None
            player = players_repo.get_by_external(session, platform, user_id)
            if player is not None and group is not None:
                groups_repo.add_member(session, group.id, player.id)
            has_farm = player is not None and farms_repo.get_by_player(session, player.id) is not None
            return ExternalContext(
                group.id if group else None,
                group.name if group else None,
                player.id if player else None,
                player.display_name if player else None,
                has_farm,
            )

    def ensure_external_player(self, platform: str, user_id: str, display_name: str | None) -> int:
        """依外部帳號取得或建立玩家（並行建立只會成功一筆），回傳玩家 ID。"""
        if not platform or not user_id or len(user_id) > 64:
            raise ValidationError("外部帳號資料無效。")
        name = external_name(display_name, balance.PLAYER_NAME_MAX_LEN, f"玩家{user_id[-6:]}")
        with self._transaction() as session:
            session.execute(
                insert(Player)
                .values(display_name=name, external_platform=platform, external_user_id=user_id)
                .on_conflict_do_nothing(
                    index_elements=["external_platform", "external_user_id"],
                    index_where=Player.external_platform.is_not(None),
                )
            )
            return session.scalar(
                select(Player.id).where(Player.external_platform == platform, Player.external_user_id == user_id)
            )

    def start_external_farm(
        self,
        platform: str,
        user_id: str,
        display_name: str | None,
        guild_id: str,
        guild_name: str | None,
        farm_name: str | None = None,
    ) -> FarmStatus:
        """外部平台建立農場：所屬群組固定為執行指令的伺服器。一個外部帳號只會有一座農場。"""
        group_id = self.external_context(platform, user_id, guild_id, guild_name).group_id
        player_id = self.ensure_external_player(platform, user_id, display_name)
        return self.create_farm(player_id, farm_name, home_group_id=group_id)

    # ---------- 群組 ----------

    def create_group(self, name: str, discord_guild_id: str | None = None) -> GroupInfo:
        name = rules.validate_name(name, label="群組名稱", max_len=balance.GROUP_NAME_MAX_LEN)
        if discord_guild_id is not None and not GUILD_ID_RE.match(discord_guild_id):
            raise ValidationError("Discord Guild ID 必須是 1–20 位數字。")
        try:
            with self._transaction() as session:
                group = groups_repo.create(session, name, discord_guild_id)
                return _group_info(group, 0)
        except IntegrityError:
            raise AlreadyExistsError("此 Discord Guild ID 已對應到其他群組。") from None

    def list_groups(self, player_id: int | None = None) -> list[GroupInfo]:
        with self._transaction() as session:
            return [_group_info(g, c, m) for g, c, m in groups_repo.list_with_counts(session, player_id)]

    def get_group(self, group_id: int) -> GroupInfo:
        with self._transaction() as session:
            for g, c, _m in groups_repo.list_with_counts(session):
                if g.id == group_id:
                    return _group_info(g, c)
        raise NotFoundError(f"找不到群組 #{group_id}。")

    def get_group_by_guild(self, guild_id: str) -> GroupInfo | None:
        """供未來 Discord 依 Guild ID 決定群組上下文。"""
        with self._transaction() as session:
            group = groups_repo.get_by_guild(session, guild_id)
            if group is None:
                return None
        return self.get_group(group.id)

    def add_member(self, group_id: int, player_id: int) -> bool:
        with self._transaction() as session:
            if groups_repo.get(session, group_id) is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            if players_repo.get(session, player_id) is None:
                raise NotFoundError(f"找不到玩家 #{player_id}。")
            return groups_repo.add_member(session, group_id, player_id)

    def remove_member(self, group_id: int, player_id: int) -> bool:
        """只移除成員關係，玩家的農場、金幣與所有紀錄都保留。"""
        with self._transaction() as session:
            if groups_repo.get(session, group_id) is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            return groups_repo.remove_member(session, group_id, player_id)

    def group_members(self, group_id: int) -> list[dict]:
        now = self._clock.now()
        with self._transaction() as session:
            if groups_repo.get(session, group_id) is None:
                raise NotFoundError(f"找不到群組 #{group_id}。")
            return groups_repo.members_with_farms(session, group_id, now, self._wither_cutoff(now))

    def is_member(self, group_id: int, player_id: int) -> bool:
        with self._transaction() as session:
            return groups_repo.is_member(session, group_id, player_id)

    @staticmethod
    def _wither_cutoff(now):
        return now - timedelta(seconds=balance.WITHER_SECONDS)

    @staticmethod
    def _require_member(session: Session, group_id: int, player_id: int) -> None:
        if groups_repo.get(session, group_id) is None:
            raise NotFoundError(f"找不到群組 #{group_id}。")
        if not groups_repo.is_member(session, group_id, player_id):
            raise NotGroupMemberError("你不是目前群組的成員，無法查看或操作此群組。")

    # ---------- 偷菜 ----------

    def steal_targets(self, group_id: int, player_id: int) -> list[StealTarget]:
        now = self._clock.now()
        with self._transaction() as session:
            self._require_member(session, group_id, player_id)
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            free = farm.warehouse_capacity - inventory_repo.total_quantity(session, farm.id)
            if free <= 0:
                return []
            rows = steals_repo.candidate_plots(session, group_id, player_id, now, self._wither_cutoff(now))
            return [
                StealTarget(
                    pid, pname, fname, plot_no, crop_name, remaining, min(balance.STEAL_MAX_PER_ACTION, remaining, free)
                )
                for pid, pname, fname, plot_no, crop_name, remaining in rows
            ]

    def steal_crop(
        self, group_id: int, thief_player_id: int, victim_player_id: int, plot_number: int, quantity: int
    ) -> ActionResult:
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValidationError("偷取數量必須是正整數。")
        if quantity > balance.STEAL_MAX_PER_ACTION:
            raise ValidationError(f"每次最多只能偷 {balance.STEAL_MAX_PER_ACTION} 個。")
        if thief_player_id == victim_player_id:
            raise ValidationError("不能偷自己的菜。")
        with self._transaction() as session:
            self._require_member(session, group_id, thief_player_id)
            # 不透露目標在其他群組的任何資訊
            if not groups_repo.is_member(session, group_id, victim_player_id):
                raise NotGroupMemberError("目標玩家不在目前群組。")
            thief_farm = farms_repo.get_by_player(session, thief_player_id)
            victim_farm = farms_repo.get_by_player(session, victim_player_id)
            if thief_farm is None:
                raise NotFoundError("你還沒有農場，請先建立農場（farm start）。")
            if victim_farm is None:
                raise NotFoundError("目標玩家還沒有農場。")
            # 依農場 ID 順序鎖定雙方，避免互偷時死結；與主人的收成也會因此序列化
            locked = {
                f.id: farms_repo.get_by_id(session, f.id, for_update=True)
                for f in sorted((thief_farm, victim_farm), key=lambda f: f.id)
            }
            thief_farm, victim_farm = locked[thief_farm.id], locked[victim_farm.id]

            plot = farms_repo.get_plot(session, victim_farm.id, plot_number, for_update=True)
            if plot is None:
                raise NotFoundError(f"目標農場沒有第 {plot_number} 格土地。")
            now = self._clock.now()
            state = rules.plot_state(plot.crop_type_id, plot.watered_at, plot.ready_at, now)
            if state == PlotState.EMPTY:
                raise PlotEmptyError("這塊土地已經沒有作物了（可能已被收成或偷光）。")
            if state == PlotState.WAITING_WATER:
                raise PlotNotWateredError("這塊土地的作物還沒澆水，不能偷。")
            if state == PlotState.GROWING:
                raise PlotNotReadyError("這塊土地的作物還沒成熟，不能偷。")
            if state == PlotState.WITHERED:
                raise PlotWitheredError("這塊土地的作物已經乾枯，不能偷。")
            if steals_repo.already_stolen(session, plot.id, plot.planting_no, thief_player_id):
                raise AlreadyStolenError("你已經偷過這株作物了，每株作物每人只能偷一次。")
            if plot.remaining_quantity < quantity:
                raise InsufficientItemsError(f"剩餘數量不足：只剩 {plot.remaining_quantity} 個。")
            used = inventory_repo.total_quantity(session, thief_farm.id)
            if used + quantity > thief_farm.warehouse_capacity:
                raise WarehouseFullError(
                    f"背包空間不足：目前 {used}/{thief_farm.warehouse_capacity}，需要再放入 {quantity} 個。"
                )

            crop = crops_repo.get(session, plot.crop_type_id)
            # 偷到的作物保留原本的品質與突變（播種時已決定，不重新抽取）
            quality, mutation = plot.quality, plot.mutation
            item = genetics.produce_code(crop, mutation)
            crop_label = genetics.produce_name(crop, mutation) + genetics_label(quality, mutation)
            planting_no = plot.planting_no
            thief = players_repo.get(session, thief_player_id)
            victim = players_repo.get(session, victim_player_id)

            # 一輪防守判定（所有驗證通過後才判定；背包不足等錯誤不會消耗機會）
            active = self._active_defenses(session, victim_farm.id, plot_number)
            blocked_by, chance, roll = None, None, None
            if active:
                roll = self._rng()
                blocked_by, chance = rules.resolve_defense(active, roll)
            if blocked_by is not None:
                # 被阻止：雙方作物與物品都不變；紀錄同樣受唯一限制保護，同一株作物不會再次判定
                device = balance.DEFENSES[blocked_by][0]
                steals_repo.add(
                    session,
                    group_id=group_id,
                    thief_player_id=thief_player_id,
                    victim_player_id=victim_player_id,
                    plot_id=plot.id,
                    plot_number=plot_number,
                    planting_no=planting_no,
                    crop_type_id=crop.id,
                    quantity=quantity,
                    created_at=now,
                    outcome="blocked",
                    blocked_by=blocked_by,
                    block_chance=chance,
                    roll=roll,
                )
                self._record(
                    session,
                    thief_farm,
                    EventType.STEAL_BLOCKED,
                    f"偷 {victim.display_name} 第 {plot_number} 格的{crop_label}時被{device}阻止",
                    item_code=item,
                    context_group_id=group_id,
                )
                self._record(
                    session,
                    victim_farm,
                    EventType.DEFENDED,
                    f"{device}在第 {plot_number} 格阻止了一次偷菜（{crop_label}）",
                    item_code=item,
                    context_group_id=group_id,
                )
                return ActionResult(
                    f"偷菜失敗！{victim.display_name} 的{device}守住了第 {plot_number} 格的{crop_label}。"
                    "這株作物你已無法再偷。",
                    thief_farm.gold,
                    item_code=item,
                    quantity=0,
                    success=False,
                )

            plot.remaining_quantity -= quantity
            emptied = plot.remaining_quantity == 0
            if emptied:
                farms_repo.clear(plot)
            inventory_repo.add(session, thief_farm.id, item, quantity, quality, mutation)
            # 唯一限制 (plot_id, planting_no, thief) 是重複指令與並行請求的最後防線
            steals_repo.add(
                session,
                group_id=group_id,
                thief_player_id=thief_player_id,
                victim_player_id=victim_player_id,
                plot_id=plot.id,
                plot_number=plot_number,
                planting_no=planting_no,
                crop_type_id=crop.id,
                quantity=quantity,
                created_at=now,
                outcome="success",
                block_chance=chance,
                roll=roll,
            )
            self._record(
                session,
                thief_farm,
                EventType.STEAL,
                f"從 {victim.display_name} 的第 {plot_number} 格偷走 {quantity} 個{crop_label}",
                item_code=item,
                quantity=quantity,
                context_group_id=group_id,
                quality=quality,
                mutation=mutation,
            )
            self._record(
                session,
                victim_farm,
                EventType.STOLEN,
                f"第 {plot_number} 格被 {thief.display_name} 偷走 {quantity} 個{crop_label}",
                item_code=item,
                quantity=quantity,
                context_group_id=group_id,
                quality=quality,
                mutation=mutation,
            )
            tail = "，這塊土地已被偷光，恢復為空地" if emptied else ""
            return ActionResult(
                f"成功從 {victim.display_name} 的第 {plot_number} 格偷走 {quantity} 個{crop_label}{tail}。",
                thief_farm.gold,
                item_code=item,
                quantity=quantity,
            )

    # ---------- 防守設施 ----------

    @staticmethod
    def find_defense(key: str) -> str | None:
        key = key.strip()
        for code, (name, _price, _max, _chance, _range, aliases) in balance.DEFENSES.items():
            if key.lower() == code or key == name or key.lower() in aliases or key in aliases:
                return code
        return None

    @staticmethod
    def _active_defenses(session: Session, farm_id: int, plot_number: int) -> list[tuple[str, float]]:
        """守護此土地的設施，依判定順序排列；同種設施重疊只計算一次。"""
        covering = set()
        for d in defenses_repo.list_for_farm(session, farm_id):
            range_size = balance.DEFENSES[d.device_code][4]
            if rules.defense_covers(range_size, d.start_plot, plot_number):
                covering.add(d.device_code)
        return [(code, balance.DEFENSES[code][3]) for code in balance.DEFENSE_ORDER if code in covering]

    def buy_defense(self, player_id: int, key: str) -> ActionResult:
        code = self.find_defense(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這種防守設施。可用 defense shop 查看。")
        name, price, max_count, _chance, range_size, _aliases = balance.DEFENSES[code]
        with self._transaction() as session:
            # 鎖定農場後再計算持有數量與分配編號：並行購買不會超過上限，也不會重複扣款
            farm = self._locked_farm(session, player_id)
            owned = {d.slot for d in defenses_repo.list_for_farm(session, farm.id) if d.device_code == code}
            if len(owned) >= max_count:
                raise AlreadyExistsError(f"{name}每座農場最多 {max_count} 個，你已經有 {len(owned)} 個。")
            if farm.gold < price:
                raise InsufficientGoldError(f"金幣不足：{name}需要 {price}，目前只有 {farm.gold}。")
            slot = min(s for s in range(1, max_count + 1) if s not in owned)
            start = 1 if range_size is not None else None
            defenses_repo.add(session, farm.id, code, slot, start, self._clock.now())
            farm.gold -= price
            self._record(
                session,
                farm,
                EventType.BUY_ITEM,
                f"購買防守設施{name}（第 {slot} 個），金幣 -{price}",
                item_code=code,
                quantity=1,
                gold_delta=-price,
            )
            if range_size is None:
                where = "守護全部土地"
            else:
                slot_arg = f" {slot}" if max_count > 1 else ""
                where = f"守護第 1–{range_size} 格，可用 defense place {code} <起始土地>{slot_arg} 調整"
            return ActionResult(
                f"已購買{name}（第 {slot} 個），花費 {price} 金幣；{where}。", farm.gold, -price, code, 1
            )

    def place_defense(self, player_id: int, key: str, start_plot: int, slot: int = 1) -> ActionResult:
        code = self.find_defense(key)
        if code is None:
            raise NotFoundError(f"沒有「{key}」這種防守設施。")
        name, _price, max_count, _chance, range_size, _aliases = balance.DEFENSES[code]
        if range_size is None:
            raise ValidationError(f"{name}守護全部土地，不需要放置。")
        if not 1 <= slot <= max_count:
            raise ValidationError(f"{name}編號必須在 1–{max_count} 之間。")
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            device = defenses_repo.get_slot(session, farm.id, code, slot)
            if device is None:
                raise NotFoundError(f"你沒有第 {slot} 個{name}，請先購買（defense buy {code}）。")
            total = farms_repo.count_plots(session, farm.id)
            if not 1 <= start_plot <= total:
                raise ValidationError(f"起始土地必須在 1–{total} 之間。")
            device.start_plot = start_plot
            end = min(start_plot + range_size - 1, total)
            self._record(
                session, farm, EventType.DEFENSE_PLACE, f"{name}（第 {slot} 個）改為守護第 {start_plot}–{end} 格"
            )
            return ActionResult(f"{name}（第 {slot} 個）現在守護第 {start_plot}–{end} 格。", farm.gold)

    def defense_status(self, player_id: int) -> dict:
        with self._transaction() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                self._locked_farm(session, player_id)
            total = farms_repo.count_plots(session, farm.id)
            devices = []
            for d in defenses_repo.list_for_farm(session, farm.id):
                name, _price, _max, chance, range_size, _aliases = balance.DEFENSES[d.device_code]
                end = None if range_size is None else min(d.start_plot + range_size - 1, total)
                devices.append(
                    {
                        "code": d.device_code,
                        "name": name,
                        "slot": d.slot,
                        "chance": chance,
                        "start_plot": d.start_plot,
                        "end_plot": end,
                        "acquired_at": d.acquired_at,
                    }
                )
            plots = []
            for n in range(1, total + 1):
                active = self._active_defenses(session, farm.id, n)
                _none, chance = rules.resolve_defense(active, 1.0)
                plots.append({"plot_number": n, "devices": [c for c, _p in active], "chance": chance})
            owned = {code: sum(1 for d in devices if d["code"] == code) for code in balance.DEFENSES}
            return {"devices": devices, "owned": owned, "plots": plots}

    def defense_log(self, player_id: int, limit: int = 20) -> list[dict]:
        """自己農場被阻止的偷菜紀錄（不顯示偷菜者；要看偷菜者需使用巡查券）。"""
        with self._transaction() as session:
            if farms_repo.get_by_player(session, player_id) is None:
                self._locked_farm(session, player_id)
            rows = steals_repo.search(
                session, limit=max(1, min(limit, 100)), victim_player_id=player_id, outcome="blocked"
            )
        hidden = ("thief_player_id", "thief_name")
        return [{k: v for k, v in r.items() if k not in hidden} for r in rows]

    def patrol(self, player_id: int) -> list[dict]:
        """消耗一張巡查券，查看自己農場近期所有偷菜嘗試（含偷菜者）。只能查看自己的農場。"""
        name = balance.ITEMS[balance.ITEM_PATROL_TICKET][0]
        with self._transaction() as session:
            farm = self._locked_farm(session, player_id)
            if not inventory_repo.remove(session, farm.id, balance.ITEM_PATROL_TICKET, 1):
                raise InsufficientItemsError(f"沒有{name}，請先購買（defense buy 巡查券 1）。")
            since = self._clock.now() - timedelta(days=balance.PATROL_DAYS)
            rows = steals_repo.search(session, limit=balance.PATROL_LIMIT, victim_player_id=player_id, since=since)
            self._record(
                session,
                farm,
                EventType.USE_ITEM,
                f"使用{name}巡查農場，查到近 {balance.PATROL_DAYS} 天 {len(rows)} 筆偷菜嘗試",
                item_code=balance.ITEM_PATROL_TICKET,
                quantity=1,
            )
            return rows

    # ---------- 排行榜 ----------

    def leaderboard(
        self, kind: str, *, group_id: int | None = None, player_id: int | None = None, limit: int = 10
    ) -> list[dict]:
        """group_id 為 None 時是總排行榜；群組排行榜需由該群組成員查看。"""
        if kind not in leaderboard_repo.KINDS:
            raise ValidationError("排行榜種類只能是 money、sales 或 goods。")
        limit = max(1, min(limit, LEADERBOARD_MAX))
        with self._transaction() as session:
            if group_id is not None:
                if player_id is None:
                    raise ValidationError("查看群組排行榜需要先選擇玩家。")
                self._require_member(session, group_id, player_id)
            return leaderboard_repo.ranking(session, kind, group_id, limit)
