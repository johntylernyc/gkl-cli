"""Statcast / Baseball Savant advanced metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import httpx


@dataclass
class StatcastBatter:
    player_name: str
    player_id: int  # MLBAM ID
    pa: int = 0
    avg_exit_velo: float | None = None
    max_exit_velo: float | None = None
    avg_launch_angle: float | None = None
    barrel_pct: float | None = None
    hard_hit_pct: float | None = None
    k_pct: float | None = None
    bb_pct: float | None = None
    whiff_pct: float | None = None
    xba: float | None = None
    xslg: float | None = None
    xwoba: float | None = None
    ba: float | None = None
    slg: float | None = None
    woba: float | None = None


@dataclass
class StatcastPitcher:
    player_name: str
    player_id: int
    pa: int = 0
    avg_exit_velo: float | None = None  # exit velo allowed
    barrel_pct: float | None = None     # barrel % allowed
    hard_hit_pct: float | None = None   # hard hit % allowed
    xba: float | None = None            # expected BA allowed
    xslg: float | None = None           # expected SLG allowed
    xwoba: float | None = None          # expected wOBA allowed
    xera: float | None = None           # expected ERA
    k_pct: float | None = None          # strikeout rate
    bb_pct: float | None = None         # walk rate
    whiff_pct: float | None = None      # whiff rate (swings & misses / swings)
    csw_pct: float | None = None        # called strikes + whiffs %
    avg_spin: float | None = None       # average spin rate
    avg_velo: float | None = None       # average fastball velocity


# Cache for the current season's leaderboard data
_batter_cache: dict[int, StatcastBatter] = {}
_pitcher_cache: dict[int, StatcastPitcher] = {}
_cache_year: int | None = None


def _ensure_cache(year: int | None = None) -> None:
    """Load statcast leaderboard data into cache if not already loaded."""
    global _batter_cache, _pitcher_cache, _cache_year

    if year is None:
        year = date.today().year
    if _cache_year == year and _batter_cache:
        return

    _cache_year = year
    _batter_cache = {}
    _pitcher_cache = {}

    _load_expected_stats(year, "batter", _batter_cache)
    _load_exit_velo(year, "batter", _batter_cache)
    _load_expected_stats(year, "pitcher", _pitcher_cache)
    _load_exit_velo(year, "pitcher", _pitcher_cache)
    _load_rate_stats(year, "batter", _batter_cache)
    _load_rate_stats(year, "pitcher", _pitcher_cache)
    _load_mlb_rate_stats_fallback(year, _pitcher_cache)
    _load_missing_pitcher_rates(year, _pitcher_cache)
    _load_percentile_data(year, "pitcher", _pitcher_cache)
    _load_percentile_data(year, "batter", _batter_cache)


def _load_expected_stats(year: int, player_type: str, cache: dict) -> None:
    """Load xBA, xSLG, xwOBA from Baseball Savant."""
    try:
        url = (
            "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
            f"?type={player_type}&year={year}&position=&team="
            "&filterType=pa&min=1&csv=true"
        )
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        _parse_csv_into_cache(resp.text, cache, player_type)
    except (httpx.HTTPError, Exception):
        pass


def _load_exit_velo(year: int, player_type: str, cache: dict) -> None:
    """Load exit velocity, barrel rate, hard hit % from Baseball Savant."""
    try:
        url = (
            "https://baseballsavant.mlb.com/leaderboard/statcast"
            f"?type={player_type}&year={year}&position=&team="
            "&min=1&csv=true"
        )
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        _parse_ev_csv_into_cache(resp.text, cache)
    except (httpx.HTTPError, Exception):
        pass


def _load_rate_stats(year: int, player_type: str, cache: dict) -> None:
    """Load actual K%, BB%, whiff% rates from Baseball Savant custom leaderboard."""
    import csv
    import io
    try:
        url = (
            "https://baseballsavant.mlb.com/leaderboard/custom"
            f"?year={year}&type={player_type}&min=0"
            "&selections=k_percent,bb_percent,whiff_percent&csv=true"
        )
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text.lstrip("\ufeff")
        reader = csv.reader(io.StringIO(text))
        headers = [h.strip() for h in next(reader)]
        col = {h: i for i, h in enumerate(headers)}

        name_col = None
        for h, i in col.items():
            if "last_name" in h.lower():
                name_col = i
                break

        for fields in reader:
            try:
                pid = int(fields[col.get("player_id", -1)])
                name = fields[name_col] if name_col is not None else str(pid)
                if player_type == "batter":
                    entry = cache.get(pid) or StatcastBatter(player_name=name, player_id=pid)
                else:
                    entry = cache.get(pid) or StatcastPitcher(player_name=name, player_id=pid)
                if "k_percent" in col:
                    val = fields[col["k_percent"]].strip()
                    if val:
                        entry.k_pct = float(val)
                if "bb_percent" in col:
                    val = fields[col["bb_percent"]].strip()
                    if val:
                        entry.bb_pct = float(val)
                if "whiff_percent" in col:
                    val = fields[col["whiff_percent"]].strip()
                    if val:
                        entry.whiff_pct = float(val)
                cache[pid] = entry
            except (ValueError, IndexError, KeyError):
                continue
    except (httpx.HTTPError, Exception):
        pass


def _load_mlb_rate_stats_fallback(year: int, cache: dict) -> None:
    """Compute K% and BB% from MLB Stats API for pitchers missing Savant data.

    The Baseball Savant custom leaderboard has a hard cap (~273 pitchers) that
    excludes many active pitchers.  The MLB Stats API bulk pitching endpoint
    provides strikeouts, walks, and batters-faced for all qualifying pitchers,
    letting us fill in the gaps.
    """
    # Only bother if there are pitchers missing k_pct
    needs_fill = [pid for pid, e in cache.items() if e.k_pct is None]
    if not needs_fill:
        return

    try:
        url = (
            "https://statsapi.mlb.com/api/v1/stats"
            f"?stats=season&season={year}&group=pitching&sportId=1&limit=1000"
        )
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for split in data.get("stats", []):
            for s in split.get("splits", []):
                pid = s.get("player", {}).get("id")
                entry = cache.get(pid)
                if entry is None:
                    continue
                stat = s.get("stat", {})
                bf = stat.get("battersFaced", 0)
                if bf <= 0:
                    continue
                if entry.k_pct is None:
                    k = stat.get("strikeOuts", 0)
                    entry.k_pct = round(k / bf * 100, 1)
                if entry.bb_pct is None:
                    bb = stat.get("baseOnBalls", 0)
                    entry.bb_pct = round(bb / bf * 100, 1)
    except (httpx.HTTPError, Exception):
        pass


def _load_missing_pitcher_rates(year: int, cache: dict) -> None:
    """Fill missing K%, BB%, Whiff% from MLB Stats API per-player endpoints.

    The Savant custom leaderboard caps at ~273 pitchers, and the bulk MLB Stats
    API has a ~10 BF minimum, so some pitchers are still missing rate stats.
    For each, we make two per-player calls in one request:
      - Season pitching stats → K% and BB% (from K/BF and BB/BF)
      - Pitch log → Whiff% (from swinging strikes / total swings)
    Requests are parallelised to keep wall time low (~2-4s for ~50 pitchers).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    needs_fill = [
        pid for pid, e in cache.items()
        if e.k_pct is None or e.bb_pct is None or e.whiff_pct is None
    ]
    if not needs_fill:
        return

    # Call codes that indicate the batter swung
    _SWING_CODES = {"S", "W", "T", "F", "X", "D", "E", "M", "L", "R"}
    # Subset that are swinging strikes / whiffs (includes foul tips and missed bunts
    # to match Baseball Savant's plate discipline Whiff% definition)
    _WHIFF_CODES = {"S", "W", "T", "M"}

    def _fetch_rates(pid: int) -> tuple[int, dict]:
        """Return (pid, {k_pct, bb_pct, whiff_pct}) with None for unavailable."""
        result: dict[str, float | None] = {
            "k_pct": None, "bb_pct": None, "whiff_pct": None,
        }
        try:
            # Season stats for K% and BB%
            url = (
                f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                f"?stats=season&season={year}&group=pitching"
            )
            resp = httpx.get(url, timeout=10)
            resp.raise_for_status()
            for grp in resp.json().get("stats", []):
                for sp in grp.get("splits", []):
                    stat = sp.get("stat", {})
                    bf = stat.get("battersFaced", 0)
                    if bf > 0:
                        result["k_pct"] = round(
                            stat.get("strikeOuts", 0) / bf * 100, 1,
                        )
                        result["bb_pct"] = round(
                            stat.get("baseOnBalls", 0) / bf * 100, 1,
                        )
                    break
        except Exception:
            pass

        try:
            # Pitch log for Whiff%
            url2 = (
                f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                f"?stats=pitchLog&season={year}&group=pitching"
            )
            resp2 = httpx.get(url2, timeout=10)
            resp2.raise_for_status()
            splits = resp2.json()["stats"][0]["splits"]
            swings = 0
            whiffs = 0
            for s in splits:
                code = (
                    s["stat"]["play"]["details"]
                    .get("call", {})
                    .get("code", "")
                )
                if code in _SWING_CODES:
                    swings += 1
                if code in _WHIFF_CODES:
                    whiffs += 1
            if swings > 0:
                result["whiff_pct"] = round(whiffs / swings * 100, 1)
        except Exception:
            pass

        return pid, result

    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_rates, pid): pid for pid in needs_fill}
            for f in as_completed(futures):
                pid, rates = f.result()
                entry = cache.get(pid)
                if entry is None:
                    continue
                if entry.k_pct is None and rates["k_pct"] is not None:
                    entry.k_pct = rates["k_pct"]
                if entry.bb_pct is None and rates["bb_pct"] is not None:
                    entry.bb_pct = rates["bb_pct"]
                if entry.whiff_pct is None and rates["whiff_pct"] is not None:
                    entry.whiff_pct = rates["whiff_pct"]
    except Exception:
        pass


def _load_percentile_data(year: int, player_type: str, cache: dict) -> None:
    """Load hard_hit%, barrel%, xERA from percentile rankings.

    Note: these are percentile ranks (1-99), not raw values.
    Used as fallback for fields not available from other endpoints.
    K%, BB%, whiff% are loaded from _load_rate_stats() instead.
    """
    import csv
    import io
    try:
        url = (
            "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"
            f"?type={player_type}&year={year}&position=&team=&csv=true"
        )
        resp = httpx.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        text = resp.text.lstrip("\ufeff")
        reader = csv.reader(io.StringIO(text))
        headers = [h.strip() for h in next(reader)]
        col = {h: i for i, h in enumerate(headers)}
        for fields in reader:
            try:
                pid = int(fields[col.get("player_id", -1)])
                entry = cache.get(pid)
                if entry is None:
                    continue
                # Fill in fields that weren't populated by other endpoints
                # (k_pct, bb_pct, whiff_pct are loaded from _load_rate_stats)
                if entry.hard_hit_pct is None and "hard_hit_percent" in col:
                    val = fields[col["hard_hit_percent"]].strip()
                    if val:
                        entry.hard_hit_pct = float(val)
                if entry.barrel_pct is None and "brl_percent" in col:
                    val = fields[col["brl_percent"]].strip()
                    if val:
                        entry.barrel_pct = float(val)
                if hasattr(entry, "xera") and entry.xera is None and "xera" in col:
                    val = fields[col["xera"]].strip()
                    if val:
                        entry.xera = float(val)
            except (ValueError, IndexError, KeyError):
                continue
    except (httpx.HTTPError, Exception):
        pass


def _opt_float(val: str) -> float | None:
    """Convert a CSV field to float, returning None for empty/missing values."""
    val = val.strip()
    return float(val) if val else None


def _parse_csv_into_cache(csv_text: str, cache: dict, player_type: str) -> None:
    """Parse expected stats CSV into cache."""
    import csv
    import io
    # Remove BOM if present
    text = csv_text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return

    col = {h: i for i, h in enumerate(headers)}
    # Find name column (may be "last_name, first_name" or similar)
    name_col = None
    for h, i in col.items():
        if "last_name" in h.lower():
            name_col = i
            break

    for fields in reader:
        try:
            pid = int(fields[col.get("player_id", -1)])
            name = fields[name_col] if name_col is not None else str(pid)
            if player_type == "batter":
                entry = cache.get(pid) or StatcastBatter(player_name=name, player_id=pid)
                entry.pa = int(fields[col.get("pa", 0)] or 0)
                entry.xba = _opt_float(fields[col.get("est_ba", 0)])
                entry.xslg = _opt_float(fields[col.get("est_slg", 0)])
                entry.xwoba = _opt_float(fields[col.get("est_woba", 0)])
                entry.ba = _opt_float(fields[col.get("ba", 0)])
                entry.slg = _opt_float(fields[col.get("slg", 0)])
                entry.woba = _opt_float(fields[col.get("woba", 0)])
                cache[pid] = entry
            else:
                entry = cache.get(pid) or StatcastPitcher(player_name=name, player_id=pid)
                entry.pa = int(fields[col.get("pa", 0)] or 0)
                entry.xba = _opt_float(fields[col.get("est_ba", 0)])
                entry.xslg = _opt_float(fields[col.get("est_slg", 0)])
                entry.xwoba = _opt_float(fields[col.get("est_woba", 0)])
                if "xera" in col:
                    entry.xera = _opt_float(fields[col["xera"]])
                cache[pid] = entry
        except (ValueError, IndexError, KeyError):
            continue


def _parse_ev_csv_into_cache(csv_text: str, cache: dict) -> None:
    """Parse exit velocity/barrels CSV into existing cache entries."""
    import csv
    import io
    text = csv_text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return
    col = {h: i for i, h in enumerate(headers)}

    for fields in reader:
        try:
            pid = int(fields[col.get("player_id", -1)])
            entry = cache.get(pid)
            if entry is None:
                continue
            if "avg_hit_speed" in col:
                entry.avg_exit_velo = _opt_float(fields[col["avg_hit_speed"]])
            if "max_hit_speed" in col:
                entry.max_exit_velo = _opt_float(fields[col["max_hit_speed"]])
            if hasattr(entry, "avg_launch_angle") and "avg_hit_angle" in col:
                entry.avg_launch_angle = _opt_float(fields[col["avg_hit_angle"]])
            if "brl_percent" in col:
                entry.barrel_pct = _opt_float(fields[col["brl_percent"]])
            if "ev95percent" in col:
                entry.hard_hit_pct = _opt_float(fields[col["ev95percent"]])
        except (ValueError, IndexError, KeyError):
            continue


def get_batter_statcast(mlbam_id: int, year: int | None = None) -> StatcastBatter | None:
    """Get statcast data for a batter by MLBAM ID."""
    _ensure_cache(year)
    return _batter_cache.get(mlbam_id)


def get_pitcher_statcast(mlbam_id: int, year: int | None = None) -> StatcastPitcher | None:
    """Get statcast data for a pitcher by MLBAM ID."""
    _ensure_cache(year)
    return _pitcher_cache.get(mlbam_id)


# Separate per-year cache so multi-year lookups don't clobber the global cache
_year_cache: dict[int, tuple[dict[int, StatcastBatter], dict[int, StatcastPitcher]]] = {}


def _load_year_data(year: int) -> tuple[dict[int, StatcastBatter], dict[int, StatcastPitcher]]:
    """Load full leaderboard for *year* into an isolated cache pair."""
    if year in _year_cache:
        return _year_cache[year]
    # If the global cache already has this year, copy it
    if _cache_year == year and _batter_cache:
        _year_cache[year] = (dict(_batter_cache), dict(_pitcher_cache))
        return _year_cache[year]
    b_cache: dict[int, StatcastBatter] = {}
    p_cache: dict[int, StatcastPitcher] = {}
    _load_expected_stats(year, "batter", b_cache)
    _load_exit_velo(year, "batter", b_cache)
    _load_expected_stats(year, "pitcher", p_cache)
    _load_exit_velo(year, "pitcher", p_cache)
    _load_rate_stats(year, "batter", b_cache)
    _load_rate_stats(year, "pitcher", p_cache)
    _load_mlb_rate_stats_fallback(year, p_cache)
    _year_cache[year] = (b_cache, p_cache)
    return _year_cache[year]


def get_batter_statcast_multi_year(
    mlbam_id: int, years: list[int],
) -> dict[int, StatcastBatter | None]:
    """Get statcast data for a batter across multiple seasons."""
    result: dict[int, StatcastBatter | None] = {}
    for year in years:
        b_cache, _ = _load_year_data(year)
        result[year] = b_cache.get(mlbam_id)
    return result


def get_pitcher_statcast_multi_year(
    mlbam_id: int, years: list[int],
) -> dict[int, StatcastPitcher | None]:
    """Get statcast data for a pitcher across multiple seasons."""
    result: dict[int, StatcastPitcher | None] = {}
    for year in years:
        _, p_cache = _load_year_data(year)
        result[year] = p_cache.get(mlbam_id)
    return result


def get_statcast_league_averages(
    years: list[int], player_type: str,
) -> dict[str, float]:
    """Compute mean of each statcast metric across the leaderboard.

    Averages across all given *years* and all qualified players (PA >= 50)
    to produce a single reference value per stat.  This represents roughly
    league-average performance and can be used as a benchmark line.
    """
    from statistics import mean

    if player_type == "batter":
        attrs = [
            "avg_exit_velo", "max_exit_velo", "avg_launch_angle",
            "barrel_pct", "hard_hit_pct", "k_pct", "bb_pct", "whiff_pct",
            "xba", "xslg", "xwoba",
        ]
    else:
        attrs = [
            "avg_exit_velo", "barrel_pct", "hard_hit_pct",
            "xba", "xslg", "xwoba", "xera",
            "k_pct", "bb_pct", "whiff_pct", "csw_pct", "avg_velo",
        ]

    # Collect all non-None values per attr across years
    collected: dict[str, list[float]] = {a: [] for a in attrs}
    for year in years:
        b_cache, p_cache = _load_year_data(year)
        cache = b_cache if player_type == "batter" else p_cache
        for entry in cache.values():
            if entry.pa < 50:
                continue
            for a in attrs:
                val = getattr(entry, a, None)
                if val is not None:
                    collected[a].append(float(val))

    return {a: mean(vals) if vals else 0.0 for a, vals in collected.items()}


def lookup_mlbam_id(player_name: str) -> int | None:
    """Try to find a player's MLBAM ID by name from cached data."""
    name_lower = player_name.lower()
    for pid, entry in _batter_cache.items():
        if name_lower in entry.player_name.lower():
            return pid
    for pid, entry in _pitcher_cache.items():
        if name_lower in entry.player_name.lower():
            return pid

    # Try MLB Stats API search
    try:
        resp = httpx.get(
            "https://statsapi.mlb.com/api/v1/people/search",
            params={"names": player_name, "sportId": 1},
            timeout=10,
        )
        resp.raise_for_status()
        people = resp.json().get("people", [])
        if people:
            return people[0]["id"]
    except (httpx.HTTPError, KeyError):
        pass
    return None
