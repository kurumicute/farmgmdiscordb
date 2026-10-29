"""集中讀取環境設定（.env 或系統環境變數）。"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty：.env 中留空的項目（例如 DISCORD_DEV_GUILD_ID=）視為未設定，使用預設值
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True)

    database_url: str = Field(..., description="SQLAlchemy URL，例如 postgresql+psycopg://...")
    app_host: str = "127.0.0.1"
    app_port: int = Field(8000, ge=1, le=65535)
    monitor_refresh_seconds: float = Field(3.0, ge=1.0, le=60.0)
    # 允許的 Host 標頭（逗號分隔），防止 DNS rebinding；僅本機使用時不需修改
    allowed_hosts: str = "127.0.0.1,localhost"
    # 網頁管理功能的管理員帳號；密碼只存雜湊（python -m app.hash_password 產生）。未設定時管理功能停用
    admin_username: str | None = None
    admin_password_hash: str | None = None
    admin_session_hours: float = Field(8.0, gt=0, le=72)
    # 背景檢查並結算到期排名獎勵與競賽的間隔（秒）
    ranking_settle_interval_seconds: float = Field(60.0, ge=10, le=3600)
    # 由哪個程序執行背景結算：web（網站）、bot（Discord Bot）或 none；另一方只在查詢時補結算。
    # 多個程序同時執行也安全（PostgreSQL advisory lock + 唯一限制），只是重複工作
    background_tasks_runner: Literal["web", "bot", "none"] = "web"

    app_env: Literal["development", "production"] = "development"

    # 資料庫連線池（每個程序各自一組）。Bot 實際大小至少為 BOT_WORKER_THREADS + 2（背景結算與 advisory lock 各一）
    db_pool_size: int = Field(10, ge=1, le=100)
    db_max_overflow: int = Field(5, ge=0, le=100)
    db_pool_timeout: float = Field(10.0, gt=0, le=120)

    # Discord Bot。Token 只放在 .env 或系統環境變數，不寫入程式或日誌
    discord_bot_token: SecretStr | None = None
    discord_application_id: int | None = None
    # 開發伺服器：設定時指令只同步到這個伺服器（立即生效）；未設定且 APP_ENV=production 時全域同步
    discord_dev_guild_id: int | None = None
    discord_sync_commands: bool = True
    # 預設只註冊 /farm（互動面板）與 /help；true 時另外註冊完整的文字指令（/farm-cmd、/buy、/sell…）供進階玩家使用
    discord_advanced_commands: bool = False
    # 執行遊戲服務（同步 SQLAlchemy）的執行緒數；等於同時處理的指令上限
    bot_worker_threads: int = Field(16, ge=1, le=64)

    # 音樂播放（原 YTDiscordBot，與農場共用同一個 Bot）
    music_enabled: bool = True
    # 設定時音樂改由獨立程序執行（python -m app.music），使用另一個 Discord 應用程式的 Token，不和農場搶 CPU
    music_bot_token: SecretStr | None = None
    music_application_id: int | None = None
    # 前綴指令（!play 等）需要 Message Content 特權 Intent；預設只用斜線指令
    music_prefix_commands: bool = False
    music_prefix: str = Field("!", min_length=1, max_length=5)
    music_volume: float = Field(0.1, ge=0, le=2)
    music_playlist_limit: int = Field(20, ge=1, le=200)
    music_idle_timeout: int = Field(300, ge=30, le=3600)
    # Lavalink 音樂伺服器（docker compose 的 lavalink 服務）；密碼與 docker-compose 的 LAVALINK_PASSWORD 相同
    lavalink_uri: str = "http://127.0.0.1:2333"
    lavalink_password: SecretStr | None = None

    # 介紹網站（python -m app.site）
    site_host: str = "127.0.0.1"
    site_port: int = Field(24444, ge=1, le=65535)
    site_support_url: str | None = None  # 支援伺服器邀請連結（選填）

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]


    def engine_options(self, *, min_pool_size: int = 0) -> dict:
        return {
            "pool_size": max(self.db_pool_size, min_pool_size),
            "max_overflow": self.db_max_overflow,
            "pool_timeout": self.db_pool_timeout,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
