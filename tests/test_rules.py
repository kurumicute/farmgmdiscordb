from datetime import UTC, datetime, timedelta

import pytest

from app.game import balance, rules
from app.game.errors import ValidationError
from app.game.rules import PlotState

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_plot_state():
    ready = NOW + timedelta(seconds=1)
    assert rules.plot_state(None, None, None, NOW) == PlotState.EMPTY
    assert rules.plot_state(1, None, None, NOW) == PlotState.WAITING_WATER
    assert rules.plot_state(1, NOW, ready, NOW) == PlotState.GROWING
    assert rules.plot_state(1, NOW, NOW, NOW) == PlotState.READY
    wither = NOW + timedelta(seconds=balance.WITHER_SECONDS)
    assert rules.plot_state(1, NOW, NOW, wither - timedelta(microseconds=1)) == PlotState.READY
    assert rules.plot_state(1, NOW, NOW, wither) == PlotState.WITHERED


def test_earthworm_multiplier():
    assert rules.grow_seconds_with_effects(600, False) == 600
    assert rules.grow_seconds_with_effects(600, True) == 450


def test_remaining_seconds_rounds_up_and_never_negative():
    assert rules.remaining_seconds(NOW + timedelta(seconds=0.2), NOW) == 1
    assert rules.remaining_seconds(NOW - timedelta(seconds=5), NOW) == 0


@pytest.mark.parametrize("bad", [0, -1, 10_000, True, 1.5])
def test_validate_quantity_rejects(bad):
    with pytest.raises(ValidationError):
        rules.validate_quantity(bad)


@pytest.mark.parametrize("bad", ["", "   ", "x" * 33, "a\x00b"])
def test_validate_name_rejects(bad):
    with pytest.raises(ValidationError):
        rules.validate_name(bad, label="名稱", max_len=32)


def test_item_codes():
    assert rules.seed_item_code("wheat") == "seed_wheat"
    assert rules.produce_item_code("wheat") == "wheat"
