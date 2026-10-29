"""獨立的音樂 Bot：python -m app.music

音訊解析與播放由 Lavalink（docker compose 的 lavalink 服務）負責；獨立程序讓音樂指令不和農場搶事件迴圈。需要另一個 Discord 應用程式：
在 .env 設定 MUSIC_BOT_TOKEN（與 MUSIC_APPLICATION_ID）；設定後農場 Bot（python -m app.discord_bot）就不再載入音樂。
不需要資料庫。指令同步規則與農場相同（DISCORD_DEV_GUILD_ID／APP_ENV／DISCORD_SYNC_COMMANDS）。
"""

import logging

import discord
from discord.ext import commands

from app.config import Settings, get_settings
from app.discord_bot.bot import music_config, sync_commands
from app.music import player as music

logger = logging.getLogger("app.music")


def music_intents(settings: Settings) -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    if settings.music_prefix_commands:  # !play 等前綴指令需要 Message Content 特權 Intent
        intents.guild_messages = True
        intents.message_content = True
    return intents


class MusicBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            command_prefix=music.get_prefix if settings.music_prefix_commands else commands.when_mentioned,
            intents=music_intents(settings),
            help_command=None,
            application_id=settings.music_application_id,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        music.setup(self, music_config(settings))

    async def setup_hook(self) -> None:
        await sync_commands(self.tree, self.settings)
        await music.connect_lavalink(self)

    async def on_ready(self) -> None:
        logger.info("音樂 Bot 已登入：%s，已加入 %d 個伺服器", self.user, len(self.guilds))
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))


def main() -> None:
    discord.utils.setup_logging(level=logging.INFO)
    try:
        settings = get_settings()
    except Exception as exc:
        raise SystemExit(f"[啟動失敗] 設定錯誤（請檢查 .env）：{type(exc).__name__}") from None
    token = settings.music_bot_token.get_secret_value().strip() if settings.music_bot_token else ""
    if not token:
        raise SystemExit(
            "[啟動失敗] 未設定 MUSIC_BOT_TOKEN。獨立音樂 Bot 需要另一個 Discord 應用程式的 Token；"
            "不設定時音樂會直接在農場 Bot（python -m app.discord_bot）中執行。"
        )
    try:
        MusicBot(settings).run(token, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("[啟動失敗] MUSIC_BOT_TOKEN 無效或已被重設，請到 Developer Portal 重新產生並更新 .env。") from None
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "[啟動失敗] MUSIC_PREFIX_COMMANDS=true 需要在 Developer Portal 開啟 Message Content Intent。"
        ) from None


if __name__ == "__main__":
    main()
