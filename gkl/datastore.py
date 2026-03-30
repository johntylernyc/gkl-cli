"""Local SQLite data cache for roster snapshots and player stats.

Fetches roster data from Yahoo once per week per team and caches it locally.
The player explorer queries this cache instead of making hundreds of live API calls.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gkl.yahoo_api import League, YahooFantasyAPI

DB_DIR = Path.home() / ".cache" / "gkl"
DB_PATH = DB_DIR / "roster_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roster_snapshots (
    league_key TEXT NOT NULL,
    team_key TEXT NOT NULL,
    team_name TEXT NOT NULL,
    player_key TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_position TEXT NOT NULL,
    selected_position TEXT NOT NULL,
    week INTEGER NOT NULL,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (league_key, team_key, player_key, week)
);

CREATE TABLE IF NOT EXISTS sync_status (
    league_key TEXT NOT NULL,
    week INTEGER NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (league_key, week)
);

CREATE TABLE IF NOT EXISTS watchlist (
    league_key TEXT NOT NULL,
    player_key TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_position TEXT NOT NULL,
    team_abbr TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY (league_key, player_key)
);
"""


class RosterDataStore:
    """SQLite-backed cache for roster snapshot data."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # --- Sync ---

    def get_synced_weeks(self, league_key: str) -> set[int]:
        """Return the set of weeks that have been synced for this league."""
        rows = self._conn.execute(
            "SELECT week FROM sync_status WHERE league_key = ?",
            (league_key,),
        ).fetchall()
        return {row["week"] for row in rows}

    def sync_week(
        self,
        api: YahooFantasyAPI,
        league: League,
        week: int,
        team_keys: list[str],
        team_names: dict[str, str],
        week_start: str,
        week_end: str,
        *,
        on_progress: object = None,
    ) -> None:
        """Fetch and cache roster data for all teams for a specific week."""
        for team_key in team_keys:
            team_name = team_names.get(team_key, team_key)
            try:
                players = api.get_roster_stats(team_key, week)
            except Exception:
                continue

            for p in players:
                self._conn.execute(
                    """INSERT OR REPLACE INTO roster_snapshots
                       (league_key, team_key, team_name, player_key, player_name,
                        player_position, selected_position, week,
                        week_start, week_end, stats_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        league.league_key,
                        team_key,
                        team_name,
                        p.player_key,
                        p.name,
                        p.position,
                        p.selected_position or "BN",
                        week,
                        week_start,
                        week_end,
                        json.dumps(p.stats),
                    ),
                )

        from datetime import datetime
        self._conn.execute(
            """INSERT OR REPLACE INTO sync_status (league_key, week, synced_at)
               VALUES (?, ?, ?)""",
            (league.league_key, week, datetime.now().isoformat()),
        )
        self._conn.commit()

    def sync_all_weeks(
        self,
        api: YahooFantasyAPI,
        league: League,
        progress_callback=None,
    ) -> int:
        """Sync all weeks up to current_week. Returns number of weeks synced."""
        # Get team info
        teams = api.get_team_season_stats(league.league_key)
        team_keys = [t.team_key for t in teams]
        team_names = {t.team_key: t.name for t in teams}

        # Get week date ranges
        week_dates = api.get_week_dates(league.league_key)

        # Check which weeks are already synced
        synced = self.get_synced_weeks(league.league_key)

        # Don't re-sync past weeks; always re-sync current week
        weeks_to_sync = []
        for w in range(1, league.current_week + 1):
            if w not in synced or w == league.current_week:
                weeks_to_sync.append(w)

        synced_count = 0
        for w in weeks_to_sync:
            if progress_callback:
                progress_callback(
                    f"Syncing week {w}/{league.current_week} "
                    f"({len(team_keys)} teams)..."
                )
            w_start, w_end = week_dates.get(w, ("", ""))
            self.sync_week(
                api, league, w, team_keys, team_names,
                w_start, w_end,
            )
            synced_count += 1

        return synced_count

    # --- Queries for Player Explorer ---

    def get_player_stints(
        self, league_key: str, player_key: str,
    ) -> list[dict]:
        """Get all roster stints for a player, grouped by team and contiguous weeks.

        Returns list of dicts with keys:
        team_key, team_name, weeks (list of week data dicts)
        """
        rows = self._conn.execute(
            """SELECT team_key, team_name, player_position, selected_position,
                      week, week_start, week_end, stats_json
               FROM roster_snapshots
               WHERE league_key = ? AND player_key = ?
               ORDER BY week ASC""",
            (league_key, player_key),
        ).fetchall()

        if not rows:
            return []

        # Group into contiguous stints by team
        stints: list[dict] = []
        current_stint: dict | None = None

        for row in rows:
            row_dict = {
                "team_key": row["team_key"],
                "team_name": row["team_name"],
                "position": row["player_position"],
                "selected_position": row["selected_position"],
                "week": row["week"],
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "stats": json.loads(row["stats_json"]),
            }

            if (current_stint is None
                    or current_stint["team_key"] != row["team_key"]
                    or row["week"] != current_stint["weeks"][-1]["week"] + 1):
                # Start new stint
                current_stint = {
                    "team_key": row["team_key"],
                    "team_name": row["team_name"],
                    "weeks": [row_dict],
                }
                stints.append(current_stint)
            else:
                current_stint["weeks"].append(row_dict)

        return stints

    def get_player_usage_summary(
        self, league_key: str, player_key: str, total_weeks: int,
    ) -> dict:
        """Get usage breakdown: started/benched/IL/not-owned weeks and stats."""
        rows = self._conn.execute(
            """SELECT selected_position, stats_json
               FROM roster_snapshots
               WHERE league_key = ? AND player_key = ?""",
            (league_key, player_key),
        ).fetchall()

        result: dict[str, dict] = {
            "started": {"weeks": 0, "stats": {}},
            "benched": {"weeks": 0, "stats": {}},
            "il": {"weeks": 0, "stats": {}},
        }

        active_positions = {
            "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF",
            "Util", "DH", "SP", "RP", "P",
        }
        il_positions = {"IL", "IL+", "DL", "NA"}

        for row in rows:
            sel_pos = row["selected_position"]
            stats = json.loads(row["stats_json"])

            if sel_pos in active_positions:
                usage = "started"
            elif sel_pos in il_positions:
                usage = "il"
            else:
                usage = "benched"

            result[usage]["weeks"] += 1
            _accumulate_stats(result[usage]["stats"], stats)

        owned_weeks = sum(r["weeks"] for r in result.values())
        result["not_owned"] = {
            "weeks": max(0, total_weeks - owned_weeks),
            "stats": {},
        }

        return result

    def get_player_timeline(
        self, league_key: str, player_key: str, total_weeks: int,
    ) -> list[dict]:
        """Get week-by-week timeline data for a player.

        Returns list of dicts per week with: week, status, team_name, stats.
        """
        rows = self._conn.execute(
            """SELECT team_name, selected_position, week, week_start,
                      week_end, stats_json
               FROM roster_snapshots
               WHERE league_key = ? AND player_key = ?
               ORDER BY week ASC""",
            (league_key, player_key),
        ).fetchall()

        # Build lookup by week
        week_data: dict[int, dict] = {}
        for row in rows:
            sel_pos = row["selected_position"]
            active_positions = {
                "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF",
                "Util", "DH", "SP", "RP", "P",
            }
            il_positions = {"IL", "IL+", "DL", "NA"}

            if sel_pos in active_positions:
                status = "started"
            elif sel_pos in il_positions:
                status = "il"
            else:
                status = "benched"

            week_data[row["week"]] = {
                "week": row["week"],
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "status": status,
                "team_name": row["team_name"],
                "stats": json.loads(row["stats_json"]),
            }

        # Fill in all weeks
        timeline = []
        for w in range(1, total_weeks + 1):
            if w in week_data:
                timeline.append(week_data[w])
            else:
                timeline.append({
                    "week": w,
                    "week_start": "",
                    "week_end": "",
                    "status": "not_owned",
                    "team_name": "",
                    "stats": {},
                })

        return timeline

    def search_players(self, league_key: str, query: str) -> list[dict]:
        """Search for players in the cache by name."""
        rows = self._conn.execute(
            """SELECT DISTINCT player_key, player_name, player_position
               FROM roster_snapshots
               WHERE league_key = ? AND player_name LIKE ?
               ORDER BY player_name
               LIMIT 20""",
            (league_key, f"%{query}%"),
        ).fetchall()
        return [dict(row) for row in rows]

    # --- Watchlist ---

    def add_to_watchlist(
        self, league_key: str, player_key: str, player_name: str,
        player_position: str, team_abbr: str = "",
    ) -> None:
        from datetime import datetime
        self._conn.execute(
            """INSERT OR IGNORE INTO watchlist
               (league_key, player_key, player_name, player_position,
                team_abbr, added_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (league_key, player_key, player_name, player_position,
             team_abbr, datetime.now().isoformat()),
        )
        self._conn.commit()

    def remove_from_watchlist(self, league_key: str, player_key: str) -> None:
        self._conn.execute(
            "DELETE FROM watchlist WHERE league_key = ? AND player_key = ?",
            (league_key, player_key),
        )
        self._conn.commit()

    def get_watchlist(self, league_key: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT player_key, player_name, player_position, team_abbr, added_at
               FROM watchlist WHERE league_key = ?
               ORDER BY added_at DESC""",
            (league_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def is_on_watchlist(self, league_key: str, player_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM watchlist WHERE league_key = ? AND player_key = ?",
            (league_key, player_key),
        ).fetchone()
        return row is not None

    def clear_watchlist(self, league_key: str) -> None:
        self._conn.execute(
            "DELETE FROM watchlist WHERE league_key = ?",
            (league_key,),
        )
        self._conn.commit()


def _accumulate_stats(target: dict, source: dict) -> None:
    """Add counting stats from source into target."""
    for sid, val in source.items():
        if sid in ("3", "4", "5"):  # rate stats (AVG, OBP, SLG)
            continue
        if "/" in str(val):
            existing = target.get(sid, "0/0")
            e_parts = str(existing).split("/")
            v_parts = str(val).split("/")
            try:
                num = int(e_parts[0]) + int(v_parts[0])
                den = int(e_parts[1]) + int(v_parts[1])
                target[sid] = f"{num}/{den}"
            except (ValueError, IndexError):
                pass
        else:
            try:
                existing = int(target.get(sid, 0))
                target[sid] = str(existing + int(val))
            except (ValueError, TypeError):
                pass
