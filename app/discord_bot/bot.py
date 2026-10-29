"""Discord Bot 程序：農場（互動面板）＋音樂播放。登入、同步 Slash Commands、對應伺服器群組，以及（設定時）背景結算。

啟動：python -m app.discord_bot
"""

import asyncio
import logging

import discord
from discord.ext import commands

from app.config import Settings, get_settings
from app.db.check import StartupCheckError, check_database
from app.db.session import make_engine, make_session_factory
from app.discord_bot.commands import FarmCommands, register
from app.discord_bot.runner import ServiceRunner
from app.music import player as music
from app.services.admin_service import AdminService

logger = logging.getLogger("app.discord_bot")


def music_embedded(settings: Settings) -> bool:
    """音樂是否在農場 Bot 程序內執行；設定 MUSIC_BOT_TOKEN 時改由獨立程序（python -m app.music）執行。"""
    return settings.music_enabled and settings.music_bot_token is None


async def sync_commands(tree, settings: Settings) -> None:
    """同步 Slash Commands：開發伺服器（立即生效）或正式環境全域同步。農場與獨立音樂 Bot 共用。"""
    s = settings
    if not s.discord_sync_commands:
        logger.info("DISCORD_SYNC_COMMANDS=false：略過指令同步")
        return
    try:
        if s.discord_dev_guild_id:
            guild = discord.Object(id=s.discord_dev_guild_id)
            tree.copy_global_to(guild=guild)
            synced = await tree.sync(guild=guild)
            logger.info("已同步 %d 個指令到開發伺服器 %s（立即生效）", len(synced), s.discord_dev_guild_id)
        elif s.app_env == "production":
            synced = await tree.sync()
            logger.info("已全域同步 %d 個指令（Discord 端可能需要數分鐘才會顯示）", len(synced))
        else:
            logger.warning(
                "開發環境未設定 DISCORD_DEV_GUILD_ID，未同步指令。"
                "請設定開發伺服器 ID，或以 APP_ENV=production 全域同步。"
            )
    except discord.Forbidden:
        logger.error(
            "指令同步失敗：Bot 沒有該伺服器的 applications.commands 權限，請用含此 scope 的邀請連結重新邀請。"
        )
    except discord.HTTPException as exc:
        # exc.text 是 Discord 對指令格式的錯誤說明（指出哪個欄位不合法），不含 Token
        logger.error("指令同步失敗：HTTP %s（Discord 錯誤碼 %s）：%s", exc.status, exc.code, exc.text[:1500])


def bot_intents(settings: Settings) -> discord.Intents:
    # Slash Commands 不需要 Message Content／Members 等特權 Intent；只需要伺服器基本資料（名稱、加入事件）
    intents = discord.Intents.none()
    intents.guilds = True
    if music_embedded(settings):
        intents.voice_states = True  # 加入語音頻道、偵測頻道沒人時自動離開
        if settings.music_prefix_commands:
            # !play 等前綴指令需要讀取訊息內容（特權 Intent，需在 Developer Portal 開啟）
            intents.guild_messages = True
            intents.message_content = True
    return intents


def music_config(settings: Settings) -> music.MusicConfig:
    return music.MusicConfig(
        volume=settings.music_volume,
        playlist_limit=settings.music_playlist_limit,
        prefix=settings.music_prefix,
        idle_timeout=settings.music_idle_timeout,
        lavalink_uri=settings.lavalink_uri,
        lavalink_password=settings.lavalink_password.get_secret_value() if settings.lavalink_password else "",
    )


class FarmBot(commands.Bot):
    def __init__(self, settings: Settings, service, runner: ServiceRunner) -> None:
        prefix_enabled = music_embedded(settings) and settings.music_prefix_commands
        super().__init__(
            command_prefix=music.get_prefix if prefix_enabled else commands.when_mentioned,
            intents=bot_intents(settings),
            help_command=None,
            application_id=settings.discord_application_id,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.service = service
        self.runner = runner
        self.farm = FarmCommands(service, runner)
        register(self.tree, self.farm, advanced=settings.discord_advanced_commands)
        if music_embedded(settings):
            music.setup(self, music_config(settings))
        self._settle_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        await self.sync_commands()
        if music_embedded(self.settings):
            await music.connect_lavalink(self)
        if self.settings.background_tasks_runner == "bot":
            self._settle_task = asyncio.create_task(self._settle_loop())
            logger.info("背景結算由 Bot 執行（每 %.0f 秒）", self.settings.ranking_settle_interval_seconds)

    async def sync_commands(self) -> None:
        await sync_commands(self.tree, self.settings)

    async def on_ready(self) -> None:
        user_id = self.user.id if self.user else "-"
        logger.info("Bot 已登入：%s（%s），已加入 %d 個伺服器", self.user, user_id, len(self.guilds))
        activity = "/farm 種田" + (" ・ /play 聽歌" if music_embedded(self.settings) else "")
        await self.change_presence(activity=discord.Game(name=activity))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            group_id = await self.runner.run(self.service.ensure_guild_group, str(guild.id), guild.name)
            logger.info("加入伺服器 %s，對應群組 #%s", guild.id, group_id)
        except Exception as exc:  # 之後第一次使用指令時仍會建立
            logger.warning("建立伺服器對應群組失敗：%s", type(exc).__name__)

    async def _settle_loop(self) -> None:
        """定期結算排名獎勵與競賽；advisory lock 與唯一限制保證重連、重啟或多程序同時執行都不會重複發獎。"""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                result = await self.runner.run(self.service.run_background_settlement, "system")
                if result and (result["rankings"] or result["competitions"]):
                    logger.info("已自動結算：%s", result)
            except Exception as exc:  # 資料庫暫時中斷：下次再試，不中斷 Bot
                logger.warning("自動結算失敗：%s", type(exc).__name__)
            await asyncio.sleep(self.settings.ranking_settle_interval_seconds)

    async def close(self) -> None:
        if self._settle_task is not None:
            self._settle_task.cancel()
        await super().close()


def main() -> None:
    discord.utils.setup_logging(level=logging.INFO)
    try:
        settings = get_settings()
    except Exception as exc:
        raise SystemExit(f"[啟動失敗] 設定錯誤（請檢查 .env）：{type(exc).__name__}") from None
    if settings.discord_bot_token is None or not settings.discord_bot_token.get_secret_value().strip():
        raise SystemExit("[啟動失敗] 未設定 DISCORD_BOT_TOKEN（請寫在 .env 或系統環境變數，不要寫進程式碼）。")

    workers = settings.bot_worker_threads
    # 連線池至少要能讓每個工作執行緒各拿一條連線，另加背景結算與 advisory lock 各一條
    engine = make_engine(settings.database_url, **settings.engine_options(min_pool_size=workers + 2))
    try:
        check_database(engine)
    except StartupCheckError as exc:
        engine.dispose()
        raise SystemExit(f"[啟動失敗] {exc}") from None

    runner = ServiceRunner(workers)
    service = AdminService(make_session_factory(engine))
    bot = FarmBot(settings, service, runner)
    logger.info(
        "啟動 Discord Bot：APP_ENV=%s，工作執行緒 %d，連線池 %d+%d，背景結算：%s",
        settings.app_env, workers, max(settings.db_pool_size, workers + 2), settings.db_max_overflow,
        settings.background_tasks_runner,
    )
    try:
        bot.run(settings.discord_bot_token.get_secret_value(), log_handler=None)
    except discord.LoginFailure:
        raise SystemExit(
            "[啟動失敗] DISCORD_BOT_TOKEN 無效或已被重設，請到 Discord Developer Portal 重新產生並更新 .env。"
        ) from None
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("[啟動失敗] Bot 要求了未在 Developer Portal 啟用的特權 Intent。") from None
    finally:
        runner.shutdown()
        engine.dispose()
