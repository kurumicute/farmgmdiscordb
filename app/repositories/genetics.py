"""品質與突變設定的版本紀錄。"""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import CropGeneticsSetting, FarmPlot, GameTransaction, InventoryItem
from app.game import balance
from app.repositories import crops as crops_repo


def current(session: Session) -> tuple[int, dict]:
    """回傳 (版本, 設定)；資料庫沒有設定時使用預設值（版本 0）。"""
    row = session.scalars(select(CropGeneticsSetting).order_by(CropGeneticsSetting.version.desc()).limit(1)).first()
    if row is None:
        return 0, balance.DEFAULT_GENETICS
    return row.version, row.settings


def add_version(session: Session, settings: dict, actor: str, now: datetime) -> int:
    # 並行更新若取得相同版本號，主鍵會擋下其中一筆（該次更新失敗並回滾），不會出現兩個相同版本
    latest = session.scalar(select(func.coalesce(func.max(CropGeneticsSetting.version), 0)))
    version = latest + 1
    session.add(CropGeneticsSetting(version=version, settings=settings, created_by=actor, created_at=now))
    session.flush()
    return version


def history(session: Session, limit: int = 20) -> list[CropGeneticsSetting]:
    stmt = select(CropGeneticsSetting).order_by(CropGeneticsSetting.version.desc()).limit(limit)
    return list(session.scalars(stmt))


def stats(session: Session) -> dict:
    """依品質與突變分組的實際紀錄：種植中、收成、偷取、販售與目前持有。沒有紀錄時回傳空清單。"""

    produce = crops_repo.produce_codes_select()

    def tx_group(event: str, with_income: bool = False):
        cols = [
            GameTransaction.quality,
            GameTransaction.mutation,
            func.count(),
            func.coalesce(func.sum(GameTransaction.quantity), 0),
        ]
        if with_income:
            cols.append(func.coalesce(func.sum(GameTransaction.gold_delta), 0))
        stmt = (
            select(*cols)
            .where(
                GameTransaction.event_type == event,
                GameTransaction.item_code.in_(produce),
                GameTransaction.quality.is_not(None),
            )
            .group_by(GameTransaction.quality, GameTransaction.mutation)
            .order_by(GameTransaction.quality, GameTransaction.mutation)
        )
        rows = []
        for r in session.execute(stmt):
            row = {"quality": r[0], "mutation": r[1], "count": r[2], "quantity": int(r[3])}
            if with_income:
                row["income"] = int(r[4])
            rows.append(row)
        return rows

    planted = [
        {"quality": q, "mutation": m, "plots": n}
        for q, m, n in session.execute(
            select(FarmPlot.quality, FarmPlot.mutation, func.count())
            .where(FarmPlot.crop_type_id.is_not(None))
            .group_by(FarmPlot.quality, FarmPlot.mutation)
            .order_by(FarmPlot.quality, FarmPlot.mutation)
        )
    ]
    holdings = [
        {"item_code": code, "quality": q, "mutation": m, "quantity": int(n)}
        for code, q, m, n in session.execute(
            select(
                InventoryItem.item_code, InventoryItem.quality, InventoryItem.mutation, func.sum(InventoryItem.quantity)
            )
            .where(InventoryItem.item_code.in_(produce), InventoryItem.quantity > 0)
            .group_by(InventoryItem.item_code, InventoryItem.quality, InventoryItem.mutation)
            .order_by(InventoryItem.item_code, InventoryItem.quality, InventoryItem.mutation)
        )
    ]
    return {
        "planted": planted,
        "harvested": tx_group("harvest"),
        "stolen": tx_group("steal"),
        "sold": tx_group("sell", with_income=True),
        "holdings": holdings,
    }
