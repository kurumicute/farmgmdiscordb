"""CMD 指令解析與輸出格式。只負責輸入／輸出，遊戲規則全部交給服務層。

CMD 以「目前玩家」與「目前群組」模擬 Discord 的使用者與伺服器上下文；
這兩個選取狀態只存在於本物件，服務層的每個方法都明確接收 player_id / group_id。
"""

import shlex
from collections.abc import Callable

from app.clock import TAIPEI_TZ
from app.game import balance, genetics, rules
from app.game import ranking as ranking_rules
from app.game.errors import GameError, ValidationError
from app.game.rules import EVENT_TYPE_LABELS, PLOT_STATE_LABELS, PlotState
from app.services.dto import EFFECT_STATUS_LABELS, Backpack, FarmStatus
from app.services.game_service import genetics_label
from app.services.social_service import SocialService

HELP_TEXT = f"""\
可用指令：
  help                          顯示說明
  player create <名稱>          建立玩家
  player list [關鍵字] [頁碼]   列出／搜尋玩家（每頁 20 筆）
  player use <id>               選擇玩家
  farm start [群組ID] [農場名稱] 建立農場並固定所屬群組（未指定時用 farm group set 或目前群組）
  farm group                    查看農場主要所屬群組
  farm group set <群組ID>       建立農場前指定所屬群組；或為尚未設定的舊農場做一次性設定
  farm group transfer <群組ID>  把農場轉移到另一個群組（須為成員；每次轉移後需等待 7 天）
  farm status                   查看農場狀態
  plots                         查看土地與作物進度
  shop                          查看種子、道具價格與農產品售價
  market prices                 各作物不同品質與突變的單價
  crop info <作物>              作物資料、珍稀變種與各品質售價
  crop quality | crop mutations  品質機率與售價倍率／突變機率與效果
  buy <作物|道具> <數量>        購買，例如：buy wheat 5、buy fertilizer 2、buy auto_waterer 1
  plant <土地編號> <作物>       種植，例如：plant 1 wheat（種下後需澆水）
  water <土地編號|all>          澆水，澆水後開始生長
  harvest <土地編號|all>        收成
  clear <土地編號>              清除乾枯的作物
  use fertilizer <土地編號>     對生長中的土地施化肥（剩餘時間減半）
  use earthworm                 使用人工養殖蚯蚓（30 分鐘內澆水的作物加速生長）
  tool on|off auto_waterer      啟用／停用自動澆水器
  inventory [items|tools]       查看背包：全部／可堆疊物品／工具與效果
  sell <作物|商品> <數量> [品質] [突變]  出售農產品或商品，例如：sell wheat 2、sell 小麥 1 稀有 黃金、sell 琥珀小麥 1
  upgrade                       擴建土地（{balance.UPGRADE_COST} 金幣，+{balance.UPGRADE_PLOTS_ADDED} 格）
  log [筆數]                    查看最近操作紀錄
群組（模擬 Discord 伺服器）：
  group create <群組名稱>       建立群組
  group list [關鍵字] [頁碼]    查看／搜尋群組
  group use <群組ID>            選擇目前群組
  group add <玩家ID>            將玩家加入目前群組
  group remove <玩家ID>         將玩家移出目前群組（不影響其農場與紀錄）
  group members [關鍵字] [頁碼] 查看／搜尋目前群組成員
  group status                  查看目前群組基本資訊
  steal targets [頁碼]          列出目前群組可以偷的作物（每頁 10 筆）
  steal <玩家ID> <土地編號> <數量>  偷菜（每次最多 {balance.STEAL_MAX_PER_ACTION} 個，每株每人限偷一次）
  defense | defense status      查看防守設施、守護範圍與各土地阻止機率
  defense shop                  查看防守商店
  defense buy <設施|巡查券> [數量]  購買防守設施（稻草人／警戒犬／防盜圍欄）或巡查券
  defense place <稻草人|圍欄> <起始土地> [編號]  調整稻草人或防盜圍欄的守護範圍
  defense log                   查看自己農場近期被阻止的偷菜（不含偷菜者）
  patrol                        消耗 1 張巡查券，查看自己農場近 7 天所有偷菜嘗試（含偷菜者）
動物養殖與加工：
  animal shop | animal list | animal status       動物商店／我的動物／養殖狀態
  animal buy <動物> <數量>      購買動物（需先建造畜舍，容量不足時不扣款）
  animal feed <編號|all>        餵食（扣餵食費用）後開始生產週期
  animal collect <編號>         收集動物產品（背包不足時保留在動物身上）
  workshop | workshop build <設施>  查看／建造畜舍、磨坊、烘焙坊、果醬工坊
  recipe list                   查看加工配方
  craft <配方> <份數> | craft status | craft collect <訂單ID>  加工、查看、領取成品
  event list                    目前進行中的限時活動（售價、生長、品質、突變、收成加成）
  leaderboard groups [money|sales|goods] [all|day|week|month]  群組總排行（各群組成員加總）
  leaderboard rewards           每日／每週／每月排名獎勵規則與我獲得的獎勵
  leaderboard global|group [money|sales|goods]  總排行榜／群組排行榜（群組排行只計算主要所屬群組為此群組的成員）
群組任務與競賽（只有在「目前群組 = 農場主要群組」時進行的操作才會計分）：
  quest list                    我所屬各群組的進行中與已完成任務
  quest group                   目前群組的全部任務（需先選擇群組）
  quest status <任務ID>         任務進度、個人貢獻與獎勵（目前群組）
  quest claim <任務ID>          領取已完成任務的獎勵（目前群組）
  competition list              競賽列表
  competition status <競賽ID>   競賽資訊與我的貢獻
  competition leaderboard <競賽ID>  群組排名與個人貢獻
  competition history           已結算競賽的最終排名
  exit                          離開遊戲
作物可輸入代碼或中文名稱：wheat/小麥、carrot/胡蘿蔔、tomato/番茄、strawberry/草莓
道具：fertilizer/化肥、earthworm/蚯蚓、auto_waterer/自動澆水器"""


class ExitRequested(Exception):
    pass


def _parse_int(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ValidationError(f"{label}必須是整數：{value}") from None


PAGE_SIZE = 20
STEAL_PAGE_SIZE = 10


def _page_args(args: list[str]) -> tuple[str | None, int]:
    """解析「[關鍵字] [頁碼]」：最後一個參數是數字時視為頁碼（只有一個數字參數時也視為頁碼）。"""
    page = 1
    if args and args[-1].isdigit():
        page = max(1, int(args[-1]))
        args = args[:-1]
    keyword = " ".join(args).strip() or None
    return keyword, page


def _slice(items: list, page: int, size: int = PAGE_SIZE) -> tuple[list, int]:
    pages = max(1, -(-len(items) // size))
    page = min(page, pages)
    return items[(page - 1) * size : page * size], page


def _page_footer(command: str, keyword: str | None, page: int, total: int, size: int = PAGE_SIZE) -> str:
    pages = max(1, -(-total // size))
    text = f"第 {page}/{pages} 頁，共 {total} 筆"
    prefix = f"{command} {keyword} " if keyword else f"{command} "
    if page < pages:
        text += f"；下一頁：{prefix}{page + 1}"
    if not keyword and total > size and command != "steal targets":
        text += f"；也可以加上關鍵字搜尋：{command} <名稱或ID>"
    return text


def _local(dt) -> str:
    return dt.astimezone(TAIPEI_TZ).strftime("%m-%d %H:%M:%S") if dt else "-"


class CommandHandler:
    def __init__(self, service: SocialService) -> None:
        self._base_service = service
        self.service = service
        self.player_id: int | None = None
        self.group_id: int | None = None
        # 建立農場前以 farm group set 預先指定的群組；只存在本次 CMD 工作階段，建立農場時寫入資料庫
        self.pending_home_group_id: int | None = None
        self._commands: dict[str, Callable[[list[str]], str]] = {
            "help": lambda a: HELP_TEXT,
            "player": self._player,
            "farm": self._farm,
            "plots": lambda a: self._plots_text(self.service.get_farm_status(self._pid())),
            "shop": self._shop,
            "market": self._market,
            "crop": self._crop_cmd,
            "buy": self._buy,
            "plant": self._plant,
            "water": self._water,
            "harvest": self._harvest,
            "clear": self._clear,
            "use": self._use,
            "tool": self._tool,
            "inventory": self._inventory,
            "inv": self._inventory,
            "sell": self._sell,
            "upgrade": lambda a: self.service.upgrade_farm(self._pid()).message,
            "log": self._log,
            "group": self._group,
            "steal": self._steal,
            "leaderboard": self._leaderboard,
            "defense": self._defense,
            "animal": self._animal,
            "workshop": self._workshop,
            "recipe": self._recipe,
            "craft": self._craft,
            "quest": self._quest,
            "competition": self._competition,
            "patrol": self._patrol,
            "event": self._event,
            "lb": self._leaderboard,
            "exit": self._exit,
            "quit": self._exit,
        }

    @property
    def prompt(self) -> str:
        parts = []
        if self.player_id:
            parts.append(f"玩家 #{self.player_id}")
        if self.group_id:
            parts.append(f"群組 #{self.group_id}")
        return f"農場[{' / '.join(parts)}]> " if parts else "農場> "

    def handle(self, line: str) -> str:
        """執行一行指令並回傳要顯示的文字；遊戲錯誤轉成友善訊息。"""
        # 從 PowerShell 管線或 UTF-8 檔案導入時，第一行可能帶有 BOM
        line = line.replace("﻿", "")
        try:
            args = shlex.split(line, posix=True)
        except ValueError:
            return "指令格式錯誤：引號沒有成對。"
        if not args:
            return ""
        cmd = args[0].lower()
        if cmd not in self._commands:
            return f"未知的指令「{cmd}」，輸入 help 查看可用指令。"
        try:
            return self.execute(args)
        except GameError as exc:
            return f"[失敗] {exc}"

    def execute(self, args: list[str]) -> str:
        """執行已拆好參數的指令並回傳輸出文字；遊戲錯誤直接拋出（Discord Bot 以此重用相同的輸出格式）。"""
        handler = self._commands.get(args[0].lower()) if args else None
        if handler is None:
            raise ValidationError("未知的指令。")
        # 每個指令都以目前群組作為操作上下文（只記錄於交易紀錄，不影響農場歸屬或排行）
        self.service = self._base_service.in_group_context(self.group_id)
        return handler(args[1:])

    def _pid(self) -> int:
        if self.player_id is None:
            raise ValidationError("請先選擇玩家：player create <名稱> 或 player use <id>。")
        return self.player_id

    def _gid(self) -> int:
        if self.group_id is None:
            raise ValidationError("請先選擇群組：group use <群組ID>（可用 group list 查看）。")
        return self.group_id

    @staticmethod
    def _need(args: list[str], count: int, usage: str) -> None:
        if len(args) != count:
            raise ValidationError(f"用法：{usage}")

    # ---------- 玩家與農場 ----------

    def _player(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "create" and len(args) >= 2:
            player = self.service.create_player(" ".join(args[1:]))
            self.player_id = player.id
            self.pending_home_group_id = None
            return (
                f"已建立玩家 #{player.id}「{player.display_name}」並切換為目前玩家。"
                "接著輸入 farm start <群組ID> 建立農場。"
            )
        if sub == "list":
            keyword, page = _page_args(args[1:])
            players, total = self.service.search_players(keyword, page, PAGE_SIZE)
            last_page = max(1, -(-total // PAGE_SIZE))
            if page > last_page:  # 超出範圍時改顯示最後一頁
                page = last_page
                players, total = self.service.search_players(keyword, page, PAGE_SIZE)
            if not total:
                return (
                    f"找不到名稱或 ID 符合「{keyword}」的玩家。"
                    if keyword
                    else "目前沒有玩家，請用 player create <名稱> 建立。"
                )
            lines = ["編號  名稱                農場"]
            for p in players:
                mark = " *" if p.id == self.player_id else ""
                lines.append(f"{p.id:<5} {p.display_name:<18}  {'有' if p.has_farm else '無'}{mark}")
            lines.append(_page_footer("player list", keyword, page, total))
            return "\n".join(lines)
        if sub == "use" and len(args) == 2:
            player = self.service.get_player(_parse_int(args[1], "玩家編號"))
            self.player_id = player.id
            self.pending_home_group_id = None
            hint = "" if player.has_farm else "（尚未建立農場，請輸入 farm start <群組ID>）"
            return f"目前玩家：#{player.id}「{player.display_name}」{hint}"
        raise ValidationError("用法：player create <名稱> | player list [關鍵字] [頁碼] | player use <id>")

    def _farm(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "start":
            return self._farm_start(args[1:])
        if sub == "status" and len(args) == 1:
            return self._status_text(self.service.get_farm_status(self._pid()))
        if sub == "group" and len(args) == 1:
            return self._farm_group_show()
        if sub == "group" and len(args) == 3 and args[1].lower() == "set":
            return self._farm_group_set(_parse_int(args[2], "群組編號"))
        if sub == "group" and len(args) == 3 and args[1].lower() == "transfer":
            return self.service.transfer_home_group(self._pid(), _parse_int(args[2], "群組編號")).message
        raise ValidationError(
            "用法：farm start [群組ID] [農場名稱] | farm status | farm group | farm group set <群組ID> | "
            "farm group transfer <群組ID>"
        )

    def _farm_start(self, args: list[str]) -> str:
        pid = self._pid()
        # 第一個參數是整數時視為群組 ID；否則依序使用 farm group set 預先指定的群組、目前群組
        if args and args[0].lstrip("-").isdigit():
            home = _parse_int(args[0], "群組編號")
            name_parts = args[1:]
            source = "指定"
        elif self.pending_home_group_id is not None:
            home, name_parts, source = self.pending_home_group_id, args, "預先設定"
        elif self.group_id is not None:
            home, name_parts, source = self.group_id, args, "目前選擇"
        else:
            raise ValidationError(
                "建立農場必須指定所屬群組：farm start <群組ID> [農場名稱]，"
                "或先用 group use <群組ID> / farm group set <群組ID>。"
            )
        status = self.service.create_farm(pid, " ".join(name_parts) or None, home_group_id=home)
        self.pending_home_group_id = None
        group = self.service.get_group(home)
        return (
            f"農場「{status.name}」建立完成！所屬群組：#{group.id}「{group.name}」"
            f"（{source}；之後可用 farm group transfer 轉移，每 7 天一次）。"
            f"金幣 {status.gold}，土地 {len(status.plots)} 格，倉庫容量 {status.warehouse_capacity}。"
        )

    def _farm_group_show(self) -> str:
        pid = self._pid()
        if not self.service.get_player(pid).has_farm:
            if self.pending_home_group_id is None:
                return "尚未建立農場，也尚未指定所屬群組。可用 farm group set <群組ID> 或 farm start <群組ID>。"
            g = self.service.get_group(self.pending_home_group_id)
            return f"尚未建立農場；建立時將綁定群組 #{g.id}「{g.name}」。"
        home = self.service.get_home_group(pid)
        if home is None:
            return "這座農場尚未設定所屬群組（舊資料），可用 farm group set <群組ID> 做一次性設定。"
        st = self.service.group_transfer_status(pid)
        if st["can_transfer"]:
            note = "目前可轉移：farm group transfer <群組ID>"
        else:
            note = f"轉移冷卻中，下次可轉移時間 {_local(st['next_transfer_at'])}"
        return f"農場主要所屬群組：#{home[0]}「{home[1]}」（{note}）。"

    def _farm_group_set(self, group_id: int) -> str:
        pid = self._pid()
        if not self.service.get_player(pid).has_farm:
            g = self.service.get_group(group_id)  # 確認群組存在
            self.pending_home_group_id = g.id
            return f"已指定：建立農場時將綁定群組 #{g.id}「{g.name}」。接著輸入 farm start 建立農場。"
        return self.service.set_home_group(pid, group_id).message

    @staticmethod
    def _status_text(s: FarmStatus) -> str:
        counts = {state: sum(1 for p in s.plots if p.state == state) for state in PlotState}
        detail = "、".join(f"{PLOT_STATE_LABELS[st]} {n}" for st, n in counts.items() if n)
        return (
            f"玩家：{s.player_name}　農場：{s.name}（等級 {s.level}）\n"
            f"金幣：{s.gold}\n"
            f"土地：{len(s.plots)} 格（{detail}）\n"
            f"倉庫：{s.warehouse_used}/{s.warehouse_capacity}"
        )

    @staticmethod
    def _plots_text(s: FarmStatus) -> str:
        lines = ["土地  作物      狀態      剩餘數量  剩餘時間        預計成熟"]
        for p in s.plots:
            label = PLOT_STATE_LABELS[p.state]
            if p.state == PlotState.EMPTY:
                lines.append(f"{p.plot_number:<5} {'-':<8}  {label}")
                continue
            if p.state == PlotState.GROWING:
                left = rules.format_duration(p.remaining_seconds)
            elif p.state == PlotState.WAITING_WATER:
                left = f"(water {p.plot_number})"
            elif p.state == PlotState.WITHERED:
                left = f"(clear {p.plot_number})"
            else:
                left = "-"
            mark = " 已施肥" if p.fertilized else ""
            genes = genetics_label(p.quality, p.mutation) + ("【珍稀變種】" if p.mutation == "variant" else "")
            lines.append(
                f"{p.plot_number:<5} {p.crop_name:<8}  {label:<6}  {p.remaining_quantity:>6}    "
                f"{left:<14}  {_local(p.ready_at)}{mark} {genes}".rstrip()
            )
        return "\n".join(lines)

    # ---------- 交易與道具 ----------

    def _shop(self, args: list[str]) -> str:
        lines = ["代碼        名稱    種子價  生長時間     收成數  單位售價"]
        for c in self.service.get_market_prices():
            lines.append(
                f"{c.code:<11} {c.name:<6}  {c.seed_price:>5}   {rules.format_duration(c.grow_seconds):<10}  "
                f"{c.harvest_quantity:>5}   {c.sell_price:>6}"
            )
        lines.append("道具：")
        for code, (name, price, _kind, desc, _aliases) in balance.ITEMS.items():
            lines.append(f"  {code:<13} {name:<8} {price:>5} 金幣  {desc}")
        lines.append(
            f"擴建土地：{balance.UPGRADE_COST} 金幣 / +{balance.UPGRADE_PLOTS_ADDED} 格（最多 {balance.MAX_PLOTS} 格）"
        )
        return "\n".join(lines)

    def _buy(self, args: list[str]) -> str:
        self._need(args, 2, "buy <作物|道具> <數量>")
        result = self.service.buy(self._pid(), args[0], _parse_int(args[1], "數量"))
        return f"{result.message} 剩餘金幣 {result.gold}。"

    def _plant(self, args: list[str]) -> str:
        self._need(args, 2, "plant <土地編號> <作物>")
        return self.service.plant_crop(self._pid(), _parse_int(args[0], "土地編號"), args[1]).message

    def _each_plot(self, args: list[str], usage: str, state: PlotState, empty_msg: str, action) -> str:
        self._need(args, 1, usage)
        pid = self._pid()
        if args[0].lower() != "all":
            return action(pid, _parse_int(args[0], "土地編號")).message
        targets = [p.plot_number for p in self.service.get_farm_status(pid).plots if p.state == state]
        if not targets:
            return empty_msg
        lines = []
        for number in targets:
            try:
                lines.append(action(pid, number).message)
            except GameError as exc:
                lines.append(f"[失敗] {exc}")
        return "\n".join(lines)

    def _water(self, args: list[str]) -> str:
        return self._each_plot(
            args, "water <土地編號|all>", PlotState.WAITING_WATER, "目前沒有需要澆水的土地。", self.service.water_crop
        )

    def _harvest(self, args: list[str]) -> str:
        return self._each_plot(
            args, "harvest <土地編號|all>", PlotState.READY, "目前沒有可收成的作物。", self.service.harvest_crop
        )

    def _clear(self, args: list[str]) -> str:
        self._need(args, 1, "clear <土地編號>")
        return self.service.clear_withered(self._pid(), _parse_int(args[0], "土地編號")).message

    def _use(self, args: list[str]) -> str:
        if len(args) not in (1, 2):
            raise ValidationError("用法：use fertilizer <土地編號> | use earthworm")
        plot = _parse_int(args[1], "土地編號") if len(args) == 2 else None
        return self.service.use_item(self._pid(), args[0], plot).message

    def _tool(self, args: list[str]) -> str:
        if len(args) != 2 or args[0].lower() not in ("on", "off"):
            raise ValidationError("用法：tool on|off auto_waterer")
        return self.service.set_tool_enabled(self._pid(), args[1], args[0].lower() == "on").message

    def _sell(self, args: list[str]) -> str:
        if not 2 <= len(args) <= 4:
            raise ValidationError("用法：sell <作物|商品> <數量> [品質] [突變]")
        quality, mutation = "normal", "none"
        for token in args[2:]:
            q, m = genetics.parse_quality(token), genetics.parse_mutation(token)
            if q is not None:
                quality = q
            elif m is not None and m != "variant":  # 珍稀變種以變種名稱販售
                mutation = m
            else:
                raise ValidationError(f"看不懂「{token}」：品質為 普通／優良／稀有／傳說，突變為 巨型／黃金／快速。")
        result = self.service.sell(self._pid(), args[0], _parse_int(args[1], "數量"), quality, mutation)
        return f"{result.message} 目前金幣 {result.gold}。"

    # ---------- 作物品質與突變 ----------

    def _market(self, args: list[str]) -> str:
        if args and [a.lower() for a in args] != ["prices"]:
            raise ValidationError("用法：market prices")
        lines = ["===== 市場價格（單價，依品質與突變；巨型作物增加數量、快速生長只影響時間，單價同無突變）====="]
        header = "作物        " + "".join(f"{balance.QUALITIES[q]:<8}" for q in balance.QUALITIES)
        lines.append(header + "黃金作物（普通→傳說）")
        for c in self.service.crop_catalog():
            normal = "".join(f"{c['prices'][q]['none']:<10}" for q in balance.QUALITIES)
            golden = "／".join(str(c["prices"][q]["golden"]) for q in balance.QUALITIES)
            lines.append(f"{c['name']:<10}{normal}{golden}")
            variant = "".join(f"{c['prices'][q]['variant']:<10}" for q in balance.QUALITIES)
            lines.append(f"  └{c['variant_name']:<8}{variant}")
        lines.append("最終單價 = 基本售價 × 品質倍率 × 突變售價倍率（無條件捨去為整數）")
        return "\n".join(lines)

    def _crop_cmd(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "info" and len(args) == 2:
            c = self.service.find_crop_info(args[1])
            prices = "、".join(f"{balance.QUALITIES[q]} {c['prices'][q]['none']}" for q in balance.QUALITIES)
            return "\n".join(
                [
                    f"===== {c['name']}（{c['code']}）=====",
                    f"種子價格：{c['seed_price']}　生長時間：{rules.format_duration(c['grow_seconds'])}"
                    f"（快速生長突變：{rules.format_duration(c['fast_grow_seconds'])}）",
                    f"每批收成：{c['harvest_quantity']} 個（巨型作物：{c['giant_quantity']} 個）",
                    f"基本售價：{c['sell_price']}；各品質單價：{prices}",
                    "黃金作物單價："
                    + "、".join(f"{balance.QUALITIES[q]} {c['prices'][q]['golden']}" for q in balance.QUALITIES),
                    f"珍稀變種：{c['variant_name']}（{c['variant_code']}），單價："
                    + "、".join(f"{balance.QUALITIES[q]} {c['prices'][q]['variant']}" for q in balance.QUALITIES),
                ]
            )
        if sub == "quality" and len(args) == 1:
            t = self.service.quality_table(self.player_id)
            lines = [
                "===== 品質 =====",
                "品質    基礎機率   售價倍率" + ("   我的實際機率" if t["effective_bp"] else ""),
            ]
            for q, name in balance.QUALITIES.items():
                mine = f"   {t['effective_bp'][q] / 100:>6.2f}%" if t["effective_bp"] else ""
                lines.append(f"{name:<6}  {t['base_bp'][q] / 100:>6.2f}%   ×{t['price_pct'][q] / 100:<6g}{mine}")
            lines.append(
                f"農場每高 1 級，{t['level_bonus_bp'] / 100:g}% 機率從普通依比例移到較高品質，"
                f"上限 {t['level_bonus_max_bp'] / 100:g}%；總和固定 100%。"
            )
            if t["level"]:
                lines.append(f"你的農場等級：{t['level']}")
            lines.append("品質與突變在播種時決定，之後不會因收成、偷菜或重新查詢而改變。")
            return "\n".join(lines)
        if sub == "mutations" and len(args) == 1:
            lines = ["===== 突變（每批最多一個主要突變，播種時決定）====="]
            lines += [f"{m['name']:<6}  {m['bp'] / 100:>5.2f}%  {m['effect']}" for m in self.service.mutation_table()]
            return "\n".join(lines)
        raise ValidationError("用法：crop info <作物> | crop quality | crop mutations")

    # ---------- 動物養殖 ----------

    def _event(self, args: list[str]) -> str:
        if args not in ([], ["list"]):
            raise ValidationError("用法：event list")
        events = self.service.active_events()
        if not events:
            return "目前沒有進行中的限時活動。"
        lines = ["===== 進行中的限時活動 ====="]
        for e in events:
            target = f"（限{e['crop_name']}）" if e["crop_name"] else "（全部作物）"
            lines.append(f"#{e['id']} {e['name']}{target}：{e['effects_text']}｜{e['schedule_text']}")
            if e["description"]:
                lines.append(f"    {e['description']}")
        lines.append("加成在操作當下判斷：播種時決定品質／突變／收成量，澆水時決定生長時間，販售時決定單價。")
        return "\n".join(lines)

    def _animal(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "shop" and len(args) == 1:
            lines = ["===== 動物商店 =====", "代碼      名稱  價格    餵食費用  生產週期   每次產量      產品售價"]
            for a in self.service.animal_types():
                lines.append(
                    f"{a['code']:<9} {a['name']:<3} {a['price']:>6}  {a['feed_cost']:>7}   "
                    f"{rules.format_duration(a['cycle_seconds']):<9}  "
                    f"{a['product_quantity']} 個{a['product_name']:<6}  {a['product_price']:>6}"
                )
            bname, bprice, bmax, _ba = balance.BUILDINGS["barn"]
            lines.append(
                f"動物須安置在{bname}：每座 {bprice} 金幣、{balance.BARN_SLOTS} 個欄位、最多 {bmax} 座"
                f"（workshop build {bname}）。"
            )
            return "\n".join(lines)
        if sub == "buy" and len(args) == 3:
            result = self.service.buy_animals(self._pid(), args[1], _parse_int(args[2], "數量"))
            return f"{result.message} 剩餘金幣 {result.gold}。"
        if sub == "list" and len(args) == 1:
            animals = self.service.list_animals(self._pid())
            if not animals:
                return "目前沒有動物。可用 animal shop 查看動物商店。"
            lines = ["編號  動物  狀態     剩餘時間        可收集時間      產品"]
            for a in animals:
                left = rules.format_duration(a["remaining_seconds"]) if a["state"] == "producing" else "-"
                lines.append(
                    f"{a['animal_no']:<5} {a['name']:<3}  {a['state_label']:<6}  {left:<14}  "
                    f"{_local(a['ready_at']):<14}  {a['product_quantity']} 個{a['product_name']}"
                )
            return "\n".join(lines)
        if sub == "feed" and len(args) == 2:
            if args[1].lower() == "all":
                result = self.service.feed_all(self._pid())
            else:
                result = self.service.feed_animal(self._pid(), _parse_int(args[1], "動物編號"))
            return f"{result.message} 剩餘金幣 {result.gold}。"
        if sub == "collect" and len(args) == 2:
            return self.service.collect_animal(self._pid(), _parse_int(args[1], "動物編號")).message
        if sub == "status" and len(args) == 1:
            st = self.service.animal_status(self._pid())
            c = st["counts"]
            return (
                f"養殖容量：{st['used']}/{st['capacity']}（畜舍每座 {balance.BARN_SLOTS} 欄位）\n"
                f"待餵食 {c['hungry']}、生產中 {c['producing']}、可收集 {c['ready']}\n"
                f"餵食全部待餵食動物需要 {st['feed_cost_all']} 金幣（目前金幣 {st['gold']}）\n"
                f"下次可收集：{_local(st['next_ready_at'])}"
            )
        raise ValidationError(
            "用法：animal shop | animal buy <動物> <數量> | animal list | animal feed <編號|all> | "
            "animal collect <編號> | animal status"
        )

    # ---------- 設施與加工 ----------

    def _workshop(self, args: list[str]) -> str:
        if len(args) == 2 and args[0].lower() == "build":
            result = self.service.build(self._pid(), args[1])
            return f"{result.message} 剩餘金幣 {result.gold}。"
        if args:
            raise ValidationError("用法：workshop | workshop build <設施名稱>")
        st = self.service.workshop_status(self._pid())
        lines = ["===== 設施 =====", "設施        價格    持有/上限  狀態"]
        for code, (name, price, max_count, _a) in balance.BUILDINGS.items():
            owned = st["counts"][code]
            if code == "barn":
                state = f"養殖容量 {st['animals']}/{st['capacity']}"
            elif not owned:
                state = "未建造"
            elif st["busy"][code]:
                b = st["busy"][code]
                left = rules.format_duration(b["remaining_seconds"])
                state = f"加工中：訂單 #{b['order_id']} {b['recipe_name']}，剩 {left}"
            else:
                state = "閒置"
            lines.append(f"{name:<8} {price:>6}  {owned:>3}/{max_count:<5}  {state}")
        lines.append("建造：workshop build <設施名稱>")
        return "\n".join(lines)

    def _recipe(self, args: list[str]) -> str:
        if not args or args[0].lower() != "list" or len(args) != 1:
            raise ValidationError("用法：recipe list")
        lines = ["===== 配方 ====="]
        for r in self.service.recipes(self.player_id):
            inputs = "、".join(f"{k} x{v}" for k, v in r["inputs"].items())
            built = "已建造" if r["built"] else "未建造"
            lines.append(
                f"{r['name']}（{r['code']}）：{inputs} → {r['output_quantity']} 份{r['output_name']}，"
                f"{rules.format_duration(r['seconds'])}／份，{r['building_name']}（{built}），售價 {r['sell_price']}"
            )
        lines.append(f"每座設施一次一張訂單，單張最多 {balance.CRAFT_MAX_QUANTITY} 份：craft <配方> <份數>")
        return "\n".join(lines)

    def _craft(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "status" and len(args) == 1:
            orders = self.service.craft_orders(self._pid())
            if not orders:
                return "目前沒有加工中或待領取的訂單。"
            lines = ["訂單  配方        份數  狀態    剩餘時間        完成時間"]
            for o in orders:
                left = rules.format_duration(o["remaining_seconds"]) if o["state"] == "in_progress" else "-"
                lines.append(
                    f"#{o['order_id']:<4} {o['recipe_name']:<8}  {o['output_quantity']:>4}  {o['state_label']:<5}  "
                    f"{left:<14}  {_local(o['ready_at'])}"
                )
            return "\n".join(lines)
        if sub == "collect" and len(args) == 2:
            return self.service.collect_craft(self._pid(), _parse_int(args[1].lstrip("#"), "訂單編號")).message
        if len(args) == 2:
            return self.service.start_craft(self._pid(), args[0], _parse_int(args[1], "份數")).message
        raise ValidationError("用法：craft <配方> <份數> | craft status | craft collect <訂單ID>")

    # ---------- 背包 ----------

    def _inventory(self, args: list[str]) -> str:
        sub = args[0].lower() if args else "all"
        if len(args) > 1 or sub not in ("all", "items", "tools"):
            raise ValidationError("用法：inventory | inventory items | inventory tools")
        bp = self.service.get_backpack(self._pid())
        lines = [f"===== 背包（倉庫 {bp.used}/{bp.capacity}）====="]
        if sub == "all" and bp.is_empty:
            lines.append("目前背包是空的。")
            return "\n".join(lines)
        if sub in ("all", "items"):
            lines += self._items_text(bp)
        if sub in ("all", "tools"):
            lines += self._tools_text(bp)
        return "\n".join(lines)

    @staticmethod
    def _items_text(bp: Backpack) -> list[str]:
        if not (bp.seeds or bp.produce or bp.consumables or bp.goods):
            return ["【物品】目前沒有任何可堆疊物品。"]
        lines = []
        if bp.seeds:
            lines.append("【種子】")
            lines += [f"  {x.name:<10} x {x.quantity:<4} 商店售價 {x.price}" for x in bp.seeds]
        if bp.produce:
            lines.append("【農產品】")
            lines += [
                f"  {x.name + genetics_label(x.quality, x.mutation):<16} x {x.quantity:<4} 單價 {x.price}"
                f"  品質：{balance.QUALITIES[x.quality]}  突變：{balance.MUTATIONS[x.mutation]}"
                for x in bp.produce
            ]
        if bp.goods:
            lines.append("【動物產品與加工品】")
            lines += [f"  {x.name:<10} x {x.quantity:<4} 售價 {x.price}" for x in bp.goods]
        if bp.consumables:
            lines.append("【消耗型道具】")
            lines += [f"  {x.name:<10} x {x.quantity:<4} {x.description}" for x in bp.consumables]
        return lines

    @staticmethod
    def _tools_text(bp: Backpack) -> list[str]:
        lines = ["【永久工具】"]
        for t in bp.tools:
            if t.owned:
                enabled = "已啟用" if t.enabled else "已停用"
                lines.append(f"  {t.name}：已永久持有，{enabled}（取得於 {_local(t.acquired_at)}）")
            else:
                lines.append(f"  {t.name}：尚未購買")
        lines.append("【限時效果】")
        for e in bp.effects:
            status = EFFECT_STATUS_LABELS[e.status]
            if e.status == "inactive":
                lines.append(f"  {e.name}：{status}")
                continue
            left = rules.format_duration(e.remaining_seconds) if e.status == "active" else "0 秒"
            lines.append(
                f"  {e.name}：{status}｜開始 {_local(e.started_at)}｜到期 {_local(e.expires_at)}｜"
                f"剩餘 {left}｜效果：生長時間 ×{e.multiplier}"
            )
        return lines

    def _log(self, args: list[str]) -> str:
        limit = _parse_int(args[0], "筆數") if args else 10
        txs = self.service.get_recent_transactions(self._pid(), limit)
        if not txs:
            return "目前沒有操作紀錄。"
        lines = []
        for t in txs:
            delta = f"{t.gold_delta:+d}" if t.gold_delta else "0"
            label = EVENT_TYPE_LABELS.get(t.event_type, t.event_type)
            lines.append(f"{_local(t.created_at)}  {label:<6}  金幣 {delta:>6}  {t.description}")
        return "\n".join(lines)

    # ---------- 群組 ----------

    def _group(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "create" and len(args) >= 2:
            group = self.service.create_group(" ".join(args[1:]))
            return f"已建立群組 #{group.id}「{group.name}」。用 group use {group.id} 選擇此群組。"
        if sub == "list":
            keyword, page = _page_args(args[1:])
            groups = self.service.list_groups(self.player_id)
            if not groups:
                return "目前沒有群組，請用 group create <群組名稱> 建立。"
            if keyword:
                groups = [g for g in groups if keyword.lower() in g.name.lower() or keyword == str(g.id)]
                if not groups:
                    return f"找不到名稱或 ID 符合「{keyword}」的群組。"
            shown, page = _slice(groups, page)
            lines = ["編號  群組名稱              成員數  目前玩家"]
            for g in shown:
                mark = " *目前" if g.id == self.group_id else ""
                joined = "已加入" if g.is_member else "-"
                lines.append(f"{g.id:<5} {g.name:<20} {g.member_count:>5}  {joined}{mark}")
            lines.append(_page_footer("group list", keyword, page, len(groups)))
            return "\n".join(lines)
        if sub == "use" and len(args) == 2:
            group = self.service.get_group(_parse_int(args[1], "群組編號"))
            self.group_id = group.id
            note = ""
            if self.player_id is not None and not self.service.is_member(group.id, self.player_id):
                note = "（目前玩家不是此群組成員，無法偷菜或查看群組排行榜）"
            return f"目前群組：#{group.id}「{group.name}」{note}"
        if sub in ("add", "remove") and len(args) == 2:
            gid = self._gid()
            player_id = _parse_int(args[1], "玩家編號")
            if sub == "add":
                added = self.service.add_member(gid, player_id)
                return f"已將玩家 #{player_id} 加入群組 #{gid}。" if added else f"玩家 #{player_id} 已經是群組成員。"
            removed = self.service.remove_member(gid, player_id)
            if not removed:
                return f"玩家 #{player_id} 不是群組 #{gid} 的成員。"
            return f"已將玩家 #{player_id} 移出群組 #{gid}（其農場、金幣與紀錄均保留）。"
        if sub == "members":
            keyword, page = _page_args(args[1:])
            members = self.service.group_members(self._gid())
            if not members:
                return "這個群組目前沒有成員，請用 group add <玩家ID> 加入。"
            if keyword:
                members = [
                    m for m in members if keyword.lower() in m["player_name"].lower() or keyword == str(m["player_id"])
                ]
                if not members:
                    return f"找不到名稱或 ID 符合「{keyword}」的成員。"
            shown, page = _slice(members, page)
            lines = ["玩家ID  名稱              農場              等級  金幣    可收成"]
            for m in shown:
                f = m["farm"]
                if f is None:
                    lines.append(f"{m['player_id']:<7} {m['player_name']:<16}  （尚未建立農場）")
                else:
                    lines.append(
                        f"{m['player_id']:<7} {m['player_name']:<16}  {f['farm_name']:<16}  "
                        f"{f['level']:>3}  {f['gold']:>6}  {f['ready']:>5}"
                    )
            lines.append(_page_footer("group members", keyword, page, len(members)))
            return "\n".join(lines)
        if sub == "status" and len(args) == 1:
            g = self.service.get_group(self._gid())
            guild = g.discord_guild_id or "（尚未對應）"
            return (
                f"群組 #{g.id}「{g.name}」\n成員數：{g.member_count}\n"
                f"建立時間：{_local(g.created_at)}\nDiscord Guild ID：{guild}"
            )
        raise ValidationError(
            "用法：group create <名稱> | group list [關鍵字] [頁碼] | group use <ID> | group add <玩家ID> | "
            "group remove <玩家ID> | group members [關鍵字] [頁碼] | group status"
        )

    def _steal(self, args: list[str]) -> str:
        if args and args[0].lower() == "targets" and len(args) <= 2:
            _keyword, page = _page_args(args[1:])
            targets = self.service.steal_targets(self._gid(), self._pid())
            if not targets:
                return "目前群組沒有可以偷取的作物。"
            all_count = len(targets)
            targets, page = _slice(targets, page, STEAL_PAGE_SIZE)
            lines = ["========== 可偷菜目標 =========="]
            for t in targets:
                lines += [
                    "",
                    f"玩家：{t.player_name}（ID {t.player_id}）",
                    f"農場：{t.farm_name}",
                    f"土地：第 {t.plot_number} 格",
                    f"作物：{t.crop_name}",
                    f"剩餘數量：{t.remaining_quantity}",
                    f"本次最多可偷：{t.max_steal}",
                ]
            lines += ["", "================================"]
            if all_count > STEAL_PAGE_SIZE:
                lines.append(_page_footer("steal targets", None, page, all_count, STEAL_PAGE_SIZE))
            return "\n".join(lines)
        if len(args) == 3:
            victim = _parse_int(args[0], "玩家編號")
            plot = _parse_int(args[1], "土地編號")
            quantity = _parse_int(args[2], "數量")
            return self.service.steal_crop(self._gid(), self._pid(), victim, plot, quantity).message
        raise ValidationError("用法：steal targets [頁碼] | steal <玩家ID> <土地編號> <數量>")

    # ---------- 防守 ----------

    def _defense(self, args: list[str]) -> str:
        sub = args[0].lower() if args else "status"
        if sub == "shop" and len(args) == 1:
            return self._defense_shop()
        if sub == "status" and len(args) <= 1:
            return self._defense_status()
        if sub == "buy" and len(args) in (2, 3):
            pid = self._pid()
            if self.service.find_defense(args[1]) is not None:
                if len(args) == 3 and args[2] != "1":
                    raise ValidationError("防守設施一次只能購買 1 個。")
                result = self.service.buy_defense(pid, args[1])
            elif self.service.find_item(args[1]) == balance.ITEM_PATROL_TICKET:
                qty = _parse_int(args[2], "數量") if len(args) == 3 else 1
                result = self.service.buy_item(pid, balance.ITEM_PATROL_TICKET, qty)
            else:
                raise ValidationError(f"沒有「{args[1]}」這種防守設施，可用 defense shop 查看。")
            return f"{result.message} 剩餘金幣 {result.gold}。"
        if sub == "place" and len(args) in (3, 4):
            start = _parse_int(args[2], "起始土地")
            slot = _parse_int(args[3], "編號") if len(args) == 4 else 1
            return self.service.place_defense(self._pid(), args[1], start, slot).message
        if sub == "log" and len(args) == 1:
            return self._defense_log()
        raise ValidationError(
            "用法：defense | defense shop | defense status | defense buy <設施|巡查券> [數量] | "
            "defense place <稻草人|圍欄> <起始土地> [編號] | defense log"
        )

    @staticmethod
    def _defense_shop() -> str:
        lines = ["===== 防守商店 =====", "代碼        名稱        價格   上限  阻止機率  守護範圍"]
        for code, (name, price, max_count, chance, range_size, _aliases) in balance.DEFENSES.items():
            area = "全部土地" if range_size is None else f"連續 {range_size} 格（可調整）"
            lines.append(f"{code:<11} {name:<8} {price:>6}  {max_count:>4}  {chance:>7.0%}   {area}")
        name, price, _kind, desc, _aliases = balance.ITEMS[balance.ITEM_PATROL_TICKET]
        lines.append(f"{balance.ITEM_PATROL_TICKET:<11} {name:<8} {price:>6}     -        -   {desc}")
        order = " → ".join(balance.DEFENSES[c][0] for c in balance.DEFENSE_ORDER)
        lines.append(
            f"判定順序：{order}；每次偷菜只判定一輪，同種設施重疊只計算一次，"
            f"整體阻止機率上限 {balance.DEFENSE_MAX_BLOCK_CHANCE:.0%}。"
        )
        return "\n".join(lines)

    def _defense_status(self) -> str:
        st = self.service.defense_status(self._pid())
        lines = ["===== 防守狀態 ====="]
        for code, (name, _price, max_count, chance, _range, _aliases) in balance.DEFENSES.items():
            lines.append(f"{name}：持有 {st['owned'][code]}/{max_count}（阻止機率 {chance:.0%}）")
        for d in st["devices"]:
            area = "全部土地" if d["end_plot"] is None else f"第 {d['start_plot']}–{d['end_plot']} 格"
            lines.append(f"  · {d['name']} #{d['slot']}：守護{area}")
        lines.append("各土地阻止機率：")
        for p in st["plots"]:
            names = "、".join(balance.DEFENSES[c][0] for c in p["devices"]) or "無防守"
            lines.append(f"  第 {p['plot_number']:>2} 格：{p['chance']:>4.0%}（{names}）")
        return "\n".join(lines)

    def _defense_log(self) -> str:
        rows = self.service.defense_log(self._pid())
        if not rows:
            return "目前沒有被阻止的偷菜紀錄。"
        lines = ["===== 防守紀錄（被阻止的偷菜；偷菜者需使用巡查券查看）====="]
        for r in rows:
            device = balance.DEFENSES[r["blocked_by"]][0]
            lines.append(
                f"{_local(r['created_at'])}  第 {r['plot_number']} 格 {r['crop_name']}  "
                f"有人想偷 {r['quantity']} 個，被{device}阻止"
            )
        return "\n".join(lines)

    def _patrol(self, args: list[str]) -> str:
        if args:
            raise ValidationError("用法：patrol（只能巡查自己的農場）")
        rows = self.service.patrol(self._pid())
        lines = [f"===== 巡查報告（近 {balance.PATROL_DAYS} 天，已消耗 1 張巡查券）====="]
        if not rows:
            lines.append("近期沒有任何偷菜嘗試。")
        for r in rows:
            if r["outcome"] == "success":
                result = f"偷走 {r['quantity']} 個"
            else:
                result = f"想偷 {r['quantity']} 個，被{balance.DEFENSES[r['blocked_by']][0]}阻止"
            lines.append(
                f"{_local(r['created_at'])}  {r['thief_name']}（ID {r['thief_player_id']}）"
                f"［{r['group_name']}］第 {r['plot_number']} 格 {r['crop_name']}：{result}"
            )
        return "\n".join(lines)

    # ---------- 群組任務 ----------

    @staticmethod
    def _quest_line(q: dict) -> str:
        return (
            f"#{q['quest_id']:<4} ［{q['group_name']}］{q['name']}  {q['progress']}/{q['target']}（{q['percent']}%）"
            f"  {q['state_label']}  截止 {_local(q['ends_at'])}"
        )

    def _quest(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "list" and len(args) == 1:
            quests = self.service.player_quests(self._pid())
            if not quests:
                return "你所屬的群組目前沒有進行中或待領獎的任務。"
            return "\n".join(
                ["===== 我的群組任務 ====="]
                + [f"{self._quest_line(q)}  我的貢獻 {q['my_contribution']}" for q in quests]
            )
        if sub == "group" and len(args) == 1:
            quests = self.service.group_quests(self._gid(), self._pid())
            if not quests:
                return "目前群組沒有任何任務。"
            return "\n".join(["===== 目前群組的任務 ====="] + [self._quest_line(q) for q in quests])
        if sub == "status" and len(args) == 2:
            q = self.service.quest_status(self._gid(), self._pid(), _parse_int(args[1].lstrip("#"), "任務編號"))
            lines = [
                f"===== 任務 #{q['quest_id']}「{q['name']}」（{q['state_label']}）=====",
                f"群組：{q['group_name']}",
            ]
            if q["description"]:
                lines.append(f"說明：{q['description']}")
            lines += [
                f"目標：{q['goal_text']}",
                f"進度：{q['progress']}/{q['target']}（{q['percent']}%）",
                f"期間：{_local(q['starts_at'])} ～ {_local(q['ends_at'])}",
                f"獎勵：{q['reward_text']}",
            ]
            if q["cancel_reason"]:
                lines.append(f"取消原因：{q['cancel_reason']}")
            lines.append(f"我的貢獻：{q['my_contribution']}")
            if q["my_claim"]:
                lines.append(f"已領取獎勵：{q['my_claim']['amount']}（{_local(q['my_claim']['claimed_at'])}）")
            elif q["state"] == "completed" and q["my_reward"]:
                lines.append(f"可領取獎勵：{q['my_reward']}（quest claim {q['quest_id']}）")
            lines.append("貢獻排行：")
            lines += [f"  {i}. {name}：{amount}" for i, (_pid, name, amount) in enumerate(q["contributors"], 1)]
            if not q["contributors"]:
                lines.append("  （尚無貢獻）")
            return "\n".join(lines)
        if sub == "claim" and len(args) == 2:
            result = self.service.claim_quest(self._gid(), self._pid(), _parse_int(args[1].lstrip("#"), "任務編號"))
            return f"{result.message} 目前金幣 {result.gold}。"
        raise ValidationError("用法：quest list | quest group | quest status <任務ID> | quest claim <任務ID>")

    # ---------- 群組競賽 ----------

    def _competition(self, args: list[str]) -> str:
        sub = args[0].lower() if args else ""
        if sub == "list" and len(args) == 1:
            comps = self.service.competitions()
            if not comps:
                return "目前沒有競賽。"
            lines = ["===== 群組競賽 ====="]
            for c in comps:
                lines.append(
                    f"#{c['competition_id']:<4} {c['name']}（{c['type_name']}）  {c['state_label']}  "
                    f"{_local(c['starts_at'])} ～ {_local(c['ends_at'])}"
                )
            return "\n".join(lines)
        if sub in ("status", "leaderboard") and len(args) == 2:
            c = self.service.competition_detail(_parse_int(args[1].lstrip("#"), "競賽編號"), self.player_id)
            return self._competition_status(c) if sub == "status" else self._competition_board(c)
        if sub == "history" and len(args) == 1:
            history = self.service.competition_history(self.player_id)
            if not history:
                return "目前沒有已結算的競賽。"
            blocks = []
            for c in history:
                top = (
                    "、".join(f"第 {r['rank']} 名 {r['group_name']}（{r['score']}）" for r in c["ranking"][:3])
                    or "無人參賽"
                )
                mine = ""
                if c.get("my_award"):
                    mine = f"｜我獲得第 {c['my_award']['rank']} 名獎勵 {c['my_award']['gold']} 金幣"
                blocks.append(f"#{c['competition_id']} {c['name']}（結算於 {_local(c['settled_at'])}）：{top}{mine}")
            return "\n".join(["===== 競賽歷史（最終排名）====="] + blocks)
        raise ValidationError(
            "用法：competition list | competition status <競賽ID> | competition leaderboard <競賽ID> | "
            "competition history"
        )

    def _competition_status(self, c: dict) -> str:
        rewards = "、".join(f"第 {i} 名 {g} 金幣／人" for i, g in enumerate(c["rewards"], 1))
        lines = [
            f"===== 競賽 #{c['competition_id']}「{c['name']}」（{c['state_label']}）=====",
            f"類型：{c['type_name']}",
            f"計分規則：{c['scoring_rule']}；只計入在主要群組中進行的操作",
            f"期間：{_local(c['starts_at'])} ～ {_local(c['ends_at'])}",
            f"結算獎勵：{rewards}（發給該群組中以此為主要群組的全體成員）",
        ]
        if c["description"]:
            lines.insert(2, f"說明：{c['description']}")
        if c["cancel_reason"]:
            lines.append(f"取消原因：{c['cancel_reason']}")
        if self.player_id is not None:
            lines.append(f"我的貢獻：{c.get('my_points', 0)} 分")
            home = c.get("my_home_group_id")
            mine = next((r for r in c["ranking"] if r["group_id"] == home), None)
            if mine:
                lines.append(f"我的主要群組：第 {mine['rank']} 名，{mine['score']} 分")
            if c.get("my_award"):
                lines.append(f"我的獎勵：第 {c['my_award']['rank']} 名 {c['my_award']['gold']} 金幣（已發放）")
        return "\n".join(lines)

    @staticmethod
    def _competition_board(c: dict) -> str:
        kind = "最終排名" if c["final"] else "即時排名"
        lines = [
            f"===== 競賽 #{c['competition_id']}「{c['name']}」{kind}（{c['state_label']}）=====",
            "排名  群組              分數   貢獻人數",
        ]
        lines += [
            f"{r['rank']:>3}   {r['group_name']:<16} {r['score']:>6}   {r['contributors']:>6}" for r in c["ranking"]
        ]
        if not c["ranking"]:
            lines.append("（尚無群組得分）")
        lines.append("個人貢獻：")
        lines += [
            f"  {i}. {p['player_name']}（{p['group_name']}）：{p['points']} 分"
            for i, p in enumerate(c["top_players"], 1)
        ]
        if not c["top_players"]:
            lines.append("  （尚無貢獻）")
        return "\n".join(lines)

    # ---------- 排行榜 ----------

    def _leaderboard(self, args: list[str]) -> str:
        if args and args[0].lower() in ("groups", "rewards"):
            # 查看排行時順便結算已到期的排名獎勵（網頁服務也會在背景定期結算）
            self.service.settle_due_rankings()
            return self._group_leaderboard(args[1:]) if args[0].lower() == "groups" else self._ranking_rewards(args[1:])
        if not args or args[0].lower() not in ("global", "group") or len(args) > 2:
            raise ValidationError(
                "用法：leaderboard global|group [money|sales|goods] | leaderboard groups [money|sales|goods] "
                "[all|day|week|month] | leaderboard rewards"
            )
        scope = args[0].lower()
        kinds = [args[1].lower()] if len(args) == 2 else ["money", "sales"]
        if any(k not in ("money", "sales", "goods") for k in kinds):
            raise ValidationError("排行榜種類只能是 money、sales 或 goods。")
        group_id = None
        title = "總排行榜"
        if scope == "group":
            group_id = self._gid()
            pid = self._pid()
            g = self.service.get_group(group_id)
            title = f"群組排行榜｜#{g.id}「{g.name}」"
        else:
            pid = self.player_id
        blocks = []
        for kind in kinds:
            rows = self.service.leaderboard(kind, group_id=group_id, player_id=pid)
            if kind == "money":
                lines = [f"===== {title}：金幣 =====", "排名  玩家              等級  目前金幣"]
                lines += [f"{r['rank']:>3}   {r['player_name']:<16}  {r['level']:>3}  {r['gold']:>8}" for r in rows]
            else:
                label = "農產品販售（作物）" if kind == "sales" else "商品總販售（作物＋動物產品＋加工品，依收入）"
                lines = [f"===== {title}：{label} =====", "排名  玩家              販售數量  販售收入  目前金幣"]
                lines += [
                    f"{r['rank']:>3}   {r['player_name']:<16}  {r['sold_quantity']:>7}  "
                    f"{r['sales_income']:>8}  {r['gold']:>8}"
                    for r in rows
                ]
            if not rows:
                lines.append("（目前沒有玩家資料）")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _group_leaderboard(self, args: list[str]) -> str:
        kind = args[0].lower() if args else "money"
        period = args[1].lower() if len(args) > 1 else "all"
        if len(args) > 2 or kind not in ("money", "sales", "goods") or period not in ranking_rules.VIEW_PERIODS:
            raise ValidationError("用法：leaderboard groups [money|sales|goods] [all|day|week|month]")
        rows = self.service.group_leaderboard(kind, period)
        metric = ranking_rules.METRICS[kind]
        if kind == "money":
            metric = "目前金幣加總" if period == "all" else "期間淨賺金幣"
        lines = [
            f"===== 群組總排行：{metric}（{ranking_rules.VIEW_PERIODS[period]}）=====",
            "排名  群組                成員   分數        目前金幣",
        ]
        lines += [
            f"{r['rank']:>3}   #{r['group_id']} {r['group_name']:<14}  {r['member_count']:>3}  "
            f"{r['score']:>10}  {r['gold']:>10}"
            for r in rows
        ]
        if not rows:
            lines.append("（目前沒有群組資料）")
        lines.append("群組分數 = 主要所屬群組為該群組的成員加總；農產品販售依數量、商品總販售依收入。")
        return "\n".join(lines)

    def _ranking_rewards(self, args: list[str]) -> str:
        if args:
            raise ValidationError("用法：leaderboard rewards")
        rules = [r for r in self.service.list_ranking_rules() if r["enabled"]]
        lines = ["===== 排名獎勵 ====="]
        for r in rules:
            lines.append(f"#{r['id']} {r['name']}｜{r['scope_label']}・{r['metric_label']}・{r['period_label']}結算")
            lines.append(f"    {r['rewards_text']}")
        if not rules:
            lines.append("目前沒有進行中的排名獎勵。")
        if self.player_id is not None:
            awards = self.service.player_ranking_awards(self.player_id)
            lines.append("----- 我最近獲得的排名獎勵 -----")
            lines += [f"{a['rule_name']}（{a['period']}）第 {a['rank']} 名：{a['reward_text']}" for a in awards]
            if not awards:
                lines.append("（尚未獲得）")
        return "\n".join(lines)

    def _exit(self, args: list[str]) -> str:
        raise ExitRequested
