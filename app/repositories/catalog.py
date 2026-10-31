"""物品目錄：整合資料庫中的作物、動物產品與程式中固定的道具、加工品，供顯示名稱與管理員驗證使用。"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AnimalType, CropType
from app.game import balance, exploration, rules
from app.repositories import animals as animals_repo
from app.repositories import crops as crops_repo

# 物品種類
SEED, PRODUCE, VARIANT, ANIMAL_PRODUCT, GOODS, ITEM = "seed", "produce", "variant", "animal_product", "goods", "item"
MATERIAL = "material"  # 山洞探索取得的素材與假幣


def items(session: Session) -> dict[str, tuple[str, str]]:
    """可放入背包的物品代碼 → (種類, 顯示名稱)。永久工具不在背包中，不列入。"""
    result: dict[str, tuple[str, str]] = {}
    for crop in crops_repo.list_all(session):
        result[rules.seed_item_code(crop.code)] = (SEED, f"{crop.name}種子")
        result[crop.code] = (PRODUCE, crop.name)
        result[crop.variant_code] = (VARIANT, crop.variant_name)
    for animal in animals_repo.list_all(session):
        result[animal.product_code] = (ANIMAL_PRODUCT, animal.product_name)
    for code, (name, _price, _aliases) in balance.GOODS.items():
        result[code] = (GOODS, name)
    for code, (name, _price, kind, _desc, _aliases) in balance.ITEMS.items():
        if kind == "consumable":
            result[code] = (ITEM, name)
    for code, (name, _rarity, _use) in exploration.MATERIALS.items():
        result[code] = (MATERIAL, name)
    return result


def item_names(session: Session) -> dict[str, str]:
    names = {code: name for code, (_kind, name) in items(session).items()}
    for code, (name, *_rest) in balance.ITEMS.items():
        names.setdefault(code, name)
    for code, machine in exploration.MACHINES.items():
        names.setdefault(code, machine.name)
    return names


def reserved_codes(session: Session) -> set[str]:
    """已被使用的所有代碼（作物、變種、動物、產品、道具、設施、配方等），新代碼不可與其重複。"""
    codes: set[str] = set()
    for code, variant in session.execute(select(CropType.code, CropType.variant_code)):
        codes.update((code, variant))
    for code, product in session.execute(select(AnimalType.code, AnimalType.product_code)):
        codes.update((code, product))
    for table in (balance.GOODS, balance.ITEMS, balance.BUILDINGS, balance.RECIPES, balance.DEFENSES,
                  exploration.MATERIALS, exploration.MACHINES, exploration.INCUBATOR_RECIPES):
        codes.update(table)
    return codes
