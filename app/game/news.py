"""農場日報：把群組前一天（台灣日期）的交易紀錄整理成八卦小報風格的頭條（純規則，不碰資料庫）。

只報導所屬群組（farms.home_group_id）是這個群組的農場。日報顯示在 /farm 總覽頁的最下方（只有開面板的人看得到），
Bot 不會主動發訊息到頻道，也沒有額外的按鈕。

農場傳聞：每天約一半的機率，日報多一條「某座農場的田底下埋了寶藏」的傳聞（主角是前一天有動靜的成員）。
傳聞不一定是真的；當天偷那座農場的菜成功時會順手挖一次（每人每天一次），傳聞是真的就有機率挖到寶藏，
寶藏只有第一個挖到的人拿得到。傳聞由（群組, 日期, 前一天有動靜的農場）決定，不必存資料庫，日報與挖寶看到的一定相同。
"""

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.clock import TAIPEI_TZ
from app.game import balance

MAX_HEADLINES = 4  # 總覽頁只放最重要的幾條，避免面板太長
FIELD_LIMIT = 1024  # Discord Embed 欄位上限
RUMOR_CHANCE = 0.5  # 每天出現傳聞的機率
RUMOR_TRUE_CHANCE = 0.7  # 傳聞是真的機率
DIG_CHANCE = 0.35  # 傳聞是真的時，每次挖寶挖到的機率
TREASURE_GOLD = (300, 800)
RUMOR_TEMPLATES = (
    "🗺️ 傳聞：據說 {who} 的田底下埋著前任農場主人的寶藏…（真假未知，偷菜時順手挖挖看？）",
    "🗺️ 傳聞：有人半夜看到 {who} 鬼鬼祟祟地在田裡挖洞，好像埋了什麼東西。",
    "🗺️ 傳聞：酒館老闆說 {who} 的田裡有一張藏寶圖指著的位置，但他喝醉了。",
)
SALES_EVENTS = ("sell", "fish_sell")
_MARKDOWN = str.maketrans({c: "\\" + c for c in "\\*_~`|>[]()#"})


def news_day(now: datetime) -> date:
    """現在顯示的日報是哪一天（台灣時間的昨天）。"""
    return now.astimezone(TAIPEI_TZ).date() - timedelta(days=1)


def day_range(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, TAIPEI_TZ)
    return start, start + timedelta(days=1)


def safe_name(name: str) -> str:
    """玩家取的農場名稱放進 Discord 訊息前跳脫 Markdown，並讓 @ 不會變成提及。"""
    return name.translate(_MARKDOWN).replace("@", "@​")


@dataclass
class DayData:
    """產生日報需要的當日彙總（由服務層從資料庫查詢後填入）。"""

    events: list[tuple] = field(default_factory=list)  # (farm_id, event_type, 筆數, 數量合計, 金幣合計)
    special: list[tuple] = field(default_factory=list)  # (farm_id, item_code, quality, mutation, 數量)
    fish: tuple | None = None  # (farm_id, 魚名, 稀有度名稱, 重量 g)
    names: dict[int, str] = field(default_factory=dict)  # farm_id → 農場名稱
    item_names: dict[str, str] = field(default_factory=dict)
    ruins: dict | None = None  # {"donated": 昨天捐的素材數, "percent": 修復進度, "completed_today": bool}
    rumor: "Rumor | None" = None


@dataclass(frozen=True)
class Rumor:
    target: int  # 傳聞主角的 farm_id
    real: bool  # 寶藏是不是真的
    template: int


def rumor(group_id: int, day: date, candidates: list[int]) -> Rumor | None:
    """day 的日報附帶的傳聞（candidates：day 當天有動靜的農場）；沒有傳聞時回傳 None。結果只由參數決定。"""
    if not candidates:
        return None
    rng = random.Random(f"rumor-{group_id}-{day.isoformat()}")
    if rng.random() >= RUMOR_CHANCE:
        return None
    target = sorted(candidates)[rng.randrange(len(candidates))]
    return Rumor(target, rng.random() < RUMOR_TRUE_CHANCE, rng.randrange(len(RUMOR_TEMPLATES)))


def treasure_gold(u: float) -> int:
    low, high = TREASURE_GOLD
    return low + min(high - low, int(u * (high - low + 1)))


@dataclass
class News:
    day: date
    headlines: list[str]
    summary: str
    active: bool = True  # 當天有沒有任何交易紀錄

    @property
    def empty(self) -> bool:
        """沒有頭條也沒有任何動靜：總覽不顯示日報。"""
        return not self.headlines and not self.active


def _who(data: DayData, farm_id: int) -> str:
    return f"**{safe_name(data.names.get(farm_id, '某座農場'))}**"


def _top(per_farm: dict[int, int], minimum: int = 1) -> tuple[int, int] | None:
    """數值最大的 (farm_id, 數值)；同分時取 farm_id 小的，結果固定。"""
    best = max(per_farm.items(), key=lambda kv: (kv[1], -kv[0]), default=None)
    return best if best and best[1] >= minimum else None


def build(day: date, data: DayData, seed: str = "") -> News:
    rng = random.Random(f"{day.isoformat()}-{seed}")  # 同一天同一群組的措辭固定（重新整理不會變）
    count, qty, gold = (defaultdict(Counter) for _ in range(3))
    for farm_id, event, n, q, g in data.events:
        count[event][farm_id] += n
        qty[event][farm_id] += q
        gold[event][farm_id] += g
    headlines: list[str] = []

    if data.ruins and data.ruins.get("collapsed_today"):
        headlines.append("🏚️ **噩耗！** 上週沒有人打倒遺跡守護者，古代遺跡崩塌了，要重新捐核心素材才能修好。")
    if data.ruins and data.ruins.get("guardian_defeated_today"):
        headlines.append("⚔️ 群組成員合力打倒了本週的遺跡守護者，本週遺跡探索每次多兩份獎勵！")
    if data.ruins and data.ruins.get("completed_today"):
        headlines.append("🏛️ **號外！古代遺跡修復完成！** 群組成員合力喚醒了遺跡核心，山洞多了一個新的探險區域。")

    sales: Counter = Counter()
    for event in SALES_EVENTS:
        sales.update(gold[event])
    if top := _top(sales):
        lead = rng.choice(("一天狂賣", "昨天豪賺", "靠著一車農產品賺進"))
        headlines.append(f"💰 {_who(data, top[0])} {lead} **{top[1]:,}** 金，榮登昨日銷售冠軍！")

    if data.special:
        farm_id, item, quality, mutation, n = data.special[0]
        crop = data.item_names.get(item, item)
        tags = ["傳說品質"] if quality == "legendary" else []
        if mutation in balance.MUTATIONS and mutation != "none":
            tags.append(balance.MUTATIONS[mutation])
        headlines.append(f"🌟 奇蹟！{_who(data, farm_id)} 收成了 **{n}** 個{'、'.join(tags)}的{crop}，鄰居都跑來圍觀。")

    victims = _top(count["stolen"], minimum=2)
    thefts = sum(count["steal"].values())
    if victims:
        sob = rng.choice(("田裡只剩稻草人在哭泣", "主人表示要去買一隻狗", "警方（並沒有）介入調查"))
        headlines.append(f"🥷 驚！{_who(data, victims[0])} 一天被偷 **{victims[1]}** 次，{sob}。")
    elif thefts:
        headlines.append(f"🥷 昨天發生 **{thefts}** 起偷菜事件，請大家看好自己的田。")
    if thief := _top(count["steal"], minimum=3):
        headlines.append(f"🧤 神偷 {_who(data, thief[0])} 昨天出手 **{thief[1]}** 次，手套都磨破了。")

    defended = sum(count["defended"].values())
    if defended:
        guard = _top(count["defended"])
        headlines.append(f"🛡️ 防守設施昨天擋下 **{defended}** 次偷菜，最堅固的是 {_who(data, guard[0])} 的農場。")

    if data.fish:
        farm_id, fish_name, rarity, weight = data.fish
        headlines.append(f"🎣 {_who(data, farm_id)} 釣到一條 **{weight / 1000:.1f}** 公斤的{rarity}魚「{fish_name}」！")

    if data.ruins and data.ruins.get("donated") and not data.ruins.get("completed_today"):
        headlines.append(f"🏛️ 古代遺跡修復進度 **{data.ruins['percent']}%**，昨天大家捐了 "
                         f"**{data.ruins['donated']}** 個素材。")

    if finder := _top(gold["rumor_treasure"]):
        headlines.insert(0, f"💎 {_who(data, finder[0])} 昨天在傳聞說的田裡挖到寶藏，進帳 **{finder[1]:,}** 金！")

    gambles = sum(count["gamble"].values())
    if gambles:
        mood = rng.choice(("有人笑著離開", "莊家依然微笑", "骰子都快滾壞了"))
        headlines.append(f"🎰 賭場昨天開了 **{gambles}** 局，{mood}。")

    farms = {farm_id for farm_id, *_rest in data.events}
    summary = (f"🌾 收成 {sum(qty['harvest'].values()):,} 個作物　💰 銷售 {sum(sales.values()):,} 金　"
               f"👨‍🌾 {len(farms)} 座農場有動靜")
    headlines = headlines[:MAX_HEADLINES - 1 if data.rumor else MAX_HEADLINES]
    if data.rumor:  # 傳聞一定放在最後一條
        headlines.append(RUMOR_TEMPLATES[data.rumor.template].format(who=_who(data, data.rumor.target)))
    return News(day, headlines, summary, active=bool(farms))


def render(news: News) -> str:
    """總覽頁「📰 昨日農場日報」欄位的內容（以整行截斷，不超過 Discord 欄位上限）。"""
    quiet = "昨天大家都在默默耕耘，沒有什麼大新聞。" if news.active else "昨天農場一片寧靜，大家都在睡覺。"
    out: list[str] = []
    for line in [*(news.headlines or [quiet]), f"-# {news.summary}"]:
        if len("\n".join([*out, line])) <= FIELD_LIMIT:  # 放不下的那一行跳過，後面較短的行（含摘要）照放
            out.append(line)
    return "\n".join(out)
