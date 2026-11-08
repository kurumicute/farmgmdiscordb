"""可在管理網站調整的新功能規則（每日簽到、寵物、特賣與升級、成就）：預設值、驗證與目前生效的設定。

- 各規則模組（checkin、pets、special_shop、achievements）在匯入時 register 自己的預設值與驗證函式，
  並一律經 get(key) 讀取目前設定（不直接用常數），因此管理員修改後不必改程式或重新部署。
- 設定存在資料表 feature_settings（每個 key 一列 JSON，只存有修改過的項目）；沒有資料時用預設值。
  這裡只保存「目前生效的設定」，不碰資料庫：讀取與快取在 repositories/feature_settings.py（各程序每隔幾秒重新讀取）。
- 存在資料庫的值不合法時（例如舊版設定）記錄警告並改用預設值，遊戲不會因設定錯誤而壞掉。
"""

import copy
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

_SPECS: dict[str, tuple[str, Callable[[], dict], Callable[[dict], dict]]] = {}
_defaults: dict[str, dict] = {}
_current: dict[str, dict] = {}
_version = 0


def register(key: str, label: str, defaults: Callable[[], dict], validate: Callable[[dict], dict]) -> None:
    """登記一組設定：label 是管理網站顯示的名稱，validate(raw) 回傳正規化後的設定或丟出 ValidationError。"""
    _SPECS[key] = (label, defaults, validate)


def keys() -> list[str]:
    return list(_SPECS)


def label(key: str) -> str:
    return _SPECS[key][0]


def defaults(key: str) -> dict:
    if key not in _defaults:
        _defaults[key] = _SPECS[key][1]()
    return _defaults[key]


def get(key: str) -> dict:
    """目前生效的設定（呼叫端不可修改回傳的 dict）。"""
    return _current.get(key) or defaults(key)


def is_default(key: str) -> bool:
    return key not in _current


def validate(key: str, raw: dict) -> dict:
    if key not in _SPECS:
        raise KeyError(key)
    return _SPECS[key][2](copy.deepcopy(raw))


def version() -> int:
    """設定每次改變就加一；規則模組用來判斷衍生的快取（例如套用設定後的成就清單）是否要重建。"""
    return _version


def apply(stored: dict[str, dict]) -> None:
    """以資料庫讀到的設定（key → JSON）取代目前設定；沒有列出的 key 回到預設值。"""
    global _version
    current: dict[str, dict] = {}
    for key, raw in stored.items():
        if key not in _SPECS:
            continue
        try:
            current[key] = validate(key, raw)
        except Exception as exc:  # 設定不合法：用預設值，不讓遊戲壞掉
            logger.warning("新功能設定 %s 不合法，改用預設值：%s", key, exc)
    if current != _current:
        _current.clear()
        _current.update(current)
        _version += 1


def set_current(key: str, value: dict | None) -> None:
    """管理員儲存（value）或還原預設（None）後，在同一個程序中立即生效。"""
    global _version
    if value is None:
        _current.pop(key, None)
    else:
        _current[key] = value
    _version += 1


def reset() -> None:
    """全部回到預設值（測試用）。"""
    global _version
    _current.clear()
    _version += 1


# ---------- 驗證工具 ----------


def int_value(value, label: str, low: int, high: int) -> int:
    from app.game.errors import ValidationError

    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValidationError(f"{label}必須是 {low:,}–{high:,} 之間的整數。")
    return value


def int_list(value, label: str, low: int, high: int, min_len: int, max_len: int) -> list[int]:
    from app.game.errors import ValidationError

    if not isinstance(value, list) or not min_len <= len(value) <= max_len:
        raise ValidationError(f"{label}必須有 {min_len}–{max_len} 個數值。")
    return [int_value(v, f"{label}第 {i} 個", low, high) for i, v in enumerate(value, 1)]


def items_value(value, label: str, max_kinds: int = 3, max_qty: int = 999) -> dict[str, int]:
    """道具獎勵 {道具代碼: 數量}；道具必須是道具商店的道具。"""
    from app.game import balance
    from app.game.errors import ValidationError

    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > max_kinds:
        raise ValidationError(f"{label}最多 {max_kinds} 種道具。")
    out = {}
    for code, qty in value.items():
        if code not in balance.ITEMS:
            raise ValidationError(f"{label}：沒有「{code}」這種道具。")
        out[code] = int_value(qty, f"{label}的{balance.ITEMS[code][0]}數量", 1, max_qty)
    return out


def title_value(value, label: str, max_len: int, *, required: bool = False) -> str | None:
    from app.game.errors import ValidationError

    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValidationError(f"{label}不可空白。")
        return None
    if not isinstance(value, str) or len(value.strip()) > max_len:
        raise ValidationError(f"{label}最多 {max_len} 字。")
    return value.strip()
