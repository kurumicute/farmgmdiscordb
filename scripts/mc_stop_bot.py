"""Minecraft 停止 Bot：只有 /stopmcserver 一個指令，在 Windows 主機上執行（不在 Docker 容器內）。

用法（在專案根目錄）：
  .venv/Scripts/python.exe scripts/mc_stop_bot.py

/stopmcserver 只能在 ALLOWED_CHANNEL_ID 頻道執行，效果等同按下 Minecraft 視窗右上角的 X：
對遊戲視窗送出 WM_CLOSE，遊戲會儲存整合伺服器（區域網路世界）的所有維度後結束。
強制結束程序（工作管理員）不會存檔，所以這裡不做 kill。

與農場 Bot 共用 DISCORD_BOT_TOKEN：
- 農場 Bot 在 production 會全域同步（整批覆蓋全域指令），所以這裡把指令註冊成「該頻道所在伺服器」的
  伺服器指令，並以 upsert 只新增／更新這一個指令，不影響其他指令。
- 同一個 Token 的兩個連線都會收到互動事件：這裡忽略其他指令（/farm 等），農場 Bot 收到 /stopmcserver
  時會在日誌記一筆找不到指令，不影響使用。
"""

import asyncio
import ctypes
import logging
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # Settings 從目前目錄讀 .env

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

from app.config import get_settings  # noqa: E402

logger = logging.getLogger("mc_stop_bot")

ALLOWED_CHANNEL_ID = 1012751500759543808
JAVA_EXES = {"java.exe", "javaw.exe"}
WINDOW_TITLE_PREFIX = "Minecraft"
EXIT_TIMEOUT_SECONDS = 180
# 遊戲日誌：用來確認結束前有存檔完成（latest.log 每次啟動遊戲會重寫）
MC_LOG = Path(
    os.environ.get(
        "MC_LATEST_LOG",
        Path.home() / "curseforge/minecraft/Instances/DeceasedCraft - Urban Zombie Apocalypse/logs/latest.log",
    )
)

WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _process_exe(pid: int) -> str | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def find_minecraft_windows() -> list[tuple[int, int, str]]:
    """回傳 (hwnd, pid, 標題)：可見、標題以 Minecraft 開頭、屬於 java／javaw 程序的頂層視窗。"""
    found: list[tuple[int, int, str]] = []

    @EnumWindowsProc
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if not buf.value.startswith(WINDOW_TITLE_PREFIX):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_exe(pid.value) in JAVA_EXES:
            found.append((hwnd, pid.value, buf.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def wait_process_exit(pid: int, timeout: float) -> bool:
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True  # 已經結束
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def saved_in_log() -> bool | None:
    """最後一次「Stopping!」之後是否有「All dimensions are saved」；找不到日誌時回傳 None。"""
    try:
        text = MC_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    stop = text.rfind("Stopping!")
    return stop != -1 and "All dimensions are saved" in text[stop:]


def stop_minecraft() -> str:
    """關閉遊戲視窗並等待程序結束（阻塞，在執行緒中呼叫）。回傳給使用者的訊息。"""
    windows = find_minecraft_windows()
    if not windows:
        return "❌ 找不到 Minecraft 遊戲視窗（遊戲可能沒有開）。"
    if len({pid for _, pid, _ in windows}) > 1:
        return "❌ 找到多個 Minecraft 程序，為避免關錯，請手動關閉：" + "、".join(t for _, _, t in windows)
    hwnd, pid, title = windows[0]
    logger.info("送出 WM_CLOSE：%s（PID %d）", title, pid)
    if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
        return f"❌ 無法關閉視窗（錯誤碼 {ctypes.get_last_error()}）。"
    started = time.monotonic()
    if not wait_process_exit(pid, EXIT_TIMEOUT_SECONDS):
        return f"⚠️ 已要求關閉，但 {EXIT_TIMEOUT_SECONDS} 秒內遊戲沒有結束，請到電腦前確認（不要強制結束，會遺失進度）。"
    elapsed = time.monotonic() - started
    saved = saved_in_log()
    if saved is True:
        return f"✅ Minecraft 已存檔並關閉（{elapsed:.0f} 秒）。"
    if saved is None:
        return f"✅ Minecraft 已關閉（{elapsed:.0f} 秒）；找不到遊戲日誌，無法確認存檔紀錄。"
    return f"⚠️ Minecraft 已關閉（{elapsed:.0f} 秒），但日誌中沒有看到存檔完成的紀錄，請檢查存檔。"


class StopTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandNotFound):
            return  # /farm 等是農場 Bot 的指令，由它回應
        await super().on_error(interaction, error)


class StopBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = StopTree(self)
        self._lock = asyncio.Lock()

        @app_commands.command(name="stopmcserver", description="存檔並關閉 Minecraft")
        async def stopmcserver(interaction: discord.Interaction) -> None:
            if interaction.channel_id != ALLOWED_CHANNEL_ID:
                await interaction.response.send_message("❌ 這個指令只能在指定頻道使用。", ephemeral=True)
                return
            if self._lock.locked():
                await interaction.response.send_message("⏳ 正在關閉中，請稍候。", ephemeral=True)
                return
            async with self._lock:
                logger.info("%s（%s）執行 /stopmcserver", interaction.user, interaction.user.id)
                await interaction.response.send_message("⏳ 正在存檔並關閉 Minecraft…")
                message = await asyncio.to_thread(stop_minecraft)
                await interaction.edit_original_response(content=message)

        self._command = stopmcserver

    async def setup_hook(self) -> None:
        try:
            channel = await self.fetch_channel(ALLOWED_CHANNEL_ID)
        except discord.HTTPException as exc:
            raise SystemExit(
                f"[啟動失敗] 無法取得頻道 {ALLOWED_CHANNEL_ID}（HTTP {exc.status}），請確認 Bot 看得到該頻道。"
            ) from None
        guild_id = channel.guild.id
        # 只加在該伺服器（不加全域），本機不呼叫 tree.sync，避免覆蓋農場 Bot 的指令
        self.tree.add_command(self._command, guild=discord.Object(id=guild_id))
        await self.http.upsert_guild_command(self.application_id, guild_id, self._command.to_dict(self.tree))
        logger.info("已註冊 /stopmcserver 到伺服器 %s（限頻道 %s）", guild_id, ALLOWED_CHANNEL_ID)

    async def on_ready(self) -> None:
        logger.info("Minecraft 停止 Bot 已登入：%s", self.user)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("[啟動失敗] 這個 Bot 要在執行 Minecraft 的 Windows 主機上執行（不能在 Docker 容器內）。")
    discord.utils.setup_logging(level=logging.INFO)
    token = get_settings().discord_bot_token
    if token is None or not token.get_secret_value().strip():
        raise SystemExit("[啟動失敗] 未設定 DISCORD_BOT_TOKEN（.env）。")
    try:
        StopBot().run(token.get_secret_value(), log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("[啟動失敗] DISCORD_BOT_TOKEN 無效。") from None


if __name__ == "__main__":
    main()
