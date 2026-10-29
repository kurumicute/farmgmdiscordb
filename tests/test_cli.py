import pytest

from app.cli.commands import CommandHandler, ExitRequested


@pytest.fixture
def cli(service):
    return CommandHandler(service)


def test_full_game_flow(cli, clock):
    assert "請先選擇玩家" in cli.handle("farm status")
    cli.handle("group create 主群")
    assert "已建立玩家 #1" in cli.handle("player create 阿明")
    out = cli.handle("farm start 1 快樂農場")
    assert "農場「快樂農場」建立完成" in out and "所屬群組：#1「主群」" in out
    assert "金幣：500" in cli.handle("farm status")
    assert "花費 50 金幣" in cli.handle("buy wheat 5")
    assert "已在第 1 格種下小麥" in cli.handle("plant 1 wheat")
    assert "還沒澆水" in cli.handle("harvest 1")
    assert "等待澆水" in cli.handle("plots")
    assert "1 分 0 秒後成熟" in cli.handle("water all")
    assert "尚未成熟" in cli.handle("harvest 1")
    assert "生長中" in cli.handle("plots")
    clock.advance(60)
    assert "可收成" in cli.handle("plots")
    assert "獲得 2 個小麥" in cli.handle("harvest 1")
    assert "小麥" in cli.handle("inventory")
    assert "目前金幣 480" in cli.handle("sell wheat 2")
    assert "金幣不足" in cli.handle("upgrade")
    log = cli.handle("log")
    assert "販售" in log and "+30" in log


def test_player_switching(cli):
    cli.handle("player create A")
    cli.handle("player create B")
    assert "B" in cli.handle("player list")
    assert "目前玩家：#1" in cli.handle("player use 1")
    assert "找不到玩家" in cli.handle("player use 99")


def test_errors_are_friendly(cli):
    cli.handle("group create g")
    cli.handle("player create 測試")
    cli.handle("farm start 1")
    assert "未知的指令" in cli.handle("dance")
    assert "用法" in cli.handle("buy wheat")
    assert "必須是整數" in cli.handle("buy wheat abc")
    assert "沒有「banana」" in cli.handle("buy banana 1")
    assert "引號" in cli.handle('player create "abc')
    assert cli.handle("") == ""
    assert "可用指令" in cli.handle("﻿help")  # PowerShell 管線帶入的 BOM
    assert "目前沒有可收成" in cli.handle("harvest all")
    with pytest.raises(ExitRequested):
        cli.handle("exit")


def test_shop_lists_db_prices(cli):
    out = cli.handle("shop")
    assert "strawberry" in out and "草莓" in out and "10 分 0 秒" in out


def _farmer(cli, name, group_id):
    cli.handle(f"player create {name}")
    assert "建立完成" in cli.handle(f"farm start {group_id}")
    return cli.player_id


def test_group_steal_leaderboard_flow(cli, clock):
    assert "已建立群組 #1" in cli.handle("group create 測試群")
    cli.handle("group create 另一群")
    a = _farmer(cli, "Farmer01", 1)
    b = _farmer(cli, "Farmer02", 1)
    # 未選擇群組：相關指令提示先選群組，不會默認使用其他群組
    for cmd in ("steal targets", "group members", "group status", "group add 1", "leaderboard group",
                "steal 1 1 1"):
        assert "請先選擇群組" in cli.handle(cmd)
    assert "目前群組：#1" in cli.handle("group use 1")
    # 在群組 1 建立農場時已自動成為成員
    assert "已經是群組成員" in cli.handle(f"group add {a}")
    members = cli.handle("group members")
    assert "Farmer01" in members and "Farmer02" in members
    assert "成員數：2" in cli.handle("group status")
    assert "*目前" in cli.handle("group list")

    # Farmer02（目前玩家）種草莓
    cli.handle("buy strawberry 1")
    cli.handle("plant 1 strawberry")
    cli.handle("water 1")
    clock.advance(600)

    cli.handle(f"player use {a}")
    out = cli.handle("steal targets")
    assert "========== 可偷菜目標 ==========" in out
    assert "玩家：Farmer02" in out and "土地：第 1 格" in out and "作物：草莓" in out
    assert "剩餘數量：5" in out and "本次最多可偷：2" in out
    assert "每次最多只能偷 2 個" in cli.handle(f"steal {b} 1 3")
    assert "成功從 Farmer02" in cli.handle(f"steal {b} 1 2")
    assert "已經偷過" in cli.handle(f"steal {b} 1 1")
    assert cli.handle("steal targets") == "目前群組沒有可以偷取的作物。"
    assert "草莓" in cli.handle("inventory items")

    # 排行榜
    out = cli.handle("leaderboard global")
    assert "總排行榜：金幣" in out and "總排行榜：農產品販售" in out
    cli.handle("sell strawberry 2")
    sales = cli.handle("leaderboard group sales")
    assert "群組排行榜" in sales and "金幣" not in sales.splitlines()[0]
    first = sales.splitlines()[2]
    assert "Farmer01" in first and " 2 " in first and "50" in first

    # 不是成員的玩家不能看群組排行榜或偷菜
    c = _farmer(cli, "Outsider", 2)
    assert "不是目前群組的成員" in cli.handle("leaderboard group")
    assert "不是目前群組的成員" in cli.handle("steal targets")
    assert "你不是目前群組的成員" in cli.handle(f"steal {b} 1 1")
    cli.handle(f"player use {a}")
    assert "目標玩家不在目前群組" in cli.handle(f"steal {c} 1 1")
    assert "移出群組" in cli.handle(f"group remove {b}")


def test_inventory_requires_player_and_shows_sections(cli, session_factory):
    from sqlalchemy import update

    from app.db.models import Farm

    for cmd in ("inventory", "inventory items", "inventory tools"):
        assert "請先選擇玩家" in cli.handle(cmd)
    assert "請先選擇群組" in cli.handle("leaderboard group")
    cli.handle("group create 空群")
    cli.handle("group use 1")
    assert "請先選擇玩家" in cli.handle("leaderboard group")  # 已選群組但未選玩家
    assert "請先選擇玩家" in cli.handle("steal targets")
    assert "目前沒有玩家資料" in cli.handle("leaderboard global money")  # 不需要選擇玩家
    _farmer(cli, "背包測試", 1)
    assert "目前背包是空的" in cli.handle("inventory")
    tools = cli.handle("inventory tools")
    assert "自動澆水器：尚未購買" in tools and "人工養殖蚯蚓：未啟用" in tools
    with session_factory.begin() as s:
        s.execute(update(Farm).values(gold=5000))
    cli.handle("buy wheat 2")
    cli.handle("buy fertilizer 3")
    cli.handle("buy earthworm 1")
    cli.handle("buy auto_waterer 1")
    assert "已啟用" in cli.handle("use earthworm")
    out = cli.handle("inventory")
    assert "【種子】" in out and "小麥種子" in out and "商店售價 10" in out
    assert "【消耗型道具】" in out and "化肥" in out and "x 3" in out
    assert "自動澆水器：已永久持有，已啟用" in out
    assert "人工養殖蚯蚓：有效中" in out and "剩餘 72 小時 0 分 0 秒" in out and "×0.75" in out
    assert "已自動澆水" in cli.handle("plant 1 wheat")
    assert "已停用" in cli.handle("tool off auto_waterer")
    assert "請記得澆水" in cli.handle("plant 2 wheat")


def test_long_lists_search_and_paging(cli, service, home_group):
    for i in range(1, 46):
        service.create_player(f"農夫{i:02d}")
    service.create_player("100%_特殊")
    first = cli.handle("player list")
    assert "農夫01" in first and "農夫21" not in first
    assert "第 1/3 頁，共 46 筆；下一頁：player list 2" in first
    third = cli.handle("player list 3")
    assert "農夫41" in third and "100%_特殊" in third and "第 3/3 頁" in third
    assert "第 3/3 頁" in cli.handle("player list 99")  # 超出範圍時顯示最後一頁
    found = cli.handle("player list 農夫1")
    assert "農夫10" in found and "農夫19" in found and "農夫20" not in found and "共 10 筆" in found
    assert "共 1 筆" in cli.handle("player list %_")  # 萬用字元被跳脫，不會比對到所有人
    assert "找不到" in cli.handle("player list 不存在的人")

    for i in range(25):
        service.add_member(home_group, i + 1)
    cli.handle(f"group use {home_group}")
    members = cli.handle("group members")
    assert "第 1/2 頁，共 25 筆" in members
    assert "農夫25" in cli.handle("group members 2")
    assert "共 1 筆" in cli.handle("group members 農夫07")
    assert "主群組" in cli.handle("group list 主群")
