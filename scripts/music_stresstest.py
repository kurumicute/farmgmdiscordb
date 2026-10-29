# ruff: noqa: E501  （報表輸出字串較長）
"""音樂壓力測試（Lavalink 版；不需要 Discord Token，也不會連到語音伺服器）。

1. Bot 端：N 個伺服器同時播放並不斷切歌。播放器是假的 Lavalink 播放器，但走的是正式的
   play_next → Lavalink TrackEnd 事件 → on_song_end 流程（含「正在播放」Embed 與按鈕），
   量測事件迴圈延遲、每次切歌的處理時間、Bot 程序 CPU 與記憶體。音訊解碼與編碼在 Lavalink，不在 Bot 程序。
2. Lavalink 解析：實際對 Lavalink（docker compose 的 lavalink 服務）同時送出點歌解析請求
   （YouTube 搜尋／網址／播放清單），量測延遲、成功率與 Lavalink 容器的 CPU／記憶體。
   會真的查詢 YouTube，因此總請求數刻意壓低，避免這台電腦的 IP 被要求驗證或限流。

不包含：實際串流到 Discord 語音的音訊負載（Lavalink 沒有語音連線時不會持續解碼），需要真實 Discord 才能量測。

用法：.venv/Scripts/python.exe scripts/music_stresstest.py [--guilds 100,500,1000] [--resolve 5,15,30] [--no-network]
"""

import argparse
import asyncio
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import aiohttp  # noqa: E402
import psutil  # noqa: E402

from app.music import player as music  # noqa: E402

LAVALINK_CONTAINER = "farmgm-lavalink-1"


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else 0.0


# ---------- 1. Bot 端：假的 Lavalink 播放器，事件流程與正式相同 ----------
class FakeTrack:
    def __init__(self, key):
        self.encoded = f"enc-{key}"
        self.title = f"壓測歌曲 {key}"
        self.uri = f"https://www.youtube.com/watch?v={key}"
        self.is_stream = False
        self.length = 200_000
        self.artwork = None
        self.author = "壓測歌手"
        self.source = "youtube"


class FakeMessage:
    def __init__(self, embed):
        self.embeds = [embed] if embed else []

    async def edit(self, **_):
        await asyncio.sleep(0)


class FakeChannel:
    def __init__(self, counter):
        self.counter = counter

    async def send(self, embed=None, **_):
        self.counter["messages"] += 1
        await asyncio.sleep(0)  # 實際送出由 Discord HTTP 處理，這裡只量測 Bot 端產生訊息的成本
        return FakeMessage(embed)


class FakePlayer:
    """模擬 wavelink.Player：play 之後經過 song_seconds 由「Lavalink」送出 TrackEnd（finished）。"""

    def __init__(self, guild, song_seconds, stats):
        self.guild = guild
        self.connected = True
        self.current = None
        self.paused = False
        self.volume = 100
        self.channel = SimpleNamespace(members=[], mention="#voice")
        self.song_seconds = song_seconds
        self.stats = stats
        self._handle = None

    async def play(self, track, *, volume=None, **_):
        self.current, self.paused = track, False
        self._started = time.perf_counter()
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(self.song_seconds, self._end, "finished")
        return track

    def _end(self, reason):
        track, self.current = self.current, None
        if track is None:
            return
        ended = time.perf_counter()
        payload = SimpleNamespace(player=self, track=track, reason=reason)

        async def dispatch():
            await music.on_wavelink_track_end(payload)
            # 從 TrackEnd 到下一首開始播放（或清單結束）的處理時間
            self.stats["switch_ms"].append((time.perf_counter() - ended) * 1000)

        asyncio.get_running_loop().create_task(dispatch())

    async def pause(self, value):
        self.paused = value

    async def set_volume(self, value=100):
        self.volume = value

    async def stop(self, *, force=True):
        if self._handle:
            self._handle.cancel()
        self._end("stopped")

    skip = stop

    async def disconnect(self, **_):
        self.connected = False


async def bot_side(guild_count, songs_each, song_seconds):
    music.configure(music.MusicConfig(settings_file=str(Path(tempfile.gettempdir()) / "farmgm_music_stress.json"),
                                      idle_timeout=3600))
    music.states.clear()
    stats = {"switch_ms": [], "messages": 0}
    guilds = {}
    for gid in range(1, guild_count + 1):
        guild = SimpleNamespace(id=gid, name=f"壓測伺服器{gid}", icon=None)
        guild.voice_client = FakePlayer(guild, song_seconds, stats)
        guilds[gid] = guild
    music.bot = SimpleNamespace(loop=asyncio.get_running_loop(), get_guild=guilds.get, user=SimpleNamespace(id=1))
    for gid in guilds:
        st = music.get_state(gid)
        st.text_channel = FakeChannel(stats)
        st.queue.extend(music.song_from_track(FakeTrack(f"{gid}-{i}"), None) for i in range(songs_each))

    proc = psutil.Process()
    proc.cpu_percent(None)
    mem0 = proc.memory_info().rss / 2**20
    lags, stop = [], asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            t = time.perf_counter()
            await asyncio.sleep(0.05)
            lags.append((time.perf_counter() - t - 0.05) * 1000)

    hb = asyncio.create_task(heartbeat())
    t0 = time.perf_counter()
    await asyncio.gather(*(music.play_next(g) for g in guilds.values()))
    while any(music.get_state(g).queue or music.get_state(g).current for g in guilds):
        await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - t0
    stop.set()
    await hb
    cpu = proc.cpu_percent(None)
    mem = proc.memory_info().rss / 2**20
    for g in guilds:
        music.get_state(g).cancel_tasks()
    plays = guild_count * songs_each
    print(f"[{guild_count:>5} 個伺服器同時播放] 共 {plays:,} 首（每首 {song_seconds} 秒就結束，切歌密度遠高於真實）"
          f" 耗時 {elapsed:5.1f} 秒")
    print(f"    切歌處理 P50 {pct(stats['switch_ms'], .5):.2f} ms、P95 {pct(stats['switch_ms'], .95):.2f} ms、"
          f"最大 {max(stats['switch_ms'] or [0]):.1f} ms｜事件迴圈延遲 P95 {pct(lags, .95):.1f} ms、最大 {max(lags or [0]):.1f} ms")
    print(f"    Bot 程序 CPU {cpu:.0f}%（單核 100%）、記憶體 {mem:,.0f} MB（增加 {mem - mem0:+.0f} MB）、"
          f"送出訊息 {stats['messages']:,} 則")
    music.states.clear()


# ---------- 2. Lavalink 解析（真的會查詢 YouTube） ----------
SEARCH_TERMS = [
    "周杰倫 晴天", "五月天 倔強", "告五人 愛人錯過", "Taylor Swift Love Story", "Ed Sheeran Shape of You",
    "YOASOBI アイドル", "米津玄師 Lemon", "鄧紫棋 光年之外", "林俊傑 江南", "BTS Dynamite",
    "Coldplay Viva La Vida", "Imagine Dragons Believer", "陳奕迅 十年", "田馥甄 小幸運", "蔡依林 日不落",
    "Adele Hello", "Bruno Mars Uptown Funk", "Queen Bohemian Rhapsody", "LiSA 紅蓮華", "Aimer 残響散歌",
]
YOUTUBE_URLS = [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=kJQP7kiw5Fk",
    "https://www.youtube.com/watch?v=JGwWNGJdvx8", "https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
]
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


def build_queries(n, offset):
    """混合：70% YouTube 關鍵字、20% YouTube 網址、10% 播放清單（B 站已停止支援）。"""
    out = []
    for i in range(n):
        k = offset + i
        r = k % 10
        if r < 7:
            out.append(("YouTube 搜尋", f"ytsearch:{SEARCH_TERMS[k % len(SEARCH_TERMS)]}"))
        elif r < 9:
            out.append(("YouTube 網址", YOUTUBE_URLS[k % len(YOUTUBE_URLS)]))
        else:
            out.append(("播放清單", PLAYLIST_URL))
    return out


class DockerSampler:
    """背景取樣 Lavalink 容器的 CPU 與記憶體（docker stats）。"""

    def __init__(self):
        self.samples, self._stop = [], threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", LAVALINK_CONTAINER],
                    capture_output=True, text=True, timeout=10).stdout.strip()
                cpu, mem = out.split("|")
                used = mem.split("/")[0].strip()
                mib = float(used[:-3]) * (1024 if used.endswith("GiB") else 1) if used[-3:] in ("MiB", "GiB") else 0
                self.samples.append((float(cpu.rstrip("%")), mib))
            except Exception:
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=15)

    def report(self):
        if not self.samples:
            return "（無法取得 docker stats）"
        return (f"Lavalink 容器 CPU 最高 {max(c for c, _ in self.samples):.0f}%（單核 100%）、"
                f"記憶體最高 {max(m for _, m in self.samples):,.0f} MiB")


async def resolve_round(session, base, password, concurrency, offset):
    queries = build_queries(concurrency, offset)
    results = []

    async def one(kind, q):
        t = time.perf_counter()
        try:
            async with session.get(f"{base}/v4/loadtracks?identifier={urllib.parse.quote(q)}",
                                   headers={"Authorization": password}, timeout=aiohttp.ClientTimeout(total=60)) as r:
                data = await r.json()
            load_type = data.get("loadType")
            ok = load_type in ("track", "search", "playlist") and bool(data.get("data"))
        except Exception as exc:
            load_type, ok = type(exc).__name__, False
        results.append((kind, ok, (time.perf_counter() - t) * 1000, load_type))

    with DockerSampler() as sampler:
        t0 = time.perf_counter()
        await asyncio.gather(*(one(k, q) for k, q in queries))
        elapsed = time.perf_counter() - t0
    lat = [ms for _, _, ms, _ in results]
    print(f"[同時 {concurrency:>3} 個點歌解析] 總耗時 {elapsed:5.1f} 秒｜延遲 P50 {pct(lat, .5):,.0f} ms、"
          f"P95 {pct(lat, .95):,.0f} ms、最大 {max(lat):,.0f} ms｜{sampler.report()}")
    for kind in ("YouTube 搜尋", "YouTube 網址", "播放清單"):
        rows = [r for r in results if r[0] == kind]
        if rows:
            ok = sum(1 for r in rows if r[1])
            fails = sorted({r[3] for r in rows if not r[1]})
            print(f"    {kind:<8} 成功 {ok}/{len(rows)}" + (f"（失敗類型：{', '.join(map(str, fails))}）" if fails else ""))
    return len(queries)


async def lavalink_side(rounds):
    from app.config import get_settings

    settings = get_settings()
    password = settings.lavalink_password.get_secret_value() if settings.lavalink_password else ""
    base = settings.lavalink_uri.rstrip("/")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{base}/v4/info", headers={"Authorization": password}) as r:
                info = await r.json()
        except Exception as exc:
            print(f"無法連線 Lavalink（{base}）：{type(exc).__name__}；請先 docker compose up -d lavalink")
            return
        plugins = ", ".join(f"{p['name']} {p['version']}" for p in info.get("plugins", []))
        print(f"Lavalink {info['version']['semver']}，來源：{', '.join(info['sourceManagers'])}；插件：{plugins}")
        with DockerSampler() as idle:
            await asyncio.sleep(3)
        print(f"待機：{idle.report()}")
        offset = total = 0
        for n in rounds:
            total += await resolve_round(session, base, password, n, offset)
            offset += n
            await asyncio.sleep(2)
        print(f"（本次共向 Lavalink 送出 {total} 個解析請求）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--guilds", default="100,500,1000", help="Bot 端同時播放的伺服器數（逗號分隔）")
    ap.add_argument("--songs", type=int, default=5, help="每個伺服器的歌曲數")
    ap.add_argument("--song-seconds", type=float, default=0.2, help="每首歌幾秒後結束（模擬密集切歌）")
    ap.add_argument("--resolve", default="5,15,30", help="Lavalink 同時解析請求數（逗號分隔；總數不宜過大）")
    ap.add_argument("--no-network", action="store_true", help="略過 Lavalink 實際解析測試")
    args = ap.parse_args()

    print("\n--- 1. Bot 端：多伺服器同時播放與切歌（假的 Lavalink 播放器，事件流程與正式相同）---")
    for n in [int(x) for x in args.guilds.split(",") if x]:
        asyncio.run(bot_side(n, args.songs, args.song_seconds))
    if args.no_network:
        return
    print("\n--- 2. Lavalink 實際解析（YouTube）---")
    asyncio.run(lavalink_side([int(x) for x in args.resolve.split(",") if x]))


if __name__ == "__main__":
    main()
