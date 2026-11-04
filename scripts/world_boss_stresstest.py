# ruff: noqa: E501  （報表輸出字串較長）
"""世界 Boss 壓力測試：大量玩家同時用 /farm 面板捐獻，並同時重複送出、重複領獎（經過與正式環境相同的執行緒池與連線池）。

只能對 TEST_DATABASE_URL（資料庫名稱以 _test 結尾）執行，會清空其中的遊戲資料。
用法：.venv/Scripts/python.exe scripts/world_boss_stresstest.py [玩家數，預設 200] [global|guild，預設 global] [工作執行緒，預設 16]
  global：所有玩家打同一場全服活動（所有捐獻都鎖同一列，最壞情況）
  guild ：每個伺服器各自一場活動（鎖分散到 20 列）
環境變數 WB_DB_CONTAINER 可指定要取樣 CPU／記憶體的 PostgreSQL 容器（預設 farmgm-postgres-1）。

每位玩家：開面板 → 世界 Boss（介紹區）→ 進入戰鬥區 → 捐獻物資 → 作物 ×5 確認 → 素材 ×5「連點兩下確認」
→ 回戰鬥區 → 回介紹區 → 排行榜 → 回介紹區，
接著以同一個 request_id 同時送出兩次捐獻（模擬網路重送）。全部捐完後管理員結束活動，每位玩家同時領獎兩次。
時鐘固定（ManualClock），因此 HP 只受捐獻的立即傷害影響，可以精確比對。

檢查：補給進度 = 捐獻積分加總；HP = 最大 HP − 立即傷害加總；每位玩家背包扣除量 = 捐獻紀錄；重複送出只算一次；
每人只領一次獎；金幣 = 交易紀錄加總；排行統計表一致；世界 Boss 圖片每位玩家只上傳一次。
"""

import asyncio
import os
import statistics
import sys
import threading
import time
from datetime import timedelta
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

from app.clock import ManualClock  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.discord_bot.commands import FarmCommands  # noqa: E402
from app.discord_bot.runner import ServiceRunner  # noqa: E402
from app.game import world_boss as wb  # noqa: E402
from app.game.errors import AlreadyExistsError  # noqa: E402
from app.repositories import farm_stats as farm_stats_repo  # noqa: E402
from app.services.admin_service import AdminService  # noqa: E402

USERS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
MODE = sys.argv[2] if len(sys.argv) > 2 else "global"
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 16  # 與 BOT_WORKER_THREADS 預設相同
GUILDS = 20
GIVE = {"carrot": 20, "wood": 20}
assert MODE in ("global", "guild")


class S(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    test_database_url: str


url = S().test_database_url
assert make_url(url).database.endswith("_test"), "只能用在測試資料庫"
engine = make_engine(url, pool_size=WORKERS + 2, max_overflow=5, pool_timeout=10)  # 與 Bot 相同的連線池
factory = make_session_factory(engine)
clock = ManualClock()
service = AdminService(factory, clock)

LAT: dict[str, list[float]] = {}
ERRORS: list[str] = []
UPLOAD = {"images": 0, "image_bytes": 0, "reused": 0}


# ---------- 模擬 Discord 互動（訊息會記住目前掛著的附件，才能驗證圖片沿用） ----------
class Message:
    def __init__(self):
        self.attachments = []
        self.uploads = 0  # 這則面板訊息被上傳過幾張圖（每位玩家各自計數）


def apply_attachments(msg, files):
    if files is None:
        return
    kept = []
    for f in files:
        if isinstance(f, discord.File):
            UPLOAD["images"] += 1
            UPLOAD["image_bytes"] += f.fp.getbuffer().nbytes
            msg.uploads += 1
            kept.append(SimpleNamespace(filename=f.filename))
        else:
            UPLOAD["reused"] += 1
            kept.append(f)
    msg.attachments = kept


class Response:
    def __init__(self, it):
        self.it, self._done = it, False

    def is_done(self):
        return self._done

    async def defer(self, **kw):
        self._done = True

    async def send_message(self, *a, **kw):
        self._done = True

    async def edit_message(self, **kw):
        self._done = True
        self.it.done = time.perf_counter()
        self.it.embed = kw.get("embed")
        apply_attachments(self.it.message, kw.get("attachments"))

    async def send_modal(self, modal):
        self._done = True


class Followup:
    def __init__(self, it):
        self.it = it

    async def send(self, *a, **kw):
        self.it.done = time.perf_counter()
        self.it.view = kw.get("view")
        self.it.embed = kw.get("embed")
        apply_attachments(self.it.message, kw.get("files"))
        return SimpleNamespace(edit=self._edit)

    async def _edit(self, **kw):
        pass


class Interaction:
    def __init__(self, uid, gid, message):
        self.user = SimpleNamespace(id=uid, display_name=f"玩家{uid % 100000}", display_avatar=None)
        self.guild_id = gid
        self.guild = SimpleNamespace(id=gid, name=f"壓測伺服器{gid % 100}")
        self.message = message
        self.response = Response(self)
        self.followup = Followup(self)
        self.start = time.perf_counter()
        self.done = None
        self.view = self.embed = None

    async def edit_original_response(self, **kw):
        self.done = time.perf_counter()
        self.embed = kw.get("embed")
        apply_attachments(self.message, kw.get("attachments"))

    async def delete_original_response(self):
        pass


def record(step, it):
    LAT.setdefault(step, []).append(((it.done or time.perf_counter()) - it.start) * 1000)
    if it.embed is not None and (it.embed.title or "").startswith("⚠️"):
        ERRORS.append(f"{step}: {it.embed.description}")


def find(panel, cls, text_):
    for item in panel.children:
        if isinstance(item, cls) and text_ in ((item.label if cls is discord.ui.Button else item.placeholder) or ""):
            return item
    return None


DUP = {"panel_double": 0, "service_double": 0, "service_duplicate_flag": 0}
BOSS_UPLOADS: dict[int, int] = {}
UPLOAD_STEPS: dict[str, int] = {}  # 各步驟上傳了幾張圖（找出哪一步重新上傳）


async def player(cmds, runner, uid, gid, group_id, event_id):
    msg = Message()
    it = Interaction(uid, gid, msg)
    await cmds.open_panel(it)
    record("開啟面板", it)
    panel = it.view
    if panel is None:
        ERRORS.append("開啟面板失敗")
        return
    panel.fish_timers_enabled = False

    async def act(step, coro_factory):
        i = Interaction(uid, gid, msg)
        before = msg.uploads
        await coro_factory(i)
        record(step, i)
        UPLOAD_STEPS[step] = UPLOAD_STEPS.get(step, 0) + msg.uploads - before
        if panel.page.startswith("boss"):
            BOSS_UPLOADS[uid] = BOSS_UPLOADS.get(uid, 0) + msg.uploads - before

    async def click(label, step):
        item = find(panel, discord.ui.Button, label)
        if item is None:
            ERRORS.append(f"{step}: 找不到按鈕「{label}」（頁面 {panel.page}）")
            return
        await act(step, item.callback)

    async def choose(placeholder, value, step):
        item = find(panel, discord.ui.Select, placeholder)
        if item is None:
            ERRORS.append(f"{step}: 找不到選單「{placeholder}」（頁面 {panel.page}）")
            return
        await act(step, lambda i: item.farm_handler(i, value))

    async def go(page, step):
        panel.page = page
        await act(step, lambda i: panel.goto(i, page))

    await go("boss", "進入世界 Boss")
    await click("進入", "進入戰鬥區")
    await click("捐獻物資", "捐獻頁")
    await choose("捐獻類別", "crop", "選類別")
    await choose("選擇物品", "0", "選物品")
    await click("× 5", "選數量")
    await click("確認捐獻", "確認捐獻（交易）")
    await choose("捐獻類別", "material", "選類別")
    await choose("選擇物品", "0", "選物品")
    await click("× 5", "選數量")
    confirm = find(panel, discord.ui.Button, "確認捐獻")
    if confirm is not None:  # 舊畫面上的同一個按鈕連點兩下
        a, b = Interaction(uid, gid, msg), Interaction(uid, gid, msg)
        before = msg.uploads
        await asyncio.gather(confirm.callback(a), confirm.callback(b))
        record("確認捐獻（連點兩下）", a)
        record("確認捐獻（連點兩下）", b)
        BOSS_UPLOADS[uid] = BOSS_UPLOADS.get(uid, 0) + msg.uploads - before
        DUP["panel_double"] += 1
    await click("回戰鬥區", "切換頁面")
    await click("回介紹區", "切換頁面")
    await click("查看排行榜", "排行榜")
    await click("回介紹區", "切換頁面")
    await click("更新狀態", "更新狀態")

    # 服務層：同一個 request_id 同時送出兩次（網路重送）
    req = f"stress-{uid}"
    t = time.perf_counter()
    r1, r2 = await asyncio.gather(
        runner.run(service.boss_donate, group_id, panel.player_id, event_id, "crop", "carrot", 1, req),
        runner.run(service.boss_donate, group_id, panel.player_id, event_id, "crop", "carrot", 1, req),
    )
    LAT.setdefault("同 request_id 同時送兩次", []).append((time.perf_counter() - t) * 1000)
    DUP["service_double"] += 1
    DUP["service_duplicate_flag"] += int(r1["duplicate"]) + int(r2["duplicate"])
    panel.stop()


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


class Sampler:
    def __init__(self, container):
        self.proc = psutil.Process()
        self.container = container
        self.cpu, self.rss, self.db_cpu = [], [], []
        self.stop = threading.Event()

    def _bot(self):
        self.proc.cpu_percent(None)
        while not self.stop.wait(0.5):
            self.cpu.append(self.proc.cpu_percent(None))
            self.rss.append(self.proc.memory_info().rss / 2**20)

    def _db(self):
        import subprocess
        while not self.stop.is_set():
            try:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", self.container],
                                     capture_output=True, text=True, timeout=10).stdout.strip()
                self.db_cpu.append(float(out.rstrip("%")))
            except Exception:
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


async def main():
    print(f"===== 世界 Boss 壓力測試：{USERS} 位玩家，{'全服單一活動' if MODE == 'global' else f'{GUILDS} 個伺服器各一場'}，工作執行緒 {WORKERS} =====")
    with engine.begin() as c:
        c.execute(text(
            "TRUNCATE world_boss_reward_claims, world_boss_contributions, world_boss_supplies, world_boss_events, "
            "world_boss_schedules, ranking_awards, ranking_settlements, ranking_reward_rules, game_events, "
            "competition_awards, competition_results, competition_contributions, competitions, quest_claims, "
            "quest_contributions, quests, admin_actions, exploration_logs, farm_explorations, fish_shares, farm_fish, "
            "fishing_sessions, craft_orders, farm_animals, farm_buildings, farm_defenses, steal_records, group_members, "
            "groups, gamble_prizes, farm_daily_stats, farm_stats, farm_effects, farm_tools, game_transactions, "
            "inventory_items, farm_plots, farms, players RESTART IDENTITY CASCADE"))
    t0 = time.perf_counter()
    users = []
    for i in range(USERS):
        gid = 10**17 + i % GUILDS
        uid = 5 * 10**17 + i
        pid = service.start_external_farm("discord", str(uid), f"玩家{i}", str(gid), f"壓測伺服器{i % GUILDS}").player_id
        for code, qty in GIVE.items():
            service.set_inventory("stress", pid, code, "normal", "none", qty)
        users.append((uid, gid, pid))
    groups = {gid: service.get_group_by_guild(str(gid)).id for gid in {u[1] for u in users}}
    now = clock.now()
    # 參與門檻設 0：壓測要檢查每位捐獻者都能領獎（各玩家捐獻量不一）
    window = {"starts_at": now, "ends_at": now + timedelta(hours=3), "max_hp": 10_000_000,
              "rewards": {**wb.DEFAULT_REWARDS, "min_points": 0}}
    events = {}
    if MODE == "global":
        eid = service.create_world_boss("stress", {"group_id": None, "name": "壓測魔龍", **window})["event_id"]
        events = {gid: eid for gid in groups}
    else:
        for gid, group_id in groups.items():
            events[gid] = service.create_world_boss("stress", {"group_id": group_id, "name": f"壓測魔龍{gid % 100}",
                                                               **window})["event_id"]
    print(f"建立 {USERS} 位玩家與活動：{time.perf_counter() - t0:.1f} 秒")

    runner = ServiceRunner(WORKERS)
    cmds = FarmCommands(service, runner)
    lags, stop = [], asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            lags.append((time.perf_counter() - t - 0.05) * 1000)

    async def background():  # 同時執行背景結算（網站或 Bot 的排程工作）
        n = 0
        while not stop.is_set():
            await runner.run(service.run_background_settlement, "system")
            n += 1
            await asyncio.sleep(0.5)
        return n

    hb, bg = asyncio.create_task(heartbeat()), asyncio.create_task(background())
    container = os.environ.get("WB_DB_CONTAINER", "farmgm-postgres-1")
    t0 = time.perf_counter()
    with Sampler(container) as sampler:
        await asyncio.gather(*(player(cmds, runner, uid, gid, groups[gid], events[gid]) for uid, gid, _pid in users))
        donate_elapsed = time.perf_counter() - t0
        # ---------- 領獎：管理員結束活動後，每位玩家同時領兩次 ----------
        for eid in set(events.values()):
            service.world_boss_action("stress", eid, "end")
        claims = {"ok": 0, "already": 0, "other": 0}
        t1 = time.perf_counter()

        async def claim(uid, gid, pid):
            async def once():
                t = time.perf_counter()
                try:
                    await runner.run(service.boss_claim, groups[gid], pid, events[gid])
                    claims["ok"] += 1
                except AlreadyExistsError:
                    claims["already"] += 1
                except Exception as exc:
                    claims["other"] += 1
                    ERRORS.append(f"領獎：{type(exc).__name__}: {exc}")
                LAT.setdefault("領獎（交易）", []).append((time.perf_counter() - t) * 1000)

            await asyncio.gather(once(), once())

        await asyncio.gather(*(claim(*u) for u in users))
        claim_elapsed = time.perf_counter() - t1
    stop.set()
    await hb
    bg_runs = await bg
    runner.shutdown()

    total = sum(len(v) for v in LAT.values())
    print(f"\n捐獻階段：{donate_elapsed:.1f} 秒；領獎階段：{claim_elapsed:.1f} 秒；共 {total} 次操作；背景結算同時執行 {bg_runs} 次")
    print("各步驟完成時間（ms）：")
    for step, xs in LAT.items():
        over3 = sum(1 for x in xs if x > 3000)
        print(f"  {step:<18} n={len(xs):<5} 平均 {statistics.mean(xs):7.0f}  P50 {pct(xs, .5):7.0f}  P95 {pct(xs, .95):7.0f}"
              f"  P99 {pct(xs, .99):7.0f}  最大 {max(xs):7.0f}  超過 3 秒 {over3}")
    print(f"事件迴圈延遲：P95 {pct(lags, .95):.1f} ms、最大 {max(lags or [0]):.1f} ms")
    print(f"錯誤：{len(ERRORS)}" + (f"（例如：{ERRORS[0]}）" if ERRORS else ""))
    print(f"Bot 程序 CPU：平均 {statistics.mean(sampler.cpu or [0]):.0f}%、峰值 {max(sampler.cpu or [0]):.0f}%"
          f"（100% = 1 核，本機 {psutil.cpu_count()} 核）；記憶體峰值 {max(sampler.rss or [0]):.0f} MB")
    if sampler.db_cpu:
        print(f"PostgreSQL 容器（{container}）CPU：平均 {statistics.mean(sampler.db_cpu):.0f}%、峰值 {max(sampler.db_cpu):.0f}%")
    print(f"圖片：上傳 {UPLOAD['images']} 張（{UPLOAD['image_bytes'] / 2**20:.1f} MB）、沿用附件 {UPLOAD['reused']} 次；"
          f"各步驟上傳：{ {k: v for k, v in UPLOAD_STEPS.items() if v} }")

    # ---------- 一致性檢查 ----------
    checks = []
    with engine.connect() as c:
        q = lambda sql: c.execute(text(sql)).all()  # noqa: E731
        sup = q("SELECT s.event_id, s.supply_code, s.progress, coalesce(sum(c.points), 0) FROM world_boss_supplies s "
                "LEFT JOIN world_boss_contributions c ON c.event_id = s.event_id AND c.supply_code = s.supply_code "
                "GROUP BY s.event_id, s.supply_code, s.progress")
        checks.append(("補給進度 = 捐獻積分加總", all(p == t for _e, _s, p, t in sup)))
        hp = q("SELECT e.id, e.hp, e.max_hp, coalesce(sum(c.damage), 0) FROM world_boss_events e "
               "LEFT JOIN world_boss_contributions c ON c.event_id = e.id GROUP BY e.id")
        checks.append(("HP = 最大 HP − 立即傷害加總", all(h == m - d for _i, h, m, d in hp)))
        n_contrib = q("SELECT count(*) FROM world_boss_contributions")[0][0]
        checks.append((f"每人恰好 3 筆捐獻（作物、素材、重送一次）：{n_contrib} 筆", n_contrib == 3 * USERS))
        inv = q("SELECT i.item_code, sum(i.quantity) FROM inventory_items i WHERE i.item_code IN ('carrot', 'wood') "
                "GROUP BY i.item_code")
        held = dict(inv)
        checks.append((f"背包剩餘 胡蘿蔔 {held.get('carrot')}、木材 {held.get('wood')}（預期 {(20 - 6) * USERS}、{(20 - 5) * USERS} 加上獎勵）",
                       held.get("carrot") == (20 - 6) * USERS and held.get("wood", 0) >= (20 - 5) * USERS))
        dup_req = q("SELECT count(*) FROM (SELECT event_id, player_id, request_id FROM world_boss_contributions "
                    "GROUP BY 1, 2, 3 HAVING count(*) > 1) x")[0][0]
        checks.append(("重複的 request_id：0", dup_req == 0))
        checks.append((f"同一請求同時送兩次，標示為重複 {DUP['service_duplicate_flag']}/{DUP['service_double']}",
                       DUP["service_duplicate_flag"] == DUP["service_double"]))
        n_claims = q("SELECT count(*) FROM world_boss_reward_claims")[0][0]
        checks.append((f"領獎：成功 {claims['ok']}、已領過 {claims['already']}、其他錯誤 {claims['other']}；紀錄 {n_claims} 筆",
                       claims["ok"] == USERS == n_claims and claims["already"] == USERS and claims["other"] == 0))
        gold = q("SELECT coalesce(sum(gold), 0) FROM farms")[0][0]
        ledger = q("SELECT coalesce(sum(gold_delta), 0) FROM game_transactions")[0][0]
        checks.append(("金幣 = 交易紀錄加總", gold == ledger))
    with factory() as s:
        stats = farm_stats_repo.check(s)
    checks.append(("排行統計表一致", stats["farm_stats_mismatches"] == 0 and stats["farm_daily_stats_mismatches"] == 0))
    # 圖片只在介紹區：第一次進入與從戰鬥區回介紹區各上傳一次，介紹區內換頁沿用
    twice = sum(1 for v in BOSS_UPLOADS.values() if v == 2)
    checks.append((f"世界 Boss 圖片每位玩家只上傳兩次（進入、從戰鬥區返回）：{twice}/{USERS}", twice == USERS))
    checks.append(("沒有錯誤畫面", not ERRORS))
    print("\n一致性檢查：")
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    engine.dispose()
    if not all(ok for _n, ok in checks):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
