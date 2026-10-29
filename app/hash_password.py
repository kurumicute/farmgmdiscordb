"""產生管理員密碼雜湊，填入 .env 的 ADMIN_PASSWORD_HASH。

用法：python -m app.hash_password   （密碼以隱藏方式輸入，不會出現在命令列歷史中）
"""

import getpass
import sys

from app.auth import hash_password


def main() -> int:
    # 從 PowerShell 管線輸入時可能帶 BOM，若不去除，雜湊會與實際密碼不符
    password = getpass.getpass("管理員密碼：").replace("﻿", "")
    if len(password) < 8:
        print("密碼至少需要 8 個字元。", file=sys.stderr)
        return 1
    if getpass.getpass("再輸入一次：").replace("﻿", "") != password:
        print("兩次輸入的密碼不一致。", file=sys.stderr)
        return 1
    print(f"ADMIN_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
