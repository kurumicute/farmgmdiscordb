"""監控資料服務：只讀取 PostgreSQL，所有查詢都在唯讀交易中執行。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.clock import TAIPEI_TZ, Clock, SystemClock
from app.db.models import RankingAward, RankingRewardRule, RankingSettlement
from app.game import balance, rules
from app.game import events as event_rules
from app.repositories import animals as animals_repo
from app.repositories import catalog as catalog_repo
from app.repositories import community as community_repo
from app.repositories import crops as crops_repo
from app.repositories import defenses as defenses_repo
from app.repositories import events as events_repo
from app.repositories import farms as farms_repo
from app.repositories import genetics as genetics_repo
from app.repositories import groups as groups_repo
from app.repositories import inventory as inventory_repo
from app.repositories import leaderboard as leaderboard_repo
from app.repositories import monitor as monitor_repo
from app.repositories import ranch as ranch_repo
from app.repositories import steals as steals_repo
from app.services.admin_service import crop_view
from app.services.community_service import competition_view, quest_view
from app.services.game_service import build_backpack
from app.services.ranch_service import animal_type_view, animal_view, order_view
from app.services.ranking_service import RankingService, rule_view, settlement_detail, settlement_view

ACTIVITY_HOURS = 12


class MonitorService:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock | None = None) -> None:
        self._session_factory = session_factory
        self._clock = clock or SystemClock()

    @contextmanager
    def _read_only(self) -> Iterator[Session]:
        with self._session_factory() as session:
            # 資料庫層級保證監控端不會寫入任何遊戲資料
            session.execute(text("SET TRANSACTION READ ONLY"))
            try:
                yield session
            finally:
                session.rollback()

    @staticmethod
    def _wither_cutoff(now: datetime) -> datetime:
        return now - timedelta(seconds=balance.WITHER_SECONDS)

    def _today_start(self, now: datetime) -> datetime:
        local = now.astimezone(TAIPEI_TZ)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    # ---------- 個別查詢 ----------

    def summary(self) -> dict:
        now = self._clock.now()
        with self._read_only() as session:
            return monitor_repo.summary(session, now, self._today_start(now), self._wither_cutoff(now))

    def farms(self, q: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        now = self._clock.now()
        with self._read_only() as session:
            rows, total = monitor_repo.farms(session, now, self._wither_cutoff(now), q, limit, offset)
            return {"items": rows, "total": total, "limit": limit, "offset": offset}

    def transactions(self, limit: int = 20) -> list[dict]:
        with self._read_only() as session:
            return monitor_repo.transactions(session, limit)

    def farm_detail(self, farm_id: int) -> dict | None:
        now = self._clock.now()
        with self._read_only() as session:
            found = monitor_repo.farm_detail(session, farm_id)
            if found is None:
                return None
            farm, player_name = found
            crop_names = {}
            plots = []
            for plot, crop in monitor_repo.plots(session, farm_id):
                state = rules.plot_state(plot.crop_type_id, plot.watered_at, plot.ready_at, now)
                if crop:
                    crop_names[crop.code] = crop.name
                plots.append(
                    {
                        "plot_number": plot.plot_number,
                        "crop_code": crop.code if crop else None,
                        "crop_name": crop.name if crop else None,
                        "planted_at": plot.planted_at,
                        "watered_at": plot.watered_at,
                        "ready_at": plot.ready_at,
                        "wither_at": rules.wither_time(plot.ready_at) if plot.ready_at else None,
                        "remaining_quantity": plot.remaining_quantity,
                        "fertilized": plot.fertilized,
                        "remaining_seconds": rules.remaining_seconds(plot.ready_at, now),
                        "state": state.value,
                        "state_label": rules.PLOT_STATE_LABELS[state],
                    }
                )
            items = [
                {"item_code": i.item_code, "quantity": i.quantity} for i in monitor_repo.inventory(session, farm_id)
            ]
            return {
                "farm_id": farm.id,
                "player_id": farm.player_id,
                "player_name": player_name,
                "farm_name": farm.name,
                "level": farm.level,
                "gold": farm.gold,
                "warehouse_used": sum(i["quantity"] for i in items),
                "warehouse_capacity": farm.warehouse_capacity,
                "updated_at": farm.updated_at,
                "plots": plots,
                "inventory": items,
                "transactions": monitor_repo.transactions(session, 20, farm_id=farm.id),
                "server_time": now,
            }

    def charts(self) -> dict:
        now = self._clock.now()
        with self._read_only() as session:
            return self._charts(session, now)

    def _charts(self, session: Session, now: datetime) -> dict:
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        since = current_hour - timedelta(hours=ACTIVITY_HOURS - 1)
        by_hour = {h.astimezone(UTC): (c, g) for h, c, g in monitor_repo.hourly_activity(session, since)}
        activity = []
        # 金幣趨勢：所有交易的 gold_delta 加總 = 全服金幣（建立農場的初始金幣也有入帳紀錄），
        # 因此由目前 SUM(farms.gold) 往回扣除近 12 小時的變化即可，不必加總全部歷史交易
        running = monitor_repo.total_gold(session) - sum(g for _c, g in by_hour.values())
        for i in range(ACTIVITY_HOURS):
            hour = since + timedelta(hours=i)
            count, gold = by_hour.get(hour, (0, 0))
            running += gold
            activity.append({"hour": hour, "transactions": count, "total_gold": running})
        return {
            "crops": monitor_repo.crop_distribution(session, now, self._wither_cutoff(now)),
            "activity": activity,
            "has_history": monitor_repo.has_transactions(session),
        }

    # ---------- WebSocket 快照 ----------

    def snapshot(self, farm_limit: int = 200) -> dict:
        now = self._clock.now()
        with self._read_only() as session:
            rows, total = monitor_repo.farms(session, now, self._wither_cutoff(now), None, farm_limit, 0)
            return {
                "server_time": now,
                "summary": monitor_repo.summary(session, now, self._today_start(now), self._wither_cutoff(now)),
                "farms": {"items": rows, "total": total},
                "transactions": monitor_repo.transactions(session, 20),
                "charts": self._charts(session, now),
            }

    # ---------- 群組、偷菜、排行榜、背包（唯讀） ----------

    def players(self) -> list[dict]:
        with self._read_only() as session:
            return monitor_repo.players(session)

    def groups(self) -> list[dict]:
        with self._read_only() as session:
            return [
                {
                    "group_id": g.id,
                    "name": g.name,
                    "discord_guild_id": g.discord_guild_id,
                    "member_count": count,
                    "created_at": g.created_at,
                }
                for g, count, _m in groups_repo.list_with_counts(session)
            ]

    def group_detail(self, group_id: int) -> dict | None:
        now = self._clock.now()
        with self._read_only() as session:
            group = groups_repo.get(session, group_id)
            if group is None:
                return None
            members = groups_repo.members_with_farms(session, group_id, now, self._wither_cutoff(now))
            return {
                "group_id": group.id,
                "name": group.name,
                "discord_guild_id": group.discord_guild_id,
                "created_at": group.created_at,
                "members": members,
            }

    def steals(
        self,
        *,
        limit: int = 50,
        group_id: int | None = None,
        player_id: int | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        with self._read_only() as session:
            rows = steals_repo.search(
                session,
                limit=limit,
                group_id=group_id,
                player_id=player_id,
                outcome=outcome,
                since=since,
                until=until,
            )
        for r in rows:
            r["blocked_by_name"] = balance.DEFENSES[r["blocked_by"]][0] if r["blocked_by"] else None
            r.pop("block_chance", None)  # 機率與亂數只留在資料庫供稽核
        return rows

    def defenses(self) -> list[dict]:
        """各農場持有的防守設施與守護範圍。"""
        farms: dict[int, dict] = {}
        with self._read_only() as session:
            for d, farm, player_name in defenses_repo.all_with_owners(session):
                total = farms_repo.count_plots(session, farm.id)
                name, _price, _max, chance, range_size, _aliases = balance.DEFENSES[d.device_code]
                entry = farms.setdefault(
                    farm.id,
                    {
                        "farm_id": farm.id,
                        "farm_name": farm.name,
                        "player_id": farm.player_id,
                        "player_name": player_name,
                        "plots": total,
                        "devices": [],
                    },
                )
                entry["devices"].append(
                    {
                        "code": d.device_code,
                        "name": name,
                        "slot": d.slot,
                        "chance": chance,
                        "start_plot": d.start_plot,
                        "end_plot": None if range_size is None else min(d.start_plot + range_size - 1, total),
                        "acquired_at": d.acquired_at,
                    }
                )
        return list(farms.values())

    def leaderboard(
        self, kind: str, group_id: int | None = None, limit: int = 20, *, scope: str | None = None, period: str = "all"
    ) -> dict | None:
        """scope：global（個人總排行）、group（單一群組內）、groups（群組總排行）。period：all／day／week／month。"""
        scope = scope or ("group" if group_id is not None else "global")
        now = self._clock.now()
        since, until = RankingService.view_bounds(period, now)
        with self._read_only() as session:
            if scope == "group" and groups_repo.get(session, group_id) is None:
                return None
            if scope == "groups":
                items = leaderboard_repo.group_ranking(session, kind, limit, since, until)
            else:
                gid = group_id if scope == "group" else None
                items = leaderboard_repo.ranking(session, kind, gid, limit, since, until)
            return {
                "scope": scope,
                "group_id": group_id if scope == "group" else None,
                "kind": kind,
                "period": period,
                "period_start": since,
                "period_end": until,
                "items": items,
            }

    def backpack(self, player_id: int) -> dict | None:
        now = self._clock.now()
        with self._read_only() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                return None
            bp = build_backpack(session, farm, now)
            return {
                "player_id": player_id,
                "farm_name": farm.name,
                "used": bp.used,
                "capacity": bp.capacity,
                "is_empty": bp.is_empty,
                "seeds": [vars(x) for x in bp.seeds],
                "produce": [vars(x) for x in bp.produce],
                "consumables": [vars(x) for x in bp.consumables],
                "goods": [vars(x) for x in bp.goods],
                "tools": [vars(x) for x in bp.tools],
                "effects": [vars(x) for x in bp.effects],
                "server_time": now,
            }

    # ---------- 動物養殖與加工（唯讀） ----------

    def ranch(self) -> list[dict]:
        now = self._clock.now()
        rows = []
        with self._read_only() as session:
            types = animals_repo.by_code(session)
            for farm, player_name in ranch_repo.farms_with_ranch(session):
                buildings = ranch_repo.list_buildings(session, farm.id)
                counts = {code: sum(1 for b in buildings if b.building_code == code) for code in balance.BUILDINGS}
                animals = [animal_view(a, now, types[a.animal_code]) for a in ranch_repo.list_animals(session, farm.id)]
                producing = [a["ready_at"] for a in animals if a["state"] == "producing"]
                rows.append(
                    {
                        "farm_id": farm.id,
                        "farm_name": farm.name,
                        "player_id": farm.player_id,
                        "player_name": player_name,
                        "gold": farm.gold,
                        "buildings": {balance.BUILDINGS[c][0]: n for c, n in counts.items() if n},
                        "capacity": counts["barn"] * balance.BARN_SLOTS,
                        "animal_count": len(animals),
                        "state_counts": {
                            s: sum(1 for a in animals if a["state"] == s) for s in ("hungry", "producing", "ready")
                        },
                        "feed_cost_hungry": sum(a["feed_cost"] for a in animals if a["state"] == "hungry"),
                        "next_ready_at": min(producing, default=None),
                        "animals": animals,
                    }
                )
        return rows

    def craft_orders(self, *, include_collected: bool = True, limit: int = 50) -> list[dict]:
        now = self._clock.now()
        with self._read_only() as session:
            rows = ranch_repo.list_orders(session, include_collected=include_collected, limit=limit)
            names = catalog_repo.item_names(session)
            return [
                {**order_view(o, now, names), "farm_name": farm_name, "player_id": pid, "player_name": pname}
                for o, farm_name, pid, pname in rows
            ]

    # ---------- 群組任務與競賽（唯讀；結算由遊戲服務或管理員觸發） ----------

    def quests(self, group_id: int | None = None, limit: int = 50) -> list[dict]:
        now = self._clock.now()
        with self._read_only() as session:
            rows = community_repo.list_quests(session, group_ids=[group_id] if group_id else None, limit=limit)
            names = catalog_repo.item_names(session)
            return [quest_view(q, name, now, names) for q, name in rows]

    def quest_detail(self, quest_id: int) -> dict | None:
        now = self._clock.now()
        with self._read_only() as session:
            quest = community_repo.get_quest(session, quest_id)
            if quest is None:
                return None
            group_name = groups_repo.get(session, quest.group_id).name
            view = quest_view(quest, group_name, now, catalog_repo.item_names(session))
            view["contributors"] = [
                {"player_id": pid, "player_name": name, "amount": amount}
                for pid, name, amount in community_repo.quest_contributors(session, quest_id)
            ]
            view["claims"] = [
                {"player_name": name, "reward_type": c.reward_type, "amount": c.amount, "claimed_at": c.claimed_at}
                for c, name in community_repo.quest_claims(session, quest_id)
            ]
            return view

    def competitions(self, limit: int = 50) -> list[dict]:
        now = self._clock.now()
        with self._read_only() as session:
            return [competition_view(c, now) for c in community_repo.list_competitions(session, limit=limit)]

    def competition_detail(self, competition_id: int) -> dict | None:
        now = self._clock.now()
        with self._read_only() as session:
            comp = community_repo.get_competition(session, competition_id)
            if comp is None:
                return None
            view = competition_view(comp, now)
            settled = comp.status == "settled"
            view["final"] = settled
            view["ranking"] = (
                community_repo.final_ranking(session, competition_id)
                if settled
                else community_repo.live_ranking(session, competition_id)
            )
            view["top_players"] = community_repo.competition_player_points(session, competition_id)
            view["awards"] = [
                {"player_name": a.player_name, "group_id": a.group_id, "rank": a.rank, "gold": a.gold}
                for a in community_repo.competition_awards(session, competition_id)
            ]
            return view

    def admin_actions(self, limit: int = 50) -> list[dict]:
        with self._read_only() as session:
            return [
                {
                    "created_at": a.created_at,
                    "actor": a.actor,
                    "action": a.action,
                    "target_type": a.target_type,
                    "target_id": a.target_id,
                    "details": a.details,
                }
                for a in community_repo.list_admin_actions(session, limit)
            ]

    # ---------- 作物品質與突變（唯讀） ----------

    def genetics(self) -> dict:
        with self._read_only() as session:
            version, settings = genetics_repo.current(session)
            all_crops = crops_repo.list_all(session)
            crops = [
                {"code": c.code, "name": c.name, "grow_seconds": c.grow_seconds, "sell_price": c.sell_price}
                for c in all_crops
            ]
            variants = {c.code: {"code": c.variant_code, "name": c.variant_name} for c in all_crops}
            history = [
                {"version": h.version, "created_by": h.created_by, "created_at": h.created_at}
                for h in genetics_repo.history(session)
            ]
        return {
            "version": version,
            "settings": settings,
            "labels": {"qualities": balance.QUALITIES, "mutations": balance.MUTATIONS},
            "variants": variants,
            "crops": crops,
            "grow_seconds_range": list(balance.CROP_GROW_SECONDS_RANGE),
            "min_grow_seconds": balance.MIN_GROW_SECONDS,
            "history": history,
        }

    def genetics_stats(self) -> dict:
        with self._read_only() as session:
            return genetics_repo.stats(session)

    # ---------- 作物、動物、事件目錄與單一農場管理資料（唯讀） ----------

    def catalog(self) -> dict:
        now = self._clock.now()
        with self._read_only() as session:
            crops = crops_repo.list_all(session)
            crop_codes = {c.id: c.code for c in crops}
            events = [
                {**event_rules.view(e, now), "crop_code": crop_codes.get(e.crop_type_id)}
                for e in events_repo.list_all(session)
            ]
            return {
                "crops": [crop_view(c) for c in crops],
                "animals": [animal_type_view(a) for a in animals_repo.list_all(session)],
                "events": events,
                "items": [
                    {"code": code, "kind": kind, "name": name}
                    for code, (kind, name) in catalog_repo.items(session).items()
                ],
                "buildings": [
                    {"code": code, "name": name, "max": max_count}
                    for code, (name, _price, max_count, _aliases) in balance.BUILDINGS.items()
                ],
                "labels": {"qualities": balance.QUALITIES, "mutations": balance.MUTATIONS},
                "ranges": {
                    "grow_seconds": list(balance.CROP_GROW_SECONDS_RANGE),
                    "seed_price": list(balance.CROP_SEED_PRICE_RANGE),
                    "harvest_quantity": list(balance.CROP_HARVEST_QUANTITY_RANGE),
                    "sell_price": list(balance.CROP_SELL_PRICE_RANGE),
                    "animal_price": list(balance.ANIMAL_PRICE_RANGE),
                    "feed_cost": list(balance.ANIMAL_FEED_RANGE),
                    "cycle_seconds": list(balance.ANIMAL_CYCLE_RANGE),
                    "product_quantity": list(balance.ANIMAL_PRODUCT_QUANTITY_RANGE),
                    "product_price": list(balance.ANIMAL_PRODUCT_PRICE_RANGE),
                    **{k: list(v[1]) for k, v in event_rules.EFFECTS.items()},
                },
                "event_caps": {k: v[3] for k, v in event_rules.EFFECTS.items()},
                "active_event_ids": [e.id for e in events_repo.active(session, now)],
                "server_time": now,
            }

    def admin_farm(self, player_id: int) -> dict | None:
        """管理員調整單一玩家時需要的完整資料：土地、建築、動物、背包庫存（含品質與突變）。"""
        now = self._clock.now()
        with self._read_only() as session:
            farm = farms_repo.get_by_player(session, player_id)
            if farm is None:
                return None
            names = catalog_repo.item_names(session)
            types = animals_repo.by_code(session)
            plots = []
            for plot in farms_repo.list_plots(session, farm.id):
                state = rules.plot_state(plot.crop_type_id, plot.watered_at, plot.ready_at, now)
                crop = plot.crop_type
                plots.append(
                    {
                        "plot_number": plot.plot_number,
                        "state": state.value,
                        "state_label": rules.PLOT_STATE_LABELS[state],
                        "crop_code": crop.code if crop else None,
                        "crop_name": crop.name if crop else None,
                        "quality": plot.quality,
                        "mutation": plot.mutation,
                        "remaining_quantity": plot.remaining_quantity,
                        "ready_at": plot.ready_at,
                    }
                )
            orders = ranch_repo.list_orders(session, farm.id, include_collected=False, limit=50)
            busy_buildings = sorted({o.building_code for o, *_rest in orders})
            home = groups_repo.get(session, farm.home_group_id) if farm.home_group_id is not None else None
            cooldown = timedelta(seconds=balance.GROUP_TRANSFER_COOLDOWN_SECONDS)
            last = farm.last_group_transfer_at
            next_at = last + cooldown if last is not None and last + cooldown > now else None
            return {
                "player_id": player_id,
                "player_name": farm.player.display_name,
                "external_user_id": farm.player.external_user_id,  # Discord ID 一律為字串
                "farm_name": farm.name,
                "home_group_id": farm.home_group_id,
                "home_group_name": home.name if home else None,
                "last_group_transfer_at": last,
                "next_group_transfer_at": next_at,
                "gold": farm.gold,
                "level": farm.level,
                "used": inventory_repo.total_quantity(session, farm.id),
                "capacity": farm.warehouse_capacity,
                "plots": plots,
                "buildings": [
                    {"code": b.building_code, "name": balance.BUILDINGS[b.building_code][0], "slot": b.slot}
                    for b in ranch_repo.list_buildings(session, farm.id)
                ],
                "busy_buildings": busy_buildings,
                "animal_capacity": ranch_repo.count_buildings(session, farm.id, "barn") * balance.BARN_SLOTS,
                "animals": [
                    animal_view(a, now, types[a.animal_code]) for a in ranch_repo.list_animals(session, farm.id)
                ],
                "inventory": [
                    {
                        "item_code": i.item_code,
                        "name": names.get(i.item_code, i.item_code),
                        "quality": i.quality,
                        "mutation": i.mutation,
                        "quantity": i.quantity,
                    }
                    for i in inventory_repo.list_items(session, farm.id)
                ],
                "server_time": now,
            }

    # ---------- 排名獎勵（唯讀） ----------

    def ranking_rewards(self, limit: int = 30) -> dict:
        now = self._clock.now()
        with self._read_only() as session:
            rules = session.scalars(select(RankingRewardRule).order_by(RankingRewardRule.id))
            counts = dict(
                session.execute(
                    select(RankingAward.settlement_id, func.count()).group_by(RankingAward.settlement_id)
                ).all()
            )
            settlements = session.scalars(
                select(RankingSettlement)
                .order_by(RankingSettlement.settled_at.desc(), RankingSettlement.id.desc())
                .limit(limit)
            )
            return {
                "rules": [rule_view(r, now) for r in rules],
                "settlements": [settlement_view(st, counts.get(st.id, 0)) for st in settlements],
                "server_time": now,
            }

    def ranking_settlement(self, settlement_id: int) -> dict | None:
        with self._read_only() as session:
            st = session.get(RankingSettlement, settlement_id)
            if st is None:
                return None
            return settlement_detail(session, st)
