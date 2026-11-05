"""把 VPS 正式資料庫備份一份到本機：python scripts/pull_vps_backup.py [--keep N]

流程：在 VPS 的 postgres 容器內 pg_dump（自訂格式，不停 Bot）→ scp 下載到 backup/。
預設保留全部舊備份；指定 --keep N 才只留最新的 N 份。
由 Windows 工作排程器「farmgm VPS 備份」每天 00:00 執行（不帶 --keep）；登入使用者的 SSH 金鑰 ~/.ssh/id_ed25519。
檔案先下載成 .tmp 再改名，下載失敗不會留下不完整的備份，也不會刪掉舊備份。
"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOST = "root@13.140.248.193"
CONTAINER = "farmgm-postgres-1"
REMOTE_FILE = "/tmp/farmgm_pc_backup.dump"
ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = ROOT / "backup"
KEY = Path.home() / ".ssh" / "id_ed25519"
PREFIX = "farmgm_"


def _tool(name: str) -> str:
    # 優先用 Windows 內建 OpenSSH（工作排程器沒有 Git Bash 的 PATH）
    builtin = Path(r"C:\Windows\System32\OpenSSH") / f"{name}.exe"
    return str(builtin) if builtin.exists() else (shutil.which(name) or name)


SSH_OPTS = ["-i", str(KEY), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=30"]


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, timeout=600)


def main() -> int:
    parser = argparse.ArgumentParser(description="下載 VPS 資料庫備份到本機 backup/")
    parser.add_argument("--keep", type=int, default=None, help="只保留最新的 N 份（預設全部保留）")
    args = parser.parse_args()
    if args.keep is not None and args.keep < 1:
        parser.error("--keep 至少為 1")

    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    target = BACKUP_DIR / f"{PREFIX}{stamp}.dump"
    tmp = target.with_suffix(".dump.tmp")

    remote = (
        f"docker exec {CONTAINER} sh -c 'pg_dump -U \"$POSTGRES_USER\" -Fc -f /tmp/pc_backup.dump farmgm' "
        f"&& docker cp {CONTAINER}:/tmp/pc_backup.dump {REMOTE_FILE} "
        f"&& docker exec {CONTAINER} rm -f /tmp/pc_backup.dump"
    )
    try:
        run([_tool("ssh"), *SSH_OPTS, HOST, remote])
        run([_tool("scp"), *SSH_OPTS, "-q", f"{HOST}:{REMOTE_FILE}", str(tmp)])
        run([_tool("ssh"), *SSH_OPTS, HOST, f"rm -f {REMOTE_FILE}"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        tmp.unlink(missing_ok=True)
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} 備份失敗：{exc}", file=sys.stderr)
        return 1

    # pg_dump 自訂格式的檔頭是 PGDMP；不是的話視為失敗，不取代舊備份
    with tmp.open("rb") as f:
        if f.read(5) != b"PGDMP":
            tmp.unlink()
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} 備份失敗：下載的檔案不是 pg_dump 格式", file=sys.stderr)
            return 1
    tmp.replace(target)

    backups = sorted(BACKUP_DIR.glob(f"{PREFIX}*.dump"), key=lambda p: p.name, reverse=True)
    removed = backups[args.keep:] if args.keep is not None else []
    for p in removed:
        p.unlink()
    size_kb = target.stat().st_size // 1024
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} 已備份 {target.name}（{size_kb} KB），刪除舊備份 {len(removed)} 份")
    return 0


if __name__ == "__main__":
    sys.exit(main())
