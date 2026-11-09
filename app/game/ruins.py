"""古代遺跡（群組合作）：群組成員捐山洞素材修復遺跡，三個階段完成後整個群組都能進入「古代遺跡」探險區域。

純規則，不碰資料庫。遺跡屬於農場的所屬群組（farms.home_group_id），和群組任務計分一樣不受開面板的伺服器影響；
所屬群組受 7 天轉移冷卻保護，不能在短時間內借用別的群組已修好的遺跡。

遺跡守護者：修好後的下一個台灣週一起，每週出現一隻守護者。成員捐素材攻擊（越稀有傷害越高），去遺跡探險回來
領獎時也會自動造成傷害；打倒後當週的遺跡探索每次多兩份獎勵。整週都沒打倒，遺跡崩壞回第三階段（要重新捐核心）。
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.clock import TAIPEI_TZ
from app.game import exploration


@dataclass(frozen=True)
class Stage:
    name: str
    description: str
    materials: dict[str, int]  # 這個階段需要的素材 {代碼: 數量}


# 數值以「3–5 位活躍玩家約一到兩週修好」估算：草原一趟約 9 個普通素材；核心只在森林（Lv.7）與礦坑（Lv.10）出現，
# 所以第三階段需要不同等級的成員合作
STAGES: tuple[Stage, ...] = (
    Stage("清除碎石", "把堵住入口的碎石與倒下的樹木搬開。", {"stone": 150, "wood": 150}),
    Stage("修補石柱", "用礦石與樹脂補強快要倒塌的石柱。", {"copper_ore": 80, "iron_ore": 50, "resin": 80}),
    Stage("喚醒遺跡核心", "把齒輪、水晶與核心裝回遺跡中央的古代裝置。",
          {"gear": 20, "crystal": 12, "ancient_core": 3, "machine_core": 3}),
)
COMPLETED = len(STAGES)  # stage == 3：修復完成
DONATE_MAX = 999  # 單次捐獻上限
TITLE = "遺跡修復者"  # 修復完成時，所有捐過素材的成員獲得的稱號（farm_titles.title 最長 20 字）
TITLE_SOURCE = "ruins"


def stage_info(stage: int) -> Stage | None:
    """目前要修的階段；已修復完成時回傳 None。"""
    return STAGES[stage] if 0 <= stage < COMPLETED else None


def remaining(stage: int, progress: dict[str, int]) -> dict[str, int]:
    """目前階段每種素材還差幾個（已足夠的不列出）。"""
    info = stage_info(stage)
    if info is None:
        return {}
    return {code: need - progress.get(code, 0) for code, need in info.materials.items() if progress.get(code, 0) < need}


def stage_done(stage: int, progress: dict[str, int]) -> bool:
    return stage_info(stage) is not None and not remaining(stage, progress)


def percent(stage: int, progress: dict[str, int]) -> int:
    """整體修復進度（0–100）：每個階段佔三分之一，階段內依已捐數量比例計算。"""
    info = stage_info(stage)
    if info is None:
        return 100
    need = sum(info.materials.values())
    have = sum(min(progress.get(c, 0), n) for c, n in info.materials.items())
    return min(99, int((stage + have / need) * 100 / COMPLETED))


# ---------- 遺跡守護者 ----------
GUARDIAN_NAME = "石像守護者"
GUARDIAN_HP_BASE = 3000  # 2026-11-09 使用者要求調為原本的 2 倍
GUARDIAN_HP_PER_MEMBER = 1000  # 每座所屬群組的農場增加的 HP
GUARDIAN_HP_MAX = 40000
GUARDIAN_DAMAGE = {"普通": 3, "稀有": 15, "極稀有": 80}  # 每個素材依稀有度造成的傷害（假幣不能用）
EXPEDITION_DAMAGE = 200  # 每次探索古代遺跡時對守護者造成的傷害
DEFEAT_BONUS_ROLLS = 2  # 打倒後，當週遺跡探索每次多抽的獎勵份數（血量 2 倍、獎勵也加倍）
ATTACK_MAX = 999


def week_start(at: datetime) -> date:
    """at 所在的台灣週一。"""
    local = at.astimezone(TAIPEI_TZ).date()
    return local - timedelta(days=local.weekday())


def week_end(week: date) -> datetime:
    return datetime.combine(week + timedelta(days=7), time.min, TAIPEI_TZ)


def first_guardian_week(completed_at: datetime) -> date:
    """修好後的下一個週一守護者才出現（修好當週不用打）。"""
    return week_start(completed_at) + timedelta(days=7)


def guardian_active(completed_at: datetime | None, now: datetime) -> bool:
    return completed_at is not None and week_start(now) >= first_guardian_week(completed_at)


def guardian_hp(members: int) -> int:
    return min(GUARDIAN_HP_MAX, GUARDIAN_HP_BASE + GUARDIAN_HP_PER_MEMBER * max(0, members))


def material_damage(code: str) -> int | None:
    """這種素材每個造成的傷害；不能用來攻擊時回傳 None。"""
    if code == exploration.FAKE_COIN or code not in exploration.MATERIALS:
        return None
    return GUARDIAN_DAMAGE.get(exploration.MATERIALS[code][1])


def weeks_to_settle(completed_at: datetime, settled: date | None, now: datetime) -> list[date]:
    """已經結束、還沒結算的守護者週（由舊到新）。"""
    week = first_guardian_week(completed_at)
    if settled is not None and settled >= week:
        week = settled + timedelta(days=7)
    current = week_start(now)
    out = []
    while week < current:
        out.append(week)
        week += timedelta(days=7)
    return out
