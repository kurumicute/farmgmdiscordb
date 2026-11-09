"""農場日報：規則（頭條、跳脫、欄位長度）、服務（只報導所屬群組、只快取已結束的日子），
以及顯示在 /farm 總覽最下方（沒有按鈕、Bot 不主動發訊息）。"""

from datetime import date, datetime, timedelta

import discord
import pytest
from sqlalchemy import func, select

from app.clock import TAIPEI_TZ
from app.db.models import Farm, GameTransaction
from app.game import news
from app.services import news_service
from app.services.admin_service import AdminService
from tests.conftest import grow, make_farmer

DAY = date(2026, 1, 1)


# ---------- 規則 ----------


def test_rules_day_range():
    assert news.news_day(datetime(2026, 1, 2, 0, 1, tzinfo=TAIPEI_TZ)) == DAY
    start, end = news.day_range(DAY)
    assert start == datetime(2026, 1, 1, tzinfo=TAIPEI_TZ) and end - start == timedelta(days=1)


def test_build_headlines_and_escaped_names():
    data = news.DayData(
        events=[(1, "sell", 3, 30, 900), (2, "fish_sell", 1, 1, 100), (2, "stolen", 3, 3, 0), (1, "steal", 3, 3, 0),
                (1, "harvest", 2, 40, 0), (2, "defended", 1, 0, 0), (1, "gamble", 4, 4, 0)],
        special=[(2, "wheat", "legendary", "golden", 2)],
        fish=(1, "黃金鯉魚", "傳說", 3500),
        names={1: "**粗體** @everyone", 2: "小華"},
        item_names={"wheat": "小麥"},
        ruins={"donated": 12, "percent": 40, "completed_today": False},
    )
    n = news.build(DAY, data, seed="1")
    text = news.render(n)
    assert not n.empty and len(n.headlines) == news.MAX_HEADLINES and len(text) <= news.FIELD_LIMIT
    assert "\\*\\*粗體\\*\\*" in text and "@​everyone" in text and "@everyone" not in text
    assert "**900** 金" in text and "傳說品質、黃金作物的小麥" in text and "被偷 **3** 次" in text
    assert text.endswith(n.summary) and "收成 40 個作物" in n.summary and "銷售 1,000 金" in n.summary
    assert news.build(DAY, data, seed="1").headlines == n.headlines  # 同一天同一群組措辭固定


def test_empty_and_quiet_days():
    assert news.build(DAY, news.DayData()).empty
    quiet = news.build(DAY, news.DayData(events=[(1, "harvest", 1, 3, 0)], names={1: "阿明"}))
    assert not quiet.empty and "默默耕耘" in news.render(quiet)


def test_render_respects_field_limit():
    long = news.News(DAY, ["🌟 " + "很長" * 600, "短的頭條"], "摘要")
    text = news.render(long)
    assert len(text) <= news.FIELD_LIMIT and "很長" not in text and "短的頭條" in text and "摘要" in text


# ---------- 服務 ----------


@pytest.fixture
def admin(session_factory, clock):
    return AdminService(session_factory, clock)


def farm_and_sell(admin, clock, *pids):
    for pid in pids:
        grow(admin, pid, 1, "wheat")
    clock.advance(600)
    for pid in pids:
        admin.harvest_crop(pid, 1)
        admin.sell_crop(pid, "wheat", 1)


def test_daily_news_only_reports_home_group(admin, clock, home_group):
    a = make_farmer(admin, "阿明", home_group)
    other = admin.create_group("隔壁").id
    c = make_farmer(admin, "隔壁老王", other)
    farm_and_sell(admin, clock, a, c)
    assert admin.daily_news(home_group)["empty"]  # 今天（2026-01-01）的事要明天才上日報
    clock.advance(24 * 3600)
    d = admin.daily_news(home_group)
    assert d["day"] == DAY and not d["empty"]
    assert "阿明的農場" in d["text"] and "隔壁老王" not in d["text"]
    assert admin.daily_news(home_group, DAY + timedelta(days=1))["empty"]


def test_past_days_are_cached_today_is_not(admin, clock, home_group):
    a = make_farmer(admin, "阿明", home_group)
    farm_and_sell(admin, clock, a)
    today = admin.daily_news(home_group, DAY)  # 還沒結束的日子不快取
    assert (home_group, DAY) not in news_service._cache and not today["empty"]
    clock.advance(24 * 3600)
    first = admin.daily_news(home_group)
    assert news_service._cache[(home_group, DAY)] is first and admin.daily_news(home_group) is first


# ---------- 面板總覽 ----------


def test_main_page_shows_news_without_buttons(session_factory, clock):
    from app.discord_bot.commands import FarmCommands
    from app.discord_bot.runner import ServiceRunner
    from tests.test_discord_bot import ALICE
    from tests.test_discord_panel import make_player

    admin = AdminService(session_factory, clock)
    runner = ServiceRunner(2)
    try:
        p = make_player(FarmCommands(admin, runner), ALICE)
        assert "昨日農場日報" not in p.text  # 昨天沒有任何動靜：不顯示
        before = p.labels()
        farm_and_sell(admin, clock, p.panel.player_id)
        clock.advance(24 * 3600)
        p.go("main")
        assert "📰 昨日農場日報" in p.text and "**15** 金" in p.text
        assert p.labels() == before  # 沒有多出任何按鈕
        assert not [c for c in p.panel.children if isinstance(c, discord.ui.Button) and "日報" in (c.label or "")]
    finally:
        runner.shutdown()


# ---------- 農場傳聞 ----------


def setup_rumor(admin, clock, home_group, monkeypatch, *, real=True):
    owner = make_farmer(admin, "地主", home_group)
    thieves = [make_farmer(admin, name, home_group) for name in ("小偷甲", "小偷乙")]
    with admin._transaction() as s:
        from app.repositories import farms as farms_repo

        target = farms_repo.get_by_player(s, owner).id
    monkeypatch.setattr(news, "rumor", lambda g, d, c: news.Rumor(target, real, 0))
    grow(admin, owner, 1, "carrot")
    grow(admin, owner, 2, "carrot")
    clock.advance(600)
    return owner, thieves


def test_rumor_treasure_first_digger_only(admin, clock, home_group, monkeypatch, session_factory):
    owner, (a, b) = setup_rumor(admin, clock, home_group, monkeypatch)
    admin._rumor_rng = lambda: 0.0  # 一定挖到、金額取最小值
    gold = admin.get_farm_status(a).gold
    r = admin.steal_crop(home_group, a, owner, 1, 1)
    assert "傳聞是真的" in r.message and admin.get_farm_status(a).gold == gold + news.TREASURE_GOLD[0]
    r = admin.steal_crop(home_group, b, owner, 2, 1)
    assert "已經被別人拿走" in r.message
    with session_factory.begin() as s:
        assert s.scalar(select(func.sum(Farm.gold))) == s.scalar(select(func.sum(GameTransaction.gold_delta)))
    clock.advance(24 * 3600)  # 隔天的日報報導挖到寶藏
    text = admin.daily_news(home_group)["text"]
    assert "挖到寶藏" in text and "小偷甲的農場" in text


def test_fake_rumor_finds_nothing(admin, clock, home_group, monkeypatch):
    owner, (a, _b) = setup_rumor(admin, clock, home_group, monkeypatch, real=False)
    admin._rumor_rng = lambda: 0.0
    gold = admin.get_farm_status(a).gold
    r = admin.steal_crop(home_group, a, owner, 1, 1)
    assert "什麼都沒有" in r.message and admin.get_farm_status(a).gold == gold


def test_rumor_headline_is_last_and_deterministic():
    assert news.rumor(1, DAY, []) is None
    picks = {news.rumor(1, DAY + timedelta(days=i), [5, 3, 9]) for i in range(40)}
    assert None in picks and any(p and p.real for p in picks) and any(p and not p.real for p in picks)
    assert all(p is None or p.target in (3, 5, 9) for p in picks)
    assert news.rumor(1, DAY, [9, 5, 3]) == news.rumor(1, DAY, [3, 5, 9])  # 只看集合，不看順序
    data = news.DayData(events=[(1, "sell", 1, 1, 50)], names={1: "阿明"}, rumor=news.Rumor(1, True, 0))
    n = news.build(DAY, data)
    assert n.headlines[-1].startswith("🗺️ 傳聞") and "**阿明**" in n.headlines[-1]
