# ruff: noqa: E501  （報表輸出字串較長）
"""Discord 面板壓力測試：模擬大量玩家同時用 /farm 面板操作（經過與正式環境相同的執行緒池與連線池）。

只能對 TEST_DATABASE_URL（資料庫名稱以 _test 結尾）執行，會清空其中的遊戲資料。
用法：.venv/Scripts/python.exe scripts/bot_stresstest.py [同時玩家數，預設 100] [同時播放音樂的伺服器數，預設 0] [工作執行緒，預設 16]

每位玩家：開面板 → 選作物種滿農田 → 全部澆水 → 回總覽 → 商店選種子 → 買 5 → 排行榜 → 賭場。
量測每次互動「完成畫面更新」的時間（首次回應一律立即 defer，符合 Discord 3 秒規則），
以及成功／失敗、資料一致性、CPU／記憶體（Bot 程序與 PostgreSQL 容器）、要上傳到 Discord 的資料量
（Embed JSON＋面板圖片；實際網路流量只能在真實 Discord 量測，這裡是 Bot 送出內容的大小）。可同時讓多個伺服器播放音樂（音訊在 Lavalink，Bot 只處理播放事件與訊息），
觀察音樂對農場的影響。
"""

import asyncio
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import discord  # noqa: E402
import psutil  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402
from sqlalchemy import make_url, text  # noqa: E402

from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.discord_bot.commands import FarmCommands  # noqa: E402
from app.discord_bot.runner import ServiceRunner  # noqa: E402
from app.repositories import farm_stats as farm_stats_repo  # noqa: E402
from app.services.admin_service import AdminService  # noqa: E402

USERS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
MUSIC_STREAMS = int(sys.argv[2]) if len(sys.argv) > 2 else 0
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 16  # 與 BOT_WORKER_THREADS 預設相同
GUILDS = 20


class S(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    test_database_url: str


url = S().test_database_url
assert make_url(url).database.endswith("_test"), "只能用在測試資料庫"
engine = make_engine(url, pool_size=WORKERS + 2, max_overflow=5, pool_timeout=10)  # 與 Bot 相同的連線池
factory = make_session_factory(engine)
service = AdminService(factory)


# ---------- 模擬 Discord 互動 ----------
class Response:
    def __init__(self, it):
        self.it, self._done = it, False

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True
        self.it.first = self.it.first or time.perf_counter()

    async def send_message(self, *a, **kw):
        self._done = True
        self.it.first = self.it.first or time.perf_counter()

    async def send_modal(self, modal):
        self._done = True


class Followup:
    def __init__(self, it):
        self.it = it

    async def send(self, *a, **kw):
        self.it.done = time.perf_counter()
        self.it.view = kw.get("view")
        self.it.embed = kw.get("embed")
        count_upload(kw, kw.get("files"))
        return SimpleNamespace(edit=self._edit)

    async def _edit(self, **kw):
        pass


class Interaction:
    def __init__(self, uid, gid):
        self.user = SimpleNamespace(id=uid, display_name=f"玩家{uid % 100000}", display_avatar=None)
        self.guild_id = gid
        self.guild = SimpleNamespace(id=gid, name=f"壓測伺服器{gid % 100}")
        self.response = Response(self)
        self.followup = Followup(self)
        self.start = time.perf_counter()
        self.first = self.done = None
        self.view = self.embed = None

    async def edit_original_response(self, **kw):
        self.done = time.perf_counter()
        self.embed = kw.get("embed")
        count_upload(kw, kw.get("attachments"))

    async def delete_original_response(self):
        pass


LAT: dict[str, list[float]] = {}
FIRST: list[float] = []
ERRORS: list[str] = []
UPLOAD = {"messages": 0, "embed_bytes": 0, "images": 0, "image_bytes": 0}


def count_upload(kw, files):
    """估算這次要送到 Discord 的資料量：Embed JSON 與附件圖片（不含元件與 HTTP 標頭）。"""
    UPLOAD["messages"] += 1
    if kw.get("embed") is not None:
        UPLOAD["embed_bytes"] += len(json.dumps(kw["embed"].to_dict(), ensure_ascii=False).encode())
    for f in files or []:
        UPLOAD["images"] += 1
        UPLOAD["image_bytes"] += f.fp.getbuffer().nbytes


class Sampler:
    """背景取樣 Bot 程序（本程序）的 CPU／記憶體，以及 PostgreSQL 容器的 CPU／記憶體（docker stats）。"""

    def __init__(self, container="farmgm-postgres-1"):
        self.proc = psutil.Process()
        self.container = container
        self.cpu, self.rss, self.db_cpu, self.db_mem = [], [], [], []
        self.stop = threading.Event()

    def _bot(self):
        self.proc.cpu_percent(None)
        while not self.stop.wait(0.5):
            self.cpu.append(self.proc.cpu_percent(None))
            self.rss.append(self.proc.memory_info().rss / 2**20)

    def _db(self):
        while not self.stop.is_set():
            try:
                out = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", self.container],
                    capture_output=True, text=True, timeout=10).stdout.strip()
                cpu, mem = out.split("|")
                self.db_cpu.append(float(cpu.rstrip("%")))
                value = mem.split("/")[0].strip()
                unit = 1024 if value.endswith("GiB") else 1 / 1024 if value.endswith("KiB") else 1
                self.db_mem.append(float(value.rstrip("GiBMKk")) * unit)
            except Exception:  # 沒有 Docker 或容器名稱不同：只量 Bot 程序
                return

    def __enter__(self):
        self.threads = [threading.Thread(target=t, daemon=True) for t in (self._bot, self._db)]
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        for t in self.threads:
            t.join(15)


def record(step, it):
    end = it.done or time.perf_counter()
    LAT.setdefault(step, []).append((end - it.start) * 1000)
    if it.first:
        FIRST.append((it.first - it.start) * 1000)
    if it.embed is not None and (it.embed.title or "").startswith("⚠️"):
        ERRORS.append(f"{step}: {it.embed.description}")


def find(panel, cls, text_):
    for item in panel.children:
        if isinstance(item, cls) and text_ in ((item.label if cls is discord.ui.Button else item.placeholder) or ""):
            return item
    return None


async def player(cmds, uid, gid):
    it = Interaction(uid, gid)
    await cmds.open_panel(it)
    record("開啟面板", it)
    panel = it.view
    if panel is None:
        ERRORS.append("開啟面板失敗")
        return

    async def click(label, step):
        item = find(panel, discord.ui.Button, label)
        if item is None:
            return
        i = Interaction(uid, gid)
        await item.callback(i)
        record(step, i)

    async def choose(placeholder, value, step):
        item = find(panel, discord.ui.Select, placeholder)
        if item is None:
            return
        i = Interaction(uid, gid)
        await item.farm_handler(i, value)
        record(step, i)

    async def go(page, step):
        i = Interaction(uid, gid)
        await panel.goto(i, page)
        record(step, i)

    await go("plots", "切換頁面")
    await choose("選擇作物", "wheat", "種滿農田（多筆交易）")
    await click("全部澆水", "全部澆水（多筆交易）")
    await go("main", "切換頁面")
    await go("shop", "切換頁面")
    await choose("選擇要買的商品", "seed:carrot", "選擇商品")
    await click("買 5", "購買")
    await go("board", "排行榜")
    await go("casino", "切換頁面")


# ---------- 背景音樂負載（音訊由 Lavalink 處理；Bot 端是播放事件、切歌與「正在播放」訊息） ----------
def start_music_load(n):
    if not n:
        return None
    from music_stresstest import bot_side

    # 每首 1 秒就結束，切歌頻率遠高於真實（真實歌曲約 3–4 分鐘一首）
    return asyncio.create_task(bot_side(n, 30, 1.0))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


async def main():
    print(f"===== Discord 面板壓力測試：{USERS} 位玩家同時操作，工作執行緒 {WORKERS}，同時播放音樂的伺服器 {MUSIC_STREAMS} =====")
    with engine.begin() as c:
        c.execute(text(
            "TRUNCATE ranking_awards, ranking_settlements, ranking_reward_rules, game_events, competition_awards, "
            "competition_results, competition_contributions, competitions, quest_claims, quest_contributions, quests, "
            "admin_actions, craft_orders, farm_animals, farm_buildings, farm_defenses, steal_records, group_members, "
            "groups, gamble_prizes, farm_daily_stats, farm_stats, farm_effects, farm_tools, game_transactions, "
            "inventory_items, farm_plots, farms, players RESTART IDENTITY CASCADE"
        ))
        c.execute(text("UPDATE crop_types SET active = true"))
    t0 = time.perf_counter()
    users = []
    for i in range(USERS):
        gid = 10**17 + i % GUILDS
        uid = 5 * 10**17 + i
        service.start_external_farm("discord", str(uid), f"玩家{i}", str(gid), f"壓測伺服器{i % GUILDS}")
        users.append((uid, gid))
    print(f"建立 {USERS} 位玩家：{time.perf_counter() - t0:.1f} 秒")

    load = start_music_load(MUSIC_STREAMS)
    runner = ServiceRunner(WORKERS)
    cmds = FarmCommands(service, runner)

    # 量測事件迴圈延遲（Gateway 心跳是否會被拖慢）
    lags = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            lags.append((time.perf_counter() - t - 0.05) * 1000)

    hb = asyncio.create_task(heartbeat())
    mem0 = psutil.Process().memory_info().rss / 2**20
    t0 = time.perf_counter()
    with Sampler() as sampler:
        await asyncio.gather(*(player(cmds, uid, gid) for uid, gid in users))
        elapsed = time.perf_counter() - t0
    stop.set()
    await hb
    runner.shutdown()
    if load:
        print("\n等待背景音樂播完…")
        await load  # bot_side 會印出音樂端的切歌延遲、CPU 與記憶體

    total = sum(len(v) for v in LAT.values())
    print(f"\n{total} 次互動，{elapsed:.1f} 秒（{total / elapsed:.0f} 次/秒）")
    print(f"首次回應（defer）：P95 {pct(FIRST, .95):.1f} ms、最大 {max(FIRST or [0]):.1f} ms（Discord 限制 3000 ms）")
    print("各步驟「畫面更新完成」時間（ms）：")
    for step, xs in LAT.items():
        over3 = sum(1 for x in xs if x > 3000)
        print(f"  {step:<16} n={len(xs):<5} 平均 {statistics.mean(xs):7.0f}  P50 {pct(xs, .5):7.0f}  P95 {pct(xs, .95):7.0f}"
              f"  P99 {pct(xs, .99):7.0f}  最大 {max(xs):7.0f}  超過 3 秒 {over3}")
    print(f"事件迴圈延遲：P95 {pct(lags, .95):.1f} ms、最大 {max(lags or [0]):.1f} ms（Gateway 心跳間隔約 41 秒，延遲數秒才會有影響）")
    print(f"錯誤畫面：{len(ERRORS)}" + (f"（例如：{ERRORS[0]}）" if ERRORS else ""))
    cores = psutil.cpu_count()
    print(f"Bot 程序 CPU：平均 {statistics.mean(sampler.cpu or [0]):.0f}%、峰值 {max(sampler.cpu or [0]):.0f}%"
          f"（100% = 1 核，本機 {cores} 核）；記憶體：開始 {mem0:.0f} MB、峰值 {max(sampler.rss or [mem0]):.0f} MB")
    if sampler.db_cpu:
        print(f"PostgreSQL 容器 CPU：平均 {statistics.mean(sampler.db_cpu):.0f}%、峰值 {max(sampler.db_cpu):.0f}%；"
              f"記憶體峰值 {max(sampler.db_mem):.0f} MB（與正式資料庫同一容器，含正式 Bot 的負載）")
    up = UPLOAD
    mb = (up["embed_bytes"] + up["image_bytes"]) / 2**20
    print(f"送往 Discord 的內容：{up['messages']} 則訊息更新，Embed {up['embed_bytes'] / 2**20:.1f} MB、"
          f"圖片 {up['images']} 張 {up['image_bytes'] / 2**20:.1f} MB（平均 {up['image_bytes'] / max(1, up['images']) / 1024:.0f} KB/張）；"
          f"合計 {mb:.1f} MB，平均 {mb * 8 / elapsed:.1f} Mbps 上傳")
    with engine.connect() as c:
        gold = c.execute(text("SELECT coalesce(sum(gold), 0) FROM farms")).scalar()
        ledger = c.execute(text("SELECT coalesce(sum(gold_delta), 0) FROM game_transactions")).scalar()
        conns = c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")).scalar()
    with factory() as s:
        stats = farm_stats_repo.check(s)
    print(f"一致性：金幣 = 交易紀錄 {gold == ledger}；統計表不一致 {stats['farm_stats_mismatches']}/{stats['farm_daily_stats_mismatches']}；"
          f"結束時資料庫連線 {conns}")
    engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
