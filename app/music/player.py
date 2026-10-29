"""音樂播放（YouTube／Spotify）。由原 YTDiscordBot 移植，與農場共用同一個 Discord Bot。

- 音訊由 Lavalink 伺服器（docker compose 的 lavalink 服務）解析與播放，Bot 透過 wavelink 控制；Bot 端不需要 ffmpeg 或 yt-dlp。
  YouTube 使用 youtube-plugin（設定在 docker/lavalink/application.yml）；B 站已停止支援（風控導致多數影片無法播放）。
- 佇列、循環、跳過、按鈕等播放邏輯沿用原專案；設定由 .env（MUSIC_* 與 LAVALINK_*）提供。
- 指令以 Hybrid Command 註冊：斜線指令一定可用；前綴指令（例如 !play）需要 MUSIC_PREFIX_COMMANDS=true
  並在 Developer Portal 開啟 Message Content Intent。
- 每個伺服器的播放狀態在記憶體中；音量與前綴存在 data/music_guild_settings.json。
"""

import asyncio
import json
import logging
import os
import random
import re
from collections import deque
from dataclasses import dataclass
from typing import Literal, Optional

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from app.music import spotify

logger = logging.getLogger(__name__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class MusicConfig:
    volume: float = 0.1
    playlist_limit: int = 20
    prefix: str = "!"
    idle_timeout: int = 300
    settings_file: str = "data/music_guild_settings.json"
    lavalink_uri: str = "http://127.0.0.1:2333"
    lavalink_password: str = ""


# ───────────────────────── 設定（由 configure() 設定） ─────────────────────────
bot: commands.Bot | None = None
default_volume = 0.1
playlist = 20
default_prefix = "!"
idle_timeout = 300
lavalink_uri = "http://127.0.0.1:2333"
lavalink_password = ""
SETTINGS_FILE = os.path.join(PROJECT_ROOT, "data", "music_guild_settings.json")
guild_settings: dict = {}


def resolve_path(path):
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def configure(cfg: MusicConfig) -> None:
    """套用設定並讀取各伺服器的音量／前綴。"""
    global default_volume, playlist, default_prefix, idle_timeout, SETTINGS_FILE, guild_settings
    global lavalink_uri, lavalink_password
    default_volume = cfg.volume
    playlist = cfg.playlist_limit
    default_prefix = cfg.prefix
    idle_timeout = cfg.idle_timeout
    lavalink_uri = cfg.lavalink_uri
    lavalink_password = cfg.lavalink_password
    SETTINGS_FILE = resolve_path(cfg.settings_file)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            guild_settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        guild_settings = {}


def get_setting(guild_id, key, default):
    return guild_settings.get(str(guild_id), {}).get(key, default)


def set_setting(guild_id, key, value):
    guild_settings.setdefault(str(guild_id), {})[key] = value
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(guild_settings, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


async def get_prefix(bot, message):
    prefix = get_setting(message.guild.id, "prefix", default_prefix) if message.guild else default_prefix
    return commands.when_mentioned_or(prefix)(bot, message)


# ───────────────────────── Lavalink 連線 ─────────────────────────
NODE_ID = "farmgm"


async def connect_lavalink(client: commands.Bot) -> None:
    """連線到 Lavalink（在 setup_hook 呼叫）。連不上時只記錄警告，Bot 照常啟動，wavelink 會在背景重試。"""
    if not lavalink_password:
        logger.warning("未設定 LAVALINK_PASSWORD，音樂功能無法使用")
        return
    node = wavelink.Node(
        identifier=NODE_ID,
        uri=lavalink_uri,
        password=lavalink_password,
        client=client,
        # 閒置斷線由本模組處理（MUSIC_IDLE_TIMEOUT），不使用 wavelink 內建的自動離開
        inactive_player_timeout=None,
        inactive_channel_tokens=None,
    )
    try:
        await wavelink.Pool.connect(nodes=[node], client=client, cache_capacity=200)
    except Exception as exc:  # 連線資訊錯誤等；不讓整個 Bot 啟動失敗
        logger.warning("連線 Lavalink 失敗（%s）：%s", lavalink_uri, type(exc).__name__)
    global _lavalink_watchdog_task
    if _lavalink_watchdog_task is None or _lavalink_watchdog_task.done():
        _lavalink_watchdog_task = asyncio.create_task(_lavalink_watchdog(client))


# wavelink 3.5.2 的 keep_alive 只處理 CLOSED／CLOSING：aiohttp 心跳逾時送出的 WSMsgType.ERROR 會讓它中止且不再重連
# （例如 Lavalink 容器重建，舊連線收不到 FIN）。因此定期用 REST 確認 Session 仍有效，失效時重新連線。
LAVALINK_CHECK_SECONDS = 30
_lavalink_watchdog_task: asyncio.Task | None = None


def _lavalink_node():
    return wavelink.Pool.nodes.get(NODE_ID)


async def ensure_lavalink_session(client: commands.Bot) -> bool:
    """wavelink 認為已連線、但 Lavalink 回報 Session 不存在（404）時重新連線；回傳是否重新連線。"""
    node = _lavalink_node()
    if node is None or node.status is not wavelink.NodeStatus.CONNECTED:
        return False  # 尚未連線或 wavelink 正在自行重試
    try:
        await node.fetch_players()
        return False
    except wavelink.LavalinkException as exc:
        if exc.status != 404:
            return False
    except Exception:  # Lavalink 暫時連不上：下一輪再檢查（恢復後舊 Session 會回 404）
        return False
    logger.warning("Lavalink Session 已失效（Lavalink 可能已重啟），重新連線")
    await node.close(eject=True)
    await connect_lavalink(client)
    return True


async def _lavalink_watchdog(client: commands.Bot) -> None:
    while not client.is_closed():
        await asyncio.sleep(LAVALINK_CHECK_SECONDS)
        try:
            await ensure_lavalink_session(client)
        except Exception:
            logger.exception("檢查 Lavalink 連線時發生錯誤")


class MusicUnavailable(Exception):
    """Lavalink 無法使用（未啟動或連線中斷）。"""


# ───────────────────────── 音源解析（Lavalink） ─────────────────────────
COLOR_YOUTUBE = 0xFF0033
COLOR_SPOTIFY = 0x1DB954
COLOR_INFO = 0x5865F2
COLOR_ERROR = 0xED4245
LOOP_LABELS = {"off": "關閉", "song": "單曲循環", "queue": "清單循環"}
SOURCES = {
    # 來源: (顯示名稱, 圖示, 顏色)
    "youtube": ("YouTube", "🔴", COLOR_YOUTUBE),
    "spotify": ("Spotify", "🟢", COLOR_SPOTIFY),
}
# B 站已停止支援：貼上 B 站連結時直接提示，不送到 Lavalink（已移除 lavabili-plugin）
BILIBILI_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:bilibili\.com|b23\.tv|bili2233\.cn)/", re.I)


def is_bilibili(url):
    return bool(url and BILIBILI_RE.match(url))


@dataclass(eq=False)
class Song:
    title: str
    url: Optional[str]            # 影片網址（Spotify 歌曲在播放前才到 YouTube 搜尋）
    requester: Optional[discord.abc.User] = None
    duration: Optional[int] = None
    thumbnail: Optional[str] = None
    author: Optional[str] = None
    source: str = "youtube"       # youtube / spotify
    page_url: Optional[str] = None  # 顯示用的連結
    search_query: Optional[str] = None
    track: Optional["wavelink.Playable"] = None  # Lavalink 解析後的音軌（Spotify 歌曲播放前才解析）

    @property
    def link(self):
        return self.page_url or self.url

    @property
    def source_name(self):
        return SOURCES[self.source][0]

    @property
    def icon(self):
        return SOURCES[self.source][1]

    @property
    def color(self):
        return SOURCES[self.source][2]


def fmt_duration(seconds):
    if seconds is None:
        return "直播/未知"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def progress_bar(elapsed, total, length=16):
    if not total:
        return "🔴 LIVE"
    filled = min(length - 1, int(length * elapsed / total))
    return "▬" * filled + "🔘" + "▬" * (length - filled - 1)


def short(text, limit=60):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape(text):
    return discord.utils.escape_markdown(text or "")


def song_from_track(track, requester):
    """Lavalink 音軌 → Song。"""
    return Song(
        title=track.title or track.uri or "未知歌曲",
        url=track.uri,
        requester=requester,
        duration=None if track.is_stream else track.length // 1000,
        thumbnail=track.artwork,
        author=track.author or None,
        track=track,
    )


async def lavalink_search(query):
    """呼叫 Lavalink 解析；回傳 wavelink.Search（list[Playable] 或 Playlist）。"""
    if not wavelink.Pool.nodes or all(n.status != wavelink.NodeStatus.CONNECTED for n in wavelink.Pool.nodes.values()):
        raise MusicUnavailable
    try:
        return await wavelink.Playable.search(query, source=None)
    except wavelink.LavalinkLoadException as exc:
        logger.warning("Lavalink 解析失敗：%s", getattr(exc, "error", exc))
        return []
    except wavelink.InvalidNodeException as exc:
        raise MusicUnavailable from exc


async def get_songs(query, requester):
    """搜尋關鍵字、單支影片或播放清單，回傳 (清單名稱或 None, [Song, ...])"""
    is_url = query.startswith("http")
    result = await lavalink_search(query if is_url else f"ytsearch:{query}")
    if isinstance(result, wavelink.Playlist):
        # YouTube 影片網址帶 list= 時 Lavalink 會回傳整個清單；沒有 list= 的網址只播單曲
        if is_url and "list=" not in query:
            pick = result.tracks[max(result.selected, 0)] if result.tracks else None
            return None, [song_from_track(pick, requester)] if pick else []
        # 從網址選中的那首開始（例如 watch?v=...&list=...），最多 MUSIC_PLAYLIST_LIMIT 首
        start = result.selected if 0 <= result.selected < len(result.tracks) else 0
        return result.name, [song_from_track(t, requester) for t in result.tracks[start:start + playlist]]
    tracks = list(result or [])
    return None, [song_from_track(t, requester) for t in tracks[:1]]


async def resolve_track(song):
    """取得可播放的音軌；Spotify 歌曲在這裡才到 YouTube 搜尋同一首歌。"""
    if song.track is not None:
        return song.track
    result = await lavalink_search(f"ytsearch:{song.search_query}")
    tracks = result.tracks if isinstance(result, wavelink.Playlist) else list(result or [])
    if not tracks:
        return None
    track = tracks[0]
    song.track = track
    song.url = track.uri
    if song.duration is None and not track.is_stream:
        song.duration = track.length // 1000
    if not song.thumbnail:
        song.thumbnail = track.artwork
    return track


async def get_spotify_songs(url, requester):
    kind, name, page_url, cover, tracks = await spotify.resolve(url, playlist)
    songs = [
        Song(
            title=f"{t['artist']} - {t['title']}" if t["artist"] else t["title"],
            url=None,
            requester=requester,
            duration=t["duration"],
            thumbnail=t["thumbnail"],
            author=t["artist"],
            source="spotify",
            page_url=t["url"],
            search_query=f"{t['title']} {t['artist']} audio",
        )
        for t in tracks
    ]
    return (name if kind != "track" else None), page_url, cover, songs


# ───────────────────── 語音連線（wavelink.Player）的狀態判斷 ─────────────────────
def vc_connected(vc):
    return bool(vc and getattr(vc, "connected", False))


def vc_paused(vc):
    return bool(vc and vc.paused)


def vc_busy(vc):
    """有音軌載入中（播放或暫停）。"""
    return bool(vc and vc.current is not None)


def vc_playing(vc):
    return vc_busy(vc) and not vc_paused(vc)


# ───────────────────── 每個伺服器的播放狀態 ─────────────────────
class GuildState:
    def __init__(self, guild_id):
        self.guild_id = guild_id
        self.queue = deque()
        self.current = None
        self.volume = get_setting(guild_id, "volume", default_volume)
        self.loop = "off"
        self.text_channel = None
        self.prefetched = None        # (Song, wavelink.Playable)：Spotify 歌曲預先到 YouTube 解析
        self.prefetch_task = None
        self.idle_task = None
        self.skip_requested = False
        self.lock = asyncio.Lock()
        self.np_message = None        # 「正在播放」訊息（附控制按鈕）
        self.started_at = 0.0
        self.paused_at = None

    def elapsed(self):
        if not self.current:
            return 0
        now = self.paused_at or asyncio.get_running_loop().time()
        return max(0, now - self.started_at)

    def cancel_tasks(self):
        for task in (self.prefetch_task, self.idle_task):
            if task and not task.done():
                task.cancel()
        self.prefetch_task = self.idle_task = None
        self.prefetched = None

    def reset(self):
        self.cancel_tasks()
        self.queue.clear()
        self.current = None
        self.loop = "off"
        self.paused_at = None


states: dict[int, GuildState] = {}


def get_state(guild_id):
    if guild_id not in states:
        states[guild_id] = GuildState(guild_id)
    return states[guild_id]


# ───────────────────────── Embed ─────────────────────────
def now_playing_embed(guild, state, paused=False):
    song = state.current
    if not song:
        return discord.Embed(description="目前沒有播放任何歌曲", color=COLOR_INFO)
    status = "⏸️ 已暫停" if paused else "🎵 正在播放"
    embed = discord.Embed(
        title=short(song.title, 200),
        url=song.link,
        color=song.color,
    )
    embed.set_author(name=status)
    elapsed = state.elapsed()
    embed.description = f"`{fmt_duration(elapsed)}` {progress_bar(elapsed, song.duration)} `{fmt_duration(song.duration)}`"
    if song.author:
        embed.add_field(name="演出者", value=escape(short(song.author, 100)))
    embed.add_field(name="來源", value=f"{song.icon} {song.source_name}")
    embed.add_field(name="點播", value=song.requester.mention if song.requester else "—")
    embed.add_field(name="音量", value=f"{state.volume * 100:.0f}%")
    embed.add_field(name="循環", value=LOOP_LABELS[state.loop])
    nxt = state.queue[0].title if state.queue else "無"
    embed.add_field(name=f"下一首（剩 {len(state.queue)} 首）", value=escape(short(nxt, 100)))
    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    embed.set_footer(text=guild.name, icon_url=guild.icon.url if guild.icon else None)
    return embed


def added_embed(songs, requester, position, playlist_name=None, playlist_url=None, cover=None):
    first = songs[0]
    if len(songs) == 1:
        embed = discord.Embed(title=short(first.title, 200), url=first.link, color=first.color)
        embed.set_author(name="✅ 已加入待播清單")
        embed.add_field(name="長度", value=fmt_duration(first.duration))
        embed.add_field(name="順位", value="即將播放" if position == 0 else f"第 {position + 1} 首")
        if first.thumbnail:
            embed.set_thumbnail(url=first.thumbnail)
    else:
        total = sum(s.duration or 0 for s in songs)
        embed = discord.Embed(title=short(playlist_name or "播放清單", 200), url=playlist_url, color=first.color)
        embed.set_author(name=f"✅ 已加入 {len(songs)} 首歌曲")
        preview = "\n".join(f"`{i + 1}.` {escape(short(s.title))}" for i, s in enumerate(songs[:5]))
        if len(songs) > 5:
            preview += f"\n…還有 {len(songs) - 5} 首"
        embed.description = preview
        embed.add_field(name="總長度", value=fmt_duration(total))
        if cover or first.thumbnail:
            embed.set_thumbnail(url=cover or first.thumbnail)
    embed.set_footer(text=f"由 {requester.display_name} 點播", icon_url=requester.display_avatar.url)
    return embed


def info_embed(text, color=COLOR_INFO):
    return discord.Embed(description=text, color=color)


def error_embed(text):
    return discord.Embed(description=f"❌ {text}", color=COLOR_ERROR)


def queue_pages(state, per_page=10):
    songs = list(state.queue)
    total = sum(s.duration or 0 for s in songs)
    header = ""
    if state.current:
        header = f"**正在播放**\n{state.current.icon} [{escape(short(state.current.title))}]({state.current.link}) `{fmt_duration(state.current.duration)}`\n\n"
    pages = []
    for i in range(0, max(len(songs), 1), per_page):
        chunk = songs[i:i + per_page]
        if chunk:
            lines = [
                f"`{i + idx + 1}.` {s.icon} {escape(short(s.title, 55))} `{fmt_duration(s.duration)}`"
                for idx, s in enumerate(chunk)
            ]
            body = "**待播清單**\n" + "\n".join(lines)
        else:
            body = "📭 待播清單是空的"
        embed = discord.Embed(title="🎶 播放清單", description=header + body, color=COLOR_INFO)
        pages.append(embed)
    for n, embed in enumerate(pages):
        embed.set_footer(
            text=f"第 {n + 1}/{len(pages)} 頁 · 共 {len(songs)} 首 · 總長 {fmt_duration(total)} · 循環：{LOOP_LABELS[state.loop]}"
        )
    return pages


# ───────────────────────── 按鈕介面 ─────────────────────────
class QueueView(discord.ui.View):
    def __init__(self, pages, author_id):
        super().__init__(timeout=120)
        self.pages = pages
        self.page = 0
        self.author_id = author_id
        self.message = None
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.page == 0
        self.next_button.disabled = self.page >= len(self.pages) - 1

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("只有下指令的人可以翻頁喔！", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction, button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction, button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.page], view=self)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class PlayerControls(discord.ui.View):
    """「正在播放」訊息下方的控制按鈕"""

    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.sync_buttons()

    def sync_buttons(self):
        state = get_state(self.guild_id)
        guild = bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        paused = vc_paused(vc)
        self.pause_button.emoji = "▶️" if paused else "⏸️"
        self.pause_button.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary
        self.loop_button.emoji = {"off": "➡️", "song": "🔂", "queue": "🔁"}[state.loop]
        self.loop_button.style = discord.ButtonStyle.secondary if state.loop == "off" else discord.ButtonStyle.primary

    async def interaction_check(self, interaction):
        vc = interaction.guild.voice_client
        voice = getattr(interaction.user, "voice", None)
        if not vc or not voice or voice.channel != vc.channel:
            await interaction.response.send_message("你需要和機器人在同一個語音頻道才能操作喔！", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction):
        state = get_state(self.guild_id)
        vc = interaction.guild.voice_client
        self.sync_buttons()
        await interaction.response.edit_message(
            embed=now_playing_embed(interaction.guild, state, paused=vc_paused(vc)), view=self
        )

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.secondary, row=0)
    async def pause_button(self, interaction, button):
        vc = interaction.guild.voice_client
        state = get_state(self.guild_id)
        if vc_paused(vc):
            await resume_playback(vc, state)
        elif vc_playing(vc):
            await pause_playback(vc, state)
        await self.refresh(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction, button):
        await skip_current(interaction.guild)
        await interaction.response.send_message(
            embed=info_embed(f"⏭️ {interaction.user.mention} 跳過了這首歌"), delete_after=10
        )

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction, button):
        await stop_playback(interaction.guild)
        await interaction.response.send_message(
            embed=info_embed(f"⏹️ {interaction.user.mention} 停止播放並清空了待播清單")
        )

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary, row=0)
    async def loop_button(self, interaction, button):
        state = get_state(self.guild_id)
        state.loop = {"off": "song", "song": "queue", "queue": "off"}[state.loop]
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=0)
    async def shuffle_button(self, interaction, button):
        state = get_state(self.guild_id)
        shuffle_queue(state)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction, button):
        await change_volume(interaction.guild, get_state(self.guild_id).volume - 0.05)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction, button):
        await change_volume(interaction.guild, get_state(self.guild_id).volume + 0.05)
        await self.refresh(interaction)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_button(self, interaction, button):
        await self.refresh(interaction)

    @discord.ui.button(emoji="📜", label="清單", style=discord.ButtonStyle.secondary, row=1)
    async def queue_button(self, interaction, button):
        pages = queue_pages(get_state(self.guild_id))
        view = QueueView(pages, interaction.user.id) if len(pages) > 1 else discord.utils.MISSING
        await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)


# ───────────────────────── 播放控制 ─────────────────────────
def lavalink_volume(volume):
    """本模組的音量（1.0 = 100%）→ Lavalink 音量（100 = 原始音量）。"""
    return int(round(volume * 100))


async def pause_playback(vc, state):
    await vc.pause(True)
    state.paused_at = bot.loop.time()


async def resume_playback(vc, state):
    if state.paused_at:
        state.started_at += bot.loop.time() - state.paused_at
    state.paused_at = None
    await vc.pause(False)


def shuffle_queue(state):
    items = list(state.queue)
    random.shuffle(items)
    state.queue = deque(items)
    state.prefetched = None


async def change_volume(guild, value):
    state = get_state(guild.id)
    state.volume = round(max(0.0, min(value, 2.0)), 2)  # 限制音量在 0.0 到 2.0 之間
    vc = guild.voice_client
    if vc_connected(vc):
        await vc.set_volume(lavalink_volume(state.volume))
    set_setting(guild.id, "volume", state.volume)


async def skip_current(guild):
    """跳過目前的歌；Lavalink 送出 TrackEnd（stopped）後由 on_song_end 播下一首。"""
    vc = guild.voice_client
    if vc_busy(vc):
        get_state(guild.id).skip_requested = True
        await vc.skip(force=True)
        return True
    return False


async def stop_playback(guild):
    state = get_state(guild.id)
    state.cancel_tasks()
    state.queue.clear()
    state.loop = "off"
    vc = guild.voice_client
    if vc_busy(vc):
        await vc.stop(force=True)  # TrackEnd 事件會觸發 play_next，清單是空的就會進入閒置


async def send(state, **kwargs):
    if not state.text_channel:
        return None
    try:
        return await state.text_channel.send(**kwargs)
    except discord.HTTPException as e:
        logger.warning("傳送訊息失敗：%s", e)
        return None


async def clear_np_message(state):
    """移除上一則「正在播放」訊息的按鈕"""
    msg, state.np_message = state.np_message, None
    if msg:
        try:
            embed = msg.embeds[0] if msg.embeds else None
            if embed:
                embed.set_author(name="✔️ 已播放")
                embed.description = None
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass


async def prefetch_next(guild_id):
    """預先解析下一首（只有 Spotify 歌曲需要到 YouTube 搜尋）。"""
    state = get_state(guild_id)
    if not state.queue:
        return
    song = state.queue[0]
    if song.track is not None or (state.prefetched and state.prefetched[0] is song):
        return
    try:
        track = await resolve_track(song)
    except MusicUnavailable:
        return
    if track and state.queue and state.queue[0] is song:
        state.prefetched = (song, track)


async def idle_disconnect(guild, delay):
    await asyncio.sleep(delay)
    state = get_state(guild.id)
    vc = guild.voice_client
    if vc and not vc_busy(vc) and not state.queue:
        await vc.disconnect()
        await clear_np_message(state)
        await send(state, embed=info_embed("💤 閒置過久，已自動離開語音頻道。"))


async def on_song_end(guild_id, song, error):
    if error:
        logger.warning("[%s] 播放時發生錯誤：%s", guild_id, error)
    state = get_state(guild_id)
    if state.current is song:
        if not state.skip_requested:
            if state.loop == "song":
                state.queue.appendleft(song)
            elif state.loop == "queue":
                state.queue.append(song)
        elif state.loop == "queue":
            state.queue.append(song)
        state.current = None
    state.skip_requested = False
    guild = bot.get_guild(guild_id)
    if guild:
        await play_next(guild)


async def play_next(guild):
    state = get_state(guild.id)
    async with state.lock:
        vc = guild.voice_client
        if not vc_connected(vc):
            logger.info("[%s] 不播下一首：語音未連線（voice_client=%s）", guild.id, type(vc).__name__)
            state.current = None
            return
        if vc_busy(vc):
            logger.info("[%s] 不播下一首：播放器仍有音軌", guild.id)
            return

        while state.queue:
            song = state.queue.popleft()
            if state.prefetched and state.prefetched[0] is song:
                track = state.prefetched[1]
                state.prefetched = None
            else:
                try:
                    track = await resolve_track(song)
                except MusicUnavailable:
                    track = None
                    await send(state, embed=error_embed("音樂伺服器（Lavalink）目前無法連線，請稍後再試。"))
                    state.queue.appendleft(song)
                    break

            if not track:
                await send(state, embed=error_embed(f"無法取得 **{escape(song.title)}** 的音訊，跳過此曲。"))
                continue

            vc = guild.voice_client
            if not vc_connected(vc):
                state.current = None
                return

            state.current = song
            state.started_at = bot.loop.time()
            state.paused_at = None
            if state.idle_task:
                state.idle_task.cancel()
                state.idle_task = None
            try:
                await vc.play(track, volume=lavalink_volume(state.volume), add_history=False)
            except Exception as e:
                state.current = None
                logger.warning("[%s] Lavalink 播放失敗：%s", guild.id, e)
                await send(state, embed=error_embed(f"播放 **{escape(song.title)}** 時發生錯誤，跳過此曲。"))
                continue

            await clear_np_message(state)
            state.np_message = await send(
                state, embed=now_playing_embed(guild, state), view=PlayerControls(guild.id)
            )
            if state.queue:
                state.prefetch_task = asyncio.create_task(prefetch_next(guild.id))
            return

        # 播放清單空了，閒置一段時間後自動斷線
        state.current = None
        await clear_np_message(state)
        if state.idle_task:
            state.idle_task.cancel()
        state.idle_task = asyncio.create_task(idle_disconnect(guild, idle_timeout))


async def ensure_voice(ctx):
    """確保機器人在使用者所在的語音頻道，回傳 wavelink.Player 或 None"""
    voice = ctx.author.voice
    vc = ctx.guild.voice_client
    if not voice or not voice.channel:
        await ctx.send(embed=error_embed("你需要先加入一個語音頻道！"))
        return None
    if vc_connected(vc):
        if vc.channel != voice.channel:
            if vc_busy(vc):
                await ctx.send(embed=error_embed(f"機器人正在 {vc.channel.mention} 播放中，請到同一個頻道點歌！"))
                return None
            await vc.move_to(voice.channel)
        return vc
    if not wavelink.Pool.nodes:
        await ctx.send(embed=error_embed("音樂伺服器（Lavalink）目前無法連線，請稍後再試。"))
        return None
    try:
        return await voice.channel.connect(cls=wavelink.Player, self_deaf=True)
    except Exception as e:
        await ctx.send(embed=error_embed(f"加入語音頻道失敗：{e}"))
        return None


def require_same_channel():
    """只有和機器人在同一個語音頻道的人可以操作"""
    async def predicate(ctx):
        vc = ctx.guild.voice_client
        if not vc:
            raise commands.CheckFailure("機器人目前不在任何語音頻道中")
        if not ctx.author.voice or ctx.author.voice.channel != vc.channel:
            raise commands.CheckFailure("你需要和機器人在同一個語音頻道才能操作")
        return True
    return commands.check(predicate)


# ───────────────────────── 事件 ─────────────────────────
async def on_guild_remove(guild):
    state = states.pop(guild.id, None)
    if state:
        state.cancel_tasks()


async def on_voice_state_update(member, before, after):
    guild = member.guild
    vc = guild.voice_client

    # 機器人被踢出 / 斷線時清除狀態
    if member.id == bot.user.id:
        if before.channel and not after.channel:
            state = states.get(guild.id)
            if state:
                state.reset()
                await clear_np_message(state)
        return

    # 頻道只剩機器人時，一段時間後自動離開
    if vc and before.channel == vc.channel and after.channel != vc.channel:
        if not any(not m.bot for m in vc.channel.members):
            state = get_state(guild.id)
            if state.idle_task:
                state.idle_task.cancel()
            state.idle_task = asyncio.create_task(leave_if_alone(guild, 60))


async def leave_if_alone(guild, delay):
    await asyncio.sleep(delay)
    vc = guild.voice_client
    if vc and not any(not m.bot for m in vc.channel.members):
        state = get_state(guild.id)
        state.reset()
        await vc.disconnect()
        await clear_np_message(state)
        await send(state, embed=info_embed("👋 語音頻道沒有人了，已自動離開。"))


# ───────────────────────── Lavalink 事件 ─────────────────────────
def _current_matches(state, track):
    """事件的音軌是否為目前這首；Lavalink 事件帶回的 encoded 可能和送出的不同（例如 YouTube 清單裡的歌），
    所以和 wavelink 的 Playable.__eq__ 一樣，encoded 不同時再比對來源的 identifier。"""
    current = state.current
    if not (current and current.track is not None and track is not None):
        return False
    if current.track.encoded == track.encoded:
        return True
    identifier = getattr(current.track, "identifier", None)
    return bool(identifier) and identifier == getattr(track, "identifier", None)


async def on_wavelink_track_end(payload):
    """歌曲結束（播完、跳過、停止或載入失敗）→ 依循環設定播下一首；換歌（replaced）不處理。"""
    player = payload.player
    if player is None or player.guild is None or payload.reason == "replaced":
        return
    state = get_state(player.guild.id)
    matched = _current_matches(state, payload.track)
    logger.info("[%s] 歌曲結束：reason=%s，是目前歌曲=%s，待播 %d 首",
                player.guild.id, payload.reason, matched, len(state.queue))
    if not matched:
        current = state.current.track if state.current else None
        logger.info("[%s] 忽略結束事件：事件 identifier=%s，目前 identifier=%s", player.guild.id,
                    getattr(payload.track, "identifier", None), getattr(current, "identifier", None))
        return
    error = "load failed" if payload.reason == "loadFailed" else None
    await on_song_end(player.guild.id, state.current, error)


async def on_wavelink_track_exception(payload):
    player = payload.player
    if player is None or player.guild is None:
        return
    state = get_state(player.guild.id)
    message = (payload.exception or {}).get("message") or "未知錯誤"
    logger.warning("[%s] Lavalink 播放例外：%s", player.guild.id, message)
    if _current_matches(state, payload.track):
        await send(state, embed=error_embed(f"播放 **{escape(state.current.title)}** 時發生錯誤，跳過此曲。"))


async def on_wavelink_track_stuck(payload):
    """音軌卡住（來源網路中斷等）→ 直接跳到下一首。"""
    player = payload.player
    if player is None or player.guild is None:
        return
    logger.warning("[%s] Lavalink 音軌卡住 %s ms，跳過", player.guild.id, payload.threshold)
    await skip_current(player.guild)


async def on_wavelink_websocket_closed(payload):
    # Discord 語音連線被關閉（例如 4014 被踢出頻道、4017 不支援 DAVE 加密）；只記錄代碼方便排查
    logger.warning("Discord 語音連線關閉：code=%s reason=%s by_remote=%s",
                   payload.code, payload.reason, payload.by_remote)


async def on_wavelink_node_ready(payload):
    logger.info("已連線 Lavalink：%s（session %s，resumed=%s）",
                payload.node.uri, payload.session_id, payload.resumed)


async def on_command_error(ctx, error):
    while getattr(error, "original", None) is not None:  # 拆開 Hybrid / Invoke 包裝的錯誤
        error = error.original
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        msg = f"缺少參數：`{error.param.name}`"
    elif isinstance(error, (commands.BadArgument, commands.BadLiteralArgument)):
        msg = "參數格式錯誤"
    elif isinstance(error, commands.NoPrivateMessage):
        msg = "這個指令只能在伺服器中使用"
    elif isinstance(error, commands.MissingPermissions):
        msg = "你沒有權限使用這個指令（需要「管理伺服器」權限）"
    elif isinstance(error, commands.CheckFailure):
        msg = str(error) or "你無法使用這個指令"
    else:
        logger.warning("指令 %s 發生錯誤：%r", ctx.command, error)
        msg = f"發生錯誤：{error}"
    try:
        await ctx.send(embed=error_embed(msg), ephemeral=True)
    except discord.HTTPException:
        pass


# ───────────────────────── 指令 ─────────────────────────
@commands.hybrid_command(name="join", description="讓機器人加入你所在的語音頻道")
@commands.guild_only()
async def join(ctx):
    await ctx.defer()
    vc = await ensure_voice(ctx)
    if not vc:
        return
    get_state(ctx.guild.id).text_channel = ctx.channel
    await ctx.send(embed=info_embed(f"🎶 已加入語音頻道：{vc.channel.mention}"))


async def enqueue(ctx, query):
    await ctx.defer()
    vc = await ensure_voice(ctx)
    if not vc:
        return
    state = get_state(ctx.guild.id)
    state.text_channel = ctx.channel
    query = query.strip().strip("<>")
    if is_bilibili(query):
        await ctx.send(embed=error_embed("目前不支援 B 站，請改用 YouTube 或 Spotify 的關鍵字、連結。"))
        return

    loading = None
    if ctx.interaction is None:
        loading = await ctx.send(embed=info_embed("🔍 搜尋中..."))

    playlist_url = cover = None
    try:
        if spotify.is_spotify(query):
            playlist_name, playlist_url, cover, songs = await get_spotify_songs(query, ctx.author)
        else:
            playlist_name, songs = await get_songs(query, ctx.author)
            playlist_url = query if playlist_name else None
    except spotify.SpotifyError as e:
        songs, error = [], str(e)
    except MusicUnavailable:
        songs, error = [], "音樂伺服器（Lavalink）目前無法連線，請稍後再試。"
    else:
        error = "未能取得音樂，請檢查網址或關鍵字。"

    if loading:
        try:
            await loading.delete()
        except discord.HTTPException:
            pass
    if not songs:
        await ctx.send(embed=error_embed(error))
        return

    position = len(state.queue) + (1 if state.current else 0)
    state.queue.extend(songs)
    await ctx.send(embed=added_embed(songs, ctx.author, position, playlist_name, playlist_url, cover))

    if not vc_busy(vc):
        await play_next(ctx.guild)
    elif len(state.queue) == len(songs):
        state.prefetch_task = asyncio.create_task(prefetch_next(ctx.guild.id))


@commands.hybrid_command(name="play", aliases=["p", "P", "PLAY"], description="播放歌曲（關鍵字、YouTube / Spotify 連結）")
@commands.guild_only()
@app_commands.describe(query="關鍵字（搜尋 YouTube），或 YouTube / Spotify 的歌曲、播放清單連結")
async def play_command(ctx, *, query: str):
    await enqueue(ctx, query)


@commands.hybrid_command(name="nowplaying", aliases=["np"], description="顯示目前播放的歌曲與控制面板")
@commands.guild_only()
async def nowplaying(ctx):
    state = get_state(ctx.guild.id)
    if not state.current:
        await ctx.send(embed=error_embed("目前沒有音樂在播放！"))
        return
    state.text_channel = ctx.channel
    vc = ctx.guild.voice_client
    await clear_np_message(state)
    state.np_message = await ctx.send(
        embed=now_playing_embed(ctx.guild, state, paused=vc_paused(vc)),
        view=PlayerControls(ctx.guild.id),
    )


@commands.hybrid_command(name="queue", aliases=["q"], description="顯示目前的播放清單")
@commands.guild_only()
async def show_queue(ctx):
    pages = queue_pages(get_state(ctx.guild.id))
    if len(pages) == 1:
        await ctx.send(embed=pages[0])
        return
    view = QueueView(pages, ctx.author.id)
    view.message = await ctx.send(embed=pages[0], view=view)


@commands.hybrid_command(name="skip", aliases=["s"], description="跳過目前播放的歌曲")
@commands.guild_only()
@require_same_channel()
async def skip(ctx):
    if await skip_current(ctx.guild):  # TrackEnd 事件會自動播放下一首
        await ctx.send(embed=info_embed("⏭️ THE 跳！"))
    else:
        await ctx.send(embed=error_embed("目前沒有音樂在播放！"))


@commands.hybrid_command(name="pause", description="暫停播放")
@commands.guild_only()
@require_same_channel()
async def pause(ctx):
    vc = ctx.guild.voice_client
    if vc_playing(vc):
        await pause_playback(vc, get_state(ctx.guild.id))
        await ctx.send(embed=info_embed("⏸️ 已暫停"))
    else:
        await ctx.send(embed=error_embed("目前沒有音樂在播放！"))


@commands.hybrid_command(name="resume", description="繼續播放")
@commands.guild_only()
@require_same_channel()
async def resume(ctx):
    vc = ctx.guild.voice_client
    if vc_paused(vc):
        await resume_playback(vc, get_state(ctx.guild.id))
        await ctx.send(embed=info_embed("▶️ 繼續播放"))
    else:
        await ctx.send(embed=error_embed("目前沒有暫停中的音樂！"))


@commands.hybrid_command(name="stop", description="停止播放並清空待播清單")
@commands.guild_only()
@require_same_channel()
async def stop(ctx):
    await stop_playback(ctx.guild)
    await ctx.send(embed=info_embed("⏹️ 已停止播放並清空待播清單"))


@commands.hybrid_command(name="loop", description="設定循環模式")
@commands.guild_only()
@app_commands.describe(mode="off = 關閉、song = 單曲循環、queue = 清單循環（不填則切換）")
async def loop_command(ctx, mode: Optional[Literal["off", "song", "queue"]] = None):
    state = get_state(ctx.guild.id)
    state.loop = mode or {"off": "song", "song": "queue", "queue": "off"}[state.loop]
    await ctx.send(embed=info_embed(f"🔁 循環模式：**{LOOP_LABELS[state.loop]}**"))


@commands.hybrid_command(name="shuffle", description="隨機打亂待播清單")
@commands.guild_only()
@require_same_channel()
async def shuffle(ctx):
    state = get_state(ctx.guild.id)
    if len(state.queue) < 2:
        await ctx.send(embed=error_embed("待播清單的歌曲太少，無法打亂"))
        return
    shuffle_queue(state)
    await ctx.send(embed=info_embed(f"🔀 已打亂 {len(state.queue)} 首歌曲"))


@commands.hybrid_command(name="remove", aliases=["rm"], description="從待播清單移除指定歌曲")
@commands.guild_only()
@require_same_channel()
@app_commands.describe(index="歌曲在待播清單中的編號（用 /queue 查看）")
async def remove(ctx, index: int):
    state = get_state(ctx.guild.id)
    if not 1 <= index <= len(state.queue):
        await ctx.send(embed=error_embed(f"編號要在 1 ~ {len(state.queue)} 之間"))
        return
    song = state.queue[index - 1]
    del state.queue[index - 1]
    if state.prefetched and state.prefetched[0] is song:
        state.prefetched = None
    await ctx.send(embed=info_embed(f"🗑️ 已移除：**{escape(song.title)}**"))


@commands.hybrid_command(name="clear", description="清空待播清單（不影響目前播放的歌）")
@commands.guild_only()
@require_same_channel()
async def clear(ctx):
    state = get_state(ctx.guild.id)
    count = len(state.queue)
    state.queue.clear()
    state.prefetched = None
    await ctx.send(embed=info_embed(f"🧹 已清空 {count} 首歌曲"))


@commands.hybrid_command(name="leave", aliases=["dc"], description="離開語音頻道並清空播放清單")
@commands.guild_only()
async def leave(ctx):
    vc = ctx.guild.voice_client
    if not vc:
        await ctx.send(embed=error_embed("機器人目前不在任何語音頻道中"))
        return
    state = get_state(ctx.guild.id)
    state.reset()
    await vc.disconnect()
    await clear_np_message(state)
    await ctx.send(embed=info_embed("👋 已離開語音頻道並清空播放清單！"))


@commands.hybrid_command(name="volume", aliases=["vol"], description="設定音量（例如 50 = 50%，最高 200）")
@commands.guild_only()
@app_commands.describe(percent="音量百分比，例如 50 代表 50%（0 ~ 200）")
async def set_volume(ctx, percent: Optional[float] = None):
    state = get_state(ctx.guild.id)
    if percent is None:
        await ctx.send(embed=info_embed(f"🔊 目前音量：{state.volume * 100:.0f}%"))
        return
    # 相容舊用法：輸入 0 ~ 2 視為倍率（例如 0.5 = 50%、1 = 100%），大於 2 視為百分比
    value = percent if percent <= 2 else percent / 100
    await change_volume(ctx.guild, value)
    await ctx.send(embed=info_embed(f"🔊 音量已設置為 {state.volume * 100:.0f}%（此伺服器會記住這個設定）"))


@commands.hybrid_command(name="prefix", description="設定此伺服器的前綴指令符號")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
@app_commands.describe(new_prefix="新的前綴符號，例如 ! 或 ?")
async def set_prefix(ctx, new_prefix: str):
    new_prefix = new_prefix.strip()
    if not new_prefix or len(new_prefix) > 5 or " " in new_prefix:
        await ctx.send(embed=error_embed("前綴需為 1 ~ 5 個字元，且不能有空白"))
        return
    set_setting(ctx.guild.id, "prefix", new_prefix)
    await ctx.send(embed=info_embed(f"✅ 此伺服器的前綴已改為 `{new_prefix}`（斜線指令照常可用）"))


@commands.hybrid_command(name="music", aliases=["musichelp"], description="音樂指令說明")
async def help_command(ctx):
    p = get_setting(ctx.guild.id, "prefix", default_prefix) if ctx.guild else default_prefix
    embed = discord.Embed(
        title="🎵 音樂機器人指令",
        description=f"所有指令都可以用 `/` 或 `{p}` 開頭\n支援 **YouTube** 與 **Spotify**（歌曲、專輯、播放清單、歌手熱門歌曲）",
        color=COLOR_INFO,
    )
    embed.add_field(name="▶️ 播放", value=(
        f"`play <關鍵字或網址>`（`{p}p`）點歌\n"
        f"`nowplaying`（`{p}np`）正在播放 + 控制面板\n"
        f"`queue`（`{p}q`）播放清單"
    ), inline=False)
    embed.add_field(name="🎛️ 控制", value=(
        f"`pause` / `resume` 暫停 / 繼續\n"
        f"`skip`（`{p}s`）跳過 · `stop` 停止並清空\n"
        f"`loop [off/song/queue]` 循環 · `shuffle` 隨機\n"
        f"`remove <編號>` 移除 · `clear` 清空待播\n"
        f"`volume <0~200>`（`{p}vol`）音量"
    ), inline=False)
    embed.add_field(name="⚙️ 其他", value=(
        f"`join` 加入語音 · `leave`（`{p}dc`）離開\n"
        f"`prefix <符號>` 修改此伺服器的前綴（需管理伺服器權限）"
    ), inline=False)
    embed.set_footer(text="「正在播放」訊息下方的按鈕也可以直接控制播放")
    await ctx.send(embed=embed)


COMMANDS = (
    join, play_command, nowplaying, show_queue, skip, pause, resume, stop, loop_command,
    shuffle, remove, clear, leave, set_volume, set_prefix, help_command,
)
LISTENERS = {
    "on_guild_remove": on_guild_remove,
    "on_voice_state_update": on_voice_state_update,
    "on_command_error": on_command_error,
    "on_wavelink_track_end": on_wavelink_track_end,
    "on_wavelink_track_exception": on_wavelink_track_exception,
    "on_wavelink_track_stuck": on_wavelink_track_stuck,
    "on_wavelink_websocket_closed": on_wavelink_websocket_closed,
    "on_wavelink_node_ready": on_wavelink_node_ready,
}


def setup(target: commands.Bot, cfg: MusicConfig) -> None:
    """把音樂指令與事件加入農場 Bot。Lavalink 連線在 Bot 的 setup_hook 中呼叫 connect_lavalink()。"""
    global bot
    bot = target
    configure(cfg)
    for command in COMMANDS:
        target.add_command(command)
    for name, listener in LISTENERS.items():
        target.add_listener(listener, name)
