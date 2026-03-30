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
