"""CMD 遊戲主迴圈。啟動：python -m app.cli"""

import sys

from app.cli.commands import CommandHandler, ExitRequested
from app.config import get_settings
from app.db.check import StartupCheckError, check_database
from app.db.session import make_engine, make_session_factory
from app.services.ranking_service import RankingService


def main() -> int:
    # 輸出被導向檔案或管線時，遇到無法編碼的字元以替代字元顯示，避免程式中斷
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    try:
        settings = get_settings()
        engine = make_engine(settings.database_url)
        check_database(engine)
    except StartupCheckError as exc:
        print(f"[啟動失敗] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 設定檔缺漏等
        print(f"[啟動失敗] 無法讀取設定：{type(exc).__name__}，請確認 .env 已建立。", file=sys.stderr)
        return 1

    handler = CommandHandler(RankingService(make_session_factory(engine)))
    print("=== 歡迎來到農場經營遊戲 ===  輸入 help 查看指令")
    try:
        while True:
            try:
                line = input(handler.prompt)
            except EOFError:
                break
            try:
                output = handler.handle(line)
            except ExitRequested:
                break
            except Exception as exc:  # 資料庫中斷等非遊戲錯誤：不中斷遊戲迴圈
                output = f"[系統錯誤] 操作未完成（{type(exc).__name__}），請確認資料庫連線後再試。"
            if output:
                print(output)
    except KeyboardInterrupt:
        print()
    finally:
        engine.dispose()
    print("再見！農場資料已保存在資料庫中。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
