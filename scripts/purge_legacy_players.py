"""一次性清除舊 CMD 測試玩家資料，讓 Discord 版本以全新玩家開始（破壞性操作，只執行一次）。

不會寫進啟動流程或 Migration。保留資料庫結構與全域設定（作物／動物種類、品質突變設定、限時活動、
排名獎勵規則、管理操作紀錄、alembic_version）；道具、防守設施、建築、加工配方在程式內，不受影響。

用法：
  1. 只檢視（預設，不修改任何資料）：
       .venv/Scripts/python.exe scripts/purge_legacy_players.py
  2. 先在容器內備份並確認可讀取（PowerShell 的 > 會破壞二進位檔）：
       docker exec farmgm-postgres-1 sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -f /tmp/before_purge.dump farmgm'
       docker exec farmgm-postgres-1 sh -c 'pg_restore --list /tmp/before_purge.dump | head'
       docker cp farmgm-postgres-1:/tmp/before_purge.dump .
  3. 執行（需輸入目標資料庫名稱與備份檔路徑）：
       .venv/Scripts/python.exe scripts/purge_legacy_players.py --execute --confirm-db farmgm
           --backup-file before_purge.dump（與上一行為同一個指令）

清除範圍（單一交易，失敗全部回滾）：
  - 所有玩家與其關聯資料：players（連帶 farms、farm_plots、inventory_items、farm_tools、farm_effects、farm_defenses、
    farm_buildings、farm_animals、craft_orders、game_transactions、farm_stats、farm_daily_stats、steal_records、
    group_members、quest_contributions、quest_claims、competition_contributions、competition_awards、ranking_awards）。
  - 全部 competition_results、competition_awards、ranking_settlements、ranking_awards（都是舊玩家的結果）。
  - 已結算或已取消的 competitions（只剩舊玩家的歷史）；進行中／未開始的競賽是管理員設定，保留。
  - 舊 CMD 模擬群組（groups.discord_guild_id IS NULL）及只屬於這些群組的 quests。
  若 Discord 群組下已有任務，代表 Discord 版本已開始營運，腳本會拒絕執行。
"""

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import make_url, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import make_engine  # noqa: E402

PURGE_TABLES = (
    "players", "farms", "farm_plots", "inventory_items", "farm_tools", "farm_effects", "farm_defenses",
    "farm_buildings", "farm_animals", "craft_orders", "game_transactions", "farm_stats", "farm_daily_stats",
    "gamble_prizes", "steal_records", "group_members", "quest_contributions", "quest_claims",
    "competition_contributions", "competition_results", "competition_awards", "ranking_settlements", "ranking_awards",
)
KEEP_TABLES = (
    "crop_types", "animal_types", "crop_genetics_settings", "game_events", "ranking_reward_rules", "admin_actions",
    "alembic_version",
)
PARTIAL = {
    "groups（舊 CMD 群組 discord_guild_id IS NULL）": "SELECT count(*) FROM groups WHERE discord_guild_id IS NULL",
    "groups（Discord 伺服器，保留）": "SELECT count(*) FROM groups WHERE discord_guild_id IS NOT NULL",
    "quests（屬於舊 CMD 群組，清除）": (
        "SELECT count(*) FROM quests WHERE group_id IN (SELECT id FROM groups WHERE discord_guild_id IS NULL)"
    ),
    "quests（屬於 Discord 群組）": (
        "SELECT count(*) FROM quests WHERE group_id IN (SELECT id FROM groups WHERE discord_guild_id IS NOT NULL)"
    ),
    "competitions（已結算／已取消，清除）": (
        "SELECT count(*) FROM competitions WHERE status IN ('settled', 'cancelled')"
    ),
    "competitions（進行中／未開始，保留）": "SELECT count(*) FROM competitions WHERE status = 'active'",
}

PURGE_SQL = (
    "DELETE FROM ranking_awards",
    "DELETE FROM ranking_settlements",
    "DELETE FROM competition_awards",
    "DELETE FROM competition_results",
    "DELETE FROM competition_contributions",
    "DELETE FROM competitions WHERE status IN ('settled', 'cancelled')",
    "DELETE FROM quest_claims",
    "DELETE FROM quest_contributions",
    "DELETE FROM players",  # 外鍵 ON DELETE CASCADE 連帶清除農場與所有玩家關聯資料
    "DELETE FROM quests WHERE group_id IN (SELECT id FROM groups WHERE discord_guild_id IS NULL)",
    "DELETE FROM groups WHERE discord_guild_id IS NULL",
)


def counts(conn) -> tuple[dict, dict, dict]:
    purge = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in PURGE_TABLES}
    keep = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in KEEP_TABLES}
    partial = {k: conn.execute(text(sql)).scalar() for k, sql in PARTIAL.items()}
    return purge, keep, partial


def show(title: str, rows: dict) -> None:
    print(f"\n[{title}]")
    for name, n in rows.items():
        print(f"  {name:<44} {n:>10,}")


def main() -> int:
    parser = argparse.ArgumentParser(description="一次性清除舊 CMD 測試玩家資料")
    parser.add_argument("--execute", action="store_true", help="實際執行清除（預設只檢視）")
    parser.add_argument("--confirm-db", help="再次輸入目標資料庫名稱以確認")
    parser.add_argument("--backup-file", help="已完成的 pg_dump -Fc 備份檔路徑")
    args = parser.parse_args()

    url = make_url(get_settings().database_url)
    print(f"目標資料庫：主機 {url.host}、Port {url.port}、資料庫 {url.database}、使用者 {url.username}")
    engine = make_engine(url.render_as_string(hide_password=False))
    try:
        with engine.connect() as conn:
            purge, keep, partial = counts(conn)
        show("預計清除", purge)
        show("部分清除／保留", partial)
        show("保留（全域設定與稽核）", keep)
        if not args.execute:
            print("\n只檢視，未修改任何資料。要執行請加上 --execute --confirm-db <資料庫名稱> --backup-file <備份檔>。")
            return 0
        if args.confirm_db != url.database:
            print(f"\n[拒絕] --confirm-db 必須等於目標資料庫名稱「{url.database}」。")
            return 2
        backup = Path(args.backup_file or "")
        if not args.backup_file or not backup.is_file() or backup.stat().st_size == 0:
            print("\n[拒絕] 請先完成 pg_dump -Fc 備份，並以 --backup-file 指定（檔案必須存在且不是空檔）。")
            return 2
        if partial["quests（屬於 Discord 群組）"]:
            print("\n[拒絕] Discord 群組下已有任務，代表 Discord 版本已開始營運，不應再執行一次性清除。")
            return 2
        with engine.begin() as conn:
            for sql in PURGE_SQL:
                n = conn.execute(text(sql)).rowcount
                print(f"  {sql:<90} {n:>8,} 筆")
        with engine.connect() as conn:
            purge_after, keep_after, partial_after = counts(conn)
        show("清除後", purge_after)
        show("清除後（部分）", partial_after)
        assert keep_after == keep, "保留的資料表筆數不應改變"
        print(f"\n完成（備份：{backup}，{backup.stat().st_size:,} bytes）。保留的資料表筆數未變。")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
