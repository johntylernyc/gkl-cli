"""MLB Stats API client for live game scores."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"


@dataclass
class MLBGame:
    away_team: str
    away_abbr: str
    away_score: int
    home_team: str
    home_abbr: str
    home_score: int
    status: str  # "Preview", "Live", "Final"
    detail_status: str  # "Scheduled", "In Progress", "Final", etc.
    inning: int
    inning_ordinal: str  # "7th"
    inning_half: str  # "Top" / "Bottom"
    outs: int
    start_time: str  # UTC ISO string
    runners: tuple[bool, bool, bool]  # (1st, 2nd, 3rd)
    away_hits: int
    away_errors: int
    home_hits: int
    home_errors: int


def get_mlb_scoreboard(game_date: date | None = None) -> list[MLBGame]:
    """Fetch today's MLB scoreboard."""
    params: dict[str, str | int] = {
        "sportId": 1,
        "hydrate": "linescore,team",
    }
    if game_date:
        params["date"] = game_date.isoformat()

    resp = httpx.get(f"{MLB_API_BASE}/schedule", params=params)
    resp.raise_for_status()
    data = resp.json()

    games: list[MLBGame] = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append(_parse_game(g))
    return games


# ---------------------------------------------------------------------------
# Historical player stats
# ---------------------------------------------------------------------------


@dataclass
class MLBBattingStats:
    season: int
    games: int = 0
    pa: int = 0
    ab: int = 0
    hits: int = 0
    hr: int = 0
    rbi: int = 0
    runs: int = 0
    sb: int = 0
    bb: int = 0
    so: int = 0
    avg: float = 0.0
    obp: float = 0.0
    slg: float = 0.0
    ops: float = 0.0


@dataclass
class MLBPitchingStats:
    season: int
    games: int = 0
    games_started: int = 0
    wins: int = 0
    losses: int = 0
    saves: int = 0
    ip: float = 0.0
    hits: int = 0
    er: int = 0
    bb: int = 0
    so: int = 0
    era: float = 0.0
    whip: float = 0.0
    k_per_9: float = 0.0
    bb_per_9: float = 0.0


def _safe_float(val: str | float | int | None, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def get_player_batting_stats(
    mlbam_id: int, years: list[int],
) -> dict[int, MLBBattingStats | None]:
    """Fetch batting stats for a player across multiple seasons."""
    result: dict[int, MLBBattingStats | None] = {}
    for year in years:
        try:
            resp = httpx.get(
                f"{MLB_API_BASE}/people/{mlbam_id}/stats",
                params={"stats": "season", "season": year, "group": "hitting"},
                timeout=10,
            )
            resp.raise_for_status()
            splits = resp.json().get("stats", [{}])[0].get("splits", [])
            if not splits:
                result[year] = None
                continue
            s = splits[0].get("stat", {})
            result[year] = MLBBattingStats(
                season=year,
                games=s.get("gamesPlayed", 0),
                pa=s.get("plateAppearances", 0),
                ab=s.get("atBats", 0),
                hits=s.get("hits", 0),
                hr=s.get("homeRuns", 0),
                rbi=s.get("rbi", 0),
                runs=s.get("runs", 0),
                sb=s.get("stolenBases", 0),
                bb=s.get("baseOnBalls", 0),
                so=s.get("strikeOuts", 0),
                avg=_safe_float(s.get("avg")),
                obp=_safe_float(s.get("obp")),
                slg=_safe_float(s.get("slg")),
                ops=_safe_float(s.get("ops")),
            )
        except (httpx.HTTPError, Exception):
            result[year] = None
    return result


def get_player_pitching_stats(
    mlbam_id: int, years: list[int],
) -> dict[int, MLBPitchingStats | None]:
    """Fetch pitching stats for a player across multiple seasons."""
    result: dict[int, MLBPitchingStats | None] = {}
    for year in years:
        try:
            resp = httpx.get(
                f"{MLB_API_BASE}/people/{mlbam_id}/stats",
                params={"stats": "season", "season": year, "group": "pitching"},
                timeout=10,
            )
            resp.raise_for_status()
            splits = resp.json().get("stats", [{}])[0].get("splits", [])
            if not splits:
                result[year] = None
                continue
            s = splits[0].get("stat", {})
            ip_str = s.get("inningsPitched", "0")
            result[year] = MLBPitchingStats(
                season=year,
                games=s.get("gamesPlayed", 0),
                games_started=s.get("gamesStarted", 0),
                wins=s.get("wins", 0),
                losses=s.get("losses", 0),
                saves=s.get("saves", 0),
                ip=_safe_float(ip_str),
                hits=s.get("hits", 0),
                er=s.get("earnedRuns", 0),
                bb=s.get("baseOnBalls", 0),
                so=s.get("strikeOuts", 0),
                era=_safe_float(s.get("era")),
                whip=_safe_float(s.get("whip")),
                k_per_9=_safe_float(s.get("strikeoutsPer9Inn")),
                bb_per_9=_safe_float(s.get("walksPer9Inn")),
            )
        except (httpx.HTTPError, Exception):
            result[year] = None
    return result


def get_league_averages_batting(years: list[int]) -> dict[str, float]:
    """Compute per-player league-average batting line from team totals.

    Uses the teams/stats endpoint to get aggregate totals for all 30 MLB
    teams, then divides by approximate roster spots to get a per-player
    baseline.  Averages across the given *years*.
    """
    from statistics import mean

    _PER_TEAM_BATTERS = 13  # typical active position players per team

    attrs = ["hr", "rbi", "runs", "sb", "avg", "obp", "slg", "ops"]
    collected: dict[str, list[float]] = {a: [] for a in attrs}

    for year in years:
        try:
            resp = httpx.get(
                f"{MLB_API_BASE}/teams/stats",
                params={
                    "stats": "season", "season": year,
                    "group": "hitting", "sportId": 1, "gameType": "R",
                },
                timeout=15,
            )
            resp.raise_for_status()
            splits = resp.json().get("stats", [{}])[0].get("splits", [])
            if not splits:
                continue
            for sp in splits:
                s = sp.get("stat", {})
                gp = s.get("gamesPlayed", 162) or 162
                n = _PER_TEAM_BATTERS
                collected["hr"].append(s.get("homeRuns", 0) / n)
                collected["rbi"].append(s.get("rbi", 0) / n)
                collected["runs"].append(s.get("runs", 0) / n)
                collected["sb"].append(s.get("stolenBases", 0) / n)
                collected["avg"].append(_safe_float(s.get("avg")))
                collected["obp"].append(_safe_float(s.get("obp")))
                collected["slg"].append(_safe_float(s.get("slg")))
                collected["ops"].append(_safe_float(s.get("ops")))
        except (httpx.HTTPError, Exception):
            continue

    return {a: mean(vals) if vals else 0.0 for a, vals in collected.items()}


def get_league_averages_pitching(years: list[int]) -> dict[str, float]:
    """Compute per-pitcher league-average pitching line from team totals."""
    from statistics import mean

    _PER_TEAM_PITCHERS = 13  # typical pitching staff size

    attrs = ["wins", "saves", "so", "ip", "era", "whip", "k_per_9", "bb_per_9"]
    collected: dict[str, list[float]] = {a: [] for a in attrs}

    for year in years:
        try:
            resp = httpx.get(
                f"{MLB_API_BASE}/teams/stats",
                params={
                    "stats": "season", "season": year,
                    "group": "pitching", "sportId": 1, "gameType": "R",
                },
                timeout=15,
            )
            resp.raise_for_status()
            splits = resp.json().get("stats", [{}])[0].get("splits", [])
            if not splits:
                continue
            for sp in splits:
                s = sp.get("stat", {})
                n = _PER_TEAM_PITCHERS
                collected["wins"].append(s.get("wins", 0) / n)
                collected["saves"].append(s.get("saves", 0) / n)
                collected["so"].append(s.get("strikeOuts", 0) / n)
                collected["ip"].append(
                    _safe_float(s.get("inningsPitched")) / n,
                )
                collected["era"].append(_safe_float(s.get("era")))
                collected["whip"].append(_safe_float(s.get("whip")))
                collected["k_per_9"].append(
                    _safe_float(s.get("strikeoutsPer9Inn")),
                )
                collected["bb_per_9"].append(
                    _safe_float(s.get("walksPer9Inn")),
                )
        except (httpx.HTTPError, Exception):
            continue

    return {a: mean(vals) if vals else 0.0 for a, vals in collected.items()}


def _parse_game(g: dict) -> MLBGame:
    status = g.get("status", {})
    teams = g.get("teams", {})
    linescore = g.get("linescore", {})
    offense = linescore.get("offense", {})

    away = teams.get("away", {})
    home = teams.get("home", {})
    away_team_info = away.get("team", {})
    home_team_info = home.get("team", {})

    ls_away = linescore.get("teams", {}).get("away", {})
    ls_home = linescore.get("teams", {}).get("home", {})

    runners = (
        "first" in offense,
        "second" in offense,
        "third" in offense,
    )

    return MLBGame(
        away_team=away_team_info.get("teamName", away_team_info.get("name", "?")),
        away_abbr=away_team_info.get("abbreviation", "???"),
        away_score=away.get("score", 0),
        home_team=home_team_info.get("teamName", home_team_info.get("name", "?")),
        home_abbr=home_team_info.get("abbreviation", "???"),
        home_score=home.get("score", 0),
        status=status.get("abstractGameState", "Preview"),
        detail_status=status.get("detailedState", "Scheduled"),
        inning=linescore.get("currentInning", 0),
        inning_ordinal=linescore.get("currentInningOrdinal", ""),
        inning_half=linescore.get("inningHalf", ""),
        outs=linescore.get("outs", 0),
        start_time=g.get("gameDate", ""),
        runners=runners,
        away_hits=ls_away.get("hits", 0),
        away_errors=ls_away.get("errors", 0),
        home_hits=ls_home.get("hits", 0),
        home_errors=ls_home.get("errors", 0),
    )
