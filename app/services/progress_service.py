"""玩家長期進度：每日簽到（之後的圖鑑、成就與稱號也放在這裡）。

繼承鏈：WorldBossService → ProgressService → AdminService（Bot 與網站都用 AdminService）。
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import TAIPEI_TZ
from app.db.models import DailyCheckin, Farm
from app.game import checkin
from app.game.rules import EventType
from app.repositories import inventory as inventory_repo
from app.services.world_boss_service import WorldBossService


class ProgressService(WorldBossService):
    # ---------- 每日簽到 ----------

    def _taipei_today(self):
        return self._clock.now().astimezone(TAIPEI_TZ).date()

    def _next_checkin_at(self, today) -> datetime:
        """下一次可以簽到的時間（台灣時間隔天 00:00）。"""
        return datetime.combine(today + timedelta(days=1), datetime.min.time(), TAIPEI_TZ)

    @staticmethod
    def _last_checkin(session: Session, farm_id: int) -> DailyCheckin | None:
        stmt = select(DailyCheckin).where(DailyCheckin.farm_id == farm_id).order_by(DailyCheckin.day.desc()).limit(1)
        return session.scalars(stmt).first()

    def daily_checkin(self, player_id: int) -> dict | None:
        """今天還沒簽到就自動簽到並發放獎勵；還沒有農場時回傳 None。今天已簽到時不鎖農場。

        回傳 {new: 這次是否剛簽到, next_at: 下一次可簽到時間}；剛簽到時另有 streak、reward_text、converted。"""
        with self._transaction() as session:
            today = self._taipei_today()
            farm_id = session.scalar(select(Farm.id).where(Farm.player_id == player_id))
            if farm_id is None:
                return None
            already = {"new": False, "next_at": self._next_checkin_at(today)}
            done = select(DailyCheckin.id).where(DailyCheckin.farm_id == farm_id, DailyCheckin.day == today)
            if session.scalar(done) is not None:
                return already
            farm = self._locked_farm(session, player_id)  # 鎖定後再確認一次，同時開兩個面板也只會簽到一次
            if session.scalar(done) is not None:
                return already
            last = self._last_checkin(session, farm.id)
            streak = checkin.next_streak(last.day if last else None, last.streak if last else 0, today)
            reward = checkin.reward_for(streak)
            label = f"每日簽到（連續第 {streak} 天）"
            gold = reward["gold"]
            items, converted = {}, {}
            space = farm.warehouse_capacity - inventory_repo.total_quantity(session, farm.id)
            for code, qty in reward["items"].items():
                if qty <= space:
                    items[code] = qty
                    space -= qty
                else:  # 倉庫放不下：改發等值金幣，自動簽到不會因倉庫滿而失敗
                    converted[code] = qty
                    gold += checkin.item_gold_value(code) * qty
            for code, qty in items.items():
                inventory_repo.add(session, farm.id, code, qty)
                self._record(session, farm, EventType.CHECKIN_REWARD,
                             f"{label}：獲得{checkin.item_name(code)} ×{qty}", item_code=code, quantity=qty)
            if gold:
                farm.gold += gold
                note = "（倉庫已滿，道具改發金幣）" if converted else ""
                self._record(session, farm, EventType.CHECKIN_REWARD, f"{label}：金幣 +{gold}{note}",
                             gold_delta=gold, quantity=0)
            session.add(DailyCheckin(farm_id=farm.id, day=today, streak=streak, gold=gold, items=items,
                                     created_at=self._clock.now()))
            session.flush()  # (farm_id, day) 唯一：萬一重複在這裡失敗並整筆回滾
            return {
                "new": True,
                "streak": streak,
                "reward_text": checkin.reward_text({"gold": gold, "items": items}),
                "converted": bool(converted),
                "next_at": self._next_checkin_at(today),
            }

    def checkin_status(self, player_id: int) -> dict:
        """總覽頁顯示：今天是否已簽到、目前連續天數、下一次簽到的獎勵。只讀取不鎖。"""
        with self._transaction() as session:
            today = self._taipei_today()
            farm_id = session.scalar(select(Farm.id).where(Farm.player_id == player_id))
            last = self._last_checkin(session, farm_id) if farm_id is not None else None
            checked = last is not None and last.day == today
            # 連續天數：今天或昨天簽到過才算延續中
            alive = last is not None and last.day >= today - timedelta(days=1)
            streak = last.streak if alive else 0
            nxt = streak + 1 if alive else 1
            return {
                "checked_today": checked,
                "streak": streak,
                "next_reward_text": checkin.reward_text(checkin.reward_for(nxt)),
                "next_at": self._next_checkin_at(today),
            }
