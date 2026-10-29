# ruff: noqa: E501  （報表輸出字串較長）
"""負載測試：在測試資料庫模擬 500 位玩家與大量歷史交易，量測指令、排行榜、監控與並行操作。

只能對 TEST_DATABASE_URL（資料庫名稱以 _test 結尾）執行，會清空其中的遊戲資料。
用法：.venv/Scripts/python.exe scripts/loadtest.py [歷史交易筆數，預設 1000000] [分布天數，預設 90]

量測：成功／失敗數、平均、P50／P95／P99、每秒處理數、連線池等待時間、資料庫連線數，
以及逾時、死結、重複扣款、重複發放與資料不一致的次數。
"""

import re
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402
from sqlalchemy import create_engine, make_url, select, text  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.exc import TimeoutError as PoolTimeoutError  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.db.models import FarmStats, GameTransaction  # noqa: E402
from app.db.session import make_engine, make_session_factory  # noqa: E402
from app.game.errors import GameError  # noqa: E402
from app.repositories import farm_stats as farm_stats_repo  # noqa: E402
from app.repositories import leaderboard as leaderboard_repo  # noqa: E402
from app.services.admin_service import AdminService  # noqa: E402
from app.services.monitor_service import MonitorService  # noqa: E402

TX_ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 1_000_000
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 90
PLAYERS, GROUPS = 500, 20
POOL_SIZE = 40  # 足以讓 32 執行緒情境不因連線池排隊（Bot 預設 8 執行緒，連線池 max(10, 8+2)+5）


class S(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    test_database_url: str


url = S().test_database_url
assert make_url(url).database.endswith("_test"), "只能用在測試資料庫"
engine = make_engine(url, pool_size=POOL_SIZE, max_overflow=5, pool_timeout=10)
factory = make_session_factory(engine)
svc = AdminService(factory)
mon = MonitorService(factory)
probe = create_engine(url, poolclass=NullPool, connect_args={"options": "-c timezone=UTC"})

# 連線池等待時間：包裝 QueuePool 取得連線的內部方法（只在負載測試中使用）
POOL_WAITS: list[float] = []
_orig_get = engine.pool._do_get


def _timed_get():
    t = time.perf_counter()
    conn = _orig_get()
    POOL_WAITS.append((time.perf_counter() - t) * 1000)
    return conn


engine.pool._do_get = _timed_get


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


def summarize(label, xs):
    print(f"{label:<40} 中位數 {statistics.median(xs):7.1f}  P95 {pct(xs, .95):7.1f}  P99 {pct(xs, .99):7.1f}  "
          f"最大 {max(xs):7.1f} ms（n={len(xs)}）")


def timed(label, fn, n=20):
    xs = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t) * 1000)
    summarize(label, xs)
    return xs


def db_connections() -> int:
    with probe.connect() as c:
        return c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")).scalar()


class ConnSampler:
    def __init__(self):
        self.peak, self._stop = 0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, db_connections())
            time.sleep(0.1)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join()


def classify(exc: BaseException) -> str:
    if isinstance(exc, GameError):
        return "遊戲規則拒絕"
    if isinstance(exc, PoolTimeoutError):
        return "連線池逾時"
    if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "40P01":
        return "死結"
    if isinstance(exc, DBAPIError) and getattr(exc.orig, "sqlstate", None) == "40001":
        return "序列化失敗"
    return f"其他錯誤 {type(exc).__name__}"


def run_ops(label, ops, workers):
    """ops：可呼叫物件清單。並行執行並統計延遲、成功／失敗、連線池等待與連線數。"""
    lat, outcomes = [], Counter()
    lock = threading.Lock()
    POOL_WAITS.clear()

    def one(op):
        t = time.perf_counter()
        try:
            op()
            kind = "成功"
        except Exception as exc:  # noqa: BLE001 - 負載測試要統計所有錯誤
            kind = classify(exc)
        ms = (time.perf_counter() - t) * 1000
        with lock:
            lat.append(ms)
            outcomes[kind] += 1

    with ConnSampler() as sampler:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, ops))
        elapsed = time.perf_counter() - t0
    waits = POOL_WAITS[:]
    print(f"[{label}] 執行緒 {workers}：{len(ops)} 個操作 {elapsed:.2f} 秒（{len(ops) / elapsed:.0f} 個/秒）")
    print(f"    延遲 平均 {statistics.mean(lat):.1f}、P50 {pct(lat, .5):.1f}、P95 {pct(lat, .95):.1f}、"
          f"P99 {pct(lat, .99):.1f}、最大 {max(lat):.1f} ms")
    print(f"    連線池等待 平均 {statistics.mean(waits or [0]):.2f}、P95 {pct(waits, .95):.2f}、最大 {max(waits or [0]):.2f} ms；"
          f"資料庫連線數峰值 {sampler.peak}")
    print(f"    結果：{dict(outcomes)}")
    return outcomes


def gold_and_ledger():
    with engine.connect() as c:
        return (
            c.execute(text("SELECT coalesce(sum(gold), 0) FROM farms")).scalar(),
            c.execute(text("SELECT coalesce(sum(gold_delta), 0) FROM game_transactions")).scalar(),
        )


# ======================================================================
print(f"===== 負載測試：{PLAYERS} 位玩家、{GROUPS} 個群組、{TX_ROWS:,} 筆歷史交易（{DAYS} 天）=====")
print(f"PostgreSQL：{probe.connect().execute(text('SHOW server_version')).scalar()}，"
      f"max_connections={probe.connect().execute(text('SHOW max_connections')).scalar()}")

with engine.begin() as c:
    c.execute(text(
        "TRUNCATE ranking_awards, ranking_settlements, ranking_reward_rules, game_events, competition_awards, "
        "competition_results, competition_contributions, competitions, quest_claims, quest_contributions, quests, "
        "admin_actions, craft_orders, farm_animals, farm_buildings, farm_defenses, steal_records, group_members, "
        "groups, gamble_prizes, farm_daily_stats, farm_stats, farm_effects, farm_tools, game_transactions, inventory_items, "
        "farm_plots, farms, players RESTART IDENTITY CASCADE"
    ))
    c.execute(text("UPDATE crop_types SET active = true, grow_seconds = 60"))

t0 = time.perf_counter()
groups = [svc.ensure_guild_group(str(10**17 + i), f"伺服器{i:02d}") for i in range(GROUPS)]
pids = []
for i in range(PLAYERS):
    pid = svc.ensure_external_player("discord", str(5 * 10**17 + i), f"玩家{i:03d}")
    svc.create_farm(pid, home_group_id=groups[i % GROUPS])
    pids.append(pid)
print(f"建立 {PLAYERS} 位玩家與農場：{time.perf_counter() - t0:.1f} 秒")

t0 = time.perf_counter()
with engine.begin() as c:
    c.execute(text("UPDATE farms SET gold = 1000000, warehouse_capacity = 100000"))
    c.execute(text(f"""
        INSERT INTO game_transactions (farm_id, event_type, item_code, quantity, gold_delta, description, created_at)
        SELECT 1 + (g % {PLAYERS}),
               (ARRAY['sell','buy_seed','plant','water','harvest'])[1 + (g % 5)],
               (ARRAY['wheat','carrot','tomato','strawberry','egg'])[1 + (g % 7 % 5)],
               1 + (g % 5),
               CASE WHEN g % 5 = 0 THEN 30 WHEN g % 5 = 1 THEN -20 ELSE 0 END,
               '壓力測試資料',
               now() - (random() * interval '{DAYS} days')
        FROM generate_series(1, {TX_ROWS}) AS g
    """))
print(f"寫入 {TX_ROWS:,} 筆歷史交易：{time.perf_counter() - t0:.1f} 秒")
t0 = time.perf_counter()
with factory.begin() as s:
    counts = farm_stats_repo.rebuild(s)
with engine.begin() as c:
    c.execute(text("ANALYZE"))
print(f"由交易紀錄重算統計表 {counts}：{time.perf_counter() - t0:.1f} 秒")
t0 = time.perf_counter()
with factory() as s:
    check = farm_stats_repo.check(s)
print(f"統計一致性檢查：farm_stats 不一致 {check['farm_stats_mismatches']}、"
      f"farm_daily_stats 不一致 {check['farm_daily_stats_mismatches']}（{time.perf_counter() - t0:.1f} 秒）")

# ----------------------------------------------------------------------
print("\n--- 單一玩家操作（一筆指令，毫秒） ---")
p = pids[0]
svc.buy_seeds(p, "wheat", 40)
timed("購買種子 buy", lambda: svc.buy_seeds(p, "wheat", 1))
plots = iter(range(1, 5))
timed("種植 plant（含活動、品質判定）", lambda: svc.plant_crop(p, next(plots), "wheat"), n=4)
plots = iter(range(1, 5))
timed("澆水 water", lambda: svc.water_crop(p, next(plots)), n=4)
timed("查看農場 farm status", lambda: svc.get_farm_status(p))
timed("查看背包 backpack", lambda: svc.get_backpack(p))
timed("Discord 情境對應 external_context", lambda: svc.external_context("discord", str(5 * 10**17), str(10**17), "伺服器00"))

print("\n--- 排行榜（改善後：統計表） ---")
lb = {}
lb["個人 金幣 全部"] = timed("個人總排行 金幣（全部）", lambda: svc.leaderboard("money"))
lb["個人 農產品 全部"] = timed("個人總排行 農產品販售（全部）", lambda: svc.leaderboard("sales"))
lb["個人 商品 全部"] = timed("個人總排行 商品總販售（全部）", lambda: svc.leaderboard("goods"))
lb["群組內 農產品 全部"] = timed("群組內排行 農產品販售（全部）", lambda: svc.leaderboard("sales", group_id=groups[0], player_id=pids[0]))
lb["群組總 農產品 全部"] = timed("群組總排行 農產品販售（全部）", lambda: svc.group_leaderboard("sales"))
lb["群組總 金幣 本週"] = timed("群組總排行 金幣（本週）", lambda: svc.group_leaderboard("money", "week"))
lb["群組總 商品 本月"] = timed("群組總排行 商品總販售（本月）", lambda: svc.group_leaderboard("goods", "month"))
lb["個人 金幣 本月"] = timed("個人排行 淨賺金幣（本月）", lambda: svc.period_leaderboard("money", "month"))

print("\n--- 排行榜（改善前：每次加總交易紀錄，對照用） ---")
fast_stats = leaderboard_repo._stats
leaderboard_repo._stats = leaderboard_repo._tx_stats
timed("[舊] 個人總排行 農產品販售（全部）", lambda: svc.leaderboard("sales"), n=3)
timed("[舊] 群組總排行 農產品販售（全部）", lambda: svc.group_leaderboard("sales"), n=3)
timed("[舊] 群組總排行 商品總販售（本月）", lambda: svc.group_leaderboard("goods", "month"), n=3)
leaderboard_repo._stats = fast_stats

print("\n--- 監控網站 ---")
timed("WebSocket 快照（每 3 秒／每個開著的網頁）", mon.snapshot, n=10)
timed("圖表（金幣趨勢與作物分布）", mon.charts, n=10)
timed("排行榜 API（群組總排行 本月）", lambda: mon.leaderboard("goods", None, 100, scope="groups", period="month"), n=10)

# ----------------------------------------------------------------------
print("\n--- 並行：100 人同時提交一般指令（每人 5 個：買、看農場、種、澆水、看背包） ---")


def player_ops(pid):
    return [
        lambda: svc.buy_seeds(pid, "carrot", 1),
        lambda: svc.get_farm_status(pid),
        lambda: svc.plant_crop(pid, 1, "carrot"),
        lambda: svc.water_crop(pid, 1),
        lambda: svc.get_backpack(pid),
    ]


def interleave(groups_of_ops):
    out = []
    for i in range(max(len(g) for g in groups_of_ops)):
        out += [g[i] for g in groups_of_ops if i < len(g)]
    return out


gold0, ledger0 = gold_and_ledger()
run_ops("一般指令", interleave([player_ops(pid) for pid in pids[100:200]]), workers=8)
run_ops("一般指令", interleave([player_ops(pid) for pid in pids[200:300]]), workers=32)
print("\n--- 500 個操作集中在短時間內（500 位玩家各買 1 次） ---")
run_ops("尖峰", [(lambda pid=pid: svc.buy_seeds(pid, "wheat", 1)) for pid in pids], workers=32)
print("\n--- 多人同時查看排行榜、背包與農場 ---")
reads = []
for i, pid in enumerate(pids[:200]):
    reads.append([lambda: svc.leaderboard("goods"), lambda pid=pid: svc.get_backpack(pid),
                  lambda pid=pid: svc.get_farm_status(pid), lambda: svc.group_leaderboard("money", "week")][i % 4])
run_ops("讀取", reads, workers=32)

# ----------------------------------------------------------------------
print("\n--- 讀寫混合競爭：同群組偷菜＋收成＋買賣＋管理員改金幣，同時網站快照與背景結算 ---")
g0 = [pid for i, pid in enumerate(pids) if i % GROUPS == 0]  # 同一群組 25 人
victims, thieves = g0[:5], g0[5:]
for v in victims:
    svc.buy_seeds(v, "strawberry", 4)
    for plot in range(1, 5):
        try:
            svc.clear_plot_for_player("loadtest", v, plot)  # 清掉前面情境留下的作物
        except GameError:
            pass
        svc.plant_crop(v, plot, "strawberry")
with engine.begin() as c:  # 讓作物立即成熟（只調整時間欄位）
    c.execute(text(
        "UPDATE farm_plots SET planted_at = now() - interval '10 seconds', watered_at = now() - interval '5 seconds', "
        "ready_at = now() - interval '1 second' WHERE farm_id IN (SELECT id FROM farms WHERE player_id = ANY(:v))"
    ), {"v": victims})
gold1, ledger1 = gold_and_ledger()
with engine.connect() as c:
    phase_start = c.execute(text("SELECT now()")).scalar()
thief_svc = svc.in_group_context(groups[0])
mixed = []
for plot in range(1, 5):
    for v in victims:
        mixed += [(lambda t=t, v=v, plot=plot: thief_svc.steal_crop(groups[0], t, v, plot, 1)) for t in thieves[:12]]
        mixed.append(lambda v=v, plot=plot: thief_svc.harvest_crop(v, plot))
for t in thieves:
    mixed += [lambda t=t: svc.buy_seeds(t, "wheat", 1), lambda t=t: svc.sell(t, "strawberry", 1),
              lambda t=t: svc.set_farm_values("loadtest", t, gold=2_000_000)]
stop = threading.Event()
bg = Counter()


def web_and_background():
    while not stop.is_set():
        mon.snapshot()
        bg["網站快照"] += 1
        if svc.run_background_settlement("loadtest") is not None:
            bg["背景結算"] += 1


bg_thread = threading.Thread(target=web_and_background, daemon=True)
bg_thread.start()
outcomes = run_ops("混合", mixed, workers=32)
stop.set()
bg_thread.join()
for v in victims:  # 收完剩下的作物，才能核對數量
    for plot in range(1, 5):
        try:
            svc.harvest_crop(v, plot)
        except GameError:
            pass
print(f"    同時執行：{dict(bg)}")

gold2, ledger2 = gold_and_ledger()
with engine.connect() as c:
    dup_steals = c.execute(text(
        "SELECT count(*) FROM (SELECT 1 FROM steal_records GROUP BY plot_id, planting_no, thief_player_id "
        "HAVING count(*) > 1) d")).scalar()
    stolen = c.execute(text("SELECT coalesce(sum(quantity), 0) FROM steal_records WHERE outcome = 'success'")).scalar()
    farm_ids = {pid: fid for pid, fid in c.execute(text("SELECT player_id, id FROM farms"))}
    victim_straw = c.execute(text(
        "SELECT coalesce(sum(quantity), 0) FROM inventory_items WHERE item_code = 'strawberry' AND farm_id = ANY(:f)"
    ), {"f": [farm_ids[v] for v in victims]}).scalar()
    sold_by_thieves = c.execute(text(
        "SELECT coalesce(sum(quantity), 0) FROM game_transactions WHERE event_type = 'sell' AND item_code = 'strawberry' "
        "AND farm_id = ANY(:f) AND created_at >= :t"), {"f": [farm_ids[t] for t in thieves], "t": phase_start}).scalar()
    thief_straw = c.execute(text(
        "SELECT coalesce(sum(quantity), 0) FROM inventory_items WHERE item_code = 'strawberry' AND farm_id = ANY(:f)"
    ), {"f": [farm_ids[t] for t in thieves]}).scalar()
    negative = c.execute(text("SELECT count(*) FROM inventory_items WHERE quantity < 0")).scalar()
    dup_awards = c.execute(text(
        "SELECT count(*) FROM (SELECT 1 FROM ranking_awards GROUP BY settlement_id, player_id HAVING count(*) > 1) d"
    )).scalar()
planted = len(victims) * 4 * 5
with factory() as s:
    stats_check = farm_stats_repo.check(s)
print("    一致性檢查：")
print(f"      金幣變化 = 交易紀錄變化：{gold2 - gold0 == ledger2 - ledger0}"
      f"（金幣 {gold2 - gold0:+}，交易 {ledger2 - ledger0:+}；重複扣款 {0 if gold2 - gold0 == ledger2 - ledger0 else '有'}）")
print(f"      草莓守恆（受害者收成 {victim_straw} + 被偷 {stolen} = 種植 {planted}）：{victim_straw + stolen == planted}")
print(f"      偷到的草莓 = 小偷背包 {thief_straw} + 已賣出 {sold_by_thieves}：{stolen == thief_straw + sold_by_thieves}")
print(f"      同一株重複偷取 {dup_steals}、負數庫存 {negative}、重複發獎 {dup_awards}")
print(f"      統計表不一致：{stats_check['farm_stats_mismatches']} / {stats_check['farm_daily_stats_mismatches']}")
assert gold1 - gold0 == ledger1 - ledger0

# ----------------------------------------------------------------------
print("\n--- EXPLAIN ANALYZE（執行時間） ---")
since, until = svc.view_bounds("month", svc._clock.now())
with factory() as s:
    def explain(label, stmt_fn):
        stmt = stmt_fn()
        compiled = stmt.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
        plan = [r[0] for r in s.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {compiled}"))]
        exec_line = next(line for line in plan if line.startswith("Execution Time"))
        index_lines = sorted({m.group(1) for line in plan for m in [re.search(r" using (\S+)", line)] if m})
        print(f"{label:<36} {exec_line}；使用索引：{', '.join(index_lines) or '（循序掃描）'}")
    explain("farm_stats 依作物販售排序 前 10", lambda: select(FarmStats).order_by(
        FarmStats.crop_sold_quantity.desc(), FarmStats.crop_sales_income.desc(), FarmStats.player_id).limit(10))
    explain("farm_daily_stats 本月加總", lambda: farm_stats_repo.period_rows(since, until).element)
    explain("[舊] 交易紀錄 全部販售加總", lambda: leaderboard_repo._tx_stats("goods", None, None).element)
    explain("[舊] 交易紀錄 本月加總", lambda: leaderboard_repo._tx_stats("goods", since, until).element)
    explain("單一農場最近 10 筆交易", lambda: select(GameTransaction).where(GameTransaction.farm_id == 1)
            .order_by(GameTransaction.created_at.desc()).limit(10))

with engine.connect() as c:
    sizes = c.execute(text(
        "SELECT pg_size_pretty(pg_total_relation_size('game_transactions')), "
        "pg_size_pretty(pg_total_relation_size('farm_stats')), pg_size_pretty(pg_total_relation_size('farm_daily_stats')), "
        "(SELECT count(*) FROM farm_daily_stats)"
    )).one()
print(f"\n資料表大小：game_transactions {sizes[0]}、farm_stats {sizes[1]}、farm_daily_stats {sizes[2]}（{sizes[3]:,} 列）")
engine.dispose()
probe.dispose()
