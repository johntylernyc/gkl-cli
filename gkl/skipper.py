"""Ask Skipper — natural language fantasy league assistant powered by Claude."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import anthropic

from gkl.yahoo_api import (
    YahooFantasyAPI, League, Matchup, StatCategory, PlayerStats, TeamStats,
    Transaction,
)
from gkl.stats import who_wins, compute_roto, simulate_h2h, compute_power_rankings
from gkl.statcast import lookup_mlbam_id
from gkl.mlb_api import (
    get_player_batting_stats, get_player_pitching_stats,
    MLBBattingStats, MLBPitchingStats,
)

ANTHROPIC_KEY_PATH = Path.home() / ".config" / "gkl" / "anthropic.json"


def load_anthropic_key() -> str | None:
    """Load the Anthropic API key from disk or environment."""
    if ANTHROPIC_KEY_PATH.exists():
        try:
            data = json.loads(ANTHROPIC_KEY_PATH.read_text())
            key = data.get("api_key", "").strip()
            if key:
                return key
        except (json.JSONDecodeError, KeyError):
            pass
    return os.environ.get("ANTHROPIC_API_KEY")


def save_anthropic_key(key: str) -> None:
    """Persist the Anthropic API key to disk."""
    ANTHROPIC_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANTHROPIC_KEY_PATH.write_text(json.dumps({"api_key": key}))


# -- Tool definitions (Anthropic tool_use format) --

TOOLS = [
    {
        "name": "get_league_standings",
        "description": (
            "Get current roto (rotisserie) standings for all teams in the league. "
            "Returns teams ranked by total roto points with their cumulative stats "
            "across all scoring categories. Roto points are computed by ranking "
            "each team per category and summing the ranks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_h2h_standings",
        "description": (
            "Get head-to-head standings with win-loss-tie records for all teams. "
            "Computes each team's matchup record across all completed weeks by "
            "comparing category-by-category results. Use this to answer questions "
            "about who is leading the league, overall records, and standings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "analyze_strength_of_schedule",
        "description": (
            "Analyze strength of schedule for all teams in the league. "
            "For each completed week, computes: (1) each team's actual opponent "
            "and the roto strength of that opponent, (2) power rankings — the "
            "hypothetical record each team would have if they played every other "
            "team that week, and (3) a luck factor comparing actual H2H record to "
            "power ranking record. Also shows remaining schedule with opponent "
            "roto ranks. Use this to contextualize H2H records — a team with a "
            "great record against weak opponents may be less impressive than one "
            "with a decent record against strong opponents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_matchup_scoreboard",
        "description": (
            "Get head-to-head matchup results for a given week. "
            "Returns all matchups with category-by-category scores. "
            "Omit week to get the current week."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week": {
                    "type": "integer",
                    "description": "Week number. Omit for current week.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_weekly_recap",
        "description": (
            "Get a comprehensive recap of a specific league week — designed "
            "for building a narrative summary of what happened across the "
            "entire league. Returns: (1) all H2H matchup results with category "
            "breakdowns, classifying each as a blowout, competitive, or upset, "
            "(2) that week's power rankings showing how each team would have "
            "fared against every opponent, (3) roto standings movement compared "
            "to the prior week, (4) standout weekly team performances (best "
            "individual stats that week), and (5) all transactions (trades, "
            "adds, drops) that happened during the week. Use this when asked "
            "for a league recap, weekly summary, or 'what happened last week'. "
            "Write the recap as a compelling league-wide narrative, not a "
            "data dump."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "week": {
                    "type": "integer",
                    "description": (
                        "Week number to recap. Omit to recap the most recently "
                        "completed week."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_team_roster",
        "description": (
            "Get a team's full roster with player stats. "
            "Use team_name to identify the team (partial match supported)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Team name (or partial match).",
                },
                "stat_type": {
                    "type": "string",
                    "enum": ["week", "season", "last7", "last30"],
                    "description": "Which stat window to return. Defaults to 'week'.",
                },
            },
            "required": ["team_name"],
        },
    },
    {
        "name": "find_trade_targets",
        "description": (
            "Analyze the entire league to find the best trade partners. "
            "Given a player the user wants to trade and a position they want "
            "to acquire, this tool examines every team's roster to find: "
            "(1) teams with surplus depth at the target position who can afford "
            "to move a player, (2) teams that have a need at the offered player's "
            "position, (3) teams whose category weaknesses align with the offered "
            "player's strengths, and (4) specific player targets on each team. "
            "Returns a ranked list of trade partners with analysis that you should "
            "use to craft a persuasive sales pitch for each viable trade."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "The user's team name (partial match supported).",
                },
                "offer_player_name": {
                    "type": "string",
                    "description": "Name of the player the user wants to trade away.",
                },
                "target_position": {
                    "type": "string",
                    "description": (
                        "Position the user wants to acquire "
                        "(e.g. SP, RP, C, 1B, 2B, 3B, SS, OF)."
                    ),
                },
            },
            "required": ["team_name", "offer_player_name", "target_position"],
        },
    },
    {
        "name": "get_free_agents",
        "description": (
            "Search available free agents in the league. "
            "Can filter by position and search by player name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "string",
                    "description": (
                        "Position filter: C, 1B, 2B, 3B, SS, LF, CF, RF, OF, "
                        "Util, SP, RP, P, BN, IL."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": "Player name search query.",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results to return (default 15, max 25).",
                },
            },
            "required": [],
        },
    },
]


class Skipper:
    """Chat assistant that uses Claude + Yahoo Fantasy API tools."""

    def __init__(
        self,
        api: YahooFantasyAPI,
        league: League,
        categories: list[StatCategory],
    ) -> None:
        self.api = api
        self.league = league
        self.categories = categories
        self._teams: list[TeamStats] | None = None
        self.history: list[dict] = []
        api_key = load_anthropic_key()
        if not api_key:
            raise ValueError("No Anthropic API key configured")
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def _build_system_prompt(self) -> str:
        cat_lines: list[str] = []
        for c in self.categories:
            if c.is_only_display:
                continue
            direction = "higher is better" if c.sort_order == "1" else "lower is better"
            ptype = "batting" if c.position_type == "B" else "pitching"
            cat_lines.append(f"  - {c.display_name} ({ptype}, {direction})")

        team_lines: list[str] = []
        if self._teams:
            for t in self._teams:
                team_lines.append(f"  - {t.name} (manager: {t.manager}, key: {t.team_key})")

        # Season phase determines how much to weight current vs prior year
        week = self.league.current_week
        if week <= 4:
            phase = "very early"
            phase_guidance = (
                f"It is VERY EARLY in the season (week {week}). Current-year stats "
                "are an extremely small sample and unreliable on their own. When "
                "evaluating players, weight prior-year performance HEAVILY — a "
                "player's track record over a full season is far more predictive "
                "than 2-4 weeks. Look for buy-low candidates whose current stats "
                "don't reflect their true talent, and sell-high candidates who are "
                "overperforming their track record."
            )
        elif week <= 8:
            phase = "early"
            phase_guidance = (
                f"It is still early in the season (week {week}). Current stats are "
                "starting to stabilize but still have significant noise. Weight "
                "prior-year stats meaningfully — a full prior season is still more "
                "informative than a few weeks. Use current stats to identify trends "
                "but don't overreact to small-sample performance."
            )
        elif week <= 14:
            phase = "mid"
            phase_guidance = (
                f"We are in the middle of the season (week {week}). Current-year "
                "stats are becoming a meaningful sample. Give roughly equal weight "
                "to current and prior-year performance. Players whose current stats "
                "diverge significantly from their track record may be adjusting or "
                "may have genuinely changed."
            )
        else:
            phase = "late"
            phase_guidance = (
                f"We are in the latter half of the season (week {week}). Current-year "
                "stats are a substantial sample and should be weighted more heavily "
                "than prior years, though career track record still provides context."
            )

        return (
            "You are Skipper, a sharp fantasy baseball analyst assistant inside the "
            "GKL Fantasy Baseball Command Center. You help the user understand their "
            "league, make roster decisions, and find advantages.\n\n"
            "## League Context\n"
            f"- League: {self.league.name}\n"
            f"- Season: {self.league.season}\n"
            f"- Current week: {week}\n"
            f"- Season phase: {phase}\n"
            f"- Teams: {self.league.num_teams}\n\n"
            f"## Season Phase Guidance\n{phase_guidance}\n\n"
            "## Scoring Categories\n"
            + "\n".join(cat_lines) + "\n\n"
            "## Teams\n"
            + ("\n".join(team_lines) if team_lines else "(loading...)") + "\n\n"
            "## Instructions\n"
            "- Use the provided tools to fetch live data before answering. "
            "Do not guess stats — always look them up.\n"
            "- Be concise and direct. Format stats in clean, readable text.\n"
            "- When comparing players or teams, highlight the key differences.\n"
            "- The user is a fantasy baseball manager in this league.\n"
            "- When tools return prior-year stats alongside current stats, always "
            "factor in the prior-year context per the season phase guidance above.\n"
            "- When suggesting trades, it's fine to trade a player at the same "
            "position the user wants to improve — but only if the return is a clear "
            "upgrade. Don't suggest trading away a team's best player at a position "
            "of need unless the incoming player is genuinely better (factoring in "
            "prior-year track record, not just a small current-year sample). "
            "Prefer trading from positions of surplus when possible.\n"
        )

    async def _ensure_teams(self) -> None:
        """Load team list once for team name resolution."""
        if self._teams is None:
            self._teams = await asyncio.to_thread(
                self.api.get_team_season_stats, self.league.league_key
            )

    def _resolve_team_key(self, name: str) -> str | None:
        """Fuzzy-match a team name to a team_key."""
        if not self._teams:
            return None
        name_lower = name.lower()
        for t in self._teams:
            if name_lower in t.name.lower() or name_lower in t.manager.lower():
                return t.team_key
        # Try substring match on team_key itself
        for t in self._teams:
            if name_lower in t.team_key.lower():
                return t.team_key
        return None

    async def _fetch_prior_year_lines(
        self, players: list[PlayerStats], prior_year: int,
    ) -> dict[str, str]:
        """Fetch prior-year stat lines for a list of players.

        Returns a dict mapping player name -> formatted prior-year stat line.
        Lookups that fail are silently skipped.
        """
        results: dict[str, str] = {}

        async def _lookup_one(p: PlayerStats) -> None:
            mlbam_id = await asyncio.to_thread(lookup_mlbam_id, p.name)
            if mlbam_id is None:
                return

            is_pitcher = p.position in ("SP", "RP", "P")
            if is_pitcher:
                stats = await asyncio.to_thread(
                    get_player_pitching_stats, mlbam_id, [prior_year]
                )
                s = stats.get(prior_year)
                if s and s.games > 0:
                    results[p.name] = (
                        f"{prior_year}: {s.games}G, {s.games_started}GS, "
                        f"{s.ip:.1f}IP, {s.era:.2f} ERA, {s.whip:.2f} WHIP, "
                        f"{s.so}K, {s.wins}W, {s.saves}SV, {s.holds}HLD"
                    )
            else:
                stats = await asyncio.to_thread(
                    get_player_batting_stats, mlbam_id, [prior_year]
                )
                s = stats.get(prior_year)
                if s and s.games > 0:
                    results[p.name] = (
                        f"{prior_year}: {s.games}G, {s.pa}PA, "
                        f".{int(s.avg*1000):03d} AVG, .{int(s.obp*1000):03d} OBP, "
                        f".{int(s.slg*1000):03d} SLG, "
                        f"{s.hr}HR, {s.rbi}RBI, {s.runs}R, {s.sb}SB"
                    )

        # Run lookups concurrently, but cap concurrency to avoid hammering the API
        sem = asyncio.Semaphore(8)

        async def _bounded(p: PlayerStats) -> None:
            async with sem:
                await _lookup_one(p)

        await asyncio.gather(*[_bounded(p) for p in players])
        return results

    async def _execute_tool(self, name: str, tool_input: dict) -> str:
        """Execute a tool call and return formatted text results."""
        try:
            match name:
                case "get_league_standings":
                    return await self._tool_standings()
                case "get_h2h_standings":
                    return await self._tool_h2h_standings()
                case "analyze_strength_of_schedule":
                    return await self._tool_strength_of_schedule()
                case "get_matchup_scoreboard":
                    return await self._tool_matchups(tool_input.get("week"))
                case "get_weekly_recap":
                    return await self._tool_weekly_recap(tool_input.get("week"))
                case "get_team_roster":
                    return await self._tool_roster(
                        tool_input["team_name"],
                        tool_input.get("stat_type", "week"),
                    )
                case "find_trade_targets":
                    return await self._tool_trade_targets(
                        tool_input["team_name"],
                        tool_input["offer_player_name"],
                        tool_input["target_position"],
                    )
                case "get_free_agents":
                    return await self._tool_free_agents(
                        tool_input.get("position"),
                        tool_input.get("search"),
                        tool_input.get("count", 15),
                    )
                case _:
                    return f"Unknown tool: {name}"
        except Exception as e:
            return f"Error executing {name}: {e}"

    async def _tool_standings(self) -> str:
        """Fetch and format league standings with roto points."""
        teams = await asyncio.to_thread(
            self.api.get_team_season_stats, self.league.league_key
        )
        self._teams = teams  # refresh cache

        scored = [c for c in self.categories if not c.is_only_display]

        # Compute roto rankings (sorted by total roto points, highest first)
        roto = compute_roto(teams, self.categories)

        lines = ["Roto Standings (ranked by total roto points):", ""]
        header = "Rank | Team | Manager | Roto Pts | " + " | ".join(c.display_name for c in scored)
        lines.append(header)
        lines.append("-" * len(header))
        for rank, r in enumerate(roto, 1):
            # Find the original TeamStats to get raw values
            raw_vals = [r.get(f"raw_{c.stat_id}", "-") for c in scored]
            lines.append(
                f"{rank}. {r['name']} | {r['manager']} | "
                f"{r['total']:.1f} | " + " | ".join(raw_vals)
            )
        return "\n".join(lines)

    async def _tool_h2h_standings(self) -> str:
        """Compute H2H standings across all completed weeks."""
        scored = [c for c in self.categories if not c.is_only_display]
        current = self.league.current_week

        # Fetch all weeks' matchups
        all_matchups: list[Matchup] = []
        for w in range(1, current + 1):
            week_matchups = await asyncio.to_thread(
                self.api.get_scoreboard, self.league.league_key, w
            )
            all_matchups.extend(week_matchups)

        # Tally records: team_key -> {wins, losses, ties, cat_wins, cat_losses, cat_ties}
        records: dict[str, dict] = {}
        for m in all_matchups:
            for tk in (m.team_a.team_key, m.team_b.team_key):
                if tk not in records:
                    records[tk] = {
                        "name": "", "manager": "",
                        "wins": 0, "losses": 0, "ties": 0,
                        "cat_wins": 0, "cat_losses": 0, "cat_ties": 0,
                    }

            a, b = m.team_a, m.team_b
            records[a.team_key]["name"] = a.name
            records[a.team_key]["manager"] = a.manager
            records[b.team_key]["name"] = b.name
            records[b.team_key]["manager"] = b.manager

            # Count category wins for this matchup
            a_cat_w, b_cat_w, cat_ties = 0, 0, 0
            for c in scored:
                result = who_wins(
                    a.stats.get(c.stat_id, "0"),
                    b.stats.get(c.stat_id, "0"),
                    c.sort_order,
                )
                if result == "a":
                    a_cat_w += 1
                elif result == "b":
                    b_cat_w += 1
                else:
                    cat_ties += 1

            # Update category totals
            records[a.team_key]["cat_wins"] += a_cat_w
            records[a.team_key]["cat_losses"] += b_cat_w
            records[a.team_key]["cat_ties"] += cat_ties
            records[b.team_key]["cat_wins"] += b_cat_w
            records[b.team_key]["cat_losses"] += a_cat_w
            records[b.team_key]["cat_ties"] += cat_ties

            # Determine matchup winner
            if a_cat_w > b_cat_w:
                records[a.team_key]["wins"] += 1
                records[b.team_key]["losses"] += 1
            elif b_cat_w > a_cat_w:
                records[b.team_key]["wins"] += 1
                records[a.team_key]["losses"] += 1
            else:
                records[a.team_key]["ties"] += 1
                records[b.team_key]["ties"] += 1

        # Sort by category record — the official league standings metric
        sorted_teams = sorted(
            records.items(),
            key=lambda x: (x[1]["cat_wins"], -x[1]["cat_losses"]),
            reverse=True,
        )

        lines = [f"H2H Standings through Week {current}:", ""]
        lines.append("  (Ranked by category record — the official league standings)")
        lines.append("Rank | Team | Manager | Cat Record (W-L-T) | Matchup Record (W-L-T)")
        lines.append("-" * 75)
        for rank, (_, r) in enumerate(sorted_teams, 1):
            cat_record = f"{r['cat_wins']}-{r['cat_losses']}-{r['cat_ties']}"
            matchup_record = f"{r['wins']}-{r['losses']}-{r['ties']}"
            lines.append(
                f"{rank}. {r['name']} | {r['manager']} | "
                f"{cat_record} | {matchup_record}"
            )
        return "\n".join(lines)

    async def _tool_strength_of_schedule(self) -> str:
        """Compute comprehensive strength-of-schedule analysis."""
        await self._ensure_teams()
        scored = [c for c in self.categories if not c.is_only_display]
        current = self.league.current_week
        league_key = self.league.league_key
        num_teams = len(self._teams)

        # Fetch weekly team stats and matchups for all completed weeks
        weekly_team_stats: dict[int, list[TeamStats]] = {}
        weekly_matchups: dict[int, list[Matchup]] = {}
        for w in range(1, current + 1):
            stats, matchups = await asyncio.gather(
                asyncio.to_thread(self.api.get_team_week_stats, league_key, w),
                asyncio.to_thread(self.api.get_scoreboard, league_key, w),
            )
            weekly_team_stats[w] = stats
            weekly_matchups[w] = matchups

        # Compute per-week roto rankings to measure team strength each week
        weekly_roto: dict[int, dict[str, dict]] = {}  # week -> team_key -> roto data
        for w, stats in weekly_team_stats.items():
            roto = compute_roto(stats, self.categories)
            weekly_roto[w] = {r["team_key"]: r for r in roto}

        # Compute season-long roto for overall team strength baseline
        season_teams = self._teams
        season_roto = compute_roto(season_teams, self.categories)
        season_roto_by_key = {r["team_key"]: r for r in season_roto}
        season_roto_rank = {
            r["team_key"]: rank for rank, r in enumerate(season_roto, 1)
        }

        # Compute per-week power rankings (hypothetical record vs all teams)
        weekly_power: dict[int, dict[str, tuple[int, int, int]]] = {}
        for w, stats in weekly_team_stats.items():
            h2h = simulate_h2h(stats, self.categories)
            rankings = compute_power_rankings(h2h, stats)
            weekly_power[w] = {
                s.team_key: (s.total_wins, s.total_losses, s.total_ties)
                for s in rankings
            }

        # For each team, build the full picture:
        # - actual opponent each week + opponent's roto rank that week
        # - actual H2H result
        # - power ranking that week
        # - aggregate SOS (avg opponent roto rank), luck factor
        team_data: dict[str, dict] = {}
        for t in self._teams:
            team_data[t.team_key] = {
                "name": t.name,
                "manager": t.manager,
                "actual_wins": 0, "actual_losses": 0, "actual_ties": 0,
                "power_wins": 0, "power_losses": 0, "power_ties": 0,
                "opp_roto_total": 0.0,
                "opp_season_roto_total": 0.0,
                "weeks_played": 0,
                "weekly_detail": [],
            }

        for w in range(1, current + 1):
            roto_this_week = weekly_roto.get(w, {})
            # Rank teams by roto points this week (highest = rank 1)
            week_roto_sorted = sorted(
                roto_this_week.values(),
                key=lambda r: r.get("total", 0),
                reverse=True,
            )
            week_roto_rank = {
                r["team_key"]: rank
                for rank, r in enumerate(week_roto_sorted, 1)
            }

            for m in weekly_matchups.get(w, []):
                a, b = m.team_a, m.team_b

                # Determine actual winner from category comparison
                a_cat_w, b_cat_w = 0, 0
                for c in scored:
                    result = who_wins(
                        a.stats.get(c.stat_id, "0"),
                        b.stats.get(c.stat_id, "0"),
                        c.sort_order,
                    )
                    if result == "a":
                        a_cat_w += 1
                    elif result == "b":
                        b_cat_w += 1

                for tk, opp_tk, cat_w, cat_l in [
                    (a.team_key, b.team_key, a_cat_w, b_cat_w),
                    (b.team_key, a.team_key, b_cat_w, a_cat_w),
                ]:
                    td = team_data.get(tk)
                    if not td:
                        continue

                    # Actual result
                    if cat_w > cat_l:
                        td["actual_wins"] += 1
                        result_str = "W"
                    elif cat_l > cat_w:
                        td["actual_losses"] += 1
                        result_str = "L"
                    else:
                        td["actual_ties"] += 1
                        result_str = "T"

                    # Opponent strength
                    opp_week_rank = week_roto_rank.get(opp_tk, num_teams)
                    opp_week_roto = roto_this_week.get(opp_tk, {}).get("total", 0)
                    opp_season_rank = season_roto_rank.get(opp_tk, num_teams)
                    opp_season_roto = season_roto_by_key.get(opp_tk, {}).get("total", 0)

                    td["opp_roto_total"] += opp_week_roto
                    td["opp_season_roto_total"] += opp_season_roto
                    td["weeks_played"] += 1

                    # Power ranking for this team this week
                    pw, pl, pt = weekly_power.get(w, {}).get(tk, (0, 0, 0))
                    td["power_wins"] += pw
                    td["power_losses"] += pl
                    td["power_ties"] += pt

                    # Find opponent name
                    opp_name = ""
                    for t in self._teams:
                        if t.team_key == opp_tk:
                            opp_name = t.name
                            break

                    td["weekly_detail"].append({
                        "week": w,
                        "opp": opp_name,
                        "opp_week_rank": opp_week_rank,
                        "opp_season_rank": opp_season_rank,
                        "result": result_str,
                        "cats": f"{cat_w}-{cat_l}",
                        "power": f"{pw}-{pl}-{pt}",
                    })

        # Try to fetch upcoming schedule (next few weeks)
        future_schedule: dict[str, list[dict]] = {
            t.team_key: [] for t in self._teams
        }
        for fw in range(current + 1, current + 4):
            try:
                future_matchups = await asyncio.to_thread(
                    self.api.get_scoreboard, league_key, fw
                )
                for m in future_matchups:
                    a_key, b_key = m.team_a.team_key, m.team_b.team_key
                    a_name = m.team_a.name
                    b_name = m.team_b.name
                    a_rank = season_roto_rank.get(a_key, "?")
                    b_rank = season_roto_rank.get(b_key, "?")
                    if a_key in future_schedule:
                        future_schedule[a_key].append({
                            "week": fw, "opp": b_name, "opp_rank": b_rank,
                        })
                    if b_key in future_schedule:
                        future_schedule[b_key].append({
                            "week": fw, "opp": a_name, "opp_rank": a_rank,
                        })
            except Exception:
                break

        # Build output sorted by SOS difficulty (highest avg opp roto = hardest)
        teams_sorted = sorted(
            team_data.values(),
            key=lambda td: (
                td["opp_season_roto_total"] / td["weeks_played"]
                if td["weeks_played"] > 0 else 0
            ),
            reverse=True,
        )

        lines = [
            f"STRENGTH OF SCHEDULE ANALYSIS (through Week {current})",
            f"({num_teams} teams, {current} weeks completed)",
            "",
        ]

        for rank, td in enumerate(teams_sorted, 1):
            wp = td["weeks_played"]
            avg_opp_roto = td["opp_season_roto_total"] / wp if wp > 0 else 0
            actual = f"{td['actual_wins']}-{td['actual_losses']}-{td['actual_ties']}"
            power = f"{td['power_wins']}-{td['power_losses']}-{td['power_ties']}"

            # Luck = actual wins - expected wins (from power rankings scaled)
            # Power rankings are vs all opponents, so scale to actual games
            total_power = td["power_wins"] + td["power_losses"] + td["power_ties"]
            if total_power > 0:
                expected_win_pct = td["power_wins"] / total_power
                expected_wins = expected_win_pct * wp
                luck = td["actual_wins"] - expected_wins
                luck_str = f"{luck:+.1f} wins"
            else:
                luck_str = "n/a"

            lines.append(
                f"#{rank} {td['name']} ({td['manager']})"
            )
            lines.append(
                f"  Actual Record: {actual} | "
                f"Power Record (vs all, all weeks): {power}"
            )
            lines.append(
                f"  Avg Opponent Roto Strength: {avg_opp_roto:.1f} pts "
                f"(higher = tougher schedule)"
            )
            lines.append(f"  Luck Factor: {luck_str}")

            # Week-by-week detail
            lines.append("  Week-by-week:")
            for wd in td["weekly_detail"]:
                lines.append(
                    f"    Wk {wd['week']}: vs {wd['opp']} "
                    f"(roto rank #{wd['opp_season_rank']}) "
                    f"=> {wd['result']} ({wd['cats']}) "
                    f"[power: {wd['power']}]"
                )

            # Upcoming schedule
            tk = None
            for t in self._teams:
                if t.name == td["name"]:
                    tk = t.team_key
                    break
            upcoming = future_schedule.get(tk, [])
            if upcoming:
                lines.append("  Upcoming:")
                for u in upcoming:
                    lines.append(
                        f"    Wk {u['week']}: vs {u['opp']} "
                        f"(roto rank #{u['opp_rank']})"
                    )

            lines.append("")

        return "\n".join(lines)

    async def _tool_matchups(self, week: int | None) -> str:
        """Fetch and format matchup scoreboard."""
        w = week if week is not None else self.league.current_week
        matchups = await asyncio.to_thread(
            self.api.get_scoreboard, self.league.league_key, w
        )
        if not matchups:
            return f"No matchups found for week {w}."

        scored = [c for c in self.categories if not c.is_only_display]
        lines = [f"Week {w} Matchups ({matchups[0].status}):"]
        for m in matchups:
            a, b = m.team_a, m.team_b
            lines.append(f"\n{a.name} vs {b.name}:")
            for c in scored:
                av = a.stats.get(c.stat_id, "-")
                bv = b.stats.get(c.stat_id, "-")
                lines.append(f"  {c.display_name}: {av} vs {bv}")
        return "\n".join(lines)

    async def _tool_weekly_recap(self, week: int | None) -> str:
        """Build a comprehensive weekly recap for narrative generation."""
        from datetime import datetime, timezone

        await self._ensure_teams()
        league_key = self.league.league_key
        scored = [c for c in self.categories if not c.is_only_display]
        current = self.league.current_week

        # Determine which week to recap
        if week is not None:
            recap_week = week
        else:
            # Most recently completed week: current if postevent, else previous
            test_matchups = await asyncio.to_thread(
                self.api.get_scoreboard, league_key, current
            )
            if test_matchups and test_matchups[0].status == "postevent":
                recap_week = current
            else:
                recap_week = max(1, current - 1)

        # Fetch all the data we need concurrently
        matchups_task = asyncio.to_thread(
            self.api.get_scoreboard, league_key, recap_week
        )
        week_stats_task = asyncio.to_thread(
            self.api.get_team_week_stats, league_key, recap_week
        )
        season_stats_task = asyncio.to_thread(
            self.api.get_team_season_stats, league_key
        )
        transactions_task = asyncio.to_thread(
            self.api.get_transactions, league_key, 100
        )
        matchups, week_stats, season_stats, all_transactions = await asyncio.gather(
            matchups_task, week_stats_task, season_stats_task, transactions_task
        )

        # Fetch all weeks' matchups for cumulative H2H standings
        all_week_matchups: list[Matchup] = list(matchups)  # include recap week
        for w in range(1, recap_week):
            wm = await asyncio.to_thread(
                self.api.get_scoreboard, league_key, w
            )
            all_week_matchups.extend(wm)

        # Compute cumulative H2H records
        h2h_records: dict[str, dict] = {}
        for m in all_week_matchups:
            a, b = m.team_a, m.team_b
            for tk in (a.team_key, b.team_key):
                if tk not in h2h_records:
                    h2h_records[tk] = {
                        "name": "", "manager": "",
                        "wins": 0, "losses": 0, "ties": 0,
                        "cat_wins": 0, "cat_losses": 0, "cat_ties": 0,
                    }
            h2h_records[a.team_key]["name"] = a.name
            h2h_records[a.team_key]["manager"] = a.manager
            h2h_records[b.team_key]["name"] = b.name
            h2h_records[b.team_key]["manager"] = b.manager

            a_cat_w, b_cat_w, cat_t = 0, 0, 0
            for c in scored:
                result = who_wins(
                    a.stats.get(c.stat_id, "0"),
                    b.stats.get(c.stat_id, "0"),
                    c.sort_order,
                )
                if result == "a":
                    a_cat_w += 1
                elif result == "b":
                    b_cat_w += 1
                else:
                    cat_t += 1
            h2h_records[a.team_key]["cat_wins"] += a_cat_w
            h2h_records[a.team_key]["cat_losses"] += b_cat_w
            h2h_records[a.team_key]["cat_ties"] += cat_t
            h2h_records[b.team_key]["cat_wins"] += b_cat_w
            h2h_records[b.team_key]["cat_losses"] += a_cat_w
            h2h_records[b.team_key]["cat_ties"] += cat_t
            if a_cat_w > b_cat_w:
                h2h_records[a.team_key]["wins"] += 1
                h2h_records[b.team_key]["losses"] += 1
            elif b_cat_w > a_cat_w:
                h2h_records[b.team_key]["wins"] += 1
                h2h_records[a.team_key]["losses"] += 1
            else:
                h2h_records[a.team_key]["ties"] += 1
                h2h_records[b.team_key]["ties"] += 1

        # Sort by category record (the official league standings metric)
        h2h_sorted = sorted(
            h2h_records.values(),
            key=lambda r: (r["cat_wins"], -r["cat_losses"]),
            reverse=True,
        )

        # ── Section 1: H2H Matchup Results ──
        matchup_results = []
        for m in matchups:
            a, b = m.team_a, m.team_b
            a_wins, b_wins, ties = 0, 0, 0
            cat_details = []
            for c in scored:
                result = who_wins(
                    a.stats.get(c.stat_id, "0"),
                    b.stats.get(c.stat_id, "0"),
                    c.sort_order,
                )
                if result == "a":
                    a_wins += 1
                elif result == "b":
                    b_wins += 1
                else:
                    ties += 1
                cat_details.append((c.display_name, a.stats.get(c.stat_id, "-"),
                                    b.stats.get(c.stat_id, "-"), result))

            margin = abs(a_wins - b_wins)
            total_cats = a_wins + b_wins + ties
            if margin >= total_cats * 0.6:
                matchup_type = "BLOWOUT"
            elif margin <= 1:
                matchup_type = "NAIL-BITER"
            else:
                matchup_type = "COMPETITIVE"

            if a_wins > b_wins:
                winner, loser = a.name, b.name
                w_cats, l_cats = a_wins, b_wins
            elif b_wins > a_wins:
                winner, loser = b.name, a.name
                w_cats, l_cats = b_wins, a_wins
            else:
                winner, loser = a.name, b.name
                w_cats, l_cats = a_wins, b_wins
                matchup_type = "TIE"

            matchup_results.append({
                "winner": winner, "loser": loser,
                "w_cats": w_cats, "l_cats": l_cats, "ties": ties,
                "type": matchup_type,
                "cat_details": cat_details,
                "team_a": a.name, "team_b": b.name,
            })

        # ── Section 2: Weekly Power Rankings ──
        h2h = simulate_h2h(week_stats, self.categories)
        power = compute_power_rankings(h2h, week_stats)

        # ── Section 3: Season Roto Standings (current) ──
        season_roto = compute_roto(season_stats, self.categories)

        # ── Section 4: Weekly Roto (who had the best week) ──
        week_roto = compute_roto(week_stats, self.categories)

        # ── Section 5: Standout Performances ──
        # Find league-leading individual week stats
        standouts = []
        bat_cats = [c for c in scored if c.position_type == "B"]
        pit_cats = [c for c in scored if c.position_type == "P"]
        week_stats_by_key = {t.team_key: t for t in week_stats}
        for c in scored:
            best_val = None
            best_team = None
            higher = c.sort_order == "1"
            for t in week_stats:
                try:
                    val = float(t.stats.get(c.stat_id, "0"))
                except (ValueError, TypeError):
                    continue
                if best_val is None or (higher and val > best_val) or (not higher and val < best_val):
                    best_val = val
                    best_team = t.name
            if best_val is not None and best_team:
                standouts.append((c.display_name, best_team, best_val))

        # ── Section 6: Transactions during this week ──
        week_start = None
        week_end = None
        if matchups:
            week_start = matchups[0].week_start
            week_end = matchups[0].week_end

        week_txns: list[Transaction] = []
        if week_start and week_end:
            try:
                start_ts = datetime.strptime(week_start, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                # End of the end date
                end_ts = datetime.strptime(week_end, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).timestamp() + 86400
                week_txns = [
                    tx for tx in all_transactions
                    if start_ts <= tx.timestamp <= end_ts
                ]
            except (ValueError, TypeError):
                week_txns = []

        trades = [tx for tx in week_txns if tx.type == "trade"]
        adds_drops = [tx for tx in week_txns if tx.type in ("add", "drop", "add/drop")]

        # ── Build Output ──
        lines = [
            f"WEEKLY LEAGUE RECAP — WEEK {recap_week}",
            f"({matchups[0].week_start} to {matchups[0].week_end})"
            if matchups else "",
            "",
        ]

        # Matchup results
        lines.append("═══ MATCHUP RESULTS ═══")
        for mr in matchup_results:
            if mr["type"] == "TIE":
                lines.append(
                    f"  [{mr['type']}] {mr['team_a']} vs {mr['team_b']}: "
                    f"{mr['w_cats']}-{mr['l_cats']}-{mr['ties']}"
                )
            else:
                lines.append(
                    f"  [{mr['type']}] {mr['winner']} def. {mr['loser']} "
                    f"{mr['w_cats']}-{mr['l_cats']}-{mr['ties']}"
                )
            # Show key categories
            decisive = [(name, av, bv, r) for name, av, bv, r in mr["cat_details"]]
            for name, av, bv, r in decisive:
                marker = "←" if r == "a" else ("→" if r == "b" else "=")
                lines.append(f"    {name}: {av} {marker} {bv}")
        lines.append("")

        # Weekly power rankings (who was actually strongest this week)
        lines.append("═══ WEEK'S POWER RANKINGS (hypothetical record vs all teams) ═══")
        for i, p in enumerate(power, 1):
            lines.append(f"  {i}. {p.name} ({p.manager}): {p.record_str}")
        lines.append("")

        # Weekly roto (best raw production this week)
        lines.append("═══ WEEKLY PRODUCTION LEADERS (roto points for this week only) ═══")
        for i, r in enumerate(week_roto[:5], 1):
            lines.append(f"  {i}. {r['name']} — {r['total']:.1f} roto pts")
        lines.append("")

        # Season standings
        lines.append("═══ SEASON ROTO STANDINGS (cumulative through this week) ═══")
        for i, r in enumerate(season_roto, 1):
            lines.append(f"  {i}. {r['name']} ({r['manager']}) — {r['total']:.1f} pts")
        lines.append("")

        # H2H standings — category record is the official standings metric
        lines.append(f"═══ H2H STANDINGS (through Week {recap_week}) ═══")
        lines.append("  (Ranked by category record — the official league standings)")
        for i, r in enumerate(h2h_sorted, 1):
            cat_record = f"{r['cat_wins']}-{r['cat_losses']}-{r['cat_ties']}"
            matchup_record = f"{r['wins']}-{r['losses']}-{r['ties']}"
            lines.append(
                f"  {i}. {r['name']} ({r['manager']}) — "
                f"{cat_record} (matchups: {matchup_record})"
            )
        lines.append("")

        # Stat leaders — every scored category, all teams shown ranked
        lines.append("═══ WEEKLY STAT LEADERS (all scored categories) ═══")
        for c in scored:
            higher = c.sort_order == "1"
            team_vals = []
            for t in week_stats:
                try:
                    val = float(t.stats.get(c.stat_id, "0"))
                except (ValueError, TypeError):
                    val = 0.0
                team_vals.append((t.name, val))
            team_vals.sort(key=lambda x: x[1], reverse=higher)
            ptype = "bat" if c.position_type == "B" else "pit"
            formatted = []
            for tname, val in team_vals:
                if val == int(val) and abs(val) < 10000:
                    formatted.append(f"{tname} {int(val)}")
                else:
                    formatted.append(f"{tname} {val:.3f}")
            lines.append(f"  {c.display_name} ({ptype}): " + " | ".join(formatted))
        lines.append("")

        # Transactions — enhanced with stats
        if trades:
            lines.append("═══ TRADES ═══")
            for tx in trades:
                involved = {}
                for p in tx.players:
                    team = p.to_team
                    if team not in involved:
                        involved[team] = []
                    involved[team].append(f"{p.name} ({p.position})")
                parts = [f"{team} gets {', '.join(players)}"
                         for team, players in involved.items()]
                lines.append(f"  {' ⟷ '.join(parts)}")
            lines.append("")

        # Build enhanced transaction wire: top adds with stats, notable drops
        _NON_TEAM = {"free agents", "freeagents", "waivers", ""}
        added_players: dict[str, str] = {}  # player_name -> to_team
        dropped_players: dict[str, str] = {}  # player_name -> from_team
        for tx in adds_drops:
            for p in tx.players:
                if p.action in ("add", "added") and p.to_team.lower() not in _NON_TEAM:
                    added_players[p.name] = p.to_team
                elif p.action in ("drop", "dropped") and p.from_team.lower() not in _NON_TEAM:
                    dropped_players[p.name] = p.from_team

        # Fetch draft costs to identify high-cost drops
        draft_costs = await asyncio.to_thread(
            self.api.get_draft_results, league_key
        )

        # Look up season stats for added players (top 5 by Yahoo rank)
        if added_players:
            add_stats: list[tuple[str, str, PlayerStats | None]] = []
            for name, team in list(added_players.items())[:10]:
                try:
                    results = await asyncio.to_thread(
                        self.api.search_players, league_key, name, 1
                    )
                    add_stats.append((name, team, results[0] if results else None))
                except Exception:
                    add_stats.append((name, team, None))

            # Sort by draft_cost descending as a proxy for perceived value,
            # then show top 5
            def _add_sort_key(item: tuple) -> float:
                _, _, ps = item
                if ps is None:
                    return 0
                try:
                    return float(ps.draft_cost) if ps.draft_cost else 0
                except (ValueError, TypeError):
                    return 0
            add_stats.sort(key=_add_sort_key, reverse=True)

            lines.append("═══ TOP ADDS THIS WEEK (with season stats) ═══")
            add_cats_bat = [c for c in scored if c.position_type == "B"]
            add_cats_pit = [c for c in scored if c.position_type == "P"]
            for name, team, ps in add_stats[:5]:
                if ps:
                    is_pit = ps.position in ("SP", "RP", "P")
                    cats = add_cats_pit if is_pit else add_cats_bat
                    vals = [f"{c.display_name}={ps.stats.get(c.stat_id, '-')}"
                            for c in cats]
                    lines.append(
                        f"  {name} ({ps.position}) → {team}: "
                        + ", ".join(vals)
                    )
                else:
                    lines.append(f"  {name} → {team}: (stats unavailable)")
            lines.append("")

        # Notable drops: players that were drafted (manager spent $ on them)
        if dropped_players:
            notable_drops: list[tuple[str, str, str, PlayerStats | None]] = []
            for name, team in dropped_players.items():
                # Look up if they were drafted by checking all player keys
                # We need to search for the player to get their key
                try:
                    results = await asyncio.to_thread(
                        self.api.search_players, league_key, name, 1
                    )
                    ps = results[0] if results else None
                    cost = draft_costs.get(ps.player_key, "") if ps else ""
                    if cost and cost != "0":
                        notable_drops.append((name, team, cost, ps))
                except Exception:
                    pass

            if notable_drops:
                notable_drops.sort(
                    key=lambda x: float(x[2]) if x[2] else 0, reverse=True
                )
                lines.append("═══ NOTABLE DROPS (drafted players hitting waivers) ═══")
                for name, team, cost, ps in notable_drops:
                    if ps:
                        is_pit = ps.position in ("SP", "RP", "P")
                        cats = add_cats_pit if is_pit else add_cats_bat
                        vals = [f"{c.display_name}={ps.stats.get(c.stat_id, '-')}"
                                for c in cats]
                        lines.append(
                            f"  {name} ({ps.position}) dropped by {team} "
                            f"[drafted ${cost}]: " + ", ".join(vals)
                        )
                    else:
                        lines.append(
                            f"  {name} dropped by {team} [drafted ${cost}]"
                        )
                lines.append("")

        lines.append(
            "INSTRUCTIONS FOR RESPONSE: Blend narrative and structured data. "
            "Use this format:\n"
            "1. **Headline** — one punchy sentence summarizing the week's biggest story\n"
            "2. **Matchup Results** — for each matchup, 1-2 sentences of context "
            "(was it an upset? a blowout? a rivalry?) plus the category record. "
            "Call out the decisive categories by name and value.\n"
            "3. **Standings Check** — show both H2H standings (win-loss record) "
            "and roto standings (points) side by side or in sequence. Note any "
            "meaningful movement, tightening races, or divergence between the two.\n"
            "4. **Power Rankings vs Reality** — highlight any teams whose power "
            "ranking diverges significantly from their actual record (lucky/unlucky).\n"
            "5. **Stat Leaders** — present all scored categories in a compact "
            "format showing who led each one. Group batting and pitching. "
            "Call out any especially dominant or surprising performances.\n"
            "6. **Transaction Wire** — lead with any trades. Then cover the top "
            "adds with their season stat lines (are they legit pickups or desperation "
            "moves?). Highlight any notable drops — players that were drafted with "
            "real auction dollars hitting waivers. Are any of them buy-low targets "
            "other managers should watch?\n"
            "7. **Looking Ahead** — a brief sentence or two about storylines to "
            "watch next week.\n\n"
            "Keep narrative sections engaging but grounded in the actual numbers. "
            "Every claim should be backed by a stat from the data above."
        )

        return "\n".join(lines)

    async def _tool_roster(self, team_name: str, stat_type: str) -> str:
        """Fetch and format a team's roster with prior-year context."""
        await self._ensure_teams()
        team_key = self._resolve_team_key(team_name)
        if not team_key:
            return f"Could not find team matching '{team_name}'."

        week = self.league.current_week
        fetch = {
            "season": self.api.get_roster_stats_season,
            "last7": self.api.get_roster_stats_last7,
            "last30": self.api.get_roster_stats_last30,
        }.get(stat_type, self.api.get_roster_stats)

        players: list[PlayerStats] = await asyncio.to_thread(fetch, team_key, week)

        # Fetch draft costs for this league and prior-year stats
        draft_costs = await asyncio.to_thread(
            self.api.get_draft_results, self.league.league_key
        )
        prior_year = int(self.league.season) - 1
        prior_stats = await self._fetch_prior_year_lines(players, prior_year)

        scored = [c for c in self.categories if not c.is_only_display]
        batters = [p for p in players if p.selected_position not in ("SP", "RP", "P", "IL", "IL+", "NA")]
        pitchers = [p for p in players if p.selected_position in ("SP", "RP", "P")]
        other = [p for p in players if p.selected_position in ("IL", "IL+", "NA", "BN")]

        def fmt_group(title: str, group: list[PlayerStats], cats: list[StatCategory]) -> list[str]:
            if not group:
                return []
            out = [f"\n{title}:"]
            for p in group:
                vals = [f"{c.display_name}={p.stats.get(c.stat_id, '-')}" for c in cats]
                pos = p.selected_position or p.position
                cost = draft_costs.get(p.player_key, "undrafted")
                cost_str = f"${cost}" if cost != "undrafted" else "undrafted"
                line = f"  {p.name} ({pos}, {p.team_abbr}) [drafted: {cost_str}]: " + ", ".join(vals)
                py = prior_stats.get(p.name)
                if py:
                    line += f"\n    Prior year: {py}"
                out.append(line)
            return out

        bat_cats = [c for c in scored if c.position_type == "B"]
        pit_cats = [c for c in scored if c.position_type == "P"]

        # Find team name for display
        display_name = team_name
        if self._teams:
            for t in self._teams:
                if t.team_key == team_key:
                    display_name = t.name
                    break

        lines = [
            f"Roster for {display_name} ({stat_type} stats):",
            f"(Prior year = {prior_year}; season phase = week {week})",
            "(Draft costs shown are what was paid in this league's auction draft. "
            "In keeper leagues, next year's cost is typically draft cost + $10. "
            "Undrafted players acquired via free agency are typically $10 next year.)",
        ]
        lines.extend(fmt_group("Batters", batters, bat_cats))
        lines.extend(fmt_group("Pitchers", pitchers, pit_cats))
        lines.extend(fmt_group("Bench/IL", other, bat_cats + pit_cats))
        return "\n".join(lines)

    async def _tool_trade_targets(
        self,
        team_name: str,
        offer_player_name: str,
        target_position: str,
    ) -> str:
        """Analyze the league to find optimal trade partners."""
        await self._ensure_teams()
        user_team_key = self._resolve_team_key(team_name)
        if not user_team_key:
            return f"Could not find team matching '{team_name}'."

        week = self.league.current_week
        scored = [c for c in self.categories if not c.is_only_display]
        bat_cats = [c for c in scored if c.position_type == "B"]
        pit_cats = [c for c in scored if c.position_type == "P"]
        target_pos_upper = target_position.upper()
        is_target_pitcher = target_pos_upper in ("SP", "RP", "P")

        # Fetch user's roster to find the offered player
        user_roster: list[PlayerStats] = await asyncio.to_thread(
            self.api.get_roster_stats_season, user_team_key, week
        )
        offer_player = None
        for p in user_roster:
            if offer_player_name.lower() in p.name.lower():
                offer_player = p
                break
        if not offer_player:
            return f"Could not find '{offer_player_name}' on your roster."

        # Fetch draft costs for keeper value context
        draft_costs = await asyncio.to_thread(
            self.api.get_draft_results, self.league.league_key
        )

        # Determine offered player's position type for need assessment
        offer_is_pitcher = offer_player.position in ("SP", "RP", "P")
        offer_positions = {pos.strip() for pos in offer_player.position.split(",")}

        # Get roto standings for category strength/weakness analysis
        roto = compute_roto(self._teams, self.categories)
        roto_by_key = {r["team_key"]: r for r in roto}

        # Fetch all other teams' rosters concurrently
        other_teams = [t for t in self._teams if t.team_key != user_team_key]
        roster_tasks = [
            asyncio.to_thread(self.api.get_roster_stats_season, t.team_key, week)
            for t in other_teams
        ]
        all_rosters = await asyncio.gather(*roster_tasks)

        # Collect all players we want prior-year stats for:
        # the offered player + all target-position players across the league
        prior_year = int(self.league.season) - 1
        players_for_prior: list[PlayerStats] = [offer_player]
        for roster in all_rosters:
            for p in roster:
                if p.selected_position in ("IL", "IL+", "NA"):
                    continue
                player_positions = {pos.strip() for pos in p.position.split(",")}
                if target_pos_upper in player_positions:
                    players_for_prior.append(p)
        prior_stats = await self._fetch_prior_year_lines(
            players_for_prior, prior_year
        )

        # Analyze each team as a potential trade partner
        candidates: list[dict] = []
        for team_stats, roster in zip(other_teams, all_rosters):
            tk = team_stats.team_key
            roto_data = roto_by_key.get(tk, {})

            # Count active players at target position (depth analysis)
            target_players = []
            for p in roster:
                if p.selected_position in ("IL", "IL+", "NA"):
                    continue
                player_positions = {pos.strip() for pos in p.position.split(",")}
                if target_pos_upper in player_positions:
                    target_players.append(p)

            depth_at_target = len(target_players)

            # Count active players at offered player's position (need analysis)
            need_players = []
            for p in roster:
                if p.selected_position in ("IL", "IL+", "NA"):
                    continue
                player_positions = {pos.strip() for pos in p.position.split(",")}
                if offer_positions & player_positions:
                    need_players.append(p)

            depth_at_offer_pos = len(need_players)

            # Category weakness analysis: where does this team rank poorly
            # in categories where the offered player excels?
            cat_fit_score = 0
            offer_cats = pit_cats if offer_is_pitcher else bat_cats
            weakness_cats = []
            for c in offer_cats:
                rank = roto_data.get(c.stat_id, 0)
                num_teams = len(self._teams)
                # Bottom half = weakness (rank closer to 1 is worse in roto)
                if isinstance(rank, (int, float)) and rank <= num_teams / 2:
                    # Check if the offered player is good in this category
                    try:
                        val = float(offer_player.stats.get(c.stat_id, "0"))
                        if val > 0 or (c.sort_order == "0" and val >= 0):
                            cat_fit_score += 1
                            weakness_cats.append(c.display_name)
                    except (ValueError, TypeError):
                        pass

            # Composite trade fit score:
            # - More depth at target = more willing to deal
            # - Less depth at offered position = more need
            # - More category weaknesses addressed = better fit
            surplus_score = max(0, depth_at_target - 2)  # >2 means surplus
            need_score = max(0, 4 - depth_at_offer_pos)  # <4 means need
            fit_score = surplus_score + need_score + cat_fit_score

            # Format target position players with stats + prior year
            relevant_cats = pit_cats if is_target_pitcher else bat_cats
            target_details = []
            for p in sorted(
                target_players,
                key=lambda p: sum(
                    float(p.stats.get(c.stat_id, "0"))
                    for c in relevant_cats
                    if c.sort_order == "1"
                ),
                reverse=True,
            ):
                vals = [
                    f"{c.display_name}={p.stats.get(c.stat_id, '-')}"
                    for c in relevant_cats
                ]
                slot = p.selected_position or p.position
                cost = draft_costs.get(p.player_key, "undrafted")
                cost_str = f"${cost}" if cost != "undrafted" else "undrafted"
                line = (
                    f"    {p.name} ({slot}, {p.team_abbr}) "
                    f"[drafted: {cost_str}]: "
                    + ", ".join(vals)
                )
                py = prior_stats.get(p.name)
                if py:
                    line += f"\n      Prior year: {py}"
                target_details.append(line)

            candidates.append({
                "team_name": team_stats.name,
                "manager": team_stats.manager,
                "fit_score": fit_score,
                "depth_at_target": depth_at_target,
                "depth_at_offer_pos": depth_at_offer_pos,
                "surplus_score": surplus_score,
                "need_score": need_score,
                "cat_fit_score": cat_fit_score,
                "weakness_cats": weakness_cats,
                "target_players": target_details,
            })

        # Sort by fit score descending
        candidates.sort(key=lambda c: c["fit_score"], reverse=True)

        # Format offered player stats
        offer_cats = pit_cats if offer_is_pitcher else bat_cats
        offer_vals = [
            f"{c.display_name}={offer_player.stats.get(c.stat_id, '-')}"
            for c in offer_cats
        ]

        # Find user team name
        user_team_name = team_name
        for t in self._teams:
            if t.team_key == user_team_key:
                user_team_name = t.name
                break

        offer_prior = prior_stats.get(offer_player.name, "")
        offer_cost = draft_costs.get(offer_player.player_key, "undrafted")
        offer_cost_str = f"${offer_cost}" if offer_cost != "undrafted" else "undrafted"

        lines = [
            f"TRADE TARGET ANALYSIS",
            f"Your team: {user_team_name}",
            f"Offering: {offer_player.name} ({offer_player.position}, "
            f"{offer_player.team_abbr}) [drafted: {offer_cost_str}]",
            f"  {self.league.season} stats: {', '.join(offer_vals)}",
        ]
        if offer_prior:
            lines.append(f"  Prior year: {offer_prior}")
        lines.extend([
            f"Looking for: {target_pos_upper}",
            f"Season phase: week {self.league.current_week} "
            f"(weight prior-year stats accordingly)",
            "",
            "(Draft costs shown are what was paid in this league's auction draft. "
            "In keeper leagues, next year's cost is typically draft cost + $10. "
            "Undrafted players acquired via free agency are typically $10 next year. "
            "Factor keeper value into trade recommendations — a great player on a "
            "cheap keeper contract is more valuable than the same player at a high cost.)",
            "",
        ])

        for i, c in enumerate(candidates):
            lines.append(f"--- #{i+1} {c['team_name']} ({c['manager']}) "
                         f"--- Fit Score: {c['fit_score']}")
            lines.append(f"  {target_pos_upper} depth: {c['depth_at_target']} players "
                         f"(surplus: {'YES' if c['surplus_score'] > 0 else 'no'})")
            lines.append(f"  Need at {offer_player.position}: "
                         f"{c['depth_at_offer_pos']} players "
                         f"({'NEED' if c['need_score'] > 0 else 'adequate'})")
            if c["weakness_cats"]:
                lines.append(f"  Weak in categories {offer_player.name} helps: "
                             f"{', '.join(c['weakness_cats'])}")
            lines.append(f"  Their {target_pos_upper} options:")
            for detail in c["target_players"]:
                lines.append(detail)
            lines.append("")

        lines.append(
            "INSTRUCTIONS FOR RESPONSE: Using this analysis, recommend the top "
            "2-3 trade targets. For each, suggest a specific player-for-player "
            "swap and write a persuasive sales pitch the user can send to that "
            "manager. The pitch should: (1) highlight how the offered player "
            "fills their specific category weaknesses, (2) explain why they can "
            "afford to move the target player given their depth, (3) frame "
            "the trade as mutually beneficial, and (4) factor in keeper value — "
            "mention draft costs and what each player will cost next year if "
            "it strengthens the case. A player on a cheap keeper deal is a major "
            "selling point. Keep pitches conversational and natural, not robotic."
        )

        return "\n".join(lines)

    async def _tool_free_agents(
        self,
        position: str | None,
        search: str | None,
        count: int,
    ) -> str:
        """Fetch and format available free agents with prior-year context."""
        count = min(count or 15, 25)
        players, total = await asyncio.to_thread(
            self.api.get_free_agents,
            self.league.league_key,
            position=position,
            search=search,
            count=count,
        )

        # Fetch prior-year stats for context
        prior_year = int(self.league.season) - 1
        prior_stats = await self._fetch_prior_year_lines(players, prior_year)

        scored = [c for c in self.categories if not c.is_only_display]
        label = f"position={position}" if position else "all positions"
        if search:
            label = f"search='{search}'"
        lines = [
            f"Free Agents ({label}, showing {len(players)} of {total}):",
            f"(Prior year = {prior_year}; season phase = week "
            f"{self.league.current_week})",
        ]
        for p in players:
            bat_cats = [c for c in scored if c.position_type == "B"]
            pit_cats = [c for c in scored if c.position_type == "P"]
            is_pitcher = p.position in ("SP", "RP", "P")
            cats = pit_cats if is_pitcher else bat_cats
            vals = [f"{c.display_name}={p.stats.get(c.stat_id, '-')}" for c in cats]
            line = f"  {p.name} ({p.position}, {p.team_abbr}): " + ", ".join(vals)
            py = prior_stats.get(p.name)
            if py:
                line += f"\n    Prior year: {py}"
            lines.append(line)
        return "\n".join(lines)

    async def chat(self, user_message: str) -> str:
        """Send a message and return the assistant's text response.

        Handles the tool_use loop internally — may make multiple API round-trips.
        """
        await self._ensure_teams()

        self.history.append({"role": "user", "content": user_message})

        # Truncate to last 20 messages to manage token usage
        if len(self.history) > 20:
            self.history = self.history[-20:]

        system_prompt = self._build_system_prompt()

        max_iterations = 10
        for _ in range(max_iterations):
            response = await self._client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                tools=TOOLS,
                messages=self.history,
            )

            # Build the assistant message content list
            assistant_content = []
            for block in response.content:
                if block.type == "text":
                    assistant_content.append({
                        "type": "text",
                        "text": block.text,
                    })
                elif block.type == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            self.history.append({"role": "assistant", "content": assistant_content})

            if response.stop_reason == "end_turn":
                # Extract text from response
                text_parts = [b.text for b in response.content if b.type == "text"]
                return "\n".join(text_parts) if text_parts else "(no response)"

            if response.stop_reason == "tool_use":
                # Execute all tool calls and append results
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = await self._execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                self.history.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason
                text_parts = [b.text for b in response.content if b.type == "text"]
                return "\n".join(text_parts) if text_parts else "(unexpected stop)"

        return "(reached maximum tool call iterations)"
