"""排行用統計表（farm_stats／farm_daily_stats）的一致性檢查與重算。

用法：
  .venv/Scripts/python.exe scripts/farm_stats.py check     # 只比對，不修改（唯讀交易）
  .venv/Scripts/python.exe scripts/farm_stats.py rebuild   # 由 game_transactions 重算（會短暫鎖定統計表）

預設使用 .env 的 DATABASE_URL；統計表只是交易紀錄的彙總，重算不會改動交易紀錄或玩家資產。
"""

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import make_url, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.repositories import farm_stats as farm_stats_repo  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("check", "rebuild"):
        print(__doc__)
        return 2
    url = make_url(get_settings().database_url)
    print(f"資料庫：{url.host}:{url.port}/{url.database}")
    engine = make_engine(url.render_as_string(hide_password=False))
    factory = make_session_factory(engine)
    try:
        if sys.argv[1] == "check":
            with factory() as s:
                s.execute(text("SET TRANSACTION READ ONLY"))
                result = farm_stats_repo.check(s)
            print(f"farm_stats 不一致 {result['farm_stats_mismatches']} 筆、"
                  f"farm_daily_stats 不一致 {result['farm_daily_stats_mismatches']} 筆")
            for row in result["examples"]:
                print("  ", row)
            return 0 if not (result["farm_stats_mismatches"] or result["farm_daily_stats_mismatches"]) else 1
        with factory.begin() as s:
            counts = farm_stats_repo.rebuild(s)
        print(f"已重算：{counts}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
