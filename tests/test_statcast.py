"""Tests for statcast data loading and display formatting."""

from unittest.mock import patch, MagicMock

from gkl.statcast import (
    StatcastBatter,
    StatcastPitcher,
    _load_rate_stats,
    _load_percentile_data,
)


def _mock_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


# -- _load_rate_stats tests --------------------------------------------------

RATE_CSV_BATTER = (
    '"last_name, first_name","player_id","year","k_percent","bb_percent","whiff_percent"\n'
    '"Baldwin, Drake",123456,2026,11.1,8.3,22.5\n'
    '"O\'Hearn, Ryan",654321,2026,10.0,12.4,18.7\n'
)

RATE_CSV_PITCHER = (
    '"last_name, first_name","player_id","year","k_percent","bb_percent","whiff_percent"\n'
    '"Soriano, José",111111,2026,29.2,16.7,41.3\n'
)


@patch("gkl.statcast.httpx.get")
def test_load_rate_stats_populates_batter_values(mock_get):
    """Actual rate values (not percentile ranks) are stored on cache entries."""
    mock_get.return_value = _mock_response(RATE_CSV_BATTER)
    cache = {
        123456: StatcastBatter(player_name="Baldwin, Drake", player_id=123456),
    }
    _load_rate_stats(2026, "batter", cache)

    entry = cache[123456]
    assert entry.k_pct == 11.1
    assert entry.bb_pct == 8.3
    assert entry.whiff_pct == 22.5


@patch("gkl.statcast.httpx.get")
def test_load_rate_stats_creates_new_entries(mock_get):
    """Players not already in the cache get new entries created."""
    mock_get.return_value = _mock_response(RATE_CSV_BATTER)
    cache = {}  # empty cache
    _load_rate_stats(2026, "batter", cache)

    assert 123456 in cache
    assert 654321 in cache
    assert isinstance(cache[123456], StatcastBatter)
    assert cache[654321].k_pct == 10.0
    assert cache[654321].bb_pct == 12.4
    assert cache[654321].whiff_pct == 18.7


@patch("gkl.statcast.httpx.get")
def test_load_rate_stats_pitcher(mock_get):
    """Pitcher entries are created with correct type."""
    mock_get.return_value = _mock_response(RATE_CSV_PITCHER)
    cache = {}
    _load_rate_stats(2026, "pitcher", cache)

    assert 111111 in cache
    entry = cache[111111]
    assert isinstance(entry, StatcastPitcher)
    assert entry.k_pct == 29.2
    assert entry.bb_pct == 16.7
    assert entry.whiff_pct == 41.3


RATE_CSV_EMPTY_VALUES = (
    '"last_name, first_name","player_id","year","k_percent","bb_percent","whiff_percent"\n'
    '"Empty, Player",999999,2026,,,\n'
)


@patch("gkl.statcast.httpx.get")
def test_load_rate_stats_handles_empty_values(mock_get):
    """Blank CSV fields leave values as None (no data)."""
    mock_get.return_value = _mock_response(RATE_CSV_EMPTY_VALUES)
    cache = {}
    _load_rate_stats(2026, "batter", cache)

    entry = cache[999999]
    assert entry.k_pct is None
    assert entry.bb_pct is None
    assert entry.whiff_pct is None


# -- _load_percentile_data tests ---------------------------------------------

PERCENTILE_CSV = (
    '"player_name","player_id","k_percent","bb_percent","whiff_percent",'
    '"hard_hit_percent","brl_percent","xera"\n'
    '"Baldwin, Drake",123456,70,45,55,60,50,3.80\n'
)


@patch("gkl.statcast.httpx.get")
def test_percentile_no_longer_sets_k_bb_whiff(mock_get):
    """_load_percentile_data must NOT overwrite k_pct, bb_pct, whiff_pct."""
    mock_get.return_value = _mock_response(PERCENTILE_CSV)
    cache = {
        123456: StatcastBatter(
            player_name="Baldwin, Drake",
            player_id=123456,
            k_pct=11.1,
            bb_pct=8.3,
            whiff_pct=22.5,
        ),
    }
    _load_percentile_data(2026, "batter", cache)

    entry = cache[123456]
    # Rate values must be preserved, not replaced by percentile ranks
    assert entry.k_pct == 11.1
    assert entry.bb_pct == 8.3
    assert entry.whiff_pct == 22.5


@patch("gkl.statcast.httpx.get")
def test_percentile_still_fills_hard_hit_barrel(mock_get):
    """hard_hit_pct and barrel_pct are still filled from percentile data."""
    mock_get.return_value = _mock_response(PERCENTILE_CSV)
    cache = {
        123456: StatcastBatter(
            player_name="Baldwin, Drake",
            player_id=123456,
        ),
    }
    _load_percentile_data(2026, "batter", cache)

    entry = cache[123456]
    assert entry.hard_hit_pct == 60.0
    assert entry.barrel_pct == 50.0


# -- Display formatter tests -------------------------------------------------

def test_rate_formatter():
    """The _rate formatter produces '33.3' for values and '-' for None."""
    # Replicate the formatter from app.py
    def _rate(v: float | None) -> str:
        return f"{v:.1f}" if v is not None else "-"

    assert _rate(33.3) == "33.3"
    assert _rate(11.1) == "11.1"
    assert _rate(0.0) == "0.0"
    assert _rate(None) == "-"
    assert _rate(100.0) == "100.0"
