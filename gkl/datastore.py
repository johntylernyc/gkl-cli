"""Local SQLite data cache for roster snapshots and player stats.

Fetches roster data from Yahoo once per day per team and caches it locally.
The player explorer queries this cache instead of making thousands of live API calls.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
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
    date TEXT NOT NULL,
    week_start TEXT NOT NULL,
    week_end TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (league_key, team_key, player_key, week, date)
);

CREATE TABLE IF NOT EXISTS sync_status (
    league_key TEXT NOT NULL,
    date TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (league_key, date)
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

CREATE TABLE IF NOT EXISTS user_prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class RosterDataStore:
    """SQLite-backed cache for roster snapshot data."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate_if_needed()
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def _migrate_if_needed(self) -> None:
        """Drop old weekly-only tables if they lack the date column."""
        try:
            cols = self._conn.execute(
                "PRAGMA table_info(roster_snapshots)"
            ).fetchall()
            if cols and not any(c["name"] == "date" for c in cols):
                self._conn.executescript(
                    "DROP TABLE IF EXISTS roster_snapshots;"
                    "DROP TABLE IF EXISTS sync_status;"
                )
        except Exception:
            pass

    # --- Sync ---

    def get_synced_dates(self, league_key: str) -> set[str]:
        """Return the set of dates (YYYY-MM-DD) that have been synced."""
        rows = self._conn.execute(
            "SELECT date FROM sync_status WHERE league_key = ?",
            (league_key,),
        ).fetchall()
        return {row["date"] for row in rows}

    def sync_date(
        self,
        api: YahooFantasyAPI,
        league: League,
        week: int,
        date: str,
        team_keys: list[str],
        team_names: dict[str, str],
        week_start: str,
        week_end: str,
    ) -> None:
        """Fetch and cache roster data for all teams for a specific date."""
        for team_key in team_keys:
            team_name = team_names.get(team_key, team_key)
            try:
                players = api.get_roster_stats_daily(team_key, week, date)
            except Exception:
                continue

            for p in players:
                self._conn.execute(
                    """INSERT OR REPLACE INTO roster_snapshots
                       (league_key, team_key, team_name, player_key, player_name,
                        player_position, selected_position, week, date,
                        week_start, week_end, stats_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        league.league_key,
                        team_key,
                        team_name,
                        p.player_key,
                        p.name,
                        p.position,
                        p.selected_position or "BN",
                        week,
                        date,
                        week_start,
                        week_end,
                        json.dumps(p.stats),
                    ),
                )

        self._conn.execute(
            """INSERT OR REPLACE INTO sync_status (league_key, date, synced_at)
               VALUES (?, ?, ?)""",
            (league.league_key, date, datetime.now().isoformat()),
        )
        self._conn.commit()

    def sync_all_days(
        self,
        api: YahooFantasyAPI,
        league: League,
        progress_callback=None,
    ) -> int:
        """Sync all days up to today. Returns number of days synced."""
        teams = api.get_team_season_stats(league.league_key)
        team_keys = [t.team_key for t in teams]
        team_names = {t.team_key: t.name for t in teams}

        week_dates = api.get_week_dates(league.league_key)

        # Build list of all dates in the season up to today
        today = datetime.now().strftime("%Y-%m-%d")
        all_dates: list[tuple[str, int, str, str]] = []  # (date, week, w_start, w_end)
        for w in range(1, league.current_week + 1):
            w_start, w_end = week_dates.get(w, ("", ""))
            if not w_start or not w_end:
                continue
            d = datetime.strptime(w_start, "%Y-%m-%d")
            end = datetime.strptime(min(w_end, today), "%Y-%m-%d")
            while d <= end:
                all_dates.append((d.strftime("%Y-%m-%d"), w, w_start, w_end))
                d += timedelta(days=1)

        synced = self.get_synced_dates(league.league_key)
        today_str = today

        # Skip synced past dates; always re-sync today
        dates_to_sync = [
            (date, w, ws, we) for date, w, ws, we in all_dates
            if date not in synced or date == today_str
        ]

        total = len(dates_to_sync)
        synced_count = 0
        for i, (date, w, ws, we) in enumerate(dates_to_sync, 1):
            if progress_callback:
                progress_callback(
                    f"Syncing day {i}/{total} ({date}, "
                    f"{len(team_keys)} teams)..."
                )
            self.sync_date(api, league, w, date, team_keys, team_names, ws, we)
            synced_count += 1

        return synced_count

    def get_total_days(self, league_key: str) -> int:
        """Return the total number of distinct dates with data for this league."""
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT date) as cnt FROM roster_snapshots WHERE league_key = ?",
            (league_key,),
        ).fetchone()
        return row["cnt"] if row else 0

    # --- Queries for Player Explorer ---

    def get_player_stints(
        self, league_key: str, player_key: str,
    ) -> list[dict]:
        """Get all roster stints for a player, grouped by team and contiguous dates.

        Returns list of dicts with keys:
        team_key, team_name, days (list of day data dicts)
        """
        rows = self._conn.execute(
            """SELECT team_key, team_name, player_position, selected_position,
                      week, date, week_start, week_end, stats_json
               FROM roster_snapshots
               WHERE league_key = ? AND player_key = ?
               ORDER BY date ASC""",
            (league_key, player_key),
        ).fetchall()

        if not rows:
            return []

        # Group into contiguous stints by team (allow 1-day gaps for off-days)
        stints: list[dict] = []
        current_stint: dict | None = None

        for row in rows:
            row_dict = {
                "team_key": row["team_key"],
                "team_name": row["team_name"],
                "position": row["player_position"],
                "selected_position": row["selected_position"],
                "week": row["week"],
                "date": row["date"],
                "week_start": row["week_start"],
                "week_end": row["week_end"],
                "stats": json.loads(row["stats_json"]),
            }

            is_contiguous = False
            if current_stint is not None and current_stint["team_key"] == row["team_key"]:
                last_date = datetime.strptime(
                    current_stint["days"][-1]["date"], "%Y-%m-%d"
                )
                this_date = datetime.strptime(row["date"], "%Y-%m-%d")
                gap = (this_date - last_date).days
                is_contiguous = gap <= 2  # allow 1-day gap for off-days

            if is_contiguous:
                current_stint["days"].append(row_dict)
            else:
                current_stint = {
                    "team_key": row["team_key"],
                    "team_name": row["team_name"],
                    "days": [row_dict],
                }
                stints.append(current_stint)

        return stints

    def get_player_usage_summary(
        self, league_key: str, player_key: str, total_days: int,
    ) -> dict:
        """Get usage breakdown: started/benched/IL/not-owned days and stats."""
        rows = self._conn.execute(
            """SELECT selected_position, stats_json
               FROM roster_snapshots
               WHERE league_key = ? AND player_key = ?""",
            (league_key, player_key),
        ).fetchall()

        result: dict[str, dict] = {
            "started": {"days": 0, "stats": {}},
            "benched": {"days": 0, "stats": {}},
            "il": {"days": 0, "stats": {}},
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

            result[usage]["days"] += 1
            _accumulate_stats(result[usage]["stats"], stats)

        owned_days = sum(r["days"] for r in result.values())
        result["not_owned"] = {
            "days": max(0, total_days - owned_days),
            "stats": {},
        }

        return result

    def get_player_timeline(
        self, league_key: str, player_key: str,
    ) -> list[dict]:
        """Get day-by-day timeline data for a player.

        Returns list of dicts per day with: date, week, status, team_name, stats.
        """
        rows = self._conn.execute(
            """SELECT team_name, selected_position, week, date,
                      week_start, week_end, stats_json
               FROM roster_snapshots
               WHERE league_key = ? AND player_key = ?
               ORDER BY date ASC""",
            (league_key, player_key),
        ).fetchall()

        active_positions = {
            "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF",
            "Util", "DH", "SP", "RP", "P",
        }
        il_positions = {"IL", "IL+", "DL", "NA"}

        # Build lookup by date
        date_data: dict[str, dict] = {}
        for row in rows:
            sel_pos = row["selected_position"]
            if sel_pos in active_positions:
                status = "started"
            elif sel_pos in il_positions:
                status = "il"
            else:
                status = "benched"

            date_data[row["date"]] = {
                "date": row["date"],
                "week": row["week"],
                "status": status,
                "team_name": row["team_name"],
                "stats": json.loads(row["stats_json"]),
            }

        if not date_data:
            return []

        # Fill in all dates from first to last
        all_dates = sorted(date_data.keys())
        first = datetime.strptime(all_dates[0], "%Y-%m-%d")
        last = datetime.strptime(all_dates[-1], "%Y-%m-%d")

        timeline = []
        d = first
        while d <= last:
            ds = d.strftime("%Y-%m-%d")
            if ds in date_data:
                timeline.append(date_data[ds])
            else:
                timeline.append({
                    "date": ds,
                    "week": 0,
                    "status": "not_owned",
                    "team_name": "",
                    "stats": {},
                })
            d += timedelta(days=1)

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

    # --- User preferences ---------------------------------------------------

    def get_pref(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM user_prefs WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_pref(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO user_prefs (key, value) VALUES (?, ?)",
            (key, value),
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
