"""遊戲規則錯誤。訊息可直接顯示給玩家（CMD、未來的 Discord 等）。"""


class GameError(Exception):
    """所有可預期的遊戲操作失敗。"""


class NotFoundError(GameError):
    pass


class ValidationError(GameError):
    pass


class InsufficientGoldError(GameError):
    pass


class InsufficientItemsError(GameError):
    pass


class WarehouseFullError(GameError):
    pass


class PlotOccupiedError(GameError):
    pass


class PlotNotReadyError(GameError):
    pass


class PlotEmptyError(GameError):
    pass


class AlreadyExistsError(GameError):
    pass


class PlotWitheredError(GameError):
    pass


class PlotNotWateredError(GameError):
    pass


class NotGroupMemberError(GameError):
    pass


class AlreadyStolenError(GameError):
    pass


class HomeGroupLockedError(GameError):
    """農場主要所屬群組不可任意更改（一次性設定已完成，或群組轉移冷卻中）。"""
