"""Yahoo Fantasy Sports API client."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from gkl.yahoo_auth import YahooAuth

BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"


@dataclass
class StatCategory:
    stat_id: str
    display_name: str
    sort_order: str  # "1" = higher is better, "0" = lower is better
    position_type: str  # "B" = batter, "P" = pitcher
    is_only_display: bool = False  # True = not scored, display only


@dataclass
class TeamStats:
    team_key: str
    name: str
    manager: str
    points: float
    projected_points: float
    stats: dict[str, str] = field(default_factory=dict)  # stat_id -> value


@dataclass
class Matchup:
    week: int
    week_start: str
    week_end: str
    status: str  # "preevent", "midevent", "postevent"
    is_playoffs: bool
    is_tied: bool
    winner_team_key: str
    team_a: TeamStats
    team_b: TeamStats


@dataclass
class Transaction:
    transaction_key: str
    type: str  # "add", "drop", "add/drop", "trade"
    timestamp: int
    status: str  # "successful", etc.
    players: list[TransactionPlayer] = field(default_factory=list)


@dataclass
class TransactionPlayer:
    player_key: str
    name: str
    position: str
    team_abbr: str  # MLB team
    action: str  # "added", "dropped", "traded"
    from_team: str  # fantasy team name (or "Free Agents"/"Waivers")
    to_team: str  # fantasy team name (or "Free Agents"/"Waivers")
    from_team_key: str = ""
    to_team_key: str = ""


@dataclass
class League:
    league_key: str
    league_id: str
    name: str
    season: str
    current_week: int
    num_teams: int


@dataclass
class PlayerStats:
    player_key: str
    name: str
    position: str
    team_abbr: str
    stats: dict[str, str] = field(default_factory=dict)  # stat_id -> value
    draft_cost: str = ""  # auction draft cost if available
    selected_position: str = ""  # roster slot (e.g. "SS", "BN", "IL", "NA")


class YahooFantasyAPI:
    def __init__(self, auth: YahooAuth) -> None:
        self.auth = auth
        self._stat_categories: dict[str, list[StatCategory]] = {}

    def _get(self, path: str, retries: int = 2) -> dict:
        token = self.auth.get_token()
        for attempt in range(retries + 1):
            try:
                resp = httpx.get(
                    f"{BASE_URL}/{path}",
                    headers=token.auth_header(),
                    params={"format": "json"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                return resp.json()["fantasy_content"]
            except (httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt == retries:
                    raise
                import time
                time.sleep(1)
        raise RuntimeError("unreachable")

    def get_current_mlb_game_key(self) -> str:
        """Get the game key for the current MLB season."""
        data = self._get("game/mlb")
        game = data["game"][0]
        return game["game_key"]

    def get_user_leagues(self) -> list[League]:
        """Get the authenticated user's MLB fantasy leagues for the current season."""
        game_key = self.get_current_mlb_game_key()
        data = self._get(f"users;use_login=1/games;game_keys={game_key}/leagues")
        leagues: list[League] = []
        try:
            users = data["users"]
            user = users["0"]["user"]
            games = user[1]["games"]
            game_count = games["count"]
            for gi in range(game_count):
                game = games[str(gi)]["game"]
                league_data = game[1]["leagues"]
                league_count = league_data["count"]
                for li in range(league_count):
                    lg = league_data[str(li)]["league"][0]
                    leagues.append(League(
                        league_key=lg["league_key"],
                        league_id=lg["league_id"],
                        name=lg["name"],
                        season=lg["season"],
                        current_week=int(lg.get("current_week", 1)),
                        num_teams=int(lg["num_teams"]),
                    ))
        except (KeyError, IndexError, TypeError):
            pass
        return leagues

    def get_stat_categories(self, league_key: str) -> list[StatCategory]:
        """Get the stat categories for a league (cached)."""
        if league_key in self._stat_categories:
            return self._stat_categories[league_key]

        data = self._get(f"league/{league_key}/settings")
        categories: list[StatCategory] = []
        try:
            settings = data["league"][1]["settings"][0]
            stat_cats = settings["stat_categories"]["stats"]
            for entry in stat_cats:
                s = entry["stat"]
                categories.append(StatCategory(
                    stat_id=str(s["stat_id"]),
                    display_name=s.get("display_name", s.get("name", "")),
                    sort_order=str(s.get("sort_order", "1")),
                    position_type=s.get("position_type", ""),
                    is_only_display=s.get("is_only_display_stat") == "1",
                ))
        except (KeyError, IndexError, TypeError):
            pass

        self._stat_categories[league_key] = categories
        return categories

    def get_roster_stats_daily(self, team_key: str, week: int, date: str) -> list[PlayerStats]:
        """Get player-level stats for a team's roster for a specific date."""
        data = self._get(
            f"team/{team_key}/roster;week={week}/players"
            f";out=stats,draft_analysis;stats.type=date;stats.date={date}"
        )
        return self._parse_roster_players(data)

    def get_roster_stats_season(self, team_key: str, week: int) -> list[PlayerStats]:
        """Get player-level season stats for a team's roster."""
        data = self._get(
            f"team/{team_key}/roster;week={week}/players"
            f";out=stats,draft_analysis;stats.type=season"
        )
        return self._parse_roster_players(data)

    def get_roster_stats_last7(self, team_key: str, week: int) -> list[PlayerStats]:
        """Get player-level last 7 days stats for a team's roster."""
        data = self._get(
            f"team/{team_key}/roster;week={week}/players"
            f";out=stats,draft_analysis;stats.type=lastweek"
        )
        return self._parse_roster_players(data)

    def get_roster_stats_last30(self, team_key: str, week: int) -> list[PlayerStats]:
        """Get player-level last 30 days stats for a team's roster."""
        data = self._get(
            f"team/{team_key}/roster;week={week}/players"
            f";out=stats;stats.type=lastmonth"
        )
        return self._parse_roster_players(data)

    def get_roster_stats(self, team_key: str, week: int) -> list[PlayerStats]:
        """Get player-level stats for a team's roster for a given week."""
        data = self._get(
            f"team/{team_key}/roster;week={week}/players"
            f";out=stats,draft_analysis;stats.type=week;stats.week={week}"
        )
        return self._parse_roster_players(data)

    def get_free_agents(
        self,
        league_key: str,
        *,
        status: str = "FA",
        stat_type: str = "season",
        position: str | None = None,
        search: str | None = None,
        sort: str | None = None,
        sort_type: str | None = None,
        start: int = 0,
        count: int = 25,
    ) -> tuple[list[PlayerStats], int]:
        """Get players with stats, optional position/search/sort filters, and pagination."""
        path = f"league/{league_key}/players"
        if status:
            path += f";status={status}"
        path += (
            f";out=stats;stats.type={stat_type}"
            f";count={count};start={start}"
        )
        if position:
            path += f";position={position}"
        if search:
            path += f";search={search}"
        if sort:
            path += f";sort={sort}"
        if sort_type:
            path += f";sort_type={sort_type}"
        data = self._get(path)
        return self._parse_free_agent_players(data)

    _AR_RANK_CACHE = Path.home() / ".cache" / "gkl" / "ar_ranks.json"
    _AR_RANK_TTL = 3600  # 1 hour

    def build_rank_lookup(
        self, league_key: str, sort: str = "AR", max_players: int = 1000,
    ) -> dict[str, int]:
        """Build a player_key -> rank mapping by paginating all players sorted by rank.

        Uses no status filter so ALL players (rostered + free agents) are included,
        giving the true overall ranking.
        """
        # Try disk cache for AR sort (most common)
        if sort == "AR" and self._AR_RANK_CACHE.exists():
            try:
                data = json.loads(self._AR_RANK_CACHE.read_text())
                if (data.get("league_key") == league_key
                        and data.get("sort") == sort
                        and time.time() - data.get("timestamp", 0) < self._AR_RANK_TTL):
                    return data["ranks"]
            except (json.JSONDecodeError, KeyError):
                pass

        lookup: dict[str, int] = {}
        for start in range(0, max_players, 25):
            players, _ = self.get_free_agents(
                league_key, status=None,
                stat_type="season", sort=sort, sort_type="season",
                start=start, count=25,
            )
            for i, p in enumerate(players):
                lookup[p.player_key] = start + i + 1
            if len(players) < 25:
                break

        # Persist AR ranks to disk
        if sort == "AR":
            try:
                self._AR_RANK_CACHE.parent.mkdir(parents=True, exist_ok=True)
                self._AR_RANK_CACHE.write_text(json.dumps({
                    "league_key": league_key,
                    "sort": sort,
                    "timestamp": time.time(),
                    "ranks": lookup,
                }))
            except OSError:
                pass

        return lookup

    _PRESEASON_CACHE = Path.home() / ".cache" / "gkl" / "preseason_ranks.json"

    def get_preseason_ranks(self, league_key: str) -> dict[str, int]:
        """Get pre-season ranks, loading from cache file if available."""
        # Try loading from cache
        if self._PRESEASON_CACHE.exists():
            try:
                data = json.loads(self._PRESEASON_CACHE.read_text())
                if data.get("league_key") == league_key and data.get("ranks"):
                    return data["ranks"]
            except (json.JSONDecodeError, KeyError):
                pass

        # Build from API and cache
        ranks = self.build_rank_lookup(league_key, sort="OR")
        try:
            self._PRESEASON_CACHE.parent.mkdir(parents=True, exist_ok=True)
            self._PRESEASON_CACHE.write_text(json.dumps({
                "league_key": league_key,
                "ranks": ranks,
            }))
        except OSError:
            pass
        return ranks

    def get_week_dates(self, league_key: str) -> dict[int, tuple[str, str]]:
        """Get start/end dates for each week. Returns {week: (start, end)}. Cached."""
        if not hasattr(self, "_week_dates_cache"):
            self._week_dates_cache: dict[str, dict[int, tuple[str, str]]] = {}
        if league_key in self._week_dates_cache:
            return self._week_dates_cache[league_key]

        week_dates: dict[int, tuple[str, str]] = {}
        try:
            data = self._get(f"league/{league_key}/settings")
            settings = data["league"][1]["settings"]
            for item in settings:
                if isinstance(item, dict) and "roster_positions" in item:
                    continue
                if isinstance(item, dict) and "stat_categories" in item:
                    continue
        except (KeyError, IndexError, TypeError):
            pass

        # Fall back to scoreboard if settings doesn't work
        if not week_dates:
            # Get current week to know how many weeks exist
            try:
                data = self._get(f"league/{league_key}")
                current_week = int(data["league"][0].get("current_week", 1))
                for w in range(1, current_week + 1):
                    matchups = self.get_scoreboard(league_key, week=w)
                    if matchups:
                        week_dates[w] = (matchups[0].week_start, matchups[0].week_end)
            except (KeyError, IndexError, TypeError):
                pass

        self._week_dates_cache[league_key] = week_dates
        return week_dates

    def search_players(
        self, league_key: str, query: str, count: int = 10,
    ) -> list[PlayerStats]:
        """Search for players by name across all statuses."""
        players, _ = self.get_free_agents(
            league_key, status=None, search=query,
            stat_type="season", count=count,
        )
        return players

    def get_player_weekly_stats(
        self, league_key: str, player_key: str, week: int,
    ) -> PlayerStats | None:
        """Get a specific player's stats for a given week."""
        try:
            players, _ = self.get_free_agents(
                league_key, status=None,
                stat_type="week", sort=None, sort_type=None,
                count=1,
            )
            # This approach won't work for specific player + week.
            # Instead use the roster endpoint if we know the team.
            return None
        except Exception:
            return None

    def _parse_free_agent_players(self, data: dict) -> tuple[list[PlayerStats], int]:
        """Parse player list from a league/players (free agent) response."""
        players: list[PlayerStats] = []
        total = 0
        try:
            players_data = data["league"][1]["players"]
            total = int(players_data.get("count", 0))
            for i in range(total):
                key = str(i)
                if key not in players_data:
                    break
                p_wrapper = players_data[key]["player"]
                players.append(self._parse_player(p_wrapper))
        except (KeyError, IndexError, TypeError):
            pass
        return players, total

    _roster_debug_dumped = False

    def _parse_roster_players(self, data: dict) -> list[PlayerStats]:
        """Parse player list from a roster response."""
        players: list[PlayerStats] = []
        try:
            team_data = data["team"]
            roster = team_data[1]["roster"]
            players_data = roster.get("0", roster).get("players", roster.get("players", {}))
            count = int(players_data.get("count", 0))
            for i in range(count):
                p_wrapper = players_data[str(i)]["player"]
                # One-time debug dump of roster player data
                if not YahooFantasyAPI._roster_debug_dumped:
                    YahooFantasyAPI._roster_debug_dumped = True
                    try:
                        Path("/tmp/yahoo_roster_player_debug.json").write_text(
                            json.dumps(p_wrapper, indent=2, default=str)
                        )
                    except Exception:
                        pass
                players.append(self._parse_player(p_wrapper))
        except (KeyError, IndexError, TypeError):
            pass
        return players

    @staticmethod
    def _parse_player(player_wrapper: list) -> PlayerStats:
        """Parse a single player from Yahoo's nested array format."""
        name = ""
        player_key = ""
        position = ""
        team_abbr = ""
        stats: dict[str, str] = {}

        selected_position = ""
        if isinstance(player_wrapper[0], list):
            for item in player_wrapper[0]:
                if isinstance(item, dict):
                    if "player_key" in item:
                        player_key = item["player_key"]
                    if "name" in item:
                        n = item["name"]
                        name = n.get("full", f"{n.get('first', '')} {n.get('last', '')}")
                    if "display_position" in item:
                        position = item["display_position"]
                    if "editorial_team_abbr" in item:
                        team_abbr = item["editorial_team_abbr"]
                    if "selected_position" in item:
                        sp = item["selected_position"]
                        if isinstance(sp, list):
                            for sp_item in sp:
                                if isinstance(sp_item, dict) and "position" in sp_item:
                                    selected_position = sp_item["position"]
                        elif isinstance(sp, dict):
                            selected_position = sp.get("position", "")

        # Stats and draft analysis are in later elements
        draft_cost = ""
        for elem in player_wrapper:
            if isinstance(elem, dict):
                if "player_stats" in elem:
                    ps = elem["player_stats"]
                    for stat_entry in ps.get("stats", []):
                        s = stat_entry.get("stat", {})
                        sid = str(s.get("stat_id", ""))
                        val = s.get("value", "")
                        if sid:
                            stats[sid] = str(val)
                if "draft_analysis" in elem:
                    da = elem["draft_analysis"]
                    if isinstance(da, list):
                        for da_item in da:
                            if isinstance(da_item, dict) and "average_cost" in da_item:
                                draft_cost = str(da_item["average_cost"])
                    elif isinstance(da, dict):
                        cost = da.get("average_cost", da.get("cost", ""))
                        if cost:
                            draft_cost = str(cost)
                # selected_position may appear as a top-level element
                # in roster responses (not inside the metadata list)
                if "selected_position" in elem and not selected_position:
                    sp = elem["selected_position"]
                    if isinstance(sp, list):
                        for sp_item in sp:
                            if isinstance(sp_item, dict) and "position" in sp_item:
                                selected_position = sp_item["position"]
                    elif isinstance(sp, dict):
                        selected_position = sp.get("position", "")

        return PlayerStats(
            player_key=player_key,
            name=name,
            position=position,
            team_abbr=team_abbr,
            stats=stats,
            draft_cost=draft_cost,
            selected_position=selected_position,
        )

    def get_draft_results(self, league_key: str) -> dict[str, str]:
        """Get draft results mapping player_key -> cost paid (cached)."""
        if not hasattr(self, "_draft_cache"):
            self._draft_cache: dict[str, dict[str, str]] = {}
        if league_key in self._draft_cache:
            return self._draft_cache[league_key]

        results: dict[str, str] = {}
        try:
            data = self._get(f"league/{league_key}/draftresults")
            draft_data = data["league"][1]["draft_results"]
            count = int(draft_data.get("count", 0))
            for i in range(count):
                dr = draft_data[str(i)]["draft_result"]
                player_key = dr.get("player_key", "")
                cost = str(dr.get("cost", "0"))
                if player_key:
                    results[player_key] = cost
        except (KeyError, IndexError, TypeError):
            pass
        self._draft_cache[league_key] = results
        return results

    def get_transactions(self, league_key: str, count: int = 100) -> list[Transaction]:
        """Get league transactions (adds, drops, trades)."""
        data = self._get(
            f"league/{league_key}/transactions"
            f";count={count}"
        )
        # Debug dump to discover Yahoo's transaction format
        try:
            Path("/tmp/yahoo_transactions_debug.json").write_text(
                json.dumps(data, indent=2, default=str)
            )
        except Exception:
            pass

        transactions: list[Transaction] = []
        try:
            tx_data = data["league"][1]["transactions"]
            tx_count = int(tx_data.get("count", 0))
            for i in range(tx_count):
                key = str(i)
                if key not in tx_data:
                    break
                tx = tx_data[key]["transaction"]
                tx_meta = tx[0] if isinstance(tx[0], dict) else {}
                tx_players_raw = tx[1] if len(tx) > 1 else {}

                players: list[TransactionPlayer] = []
                if isinstance(tx_players_raw, dict) and "players" in tx_players_raw:
                    p_data = tx_players_raw["players"]
                    p_count = int(p_data.get("count", 0))
                    for j in range(p_count):
                        pk = str(j)
                        if pk not in p_data:
                            break
                        p_wrapper = p_data[pk]["player"]
                        p_info = p_wrapper[0] if isinstance(p_wrapper[0], list) else []
                        p_tx = p_wrapper[1] if len(p_wrapper) > 1 else {}

                        p_name = ""
                        p_key = ""
                        p_pos = ""
                        p_team = ""
                        for item in p_info:
                            if isinstance(item, dict):
                                if "player_key" in item:
                                    p_key = item["player_key"]
                                if "name" in item:
                                    n = item["name"]
                                    p_name = n.get("full", "")
                                if "display_position" in item:
                                    p_pos = item["display_position"]
                                if "editorial_team_abbr" in item:
                                    p_team = item["editorial_team_abbr"]

                        tx_detail = p_tx.get("transaction_data", p_tx)
                        if isinstance(tx_detail, list):
                            tx_detail = tx_detail[0] if tx_detail else {}
                        action = tx_detail.get("type", "")
                        src = tx_detail.get("source_team_name", "")
                        dst = tx_detail.get("destination_team_name", "")
                        src_key = tx_detail.get("source_team_key", "")
                        dst_key = tx_detail.get("destination_team_key", "")
                        if not src:
                            src = tx_detail.get("source_type", "")
                        if not dst:
                            dst = tx_detail.get("destination_type", "")

                        players.append(TransactionPlayer(
                            player_key=p_key,
                            name=p_name,
                            position=p_pos,
                            team_abbr=p_team,
                            action=action,
                            from_team=src,
                            to_team=dst,
                            from_team_key=src_key,
                            to_team_key=dst_key,
                        ))

                transactions.append(Transaction(
                    transaction_key=tx_meta.get("transaction_key", ""),
                    type=tx_meta.get("type", ""),
                    timestamp=int(tx_meta.get("timestamp", 0)),
                    status=tx_meta.get("status", ""),
                    players=players,
                ))
        except (KeyError, IndexError, TypeError):
            pass
        return transactions

    def get_team_season_stats(self, league_key: str) -> list[TeamStats]:
        """Get season-long cumulative stats for all teams in the league."""
        return self._get_all_team_stats(f"league/{league_key}/teams;out=stats")

    def get_team_week_stats(self, league_key: str, week: int) -> list[TeamStats]:
        """Get stats for all teams for a specific week."""
        return self._get_all_team_stats(
            f"league/{league_key}/teams/stats;type=week;week={week}"
        )

    def _get_all_team_stats(self, path: str) -> list[TeamStats]:
        """Fetch and parse all team stats from a given endpoint."""
        data = self._get(path)
        teams: list[TeamStats] = []
        try:
            teams_data = data["league"][1]["teams"]
            count = int(teams_data["count"])
            for i in range(count):
                team_wrapper = teams_data[str(i)]["team"]
                teams.append(self._parse_team(team_wrapper))
        except (KeyError, IndexError, TypeError):
            pass
        return teams

    def get_scoreboard(self, league_key: str, week: int | None = None) -> list[Matchup]:
        """Get matchups for a given week (defaults to current week)."""
        path = f"league/{league_key}/scoreboard"
        if week is not None:
            path = f"league/{league_key}/scoreboard;week={week}"
        data = self._get(path)

        matchups: list[Matchup] = []
        try:
            league_arr = data["league"]
            scoreboard = league_arr[1]["scoreboard"]
            matchup_data = scoreboard["0"]["matchups"]
            count = int(matchup_data["count"])
            for i in range(count):
                m = matchup_data[str(i)]["matchup"]
                teams = self._parse_matchup_teams(m)
                if len(teams) == 2:
                    matchups.append(Matchup(
                        week=int(m.get("week", 0)),
                        week_start=m.get("week_start", ""),
                        week_end=m.get("week_end", ""),
                        status=m.get("status", ""),
                        is_playoffs=bool(int(m.get("is_playoffs", "0"))),
                        is_tied=bool(int(m.get("is_tied", "0"))),
                        winner_team_key=m.get("winner_team_key", ""),
                        team_a=teams[0],
                        team_b=teams[1],
                    ))
        except (KeyError, IndexError, TypeError):
            pass
        return matchups

    def _parse_matchup_teams(self, matchup: dict) -> list[TeamStats]:
        """Parse the teams from a matchup's weird Yahoo JSON structure."""
        teams: list[TeamStats] = []
        teams_data = matchup.get("0", matchup).get("teams", matchup.get("teams", {}))
        count = int(teams_data.get("count", 0))
        for i in range(count):
            team_wrapper = teams_data[str(i)]["team"]
            teams.append(self._parse_team(team_wrapper))
        return teams

    @staticmethod
    def _parse_team(team_wrapper: list) -> TeamStats:
        """Parse a single team from Yahoo's nested array format."""
        name = ""
        team_key = ""
        manager = ""
        points = 0.0
        projected = 0.0
        stats: dict[str, str] = {}

        # First element is an array of metadata dicts
        if isinstance(team_wrapper[0], list):
            for item in team_wrapper[0]:
                if isinstance(item, dict):
                    if "team_key" in item:
                        team_key = item["team_key"]
                    if "name" in item:
                        name = item["name"]
                    if "managers" in item:
                        try:
                            mgrs = item["managers"]
                            manager = mgrs[0]["manager"]["nickname"]
                        except (KeyError, IndexError, TypeError):
                            pass

        # Second element has points and stats
        if len(team_wrapper) > 1 and isinstance(team_wrapper[1], dict):
            tp = team_wrapper[1].get("team_points", {})
            points = float(tp.get("total", 0))
            pp = team_wrapper[1].get("team_projected_points", {})
            projected = float(pp.get("total", 0))
            # Parse individual stat values
            ts = team_wrapper[1].get("team_stats", {})
            for stat_entry in ts.get("stats", []):
                s = stat_entry.get("stat", {})
                sid = str(s.get("stat_id", ""))
                val = s.get("value", "")
                if sid:
                    stats[sid] = str(val)

        return TeamStats(
            team_key=team_key,
            name=name,
            manager=manager,
            points=points,
            projected_points=projected,
            stats=stats,
        )
