"""面板延遲統計（app/discord_bot/latency.py）與面板的記錄點。"""

import logging

from app.discord_bot import latency
from tests.test_discord_bot import ALICE
from tests.test_discord_panel import admin, cmds, make_player  # noqa: F401  admin、cmds 是 fixture


def test_percentile_and_summary(caplog):
    assert latency.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5
    assert latency.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert latency.percentile([7], 0.95) == 7
    s = latency.LatencyStats()
    assert s.summary() == []
    for ms in range(1, 101):
        s.record("換頁 main", ms)
    s.record("操作 plots", 50, deferred=True)
    rows = s.summary()
    assert [r["name"] for r in rows] == ["全部", "換頁 main", "操作 plots"]
    assert rows[0]["count"] == 101 and rows[0]["deferred"] == 1 and rows[0]["max"] == 100
    main = rows[1]
    assert main["avg"] == 50.5 and main["p50"] == 50 and main["p95"] == 95
    with caplog.at_level(logging.WARNING, logger=latency.__name__):
        s.record("操作 plots", latency.SLOW_MS + 1)
    assert "面板延遲偏高" in caplog.text
    with caplog.at_level(logging.INFO, logger=latency.__name__):
        s.log_and_reset()
    assert "面板延遲（最近" in caplog.text and "換頁 main：100 次" in caplog.text
    assert s.summary() == []


def test_panel_records_open_goto_and_act(cmds, monkeypatch):  # noqa: F811
    stats = latency.LatencyStats()
    monkeypatch.setattr(latency, "STATS", stats)
    monkeypatch.setattr("app.discord_bot.panels.LATENCY", stats)
    monkeypatch.setattr("app.discord_bot.commands.LATENCY", stats)
    p = make_player(cmds, ALICE)
    p.go("shop")
    p.click("回總覽")  # 一般按鈕也是經 goto
    p.click("全部收成")  # 沒有成熟的作物：操作失敗也要記錄
    names = {r["name"]: r["count"] for r in stats.summary()}
    assert names["開啟面板"] == 1 and names["換頁 shop"] == 1 and names["換頁 main"] == 1
    assert any(n.startswith(("操作 ", "操作失敗 ")) for n in names)
