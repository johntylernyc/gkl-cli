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
from gkl.stats import (
    who_wins, compute_roto, simulate_h2h, compute_power_rankings,
    SGPCalculator,
)
from gkl.statcast import (
    lookup_mlbam_id, get_batter_statcast, get_pitcher_statcast,
)
from gkl.mlb_api import (
    get_player_batting_stats, get_player_pitching_stats,
    MLBBattingStats, MLBPitchingStats,
    get_mlb_scoreboard, get_mlb_boxscore,
)
from gkl.trade import (
    TradeSide,
    apply_trade_to_team,
    compute_trade_impact,
    replay_h2h_with_trade,
    compute_h2h_hypothetical,
    find_trade_targets as trade_find_targets,
    discover_trades,
    compute_compare_scenarios,
    project_player_per_week,
)

ANTHROPIC_KEY_PATH = Path.home() / ".config" / "gkl" / "anthropic.json"


def _web_key_path() -> Path | None:
    """Get the per-user Anthropic key path in web mode."""
    user_id = os.environ.get("GKL_USER_ID")
    db_dir = os.environ.get("GKL_DB_PATH")
    if not user_id or not db_dir:
        return None
    return Path(db_dir).parent / "anthropic_keys" / f"{user_id}.json"


def load_anthropic_key() -> str | None:
    """Load the Anthropic API key from env var, per-user file, or disk."""
    # GKL_ANTHROPIC_KEY is used in web mode (injected per-user by server)
    env_key = os.environ.get("GKL_ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    # In web mode, check per-user key file on shared volume
    web_path = _web_key_path()
    if web_path and web_path.exists():
        try:
            data = json.loads(web_path.read_text())
            key = data.get("api_key", "").strip()
            if key:
                return key
        except (json.JSONDecodeError, KeyError):
            pass

    if ANTHROPIC_KEY_PATH.exists():
        try:
            data = json.loads(ANTHROPIC_KEY_PATH.read_text())
            key = data.get("api_key", "").strip()
            if key:
                return key
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def save_anthropic_key(key: str) -> None:
    """Persist the Anthropic API key to disk."""
    if os.environ.get("GKL_MODE", "local").lower() == "web":
        # Save to per-user file on shared volume
        web_path = _web_key_path()
        if web_path:
            web_path.parent.mkdir(parents=True, exist_ok=True)
            web_path.write_text(json.dumps({"api_key": key}))
        return
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
            "Use team_name to identify the team (partial match supported). "
            "OMIT team_name to use the user's default team."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": (
                        "Team name (partial match). OMIT to use the user's team."
                    ),
                },
                "stat_type": {
                    "type": "string",
                    "enum": ["week", "season", "last7", "last30"],
                    "description": "Which stat window to return. Defaults to 'week'.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_trade_targets",
        "description": (
            "Trading Block: given a player the user wants to trade away, scan "
            "every other roster for realistic trade candidates and rank them by "
            "ΔSGP (player value swap), ΔRoto (projected roto points change using "
            "actual season stats), and ΔWin% (H2H record change using per-player "
            "weekly replay of completed weeks). Deals where the trade partner "
            "would lose too many roto points are filtered out as unrealistic. "
            "\n\n"
            "Use target_position to filter candidates to a specific position the "
            "user wants to acquire. This is the common case — the user is trading "
            "FROM a position of surplus TO a position of need (e.g., trading an OF "
            "bat for an SP). Without target_position, the tool defaults to the "
            "offered player's own positions, which is usually NOT what the user "
            "wants."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": (
                        "The user's team name. OMIT this field if the user has a "
                        "default team set — the tool will use it automatically."
                    ),
                },
                "offer_player_name": {
                    "type": "string",
                    "description": "Name of the player the user wants to trade away.",
                },
                "target_position": {
                    "type": "string",
                    "description": (
                        "Filter candidates to this position (e.g. 'SP', 'RP', "
                        "'C', '1B', '2B', '3B', 'SS', 'OF'). Pass this whenever "
                        "the user explicitly names a position they want to acquire."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max targets to return (default 20).",
                },
            },
            "required": ["offer_player_name"],
        },
    },
    {
        "name": "analyze_trade",
        "description": (
            "Analyze a specific two-sided trade between two teams. Given the "
            "players each team would send, computes full impact: per-category "
            "deltas, full league roto standings before/after (with batting and "
            "pitching subtotals), H2H weekly replay (completed weeks re-simulated "
            "with the trade applied, flagging matchups that would have flipped), "
            "and H2H hypothetical (your weekly stats vs all opponents across all "
            "completed weeks). Use this when the user proposes or describes a "
            "specific trade. If you don't know which players are involved, ask "
            "first — don't guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_a_name": {
                    "type": "string",
                    "description": "The user's team (or team A) name.",
                },
                "team_b_name": {
                    "type": "string",
                    "description": "The trade partner (team B) name.",
                },
                "team_a_players": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Player names Team A is sending.",
                },
                "team_b_players": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Player names Team B is sending.",
                },
            },
            "required": [
                "team_a_name", "team_b_name",
                "team_a_players", "team_b_players",
            ],
        },
    },
    {
        "name": "discover_trade_scenarios",
        "description": (
            "Trade Discovery: given a set of stat categories the user wants to "
            "improve, scan all opposing rosters for players strong in those "
            "categories and pair each target with a suggested trade offer from "
            "the user's roster (preferring cross-position offers so both sides "
            "benefit). Returns ranked scenarios with ΔSGP, ΔRoto, and partner "
            "impact. Use this when the user says 'I need help with HRs' or "
            "'how do I improve my pitching' — pick the stat categories that "
            "match the user's need and invoke this tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Team name. OMIT to use the user's default team.",
                },
                "stat_categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Display names of categories to improve (e.g. ['HR','RBI'] "
                        "or ['ERA','WHIP','K']). Must match category display names "
                        "from the scoring categories list in the system prompt."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max scenarios to return (default 15).",
                },
            },
            "required": ["stat_categories"],
        },
    },
    {
        "name": "compare_add_drop",
        "description": (
            "Evaluate adding a player (free agent or watchlisted) and dropping "
            "one of the user's current roster players. For each position-eligible "
            "drop candidate, computes ΔSGP, ΔRoto, and ΔWin% for the user's team. "
            "Also includes season Statcast metrics (xBA, xSLG, xwOBA, Barrel%, "
            "HardHit%, K%, BB%) for both the added and dropped players to inform "
            "regression judgments. Use this when a user asks 'should I pick up X' "
            "or 'is Y better than what I have'. Note: this models only the user's "
            "team — if the added player is on another team, use analyze_trade for "
            "the full league-wide impact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Team name. OMIT to use the user's default team.",
                },
                "add_player_name": {
                    "type": "string",
                    "description": "Player being added (free agent, watchlist, or other team's player).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max drop-candidate scenarios (default 15).",
                },
            },
            "required": ["add_player_name"],
        },
    },
    {
        "name": "get_mlb_scoreboard",
        "description": (
            "Get MLB game scores for a specific date. Returns all games with "
            "status (Preview/Live/Final), score, inning, runners on base, and "
            "inning-by-inning run data when available. Use when the user asks "
            "about today's (or a specific date's) MLB games, scores, or how "
            "live games are affecting fantasy rosters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": (
                        "Date in YYYY-MM-DD format. Omit for today."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_mlb_boxscore",
        "description": (
            "Get the full box score for a specific MLB game: batter lines (AB, H, "
            "R, HR, RBI, SB, BB, K) and pitcher lines (IP, H, R, ER, BB, K, ERA). "
            "Use when the user asks about a specific game, a player's performance "
            "in a game, or how a game affected fantasy standings. Requires gamePk "
            "from get_mlb_scoreboard."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "game_pk": {
                    "type": "string",
                    "description": "MLB gamePk ID from get_mlb_scoreboard.",
                },
            },
            "required": ["game_pk"],
        },
    },
    {
        "name": "get_statcast_profile",
        "description": (
            "Get season Statcast quality-of-contact metrics for a player: exit "
            "velocity, barrel rate, hard-hit rate, expected stats (xBA, xSLG, "
            "xwOBA), K% and BB%. For pitchers: also xERA and opposing-batter "
            "metrics. Use this to assess whether a player's surface stats are "
            "sustainable (actual better than expected = regression candidate; "
            "actual worse than expected = bounceback candidate)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "player_name": {
                    "type": "string",
                    "description": "Player name.",
                },
                "is_pitcher": {
                    "type": "boolean",
                    "description": (
                        "True for pitcher Statcast, false for batter. Omit "
                        "to auto-detect based on typical positions."
                    ),
                },
            },
            "required": ["player_name"],
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


AVAILABLE_MODELS = [
    ("claude-sonnet-4-6", "Sonnet 4.6"),
    ("claude-haiku-4-5-20251001", "Haiku 4.5"),
    ("claude-opus-4-6", "Opus 4.6"),
]
DEFAULT_MODEL = "claude-sonnet-4-6"


class Skipper:
    """Chat assistant that uses Claude + Yahoo Fantasy API tools."""

    def __init__(
        self,
        api: YahooFantasyAPI,
        league: League,
        categories: list[StatCategory],
        model: str = DEFAULT_MODEL,
        user_team_key: str | None = None,
        user_team_name: str | None = None,
    ) -> None:
        self.api = api
        self.league = league
        self.categories = categories
        self.model = model
        self.user_team_key = user_team_key
        self.user_team_name = user_team_name
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

        user_context = ""
        if self.user_team_name:
            user_context = (
                f"\n## The User's Team\n"
                f"- User is managing: **{self.user_team_name}**\n"
                f"- Always default to this team when the user asks 'who should I…', "
                f"'should I pick up…', 'my roster', etc. Do NOT ask for their team name.\n"
                f"- Only ask for a team name when the user is clearly referring to "
                f"another team (a trade partner, an opponent, etc.).\n"
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
            f"- Teams: {self.league.num_teams}\n"
            f"{user_context}\n"
            f"## Season Phase Guidance\n{phase_guidance}\n\n"
            "## Scoring Categories\n"
            + "\n".join(cat_lines) + "\n\n"
            "## Teams\n"
            + ("\n".join(team_lines) if team_lines else "(loading...)") + "\n\n"
            "## Instructions\n"
            "- Use the provided tools to fetch live data before answering. "
            "Do not guess stats — always look them up.\n"
            "- Be concise and direct.\n"
            "- When comparing players or teams, highlight the key differences.\n"
            "- When tools return prior-year stats alongside current stats, always "
            "factor in the prior-year context per the season phase guidance above.\n"
            "\n"
            "## Diagnosing roster strength — blend season roto with recent trends\n"
            "- Before concluding that a team's offense or pitching is 'weak', ALWAYS "
            "call `get_league_standings` first to see where they actually sit in the "
            "roto standings across each category. A team's most recent matchup is a "
            "very small sample and often misleading — a top-3 offense can lose a "
            "single week while still leading the league overall.\n"
            "- A team is 'strong' in a category when they're in the top third of the "
            "roto standings for it (roto points of 13+ in an 18-team league). "
            "'Weak' means bottom third (roto points of 6 or lower). In between is "
            "middle-of-the-pack. State the actual numbers; don't speculate.\n"
            "- ALSO call `get_team_roster` with stat_type='last30' to see how the "
            "roster has actually been producing over the past 30 days. Rosters "
            "change (trades, call-ups, injuries), so recent trends may differ from "
            "season totals. When season and last-30 disagree, call out the trend — "
            "e.g., 'season-long they're a top-5 offense, but they've been scuffling "
            "in the last month — Judge is hitting .190 since his IL return'.\n"
            "- If the user asks about improving their roster, anchor the recommendation "
            "in season roto gaps (categories where they have 6 or fewer roto points) "
            "AND recent under-performers on the roster. Both matter.\n"
            "- `get_weekly_recap` is for narrative summaries of a specific week. Do "
            "NOT use it as the basis for roster-construction advice — it reflects one "
            "week's variance, not season-long strength.\n"
            "\n"
            "## Player performance — always ground claims in current-year data\n"
            "- Do NOT make claims about a player's current-season performance "
            "without pulling the actual data first. If you're going to say "
            "'Bryce Elder hasn't shown anything better this season', you must have "
            "just fetched his 2026 ERA/WHIP/K via `get_team_roster` (if rostered), "
            "`get_free_agents` with a search, or `get_statcast_profile`.\n"
            "- For free agent or trade-target recommendations, pull BOTH season and "
            "recent-window stats. The tools support this:\n"
            "  - `get_free_agents` returns season stats by default — but if the user "
            "is asking about who's HOT right now, explicitly note that you'd need "
            "recent-window data to fully answer.\n"
            "  - `get_team_roster` supports stat_type='last30' (or 'last7') to see "
            "trailing performance — USE this when a player's season numbers look "
            "bad but they may have turned a corner, or vice versa.\n"
            "  - `get_statcast_profile` surfaces regression signals (xBA vs AVG, "
            "xERA vs ERA, Barrel%) to distinguish sustainable performance from "
            "luck.\n"
            "- If you don't have current-year data for a player you're mentioning, "
            "say so plainly ('haven't pulled his 2026 line, but last year he was…') "
            "rather than fabricating a characterization.\n"
            "\n"
            "## Injury / availability status — never speculate\n"
            "- NEVER say a player is 'injured', 'coming back', 'on the mend', or "
            "similar without evidence. The `get_team_roster` tool tags every "
            "player explicitly with one of: [ACTIVE — in starting lineup], "
            "[BENCH — active], [INJURED/IL], [NOT-ACTIVE]. Read that tag and "
            "report it faithfully.\n"
            "- A pitcher with modest recent innings might simply be a long-reliever, "
            "mid-rotation starter with occasional skipped turns, or in between "
            "starts. Low volume ≠ injured. If the status tag says [ACTIVE] or "
            "[BENCH — active], the player is available.\n"
            "- If the user asks whether someone is hurt and the tag doesn't say "
            "[INJURED/IL], say 'he's listed as active on the roster — I don't "
            "have injury news beyond that' rather than guessing.\n"
            "\n"
            "## Formatting — write like a sports broadcaster, not a dashboard\n"
            "- Do NOT use markdown tables — they render as raw pipes in the "
            "terminal and are unreadable.\n"
            "- Avoid heavy markdown: no bold (`**text**`), no `###` section "
            "headers, no bullet-point walls, no `---` rules. These clutter the "
            "output and make it feel robotic.\n"
            "- Prefer flowing natural-language paragraphs that sound like an "
            "analyst talking. A short unstyled header or two is fine for long "
            "answers; otherwise just write prose.\n"
            "- Use a small dash of light bullets only when you're genuinely "
            "listing parallel items (e.g., three candidate players). Even then, "
            "describe each in sentence form, not as a stat dump.\n"
            "- Never shorthand with symbols the user won't recognize (ΔRoto, "
            "ΔSGP, ΔWin%). Use plain language: 'gains 20 roto points', "
            "'improves his H2H record by 3 wins', 'slight upgrade in value'.\n"
            "- Do NOT name internal tools to the user. Never say 'run "
            "analyze_trade' or 'let me call find_trade_targets'. Say 'I can "
            "break down the category-by-category impact if you like' or 'happy "
            "to dig deeper into any of these'.\n"
            "\n"
            "## Using the Trade Analyzer Suite\n"
            "Prefer the trade-analyzer tools over ad-hoc reasoning for any trade "
            "question. These tools use the same SGP-based engine that powers the "
            "in-app Trade Analyzer and produce consistent, honest numbers:\n"
            "- `find_trade_targets`: user has a specific player to trade and wants "
            "targets. Returns ΔSGP, ΔRoto, ΔWin% per candidate plus a partner-"
            "benefit column. Realistic deals only (partner wouldn't lose heavily).\n"
            "- `analyze_trade`: two specific teams and named players on each side. "
            "Returns full roto, H2H, and category impact plus a weekly replay of "
            "which actual matchups would have flipped. Ask for specifics before "
            "running this — don't guess.\n"
            "- `discover_trade_scenarios`: user wants to improve specific stat "
            "categories (e.g. 'I need more HRs'). Scans all rosters and returns "
            "ranked target + suggested offer pairs.\n"
            "- `compare_add_drop`: user is considering a free agent or other "
            "team's player as an add. Returns ranked drop-candidates with roto "
            "and H2H impact. If the add player is rostered on another team, "
            "mention that `analyze_trade` gives the full league-wide view.\n"
            "\n"
            "## Statcast Regression Signals\n"
            "`get_statcast_profile` and the Statcast line inside `compare_add_drop` "
            "surface expected-stats metrics. Use them like this:\n"
            "- Actual AVG/SLG > xBA/xSLG → regression candidate (overperforming)\n"
            "- Actual AVG/SLG < xBA/xSLG → bounceback candidate (underperforming)\n"
            "- Low Barrel% and HardHit% despite strong surface stats → unsustainable\n"
            "- High K%, low BB% → volatile floor even with good current production\n"
            "- For pitchers: ERA vs xERA, opposing xwOBA, and K%/BB% tell the same "
            "story.\n"
            "\n"
            "## MLB Game Data\n"
            "Use `get_mlb_scoreboard` for game-day questions ('how are my players "
            "doing today?', 'who's playing tonight?'). Use `get_mlb_boxscore` with "
            "a gamePk from the scoreboard when the user asks about a specific "
            "game or player performance in a game.\n"
            "\n"
            "## General Trade Heuristics\n"
            "- **Trade surplus to fill needs.** If the user offers an OF to target "
            "an SP, the realistic deal pairs their OF surplus with the partner's "
            "SP surplus — NOT an OF-for-OF swap. Always think about what position "
            "the user wants to acquire and filter `find_trade_targets` results to "
            "players at that position. Pass `target_position` when the user names "
            "one (e.g. 'trading Teoscar for an SP' → target_position='SP').\n"
            "- Don't trade a team's best player at a position of need unless the "
            "return is a clear upgrade (factoring in track record).\n"
            "- Cross-position trades (batter for pitcher) are the norm, not the "
            "exception. Most managers prefer filling a different need.\n"
            "- If `find_trade_targets` returns only same-position candidates when "
            "the user asked for a specific position, you should call it again with "
            "the explicit `target_position` filter (or rerun `discover_trade_scenarios` "
            "using categories that represent the target position).\n"
        )

    async def _ensure_teams(self) -> None:
        """Load team list once for team name resolution."""
        if self._teams is None:
            self._teams = await asyncio.to_thread(
                self.api.get_team_season_stats, self.league.league_key
            )

    def _resolve_team_key(self, name: str | None) -> str | None:
        """Fuzzy-match a team name to a team_key.

        If name is None/empty, falls back to the user's configured default team.
        """
        if not name:
            return self.user_team_key
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

    def _team_display_name(self, team_key: str) -> str:
        """Get the display name for a team key, falling back to the key itself."""
        if self._teams:
            for t in self._teams:
                if t.team_key == team_key:
                    return t.name
        return team_key

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
                        tool_input.get("team_name"),
                        tool_input.get("stat_type", "week"),
                    )
                case "find_trade_targets":
                    return await self._tool_trade_targets(
                        tool_input.get("team_name"),
                        tool_input["offer_player_name"],
                        tool_input.get("target_position"),
                        tool_input.get("max_results", 20),
                    )
                case "analyze_trade":
                    return await self._tool_analyze_trade(
                        tool_input["team_a_name"],
                        tool_input["team_b_name"],
                        tool_input["team_a_players"],
                        tool_input["team_b_players"],
                    )
                case "discover_trade_scenarios":
                    return await self._tool_discover_trades(
                        tool_input.get("team_name"),
                        tool_input["stat_categories"],
                        tool_input.get("max_results", 15),
                    )
                case "compare_add_drop":
                    return await self._tool_compare_add_drop(
                        tool_input.get("team_name"),
                        tool_input["add_player_name"],
                        tool_input.get("max_results", 15),
                    )
                case "get_mlb_scoreboard":
                    return await self._tool_mlb_scoreboard(
                        tool_input.get("date"),
                    )
                case "get_mlb_boxscore":
                    return await self._tool_mlb_boxscore(
                        tool_input["game_pk"],
                    )
                case "get_statcast_profile":
                    return await self._tool_statcast_profile(
                        tool_input["player_name"],
                        tool_input.get("is_pitcher"),
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
            "IMPORTANT: Stats are always explicitly attributed to a team by name",
            "(e.g., 'ERA: Mary's Little Lambs 1.25 (W) vs The Revs. 4.14 (L)').",
            "Never swap which team owns which number.",
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
            # Show each category with explicit team attribution so the LLM
            # can't mis-attribute stats (e.g., "ERA 1.25 ← 4.14" is too
            # ambiguous — use "ERA: Mary's Little Lambs 1.25 (W) vs The Revs. 4.14").
            for name, av, bv, r in mr["cat_details"]:
                if r == "a":
                    a_tag, b_tag = "(W)", "(L)"
                elif r == "b":
                    a_tag, b_tag = "(L)", "(W)"
                else:
                    a_tag = b_tag = "(T)"
                lines.append(
                    f"    {name}: {mr['team_a']} {av} {a_tag} vs "
                    f"{mr['team_b']} {bv} {b_tag}"
                )
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

    async def _tool_roster(self, team_name: str | None, stat_type: str) -> str:
        """Fetch and format a team's roster with prior-year context."""
        await self._ensure_teams()
        team_key = self._resolve_team_key(team_name)
        if not team_key:
            label = team_name or "(no default team configured)"
            return f"Could not find team matching '{label}'."
        team_name = self._team_display_name(team_key)

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
                # Explicit status tag so the LLM never has to infer injury
                if p.selected_position in ("IL", "IL+"):
                    status = "[INJURED/IL]"
                elif p.selected_position == "NA":
                    status = "[NOT-ACTIVE]"
                elif p.selected_position == "BN":
                    status = "[BENCH — active]"
                else:
                    status = "[ACTIVE — in starting lineup]"
                line = (
                    f"  {p.name} ({pos}, {p.team_abbr}) {status} "
                    f"[drafted: {cost_str}]: " + ", ".join(vals)
                )
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

    # -- Helpers for trade/compare tools --

    def _find_player_on_any_roster(
        self,
        player_name: str,
        all_rosters: dict[str, list[PlayerStats]],
    ) -> tuple[str | None, PlayerStats | None]:
        """Find a player by name across any roster. Returns (team_key, player)."""
        name_lower = player_name.lower()
        for team_key, roster in all_rosters.items():
            for p in roster:
                if p.name.lower() == name_lower:
                    return team_key, p
        for team_key, roster in all_rosters.items():
            for p in roster:
                if name_lower in p.name.lower():
                    return team_key, p
        return None, None

    async def _load_league_rosters(self) -> dict[str, list[PlayerStats]]:
        """Fetch season rosters for every team, parallelized."""
        await self._ensure_teams()
        week = self.league.current_week
        tasks = [
            asyncio.to_thread(
                self.api.get_roster_stats_season, t.team_key, week)
            for t in self._teams
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        rosters: dict[str, list[PlayerStats]] = {}
        for t, r in zip(self._teams, results):
            if not isinstance(r, Exception):
                rosters[t.team_key] = r
        return rosters

    async def _load_weekly_rosters(
        self, team_keys: list[str], weeks: list[int],
    ) -> dict[str, dict[int, list[PlayerStats]]]:
        """Fetch per-week rosters for the given teams and weeks."""
        out: dict[str, dict[int, list[PlayerStats]]] = {tk: {} for tk in team_keys}
        for w in weeks:
            tasks = [
                asyncio.to_thread(self.api.get_roster_stats, tk, w)
                for tk in team_keys
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for tk, r in zip(team_keys, results):
                if not isinstance(r, Exception):
                    out[tk][w] = r
        return out

    async def _load_week_matchups(self, weeks: list[int]) -> dict[int, list[Matchup]]:
        """Fetch matchup data for the given weeks."""
        out: dict[int, list[Matchup]] = {}
        tasks = [
            asyncio.to_thread(
                self.api.get_scoreboard, self.league.league_key, w)
            for w in weeks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for w, r in zip(weeks, results):
            if not isinstance(r, Exception):
                out[w] = r
        return out

    async def _build_sgp_calc(
        self, all_rosters: dict[str, list[PlayerStats]],
    ) -> SGPCalculator | None:
        """Build an SGPCalculator using current season team stats."""
        await self._ensure_teams()
        if not self._teams:
            return None
        positions = ("C", "1B", "2B", "3B", "SS", "OF", "SP", "RP")
        replacement_by_pos: dict[str, list[PlayerStats]] = {}
        for pos in positions:
            try:
                players, _ = await asyncio.to_thread(
                    self.api.get_free_agents,
                    self.league.league_key,
                    position=pos, count=25,
                )
                replacement_by_pos[pos] = players
            except Exception:
                replacement_by_pos[pos] = []
        try:
            return SGPCalculator(
                all_teams=self._teams,
                categories=self.categories,
                replacement_by_pos=replacement_by_pos,
            )
        except Exception:
            return None

    async def _get_statcast_description(self, player: PlayerStats) -> str:
        """Compact Statcast description for a player."""
        mlbam_id = await asyncio.to_thread(lookup_mlbam_id, player.name)
        if mlbam_id is None:
            return "no Statcast data available"

        is_pitcher = any(p in ("SP", "RP", "P") for p in player.position.split(","))
        parts: list[str] = []
        if is_pitcher:
            sc = await asyncio.to_thread(get_pitcher_statcast, mlbam_id)
            if sc is None:
                return "no Statcast data available"
            if sc.avg_exit_velo is not None:
                parts.append(f"EV allowed {sc.avg_exit_velo:.1f}")
            if sc.barrel_pct is not None:
                parts.append(f"Barrel% {sc.barrel_pct:.1f}")
            if sc.hard_hit_pct is not None:
                parts.append(f"HardHit% {sc.hard_hit_pct:.1f}")
            if sc.xba is not None:
                parts.append(f"xBA {sc.xba:.3f}")
            if sc.xslg is not None:
                parts.append(f"xSLG {sc.xslg:.3f}")
            if sc.xwoba is not None:
                parts.append(f"xwOBA {sc.xwoba:.3f}")
            if sc.xera is not None:
                parts.append(f"xERA {sc.xera:.2f}")
            if sc.k_pct is not None:
                parts.append(f"K% {sc.k_pct:.1f}")
            if sc.bb_pct is not None:
                parts.append(f"BB% {sc.bb_pct:.1f}")
        else:
            sc = await asyncio.to_thread(get_batter_statcast, mlbam_id)
            if sc is None:
                return "no Statcast data available"
            if sc.avg_exit_velo is not None:
                parts.append(f"EV {sc.avg_exit_velo:.1f}")
            if sc.max_exit_velo is not None:
                parts.append(f"MaxEV {sc.max_exit_velo:.1f}")
            if sc.barrel_pct is not None:
                parts.append(f"Barrel% {sc.barrel_pct:.1f}")
            if sc.hard_hit_pct is not None:
                parts.append(f"HardHit% {sc.hard_hit_pct:.1f}")
            if sc.xba is not None:
                parts.append(f"xBA {sc.xba:.3f}")
            if sc.xslg is not None:
                parts.append(f"xSLG {sc.xslg:.3f}")
            if sc.xwoba is not None:
                parts.append(f"xwOBA {sc.xwoba:.3f}")
            if sc.k_pct is not None:
                parts.append(f"K% {sc.k_pct:.1f}")
            if sc.bb_pct is not None:
                parts.append(f"BB% {sc.bb_pct:.1f}")
        return ", ".join(parts) if parts else "no Statcast data available"

    # -- Tool handlers: Trade Analyzer suite --

    async def _tool_trade_targets(
        self,
        team_name: str | None,
        offer_player_name: str,
        target_position: str | None = None,
        max_results: int = 20,
    ) -> str:
        """Find best trade targets using the SGP-based engine from trade.py."""
        await self._ensure_teams()
        user_team_key = self._resolve_team_key(team_name)
        if not user_team_key:
            label = team_name or "(no default team configured)"
            return (
                f"Could not resolve a team for '{label}'. Set a default team in "
                f"Settings or pass team_name explicitly."
            )

        resolved_team_name = self._team_display_name(user_team_key)
        all_rosters = await self._load_league_rosters()
        my_roster = all_rosters.get(user_team_key, [])
        offer_player = next(
            (p for p in my_roster
             if offer_player_name.lower() in p.name.lower()),
            None,
        )
        if offer_player is None:
            return (
                f"Could not find player '{offer_player_name}' on "
                f"{resolved_team_name}'s roster. Check spelling or use "
                f"get_team_roster first."
            )

        sgp_calc = await self._build_sgp_calc(all_rosters)

        weeks = list(range(1, self.league.current_week + 1))
        week_matchups = await self._load_week_matchups(weeks)
        team_keys = list(all_rosters.keys())
        weekly_rosters = await self._load_weekly_rosters(team_keys, weeks)

        target_positions: set[str] | None = None
        if target_position:
            target_positions = {target_position.upper()}
            # 'OF' is a meta-position for LF/CF/RF — expand
            if target_position.upper() == "OF":
                target_positions = {"LF", "CF", "RF", "OF"}

        team_names_map = {t.team_key: t.name for t in self._teams}
        targets = await asyncio.to_thread(
            trade_find_targets,
            offer_player,
            user_team_key,
            all_rosters,
            self._teams,
            team_names_map,
            self.categories,
            sgp_calc,
            week_matchups,
            weekly_rosters,
            self.league.current_week,
            max_results,
            target_positions,
        )

        if not targets:
            pos_clause = f" at position {target_position}" if target_position else ""
            return (
                f"No viable trade targets found for {offer_player.name}{pos_clause}. "
                f"(Tool filters out deals where the partner would lose heavy roto "
                f"points — the partner may not have the surplus to make a deal work.)"
            )

        header = f"Trade Targets for {offer_player.name} ({offer_player.position})"
        if target_position:
            header += f" — seeking {target_position}"
        header += f" — {resolved_team_name}:"

        lines = [
            header,
            "",
            "(ΔSGP = player value swap; ΔRoto = your roto points change; "
            "ΔWin% = actual H2H record change from weekly replay; "
            "Partner = trade partner's roto change — positive means they benefit.)",
            "",
        ]
        for t in targets:
            sgp = f"{t.sgp:+.1f}" if t.sgp is not None else "N/A"
            win_pct = (f"{t.h2h_win_pct_delta:+.1%}"
                       if abs(t.h2h_win_pct_delta) > 0.001 else "—")
            lines.append(
                f"  {t.player.name} ({t.player.position}, {t.team_name}) — "
                f"SGP {sgp}, ΔSGP {t.net_sgp:+.1f}, ΔRoto {t.roto_delta:+.1f}, "
                f"ΔWin% {win_pct}, Partner {t.partner_roto_delta:+.1f}"
            )
        return "\n".join(lines)

    async def _tool_analyze_trade(
        self,
        team_a_name: str,
        team_b_name: str,
        team_a_players: list[str],
        team_b_players: list[str],
    ) -> str:
        """Full impact analysis for a two-sided trade."""
        await self._ensure_teams()
        team_a_key = self._resolve_team_key(team_a_name)
        team_b_key = self._resolve_team_key(team_b_name)
        if not team_a_key:
            return f"Could not find team matching '{team_a_name}'."
        if not team_b_key:
            return f"Could not find team matching '{team_b_name}'."

        all_rosters = await self._load_league_rosters()
        roster_a = all_rosters.get(team_a_key, [])
        roster_b = all_rosters.get(team_b_key, [])

        a_players: list[PlayerStats] = []
        for name in team_a_players:
            p = next((p for p in roster_a if name.lower() in p.name.lower()), None)
            if p is None:
                return f"Could not find '{name}' on {team_a_name}'s roster."
            a_players.append(p)
        b_players: list[PlayerStats] = []
        for name in team_b_players:
            p = next((p for p in roster_b if name.lower() in p.name.lower()), None)
            if p is None:
                return f"Could not find '{name}' on {team_b_name}'s roster."
            b_players.append(p)

        team_a_name_resolved = next(
            (t.name for t in self._teams if t.team_key == team_a_key), team_a_name)
        team_b_name_resolved = next(
            (t.name for t in self._teams if t.team_key == team_b_key), team_b_name)

        side_a = TradeSide(team_a_key, team_a_name_resolved, a_players)
        side_b = TradeSide(team_b_key, team_b_name_resolved, b_players)

        impact = await asyncio.to_thread(
            compute_trade_impact,
            self._teams, roster_a, roster_b, side_a, side_b, self.categories,
        )

        weeks = list(range(1, self.league.current_week + 1))
        week_matchups = await self._load_week_matchups(weeks)
        weekly_rosters = await self._load_weekly_rosters(
            [team_a_key, team_b_key], weeks)
        replay = None
        try:
            replay = await asyncio.to_thread(
                replay_h2h_with_trade,
                team_a_key, team_b_key,
                {p.player_key for p in a_players},
                {p.player_key for p in b_players},
                week_matchups,
                weekly_rosters.get(team_a_key, {}),
                weekly_rosters.get(team_b_key, {}),
                self.categories, self.league.current_week,
            )
        except Exception:
            pass

        a_names = ", ".join(p.name for p in a_players)
        b_names = ", ".join(p.name for p in b_players)
        lines = [
            f"Trade Analysis — {team_a_name_resolved} sends: {a_names}",
            f"                 {team_b_name_resolved} sends: {b_names}",
            "",
            f"{team_a_name_resolved} impact:",
            f"  Roto: #{impact.roto_rank_before_a} → #{impact.roto_rank_after_a} "
            f"({impact.roto_points_before_a:.1f} → {impact.roto_points_after_a:.1f} pts)",
            f"  H2H power ranking: "
            f"{impact.h2h_before_a.record_str} → {impact.h2h_after_a.record_str}",
            "",
            f"{team_b_name_resolved} impact:",
            f"  Roto: #{impact.roto_rank_before_b} → #{impact.roto_rank_after_b} "
            f"({impact.roto_points_before_b:.1f} → {impact.roto_points_after_b:.1f} pts)",
            f"  H2H power ranking: "
            f"{impact.h2h_before_b.record_str} → {impact.h2h_after_b.record_str}",
            "",
            f"Category impact for {team_a_name_resolved}:",
        ]
        for ci in impact.cat_impacts:
            direction = "✓" if ci.favorable else ("✗" if ci.delta != 0 else "—")
            lines.append(
                f"  {ci.display_name}: {ci.before} → {ci.after}  ({direction})"
            )

        if replay and replay.weeks:
            lines.append("")
            lines.append(
                f"H2H weekly replay — {team_a_name_resolved}:"
            )
            lines.append(
                f"  Actual season record: "
                f"{replay.actual_season_w}-{replay.actual_season_l}-{replay.actual_season_t}"
            )
            lines.append(
                f"  With trade:           "
                f"{replay.trade_season_w}-{replay.trade_season_l}-{replay.trade_season_t}"
            )
            flips = [w for w in replay.weeks if w.changed]
            if flips:
                lines.append(f"  Matchups that would have flipped: {len(flips)}")
                for w in flips:
                    lines.append(
                        f"    Week {w.week} vs {w.opponent_name}: "
                        f"{w.actual_result} → {w.trade_result}"
                    )
        return "\n".join(lines)

    async def _tool_discover_trades(
        self,
        team_name: str | None,
        stat_categories: list[str],
        max_results: int = 15,
    ) -> str:
        """Find trade scenarios to improve specific stat categories."""
        await self._ensure_teams()
        user_team_key = self._resolve_team_key(team_name)
        if not user_team_key:
            label = team_name or "(no default team configured)"
            return f"Could not find team matching '{label}'."
        team_name = self._team_display_name(user_team_key)

        scored = [c for c in self.categories if not c.is_only_display]
        target_stat_ids: list[str] = []
        unmatched: list[str] = []
        for cat_name in stat_categories:
            match = next(
                (c for c in scored
                 if c.display_name.lower() == cat_name.lower()),
                None,
            )
            if match:
                target_stat_ids.append(match.stat_id)
            else:
                unmatched.append(cat_name)
        if not target_stat_ids:
            avail = ", ".join(c.display_name for c in scored)
            return (
                f"None of the categories matched. Unmatched: {unmatched}. "
                f"Available: {avail}"
            )

        all_rosters = await self._load_league_rosters()
        sgp_calc = await self._build_sgp_calc(all_rosters)
        team_names_map = {t.team_key: t.name for t in self._teams}

        scenarios = await asyncio.to_thread(
            discover_trades,
            user_team_key,
            target_stat_ids,
            all_rosters,
            self._teams,
            team_names_map,
            self.categories,
            sgp_calc,
            max_results,
        )

        if not scenarios:
            return f"No viable trade scenarios for improving {stat_categories}."

        cat_display = ", ".join([c.display_name for c in scored
                                 if c.stat_id in target_stat_ids])
        lines = [
            f"Trade Discovery — {team_name} seeking to improve: {cat_display}",
            "",
            "(Net SGP = value swap; ΔRoto = your roto points change; "
            "Partner = partner's roto impact — filtered if partner loses heavily.)",
            "",
        ]
        for s in scenarios:
            target_sgp = f"{s.target_sgp:+.1f}" if s.target_sgp is not None else "N/A"
            offer_sgp = f"{s.offer_sgp:+.1f}" if s.offer_sgp is not None else "N/A"
            lines.append(
                f"  You get {s.target.name} ({s.target.position}) from "
                f"{s.target_team_name}  ↔  You send {s.offer.name} "
                f"({s.offer.position})"
            )
            lines.append(
                f"    Target SGP {target_sgp}, Offer SGP {offer_sgp}, "
                f"Net SGP {s.net_sgp:+.1f}, ΔRoto {s.roto_delta:+.1f}, "
                f"Partner {s.partner_roto_delta:+.1f}"
            )
        return "\n".join(lines)

    async def _tool_compare_add_drop(
        self,
        team_name: str | None,
        add_player_name: str,
        max_results: int = 15,
    ) -> str:
        """Evaluate adding a player and dropping candidates."""
        await self._ensure_teams()
        user_team_key = self._resolve_team_key(team_name)
        if not user_team_key:
            label = team_name or "(no default team configured)"
            return f"Could not find team matching '{label}'."
        team_name = self._team_display_name(user_team_key)

        all_rosters = await self._load_league_rosters()

        owner_team_key, add_player = self._find_player_on_any_roster(
            add_player_name, all_rosters)
        if add_player is None:
            try:
                fas, _ = await asyncio.to_thread(
                    self.api.get_free_agents,
                    self.league.league_key,
                    status=None,
                    search=add_player_name,
                    count=5,
                )
                add_player = next(
                    (p for p in fas
                     if add_player_name.lower() in p.name.lower()),
                    None,
                )
            except Exception:
                pass
        if add_player is None:
            return f"Could not find player '{add_player_name}'."

        my_roster = all_rosters.get(user_team_key, [])
        sgp_calc = await self._build_sgp_calc(all_rosters)

        weeks = list(range(1, self.league.current_week + 1))
        week_matchups = await self._load_week_matchups(weeks)
        weekly_rosters_target = await self._load_weekly_rosters(
            [user_team_key], weeks)
        weekly_target = weekly_rosters_target.get(user_team_key, {})

        scenarios = await asyncio.to_thread(
            compute_compare_scenarios,
            add_player,
            user_team_key,
            my_roster,
            self._teams,
            self.categories,
            sgp_calc,
            week_matchups,
            weekly_target,
            self.league.current_week,
        )
        scenarios = scenarios[:max_results]

        add_sc_desc = await self._get_statcast_description(add_player)

        rostered_note = ""
        if owner_team_key and owner_team_key != user_team_key:
            owner_name = next(
                (t.name for t in self._teams if t.team_key == owner_team_key),
                "another team",
            )
            rostered_note = (
                f"Note: {add_player.name} is currently rostered on "
                f"{owner_name}. This analysis models only {team_name}'s side; "
                f"use analyze_trade for the full view."
            )

        if not scenarios:
            base = (
                f"No position-eligible drop candidates for {add_player.name} "
                f"on {team_name}."
            )
            return base + ("\n" + rostered_note if rostered_note else "")

        lines = [
            f"Add/Drop Analysis — considering adding {add_player.name} "
            f"({add_player.position}, {add_player.team_abbr}) to {team_name}:",
        ]
        if rostered_note:
            lines.append(rostered_note)
        lines.append("")
        lines.append(f"Statcast for {add_player.name}: {add_sc_desc}")
        lines.append("")
        lines.append("Drop candidates ranked by ΔRoto:")
        lines.append("")
        for s in scenarios:
            drop = s.drop_player
            drop_sgp = f"{s.drop_sgp:+.1f}" if s.drop_sgp is not None else "N/A"
            win_pct = (f"{s.h2h_win_pct_delta:+.1%}"
                       if abs(s.h2h_win_pct_delta) > 0.001 else "—")
            lines.append(
                f"  Drop {drop.name} ({drop.position}, {drop.team_abbr}) — "
                f"SGP {drop_sgp}, ΔSGP {s.net_sgp:+.1f}, "
                f"ΔRoto {s.roto_delta:+.1f}, ΔWin% {win_pct}"
            )
        return "\n".join(lines)

    # -- Tool handlers: MLB game data --

    async def _tool_mlb_scoreboard(self, date_str: str | None) -> str:
        """Get MLB games for a specific date with scores and status."""
        from datetime import date as _date, datetime

        if date_str:
            try:
                game_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                return f"Invalid date format '{date_str}'. Use YYYY-MM-DD."
        else:
            game_date = _date.today()

        games = await asyncio.to_thread(get_mlb_scoreboard, game_date)
        if not games:
            return f"No MLB games found for {game_date.isoformat()}."

        lines = [f"MLB Scoreboard for {game_date.isoformat()}:"]
        for g in games:
            status_label = g.detail_status or g.status
            if g.status != "Preview":
                score_str = f"  {g.away_abbr} {g.away_score} @ {g.home_abbr} {g.home_score}"
            else:
                score_str = f"  {g.away_abbr} @ {g.home_abbr}"
            inning_str = ""
            if g.status == "Live":
                inning_str = f"  {g.inning_half} {g.inning_ordinal}, {g.outs} out"
            lines.append(
                f"  [{status_label}] gamePk={g.gamePk}{score_str}{inning_str}"
            )
            if g.status != "Preview":
                lines.append(
                    f"    Hits: {g.away_abbr} {g.away_hits}, "
                    f"{g.home_abbr} {g.home_hits}  "
                    f"Errors: {g.away_abbr} {g.away_errors}, "
                    f"{g.home_abbr} {g.home_errors}"
                )
        return "\n".join(lines)

    async def _tool_mlb_boxscore(self, game_pk: str) -> str:
        """Get the full box score for a specific MLB game."""
        try:
            box = await asyncio.to_thread(get_mlb_boxscore, game_pk)
        except Exception as e:
            return f"Could not fetch box score for game {game_pk}: {e}"
        if box is None:
            return f"No box score data for game {game_pk}."

        lines = [f"Box Score — gamePk={game_pk}:"]
        for team_label, team in [("Away", box.away), ("Home", box.home)]:
            lines.append("")
            lines.append(f"{team_label}: {team.name} ({team.abbr})")
            if team.batters:
                lines.append("  Batters:")
                for b in team.batters:
                    lines.append(
                        f"    {b.name} ({b.position}): "
                        f"AB {b.ab}, H {b.h}, R {b.r}, HR {b.hr}, "
                        f"RBI {b.rbi}, SB {b.sb}, BB {b.bb}, K {b.so}"
                    )
            if team.pitchers:
                lines.append("  Pitchers:")
                for p in team.pitchers:
                    dec = f" ({p.decision})" if p.decision else ""
                    lines.append(
                        f"    {p.name}{dec}: IP {p.ip}, H {p.h}, R {p.r}, "
                        f"ER {p.er}, BB {p.bb}, K {p.so}, ERA {p.era}"
                    )
        return "\n".join(lines)

    # -- Tool handler: Statcast profile --

    async def _tool_statcast_profile(
        self,
        player_name: str,
        is_pitcher: bool | None = None,
    ) -> str:
        """Get season Statcast metrics for a player."""
        mlbam_id = await asyncio.to_thread(lookup_mlbam_id, player_name)
        if mlbam_id is None:
            return f"Could not find MLB data for '{player_name}'."

        if is_pitcher is None:
            sc_b = await asyncio.to_thread(get_batter_statcast, mlbam_id)
            sc_p = await asyncio.to_thread(get_pitcher_statcast, mlbam_id)
            if sc_b and not sc_p:
                is_pitcher = False
            elif sc_p and not sc_b:
                is_pitcher = True
            elif sc_b and sc_p:
                is_pitcher = False
            else:
                return f"No Statcast data available for {player_name}."

        lines = [f"Statcast profile for {player_name}:"]
        if is_pitcher:
            sc = await asyncio.to_thread(get_pitcher_statcast, mlbam_id)
            if sc is None:
                return f"No pitcher Statcast data for {player_name}."
            lines.append(
                f"  Quality of contact allowed: EV {sc.avg_exit_velo}, "
                f"Barrel% {sc.barrel_pct}, HardHit% {sc.hard_hit_pct}"
            )
            lines.append(
                f"  Expected: xBA {sc.xba}, xSLG {sc.xslg}, xwOBA {sc.xwoba}, "
                f"xERA {sc.xera}"
            )
            lines.append(
                f"  Plate discipline: K% {sc.k_pct}, BB% {sc.bb_pct}, "
                f"Whiff% {sc.whiff_pct}"
            )
        else:
            sc = await asyncio.to_thread(get_batter_statcast, mlbam_id)
            if sc is None:
                return f"No batter Statcast data for {player_name}."
            lines.append(
                f"  Quality of contact: EV {sc.avg_exit_velo}, "
                f"MaxEV {sc.max_exit_velo}, LA {sc.avg_launch_angle}, "
                f"Barrel% {sc.barrel_pct}, HardHit% {sc.hard_hit_pct}"
            )
            lines.append(
                f"  Expected: xBA {sc.xba}, xSLG {sc.xslg}, xwOBA {sc.xwoba}"
            )
            lines.append(
                f"  Plate discipline: K% {sc.k_pct}, BB% {sc.bb_pct}, "
                f"Whiff% {sc.whiff_pct}"
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
                model=self.model,
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
