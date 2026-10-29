"""音樂模組（原 YTDiscordBot 移植）：與農場合併後的指令註冊，以及播放佇列邏輯（假的 Lavalink 播放器，不連網）。"""

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.discord_bot.bot import FarmBot, bot_intents
from app.discord_bot.runner import ServiceRunner
from app.music import player as music

MUSIC_COMMANDS = {"join", "play", "nowplaying", "queue", "skip", "pause", "resume", "stop", "loop",
                  "shuffle", "remove", "clear", "leave", "volume", "prefix", "music"}


def settings(**kw):
    return Settings(_env_file=None, database_url="postgresql+psycopg://x@localhost/x_test", **kw)


@pytest.fixture
def runner():
    r = ServiceRunner(1)
    yield r
    r.shutdown()


def test_merged_bot_registers_farm_and_music(runner, tmp_path):
    bot = FarmBot(settings(music_volume=0.3), None, runner)
    names = [c.name for c in bot.tree.get_commands()]
    assert len(names) == len(set(names))
    assert {"farm", "help"} | MUSIC_COMMANDS == set(names)
    for command in bot.tree.get_commands():
        command.to_dict(bot.tree)  # Discord 指令格式驗證
    assert {c.name for c in bot.commands} == MUSIC_COMMANDS  # 前綴版本
    assert music.default_volume == 0.3 and music.lavalink_uri == "http://127.0.0.1:2333"
    listeners = {name for name, _ in bot.extra_events.items()}
    assert {"on_wavelink_track_end", "on_wavelink_track_stuck"} <= listeners  # Lavalink 事件已註冊


def test_intents_and_music_toggle(runner):
    plain = bot_intents(settings())
    assert plain.guilds and plain.voice_states and not plain.message_content
    prefixed = bot_intents(settings(music_prefix_commands=True))
    assert prefixed.message_content and prefixed.guild_messages
    no_music = FarmBot(settings(music_enabled=False), None, runner)
    assert {c.name for c in no_music.tree.get_commands()} == {"farm", "help"}
    assert not bot_intents(settings(music_enabled=False)).voice_states


def test_advanced_farm_commands_do_not_collide(runner):
    bot = FarmBot(settings(discord_advanced_commands=True), None, runner)
    names = [c.name for c in bot.tree.get_commands()]
    assert len(names) == len(set(names)) and "clear-withered" in names and "clear" in names




# ---------- 假的 Lavalink 播放器（模擬 wavelink.Player 與 TrackEnd 事件，不連網） ----------


class FakeTrack:
    def __init__(self, n, source="youtube"):
        self.encoded = f"enc-{source}-{n}"
        self.identifier = f"id-{source}-{n}"
        self.title = f"song{n}"
        self.uri = f"https://www.youtube.com/watch?v={n}"
        self.is_stream = False
        self.length = 180_000
        self.artwork = None
        self.author = "歌手"
        self.source = source


class FakePlayer:
    """和 wavelink.Player 相同的非同步介面；skip／stop 會像 Lavalink 一樣送出 TrackEnd 事件。"""

    def __init__(self, guild):
        self.guild = guild
        self.connected = True
        self.current = None
        self.paused = False
        self.volume = 100
        self.played = []
        self.channel = SimpleNamespace(members=[], mention="#voice")

    async def play(self, track, *, volume=None, add_history=True, **_):
        self.current, self.paused = track, False
        if volume is not None:
            self.volume = volume
        self.played.append(track.title)
        return track

    async def pause(self, value):
        self.paused = value

    async def set_volume(self, value=100):
        self.volume = value

    def _end(self, reason):
        track, self.current, self.paused = self.current, None, False
        if track is not None:
            payload = SimpleNamespace(player=self, track=track, reason=reason)
            asyncio.get_running_loop().create_task(music.on_wavelink_track_end(payload))
        return track

    async def skip(self, *, force=True):
        return self._end("stopped")

    async def stop(self, *, force=True):
        return self._end("stopped")

    def finish(self):
        """歌曲自然播完（Lavalink 送出 reason=finished）。"""
        return self._end("finished")

    async def disconnect(self, **_):
        self.connected = False


def install_fakes(monkeypatch, tmp_path, guild_ids):
    music.configure(music.MusicConfig(settings_file=str(tmp_path / "s.json")))
    guilds = {}
    for gid in guild_ids:
        guild = SimpleNamespace(id=gid, name=f"g{gid}", icon=None)
        guild.voice_client = FakePlayer(guild)
        guilds[gid] = guild
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(music, "bot", SimpleNamespace(loop=loop, get_guild=guilds.get, user=SimpleNamespace(id=1)))
    music.states.clear()
    return guilds


def song(n, source="youtube"):
    return music.song_from_track(FakeTrack(n, source), None)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_queue_play_skip_loop_stop(monkeypatch, tmp_path):
    async def scenario():
        guild = install_fakes(monkeypatch, tmp_path, [10])[10]
        vc = guild.voice_client
        state = music.get_state(10)
        state.queue.extend(song(i) for i in range(3))
        await music.play_next(guild)
        assert state.current.title == "song0" and vc.played == ["song0"]
        assert vc.volume == 10  # 預設音量 0.1 → Lavalink 10
        vc.finish()  # 歌曲結束 → 自動播下一首
        await settle()
        assert state.current.title == "song1"
        state.loop = "song"
        vc.finish()  # 單曲循環：同一首再播一次
        await settle()
        assert state.current.title == "song1"
        assert await music.skip_current(guild)  # 跳過：即使單曲循環也前進
        await settle()
        assert state.current.title == "song2"
        await music.pause_playback(vc, state)
        assert music.vc_paused(vc) and not music.vc_playing(vc)
        await music.resume_playback(vc, state)
        assert music.vc_playing(vc)
        await music.stop_playback(guild)
        await settle()
        assert state.current is None and not state.queue and vc.current is None
        state.cancel_tasks()

    asyncio.run(scenario())


def test_track_end_from_other_track_is_ignored(monkeypatch, tmp_path):
    async def scenario():
        guild = install_fakes(monkeypatch, tmp_path, [11])[11]
        state = music.get_state(11)
        state.queue.extend([song(1), song(2)])
        await music.play_next(guild)
        # 換歌（replaced）或不是目前這首的結束事件都不能讓佇列前進
        for payload in (SimpleNamespace(player=guild.voice_client, track=FakeTrack(1), reason="replaced"),
                        SimpleNamespace(player=guild.voice_client, track=FakeTrack(99), reason="finished")):
            await music.on_wavelink_track_end(payload)
        assert state.current.title == "song1" and len(state.queue) == 1
        # Lavalink 事件帶回的 encoded 和送出的不同（YouTube 清單裡的歌），但 identifier 相同 → 仍要播下一首
        guild.voice_client.current = None
        other_encoding = FakeTrack(1)
        other_encoding.encoded = "enc-from-lavalink-event"
        await music.on_wavelink_track_end(SimpleNamespace(player=guild.voice_client, track=other_encoding,
                                                          reason="stopped"))
        await settle()
        assert state.current.title == "song2" and not state.queue
        state.cancel_tasks()

    asyncio.run(scenario())


def test_many_guilds_are_independent(monkeypatch, tmp_path):
    async def scenario():
        guilds = install_fakes(monkeypatch, tmp_path, range(1, 51))
        for gid in guilds:
            music.get_state(gid).queue.extend(song(gid * 100 + i) for i in range(2))
        await asyncio.gather(*(music.play_next(g) for g in guilds.values()))
        assert all(music.get_state(gid).current.title == f"song{gid * 100}" for gid in guilds)
        for g in guilds.values():
            g.voice_client.finish()
        await settle()
        assert all(music.get_state(gid).current.title == f"song{gid * 100 + 1}" for gid in guilds)
        for gid in guilds:
            music.get_state(gid).cancel_tasks()

    asyncio.run(scenario())


def test_volume_setting_persists(monkeypatch, tmp_path):
    async def scenario():
        guild = install_fakes(monkeypatch, tmp_path, [7])[7]
        await music.change_volume(guild, 0.8)
        assert guild.voice_client.volume == 80
        music.configure(music.MusicConfig(settings_file=str(tmp_path / "s.json")))  # 重新讀檔（模擬重啟）
        music.states.clear()
        assert music.get_state(7).volume == 0.8

    asyncio.run(scenario())


def test_get_songs_uses_lavalink_prefixes_and_limits(monkeypatch):
    import wavelink

    queries = []

    def fake_playlist(n):
        pl = wavelink.Playlist.__new__(wavelink.Playlist)
        pl.name, pl.selected, pl.tracks = "我的清單", 2, [FakeTrack(i) for i in range(n)]
        return pl

    async def fake_search(query):
        queries.append(query)
        return fake_playlist(30) if "list=" in query or "PLAYLIST" in query else [FakeTrack(1), FakeTrack(2)]

    monkeypatch.setattr(music, "lavalink_search", fake_search)
    monkeypatch.setattr(music, "playlist", 20)

    async def scenario():
        name, songs = await music.get_songs("晴天", None)
        assert queries[-1] == "ytsearch:晴天" and name is None and [s.title for s in songs] == ["song1"]
        name, songs = await music.get_songs("https://www.youtube.com/playlist?list=PL1", None)
        assert name == "我的清單" and len(songs) == 20  # 播放清單受 MUSIC_PLAYLIST_LIMIT 限制
        assert songs[0].title == "song2"  # 從網址選中的那首開始
        name, songs = await music.get_songs("https://www.youtube.com/watch?v=x&PLAYLIST", None)
        assert name is None and [s.title for s in songs] == ["song2"]  # 沒有 list= 的影片網址只播選中的那首
        song = music.song_from_track(FakeTrack(5), None)
        assert song.source == "youtube" and song.duration == 180

    asyncio.run(scenario())


def test_bilibili_links_are_rejected_without_lavalink(monkeypatch):
    """B 站已停止支援：貼 B 站連結直接回覆不支援，不送到 Lavalink、不加入佇列。"""
    sent = []

    async def no_search(query):
        raise AssertionError("不應查詢 Lavalink")

    async def fake_voice(ctx):
        return SimpleNamespace()

    async def defer():
        pass

    async def send(**kw):
        sent.append(kw["embed"].description)

    monkeypatch.setattr(music, "lavalink_search", no_search)
    monkeypatch.setattr(music, "ensure_voice", fake_voice)
    ctx = SimpleNamespace(defer=defer, send=send, guild=SimpleNamespace(id=987654), channel=None, interaction=object())

    async def scenario():
        for url in ("https://www.bilibili.com/video/BV1kbxXzeEEV", "https://b23.tv/abc123"):
            await music.enqueue(ctx, url)
        assert len(sent) == 2 and all("不支援 B 站" in s for s in sent)
        assert not music.get_state(987654).queue

    asyncio.run(scenario())


def test_spotify_song_resolves_on_youtube(monkeypatch):
    queries = []

    async def fake_search(query):
        queries.append(query)
        return [FakeTrack(7)]

    monkeypatch.setattr(music, "lavalink_search", fake_search)
    sp = music.Song(title="A - B", url=None, source="spotify", search_query="B A audio")

    async def scenario():
        track = await music.resolve_track(sp)
        assert queries == ["ytsearch:B A audio"] and track.title == "song7" and sp.url.endswith("v=7")
        await music.resolve_track(sp)
        assert len(queries) == 1  # 已解析過不再查詢

    asyncio.run(scenario())


def test_lavalink_unavailable_is_reported(monkeypatch):
    import wavelink

    monkeypatch.setattr(wavelink.Pool, "_Pool__nodes", {}, raising=False)

    async def scenario():
        with pytest.raises(music.MusicUnavailable):
            await music.lavalink_search("ytsearch:x")

    asyncio.run(scenario())


class FakeNode:
    def __init__(self, status, error=None):
        self.status, self.error, self.closed = status, error, None

    async def fetch_players(self):
        if self.error:
            raise self.error
        return []

    async def close(self, eject=False):
        self.closed = eject


def test_lavalink_session_lost_reconnects(monkeypatch):
    """Lavalink 重建後 wavelink 不會自行重連：Session 404 時清掉 Node 並重新連線，其他情況不動作。"""
    import wavelink

    def lavalink_error(status):
        return wavelink.LavalinkException(data={"timestamp": 0, "status": status, "error": "x", "path": "/"})

    reconnects = []

    async def fake_connect(client):
        reconnects.append(client)

    monkeypatch.setattr(music, "connect_lavalink", fake_connect)
    connected = wavelink.NodeStatus.CONNECTED

    async def check(node):
        monkeypatch.setattr(music, "_lavalink_node", lambda: node)
        return await music.ensure_lavalink_session("bot")

    async def scenario():
        healthy = FakeNode(connected)
        assert not await check(healthy) and healthy.closed is None
        assert not await check(None)
        assert not await check(FakeNode(wavelink.NodeStatus.CONNECTING, lavalink_error(404)))  # wavelink 正在重試
        assert not await check(FakeNode(connected, OSError("connection refused")))  # Lavalink 暫時連不上
        assert not await check(FakeNode(connected, lavalink_error(500)))
        assert reconnects == []
        lost = FakeNode(connected, lavalink_error(404))
        assert await check(lost)
        assert lost.closed is True and reconnects == ["bot"]

    asyncio.run(scenario())


def test_separate_music_process_mode(runner):
    from pydantic import SecretStr

    from app.music.__main__ import MusicBot, music_intents

    split = settings(music_bot_token=SecretStr("x" * 10), music_application_id=123)
    farm_only = FarmBot(split, None, runner)
    assert {c.name for c in farm_only.tree.get_commands()} == {"farm", "help"}  # 農場程序不再載入音樂
    assert not bot_intents(split).voice_states
    music_bot = MusicBot(split)
    names = {c.name for c in music_bot.tree.get_commands()}
    assert names == MUSIC_COMMANDS
    for command in music_bot.tree.get_commands():
        command.to_dict(music_bot.tree)
    assert music_intents(split).voice_states and not music_intents(split).message_content


def test_spotify_link_kinds():
    from app.music import spotify

    artist = "https://open.spotify.com/artist/2elBjNSdBE2Y3f0j1mjrql?flow_ctx=4f84c469%3A1793300017"
    assert spotify.SPOTIFY_RE.search(artist).groups() == ("artist", "2elBjNSdBE2Y3f0j1mjrql")
    episode = "https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk"
    assert spotify.is_spotify(episode)  # 不支援的 Spotify 網址不拿去 YouTube 搜尋
    with pytest.raises(spotify.SpotifyError, match="不支援"):
        asyncio.run(spotify.resolve(episode, 20))  # 判斷連結類型時就拒絕，不連網路
    assert not spotify.is_spotify("周杰倫 晴天")


def test_stats_update_logs_only_dropped_frames(caplog):
    caplog.set_level("WARNING", logger=music.logger.name)
    frames = SimpleNamespace(sent=3000, nulled=0, deficit=0)
    asyncio.run(music.on_wavelink_stats_update(SimpleNamespace(frames=frames)))
    asyncio.run(music.on_wavelink_stats_update(SimpleNamespace(frames=None)))
    assert not caplog.records
    dropped = SimpleNamespace(sent=2950, nulled=40, deficit=10)
    asyncio.run(music.on_wavelink_stats_update(SimpleNamespace(frames=dropped)))
    assert "來源不足 40" in caplog.text and "未送出 10" in caplog.text
