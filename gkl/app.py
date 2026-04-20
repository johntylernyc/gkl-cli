"""GKL CLI — Fantasy Baseball Command Center."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date

import webbrowser

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    Static,
)

from textual.widgets._footer import FooterKey


class WrappingFooter(Footer):
    """Footer that wraps key bindings to multiple rows."""

    DEFAULT_CSS = """
    WrappingFooter {
        dock: bottom;
        height: auto;
        color: $footer-foreground;
        background: $footer-background;
        scrollbar-size: 0 0;
        grid-gutter: 0;
        grid-rows: 1;
        FooterKey.-command-palette {
            dock: right;
            padding-right: 1;
            border-left: vkey $foreground 20%;
        }
    }
    """

    def _cap_columns(self) -> None:
        keys = list(self.query(FooterKey))
        if not keys:
            return
        # Measure total width needed: key display + description + spacing
        total = sum(
            len(k.key_display or "") + len(k.description or "") + 3
            for k in keys
        )
        # Target ~2 rows for a typical set of bindings
        target_per_row = max(1, (total + self.size.width - 1) // self.size.width)
        cols = max(1, (len(keys) + target_per_row - 1) // target_per_row)
        self.styles.grid_size_columns = cols

    def bindings_changed(self, screen) -> None:
        super().bindings_changed(screen)
        self.call_after_refresh(self._cap_columns)

    def on_mount(self) -> None:
        self.call_after_refresh(self._cap_columns)

    def on_resize(self) -> None:
        self._cap_columns()


from gkl.shared_cache import SharedDataCache
from gkl.updater import (
    UpdateInfo, UpdateModal, apply_update, check_for_update,
    cleanup_old_binary, download_update,
)
from gkl.yahoo_api import (
    League, Matchup, PlayerStats, StatCategory, TeamStats, Transaction, TransactionPlayer,
    YahooFantasyAPI,
)
from gkl.datastore import RosterDataStore
from gkl.yahoo_auth import YahooAuth, load_credentials, save_credentials, is_web_mode
from gkl.stats import (
    RATE_STATS,
    who_wins, simulate_h2h, compute_power_rankings, aggregate_h2h_season,
    H2HResult, TeamH2HSummary, SGPCalculator,
    build_stat_columns, get_stat_value, compute_roto,
)
from gkl.datastore import RosterDataStore
from gkl.mlb_api import (
    MLBGame, BoxScore, BoxScoreTeam, get_mlb_scoreboard, get_mlb_boxscore,
    get_player_ages, get_player_games,
)
from gkl.statcast import (
    get_batter_statcast, get_pitcher_statcast, lookup_mlbam_id,
    StatcastBatter, StatcastPitcher,
)
from gkl.skipper import Skipper, load_anthropic_key, save_anthropic_key, AVAILABLE_MODELS, DEFAULT_MODEL

# Consistent team colors used across the entire app
TEAM_A_COLOR = "#E8A735"  # warm amber/gold
TEAM_B_COLOR = "#5BA4CF"  # cool sky blue
TEAM_A_BG = "#332B1A"     # subtle warm row background
TEAM_B_BG = "#1A2A38"     # subtle cool row background
TIED_BG = "#252525"       # neutral for ties

BASEBALL_THEME = Theme(
    name="baseball",
    primary="#4A7C59",
    secondary="#6B5B4E",
    accent="#D4A84B",
    foreground="#E8E4DF",
    background="#181818",
    surface="#222222",
    panel="#1E1E1E",
    success="#6AAF6E",
    warning="#D4A84B",
    error="#C75D5D",
    dark=True,
    variables={
        "block-cursor-foreground": "#E8E4DF",
        "block-cursor-background": "#4A7C59",
        "footer-key-foreground": "#D4A84B",
    },
)


# who_wins imported from gkl.stats


# --- Roto Standings Calculation ---


_compute_roto = compute_roto  # alias for backward compatibility


# --- Player Comparison Mixin ---


class PlayerCompareMixin:
    """Mixin providing compare-to-roster functionality for any screen with player DataTables.

    Requires the screen to have: self.api, self.league, self.categories.
    Optionally uses self._sgp_calc if available.
    """

    def action_compare(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
            if not isinstance(table, DataTable):
                return
        except Exception:
            return

        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return

        self._compare_player = players[row_idx]

        teams = self.api.get_team_season_stats(self.league.league_key)
        self._compare_team_names = {t.team_key: t.name for t in teams}
        options = [(t.team_key, t.name) for t in teams]
        self.app.push_screen(
            TeamSelectModal(options),
            callback=self._on_compare_team_selected,
        )

    def _on_compare_team_selected(self, team_key: str | None) -> None:
        if team_key is None or not hasattr(self, "_compare_player"):
            return
        p = self._compare_player
        team_name = self._compare_team_names.get(team_key, team_key)
        sgp = getattr(self, "_sgp_calc", None)
        self.app.push_screen(
            ComparisonScreen(
                self.api, self.league, self.categories,
                p, team_key, team_name, sgp,
            )
        )


# --- Week Range Modal ---


def _is_monday() -> bool:
    """Monday = 0 in weekday(). On Mondays, default to completed weeks only."""
    return date.today().weekday() == 0


# --- League Standings Screen ---


class LeagueStandingsScreen(Screen):
    """Combined league standings: H2H record on top, full roto table on bottom."""
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("1", "show_overall", "Overall"), ("2", "show_batting", "Batting"),
                ("3", "show_pitching", "Pitching"),
                ("w", "set_weeks", "Set Weeks"),
                ("i", "toggle_in_progress", "Incl. In-Progress")]
    CSS = """
    #ls-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #ls-top {
        height: auto;
        max-height: 50%;
    }
    #ls-top-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #ls-table {
        height: auto;
        max-height: 100%;
        background: $panel;
    }
    #ls-bottom {
        height: 1fr;
        border-top: solid $primary;
    }
    #roto-view-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #roto-table {
        height: 1fr;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    #ls-loading {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._roto_teams: list[TeamStats] = []
        self._current_view = "overall"
        self._week_start = 1
        self._max_week = max(1, league.current_week)
        self._last_completed_week = self._max_week  # updated in _load
        self._week_end = self._max_week  # updated in _load to last completed
        self._include_in_progress = False
        self._num_scored_cats = len([c for c in categories if not c.is_only_display])

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="ls-header")
        with Vertical(id="ls-top"):
            yield Static("", id="ls-top-label")
            yield DataTable(id="ls-table")
        with Vertical(id="ls-bottom"):
            yield Static("", id="roto-view-label")
            yield DataTable(id="roto-table")
        yield Static("Loading standings...", id="ls-loading")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#ls-header", Static).update(
            f" {self.league.name} — League Standings "
        )
        self.query_one("#ls-top").display = False
        self.query_one("#ls-bottom").display = False
        self.run_worker(self._load)

    async def _load(self) -> None:
        cache = self.app.shared_cache

        # Fetch all weeks' matchups and team stats in parallel
        all_weeks = list(range(1, self.league.current_week + 1))
        await cache.prefetch_weeks(self.api, self.league.league_key, all_weeks)

        # Prefetch matchups
        missing_matchups = [w for w in all_weeks if w not in cache.week_matchups]
        if missing_matchups:
            matchup_results = await asyncio.gather(*[
                asyncio.to_thread(self.api.get_scoreboard,
                                  self.league.league_key, w)
                for w in missing_matchups
            ])
            for w, data in zip(missing_matchups, matchup_results):
                cache.week_matchups[w] = data

        # Determine last fully completed week
        self._last_completed_week = 0
        for w in all_weeks:
            matchups = cache.week_matchups.get(w, [])
            if matchups and all(m.status == "postevent" for m in matchups):
                self._last_completed_week = w
        self._last_completed_week = max(1, self._last_completed_week)

        # Mon: show completed weeks only; Tue-Sun: include in-progress week
        if _is_monday() or self._last_completed_week >= self._max_week:
            self._week_end = self._last_completed_week
            self._include_in_progress = False
        else:
            self._week_end = self._max_week
            self._include_in_progress = True

        # Remove loading indicator, show both panels
        loading = self.query("#ls-loading")
        if loading:
            loading.first().remove()
        self.query_one("#ls-top").display = True
        self.query_one("#ls-bottom").display = True

        await self._render_standings()

    async def _render_standings(self) -> None:
        """Recompute and render both H2H and roto tables for current week range."""
        from gkl.stats import aggregate_weekly_stats

        cache = self.app.shared_cache
        needed = list(range(self._week_start, self._week_end + 1))
        await cache.prefetch_weeks(self.api, self.league.league_key, needed)

        # Ensure matchups are cached
        missing_matchups = [w for w in needed if w not in cache.week_matchups]
        if missing_matchups:
            matchup_results = await asyncio.gather(*[
                asyncio.to_thread(self.api.get_scoreboard,
                                  self.league.league_key, w)
                for w in missing_matchups
            ])
            for w, data in zip(missing_matchups, matchup_results):
                cache.week_matchups[w] = data

        # Compute H2H records from matchups in the selected range
        h2h_records: dict[str, dict] = {}
        for w in needed:
            matchups = cache.week_matchups.get(w, [])
            for m in matchups:
                if m.status == "preevent":
                    continue
                pa, pb = m.team_a.points, m.team_b.points
                for team_key in (m.team_a.team_key, m.team_b.team_key):
                    if team_key not in h2h_records:
                        h2h_records[team_key] = {
                            "wins": 0, "losses": 0, "ties": 0,
                            "cat_wins": 0, "cat_losses": 0, "cat_ties": 0,
                        }
                # Weekly matchup winner
                if pa > pb:
                    h2h_records[m.team_a.team_key]["wins"] += 1
                    h2h_records[m.team_b.team_key]["losses"] += 1
                elif pb > pa:
                    h2h_records[m.team_a.team_key]["losses"] += 1
                    h2h_records[m.team_b.team_key]["wins"] += 1
                else:
                    h2h_records[m.team_a.team_key]["ties"] += 1
                    h2h_records[m.team_b.team_key]["ties"] += 1
                # Category-level totals (only from completed weeks to
                # avoid phantom ties from in-progress weeks with partial
                # or zero point data)
                if m.status == "postevent":
                    cat_ties = int(self._num_scored_cats - pa - pb)
                    h2h_records[m.team_a.team_key]["cat_wins"] += int(pa)
                    h2h_records[m.team_a.team_key]["cat_losses"] += int(pb)
                    h2h_records[m.team_a.team_key]["cat_ties"] += cat_ties
                    h2h_records[m.team_b.team_key]["cat_wins"] += int(pb)
                    h2h_records[m.team_b.team_key]["cat_losses"] += int(pa)
                    h2h_records[m.team_b.team_key]["cat_ties"] += cat_ties

        # Compute roto stats for the selected week range
        if self._week_start == 1 and self._week_end == self._max_week:
            roto_teams = await asyncio.to_thread(
                self.api.get_team_season_stats, self.league.league_key)
        else:
            weekly_data = [cache.week_team_stats[w] for w in needed]
            roto_teams = aggregate_weekly_stats(weekly_data, self.categories)
        self._roto_teams = roto_teams

        # Compute roto rank summaries for the H2H table
        scored = [c for c in self.categories if not c.is_only_display]
        bat_scored = [c for c in scored if c.position_type == "B"]
        pitch_scored = [c for c in scored if c.position_type == "P"]

        overall_roto = _compute_roto(roto_teams, scored)
        batting_roto = _compute_roto(roto_teams, bat_scored)
        pitching_roto = _compute_roto(roto_teams, pitch_scored)

        overall_rank = {e["team_key"]: r for r, e in enumerate(overall_roto, 1)}
        batting_rank = {e["team_key"]: r for r, e in enumerate(batting_roto, 1)}
        pitching_rank = {e["team_key"]: r for r, e in enumerate(pitching_roto, 1)}

        # Build combined data sorted by H2H standings
        team_info = {t.team_key: t for t in roto_teams}
        standings = []
        for team_key, rec in h2h_records.items():
            total = rec["wins"] + rec["losses"] + rec["ties"]
            pct = (rec["wins"] + 0.5 * rec["ties"]) / total if total > 0 else 0.0
            team = team_info.get(team_key)
            cat_total = rec["cat_wins"] + rec["cat_losses"] + rec["cat_ties"]
            cat_pct = ((rec["cat_wins"] + 0.5 * rec["cat_ties"]) / cat_total
                       if cat_total > 0 else 0.0)
            standings.append({
                "team_key": team_key,
                "name": team.name if team else team_key,
                "manager": team.manager if team else "",
                "wins": rec["wins"],
                "losses": rec["losses"],
                "ties": rec["ties"],
                "win_pct": pct,
                "cat_wins": rec["cat_wins"],
                "cat_losses": rec["cat_losses"],
                "cat_ties": rec["cat_ties"],
                "cat_pct": cat_pct,
                "overall_rank": overall_rank.get(team_key, 0),
                "batting_rank": batting_rank.get(team_key, 0),
                "pitching_rank": pitching_rank.get(team_key, 0),
            })
        standings.sort(key=lambda s: (s["cat_pct"], s["cat_wins"]), reverse=True)

        self._render_h2h_table(standings)
        self._render_roto_table()

    def _render_h2h_table(self, standings: list[dict]) -> None:
        week_label = f"Weeks {self._week_start}-{self._week_end}"
        if self._include_in_progress:
            week_label += " (incl. in-progress)"
        self.query_one("#ls-top-label", Static).update(
            f" H2H Standings — {week_label} "
        )
        table = self.query_one("#ls-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Team", "Manager",
                          "Official W-L-T", "Official Win %",
                          "H2H Matchup Record", "Matchup Win %",
                          "Roto Overall", "Roto Batting", "Roto Pitching")

        num_teams = len(standings)
        for rank, s in enumerate(standings, 1):
            pct = s["win_pct"]
            pct_style = "bold green" if pct >= 0.6 else "bold red" if pct < 0.4 else ""

            cat_pct = s["cat_pct"]
            cat_pct_style = ("bold green" if cat_pct >= 0.6
                             else "bold red" if cat_pct < 0.4 else "")

            def _roto_style(r: int, n: int = num_teams) -> str:
                if r <= 3:
                    return "bold green"
                elif r >= n - 2:
                    return "bold red"
                return ""

            table.add_row(
                Text(str(rank), justify="right"),
                Text(s["name"], style="bold"),
                Text(s["manager"], style="dim"),
                Text(f"{s['cat_wins']}-{s['cat_losses']}-{s['cat_ties']}",
                     justify="center"),
                Text(f"{cat_pct:.1%}", style=cat_pct_style, justify="right"),
                Text(f"{s['wins']}-{s['losses']}-{s['ties']}", justify="center"),
                Text(f"{pct:.1%}", style=pct_style, justify="right"),
                Text(str(s["overall_rank"]), style=_roto_style(s["overall_rank"]),
                     justify="center"),
                Text(str(s["batting_rank"]), style=_roto_style(s["batting_rank"]),
                     justify="center"),
                Text(str(s["pitching_rank"]), style=_roto_style(s["pitching_rank"]),
                     justify="center"),
            )

    def _render_roto_table(self) -> None:
        scored = [c for c in self.categories if not c.is_only_display]
        bat_scored = [c for c in scored if c.position_type == "B"]
        pitch_scored = [c for c in scored if c.position_type == "P"]

        if self._current_view == "batting":
            cats = bat_scored
            label = f" BATTING ROTO — Weeks {self._week_start}-{self._week_end} "
        elif self._current_view == "pitching":
            cats = pitch_scored
            label = f" PITCHING ROTO — Weeks {self._week_start}-{self._week_end} "
        else:
            cats = bat_scored + pitch_scored
            label = f" OVERALL ROTO — Weeks {self._week_start}-{self._week_end} "

        self.query_one("#roto-view-label", Static).update(label)

        standings = _compute_roto(self._roto_teams, cats)
        table = self.query_one("#roto-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        col_keys: list[str | Text] = ["Rank", "Team", "Manager"]
        for cat in cats:
            col_keys.append(cat.display_name)
        col_keys.append("Total")
        table.add_columns(*col_keys)

        for rank, entry in enumerate(standings, 1):
            row: list[str | Text] = [
                Text(str(rank), justify="right"),
                Text(entry["name"], style="bold"),
                Text(entry["manager"], style="dim"),
            ]
            for cat in cats:
                pts = entry.get(cat.stat_id, 0)
                raw = entry.get(f"raw_{cat.stat_id}", "-")
                cell = Text(f"{pts:.1f}", justify="right")
                cell.append(f" ({raw})", style="dim")
                row.append(cell)
            row.append(Text(f"{entry['total']:.1f}", style="bold", justify="right"))
            table.add_row(*row)

    def action_show_overall(self) -> None:
        self._current_view = "overall"
        self._render_roto_table()

    def action_show_batting(self) -> None:
        self._current_view = "batting"
        self._render_roto_table()

    def action_show_pitching(self) -> None:
        self._current_view = "pitching"
        self._render_roto_table()

    def action_set_weeks(self) -> None:
        modal = WeekRangeModal(
            max_week=self._max_week,
            week_start=self._week_start,
            week_end=self._week_end,
        )
        self.app.push_screen(modal, self._on_week_range_selected)

    def _on_week_range_selected(self, result: tuple[int, int] | None) -> None:
        if result is None:
            return
        self._week_start, self._week_end = result
        # Track whether the user explicitly included the in-progress week
        self._include_in_progress = self._week_end > self._last_completed_week
        self.run_worker(self._render_standings, group="standings-fetch", exclusive=True)

    def action_toggle_in_progress(self) -> None:
        if self._last_completed_week >= self._max_week:
            return  # No in-progress week exists
        self._include_in_progress = not self._include_in_progress
        if self._include_in_progress:
            self._week_end = self._max_week
        else:
            self._week_end = min(self._week_end, self._last_completed_week)
        self.run_worker(self._render_standings, group="standings-fetch", exclusive=True)

    def action_go_back(self) -> None:
        self.app.pop_screen()


# --- H2H Simulator Screen ---


class H2HSimulatorScreen(Screen):
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("left", "prev_week", "Prev Week"), ("right", "next_week", "Next Week"),
                ("a", "toggle_season", "Season")]
    CSS = """
    #h2h-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #h2h-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #h2h-top {
        height: 55%;
    }
    #h2h-top-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #h2h-actual {
        height: 1;
        content-align: center middle;
        background: #2A2A2A;
        text-style: bold;
    }
    #matchups-table {
        height: 1fr;
        background: $panel;
    }
    #h2h-bottom {
        height: 45%;
        border-top: solid $primary;
    }
    #h2h-bottom-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #rankings-table {
        height: 1fr;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    #h2h-loading {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._max_week = league.current_week
        # Mon: default to last completed week; Tue-Sun: current (in-progress)
        if _is_monday() and league.current_week > 1:
            self._week = league.current_week - 1
        else:
            self._week = league.current_week
        self._season_mode = "off"  # "off" | "completed" | "all"
        self._team_keys: list[str] = []
        self._team_idx = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="h2h-header")
        yield Static("", id="h2h-controls")
        with Vertical(id="h2h-top"):
            yield Static("", id="h2h-top-label")
            yield Static("", id="h2h-actual")
            yield Static("Loading...", id="h2h-loading")
            yield DataTable(id="matchups-table")
        with Vertical(id="h2h-bottom"):
            yield Static("", id="h2h-bottom-label")
            yield DataTable(id="rankings-table")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#h2h-header", Static).update(
            f" {self.league.name} — H2H Simulator "
        )
        self.query_one("#matchups-table", DataTable).display = False
        self.run_worker(self._load)

    async def _load(self) -> None:
        teams = await self._get_week_teams(self._week)
        self._team_keys = [t.team_key for t in teams]
        if not self._team_keys:
            return

        # Default to first team
        if self._team_idx >= len(self._team_keys):
            self._team_idx = 0

        await self._render_all()

    async def _get_week_teams(self, week: int) -> list[TeamStats]:
        return await self.app.shared_cache.get_week_teams(
            self.api, self.league.league_key, week)

    async def _get_week_matchups(self, week: int) -> list[Matchup]:
        return await self.app.shared_cache.get_week_matchups(
            self.api, self.league.league_key, week)

    def _find_actual_opponent(self, team_key: str, matchups: list[Matchup]) -> tuple[str, str]:
        """Find the actual opponent and result for a team in a week's matchups.
        Returns (opponent_team_key, result_str like 'W 9-7' or 'L 3-12').
        """
        for m in matchups:
            if m.team_a.team_key == team_key:
                opp = m.team_b
                pts_a, pts_b = m.team_a.points, m.team_b.points
                if pts_a > pts_b:
                    res = f"W {pts_a:.0f}-{pts_b:.0f}"
                elif pts_b > pts_a:
                    res = f"L {pts_a:.0f}-{pts_b:.0f}"
                else:
                    res = f"T {pts_a:.0f}-{pts_b:.0f}"
                return opp.team_key, res
            elif m.team_b.team_key == team_key:
                opp = m.team_a
                pts_a, pts_b = m.team_b.points, m.team_a.points
                if pts_a > pts_b:
                    res = f"W {pts_a:.0f}-{pts_b:.0f}"
                elif pts_b > pts_a:
                    res = f"L {pts_a:.0f}-{pts_b:.0f}"
                else:
                    res = f"T {pts_a:.0f}-{pts_b:.0f}"
                return opp.team_key, res
        return "", ""

    async def _render_all(self) -> None:
        teams = await self._get_week_teams(self._week)
        selected_key = self._team_keys[self._team_idx]
        selected_team = next((t for t in teams if t.team_key == selected_key), None)
        if not selected_team:
            return

        if self._season_mode != "off":
            await self._render_season(teams, selected_key, selected_team)
        else:
            await self._render_week(teams, selected_key, selected_team)

    @staticmethod
    def _week_is_preevent(matchups: list[Matchup]) -> bool:
        """Check if all matchups for a week are preevent (no games started)."""
        return bool(matchups) and all(m.status == "preevent" for m in matchups)

    @staticmethod
    def _week_is_completed(matchups: list[Matchup]) -> bool:
        """Check if all matchups for a week are postevent (fully completed)."""
        return bool(matchups) and all(m.status == "postevent" for m in matchups)

    async def _render_week(self, teams: list[TeamStats], selected_key: str,
                           selected_team: TeamStats) -> None:
        matchups = await self._get_week_matchups(self._week)
        preevent = self._week_is_preevent(matchups)
        actual_opp_key, actual_result = self._find_actual_opponent(selected_key, matchups)

        # Controls
        ctrl = Text()
        ctrl.append(f"Week {self._week}", style="bold")
        ctrl.append(f"  (←→ week)  |  ", style="dim")
        ctrl.append(f"Manager: {selected_team.name}", style=f"bold {TEAM_A_COLOR}")
        ctrl.append(f"  (Enter on rankings to select)  |  [a] Season View", style="dim")
        self.query_one("#h2h-controls", Static).update(ctrl)

        # Actual matchup result
        if preevent:
            notice = Text()
            notice.append(" This week's games have not yet started ", style="bold on #3A3A3A")
            self.query_one("#h2h-actual", Static).update(notice)
        elif actual_result:
            actual_text = Text()
            actual_text.append(" Actual Matchup: ", style="dim")
            actual_text.append(f" {actual_result} ", style="bold")
            opp_name = next((t.name for t in teams if t.team_key == actual_opp_key), "?")
            actual_text.append(f" vs {opp_name}", style="dim")
            self.query_one("#h2h-actual", Static).update(actual_text)
        else:
            self.query_one("#h2h-actual", Static).update("")

        if preevent:
            # Build empty results — all 0-0-0 records
            h2h: dict[str, dict[str, H2HResult]] = {}
            for a in teams:
                h2h[a.team_key] = {}
                for b in teams:
                    if a.team_key != b.team_key:
                        h2h[a.team_key][b.team_key] = H2HResult()
            rankings = compute_power_rankings(h2h, teams)
        else:
            h2h = simulate_h2h(teams, self.categories)
            rankings = compute_power_rankings(h2h, teams)

        self.query_one("#h2h-top-label", Static).update(
            f" Hypothetical Matchups — {selected_team.name} vs All — Week {self._week} "
        )
        self.query_one("#h2h-bottom-label", Static).update(
            f" League Power Rankings — Week {self._week} "
        )

        loading = self.query("#h2h-loading")
        if loading:
            loading.first().remove()
        self.query_one("#matchups-table", DataTable).display = True

        self._render_matchups_table(h2h, teams, selected_key, actual_opp_key)
        self._render_rankings_table(rankings, matchups)

    async def _render_season(self, teams: list[TeamStats], selected_key: str,
                             selected_team: TeamStats) -> None:
        completed_only = self._season_mode == "completed"
        mode_label = "Completed Weeks" if completed_only else "All Weeks"
        next_label = "All Weeks" if completed_only else "Week View"
        ctrl = Text()
        ctrl.append(f"SEASON — {mode_label}", style="bold")
        ctrl.append(f"  (←→ week)  |  ", style="dim")
        ctrl.append(f"Manager: {selected_team.name}", style=f"bold {TEAM_A_COLOR}")
        ctrl.append(f"  (Enter on rankings to select)  |  [a] {next_label}", style="dim")
        self.query_one("#h2h-controls", Static).update(ctrl)
        self.query_one("#h2h-actual", Static).update("")

        # Prefetch all weeks in parallel via shared cache
        cache = self.app.shared_cache
        all_weeks = list(range(1, self._max_week + 1))
        await cache.prefetch_weeks(self.api, self.league.league_key, all_weeks)
        # Also prefetch matchups in parallel
        missing_matchups = [w for w in all_weeks if w not in cache.week_matchups]
        if missing_matchups:
            matchup_results = await asyncio.gather(*[
                asyncio.to_thread(self.api.get_scoreboard,
                                  self.league.league_key, w)
                for w in missing_matchups
            ])
            for w, data in zip(missing_matchups, matchup_results):
                cache.week_matchups[w] = data

        # Aggregate across weeks (skip preevent; optionally skip in-progress)
        all_rankings: list[list[TeamH2HSummary]] = []
        all_h2h: dict[str, dict[str, H2HResult]] = {}
        for w in all_weeks:
            w_matchups = cache.week_matchups[w]
            if self._week_is_preevent(w_matchups):
                continue
            if completed_only and not self._week_is_completed(w_matchups):
                continue
            w_teams = cache.week_team_stats[w]
            h2h = simulate_h2h(w_teams, self.categories)
            rankings = compute_power_rankings(h2h, w_teams)
            all_rankings.append(rankings)
            # Aggregate per-opponent results for selected team
            for opp_key, result in h2h.get(selected_key, {}).items():
                if opp_key not in all_h2h:
                    all_h2h[opp_key] = {}
                if "agg" not in all_h2h[opp_key]:
                    all_h2h[opp_key]["agg"] = H2HResult()
                a = all_h2h[opp_key]["agg"]
                if result.result == "WIN":
                    a.wins += 1
                elif result.result == "LOSS":
                    a.losses += 1
                else:
                    a.ties += 1

        season_rankings = aggregate_h2h_season(all_rankings)

        self.query_one("#h2h-top-label", Static).update(
            f" Season H2H — {selected_team.name} — Matchup W/L vs Each Opponent ({mode_label}) "
        )
        self.query_one("#h2h-bottom-label", Static).update(
            f" Season Power Rankings — {mode_label} Combined "
        )

        loading = self.query("#h2h-loading")
        if loading:
            loading.first().remove()
        self.query_one("#matchups-table", DataTable).display = True

        # Season matchups table (W/L per opponent across weeks)
        table = self.query_one("#matchups-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Opponent", "Matchup W-L-T", "Win %")

        opp_rows = []
        for opp_key, data in all_h2h.items():
            a = data["agg"]
            opp_name = next((t.name for t in teams if t.team_key == opp_key),
                            opp_key)
            total = a.wins + a.losses + a.ties
            pct = a.wins / total if total > 0 else 0
            opp_rows.append((opp_key, opp_name, a, pct))
        opp_rows.sort(key=lambda x: x[3], reverse=True)

        self._matchups_keys = [opp_key for opp_key, _, _, _ in opp_rows]

        for _opp_key, opp_name, a, pct in opp_rows:
            pct_style = "bold green" if pct > 0.5 else "bold red" if pct < 0.5 else ""
            table.add_row(
                Text(opp_name, style="bold"),
                Text(f"{a.wins}-{a.losses}-{a.ties}", justify="center"),
                Text(f"{pct:.1%}", style=pct_style, justify="right"),
            )

        # Season rankings table
        self._render_season_rankings_table(season_rankings)

    def _render_matchups_table(self, h2h: dict, teams: list[TeamStats],
                               selected_key: str, actual_opp_key: str) -> None:
        table = self.query_one("#matchups-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        scored = [c for c in self.categories if not c.is_only_display]
        cat_names = [c.display_name for c in scored]
        table.add_columns("Opponent", "Record", "Result", *cat_names)

        my_results = h2h.get(selected_key, {})
        rows: list[tuple[bool, str, str, H2HResult]] = []
        for opp_key, result in my_results.items():
            opp_name = next((t.name for t in teams if t.team_key == opp_key), opp_key)
            is_actual = opp_key == actual_opp_key
            rows.append((is_actual, opp_key, opp_name, result))

        # Sort: actual first, then by opponent wins descending (easiest matchups first)
        rows.sort(key=lambda r: (not r[0], -r[3].wins))

        self._matchups_keys = [opp_key for _, opp_key, _, _ in rows]

        for is_actual, _opp_key, opp_name, result in rows:
            name_text = Text()
            if is_actual:
                name_text.append("ACTUAL ", style="bold on #4A7C59")
            name_text.append(opp_name, style="bold")

            if result.result == "WIN":
                result_text = Text(" WIN ", style="bold on #2D4A2D")
            elif result.result == "LOSS":
                result_text = Text(" LOSS ", style="bold on #4A2D2D")
            else:
                result_text = Text(" TIE ", style="bold on #3A3A3A")

            row: list[Text] = [
                name_text,
                Text(result.record_str, justify="center"),
                result_text,
            ]
            for cat_name, cat_result in result.cat_results:
                if cat_result == "w":
                    row.append(Text(f" W ", style="bold on #2D4A2D"))
                elif cat_result == "l":
                    row.append(Text(f" L ", style="bold on #4A2D2D"))
                else:
                    row.append(Text(f" T ", style="on #3A3A3A"))
            table.add_row(*row)

    def _render_rankings_table(self, rankings: list[TeamH2HSummary],
                               matchups: list[Matchup]) -> None:
        self._rankings_keys = [s.team_key for s in rankings]
        table = self.query_one("#rankings-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Team", "Manager", "H2H Record", "Win %",
                          "Actual", "Luck")

        # Build actual results map
        actual: dict[str, str] = {}
        for m in matchups:
            pa, pb = m.team_a.points, m.team_b.points
            if pa > pb:
                actual[m.team_a.team_key] = "W"
                actual[m.team_b.team_key] = "L"
            elif pb > pa:
                actual[m.team_a.team_key] = "L"
                actual[m.team_b.team_key] = "W"
            else:
                actual[m.team_a.team_key] = "T"
                actual[m.team_b.team_key] = "T"

        for rank, s in enumerate(rankings, 1):
            pct = s.win_pct
            pct_style = "bold green" if pct >= 0.6 else "bold red" if pct < 0.4 else ""

            actual_result = actual.get(s.team_key, "-")
            if actual_result == "W":
                actual_text = Text(" W ", style="bold on #2D4A2D")
            elif actual_result == "L":
                actual_text = Text(" L ", style="bold on #4A2D2D")
            else:
                actual_text = Text(f" {actual_result} ", style="on #3A3A3A")

            # Luck: if you had high win% but lost, unlucky
            if actual_result == "W" and pct < 0.5:
                luck = Text(" Lucky ", style="bold green")
            elif actual_result == "L" and pct >= 0.5:
                luck = Text(" Unlucky ", style="bold red")
            else:
                luck = Text("")

            table.add_row(
                Text(str(rank), justify="right"),
                Text(s.name, style="bold"),
                Text(s.manager, style="dim"),
                Text(s.record_str, justify="center"),
                Text(f"{pct:.1%}", style=pct_style, justify="right"),
                actual_text,
                luck,
            )

    def _render_season_rankings_table(self, rankings: list[TeamH2HSummary]) -> None:
        self._rankings_keys = [s.team_key for s in rankings]
        table = self.query_one("#rankings-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Team", "Manager", "H2H Record", "Win %")

        for rank, s in enumerate(rankings, 1):
            pct = s.win_pct
            pct_style = "bold green" if pct >= 0.6 else "bold red" if pct < 0.4 else ""
            table.add_row(
                Text(str(rank), justify="right"),
                Text(s.name, style="bold"),
                Text(s.manager, style="dim"),
                Text(s.record_str, justify="center"),
                Text(f"{pct:.1%}", style=pct_style, justify="right"),
            )

    def action_prev_week(self) -> None:
        if self._season_mode != "off":
            self._season_mode = "off"
        if self._week > 1:
            self._week -= 1
            self.run_worker(self._render_all, group="h2h-load", exclusive=True)

    def action_next_week(self) -> None:
        if self._season_mode != "off":
            self._season_mode = "off"
        if self._week < self._max_week:
            self._week += 1
            self.run_worker(self._render_all, group="h2h-load", exclusive=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Select a manager from either table."""
        table = event.data_table
        row_idx = event.cursor_row

        if table.id == "rankings-table" and hasattr(self, "_rankings_keys"):
            if 0 <= row_idx < len(self._rankings_keys):
                key = self._rankings_keys[row_idx]
                if key in self._team_keys:
                    self._team_idx = self._team_keys.index(key)
                    self.run_worker(self._render_all, group="h2h-load", exclusive=True)
        elif table.id == "matchups-table" and hasattr(self, "_matchups_keys"):
            if 0 <= row_idx < len(self._matchups_keys):
                key = self._matchups_keys[row_idx]
                if key in self._team_keys:
                    self._team_idx = self._team_keys.index(key)
                    self.run_worker(self._render_all, group="h2h-load", exclusive=True)

    def action_toggle_season(self) -> None:
        if self._season_mode == "off":
            # Mon: default to completed only; Tue-Sun: show all weeks
            self._season_mode = "completed" if _is_monday() else "all"
        elif self._season_mode == "completed":
            self._season_mode = "all"
        else:
            self._season_mode = "off"
        self.run_worker(self._render_all, group="h2h-load", exclusive=True)

    def action_go_back(self) -> None:
        self.app.pop_screen()


# --- Team Selection Modal ---


class WeekSelectModal(Screen):
    """Modal for selecting a scoring week."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    WeekSelectModal {
        align: center middle;
    }
    #week-select-container {
        width: 40;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #week-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #week-select-list {
        height: auto;
        max-height: 70%;
    }
    #week-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #week-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, max_week: int, selected_week: int, league_week: int | None = None) -> None:
        super().__init__()
        self.max_week = max_week
        self.selected_week = selected_week
        self.league_week = league_week  # actual current week of the season

    def compose(self) -> ComposeResult:
        with Vertical(id="week-select-container"):
            yield Static("Select Week", id="week-select-title")
            yield ListView(id="week-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#week-select-list", ListView)
        for w in range(1, self.max_week + 1):
            label = f"Week {w}"
            if self.league_week and w == self.league_week:
                label += "  (current)"
            elif self.league_week and w > self.league_week:
                label += "  (projected)"
            if w == self.selected_week:
                label += "  ●"
            item = ListItem(Label(label))
            item._week = w
            lv.mount(item)
        lv.index = self.selected_week - 1

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        week = getattr(event.item, "_week", None)
        self.dismiss(week)

    def action_cancel(self) -> None:
        self.dismiss(None)


class WeekRangeModal(Screen):
    """Modal for selecting a start and end week range."""
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("up", "switch_field", "Switch"),
        ("down", "switch_field", "Switch"),
        ("left", "decrement", "-1"),
        ("right", "increment", "+1"),
        ("enter", "confirm", "Confirm"),
    ]
    CSS = """
    WeekRangeModal {
        align: center middle;
    }
    #week-range-container {
        width: 44;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #week-range-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
        margin-bottom: 1;
    }
    .week-range-row {
        height: 1;
        padding: 0 1;
    }
    .week-range-row.--active {
        background: #3A5A3A;
    }
    #week-range-hint {
        height: 1;
        content-align: center middle;
        color: $text-muted;
        margin-top: 1;
    }
    """

    def __init__(self, max_week: int, week_start: int, week_end: int) -> None:
        super().__init__()
        self.max_week = max_week
        self._start = week_start
        self._end = week_end
        self._field = 0  # 0 = start, 1 = end

    def compose(self) -> ComposeResult:
        with Vertical(id="week-range-container"):
            yield Static("Select Week Range", id="week-range-title")
            yield Static("", id="week-range-start", classes="week-range-row")
            yield Static("", id="week-range-end", classes="week-range-row")
            yield Static("↑↓ switch  ←→ adjust  enter confirm", id="week-range-hint")

    def on_mount(self) -> None:
        self._refresh_display()

    def _refresh_display(self) -> None:
        start_label = self.query_one("#week-range-start", Static)
        end_label = self.query_one("#week-range-end", Static)

        s_text = Text()
        s_text.append("  Start Week:  ", style="bold" if self._field == 0 else "")
        s_text.append(f"◀ Week {self._start} ▶", style="bold")
        start_label.update(s_text)

        e_text = Text()
        e_text.append("  End Week:    ", style="bold" if self._field == 1 else "")
        e_text.append(f"◀ Week {self._end} ▶", style="bold")
        end_label.update(e_text)

        start_label.set_class(self._field == 0, "--active")
        end_label.set_class(self._field == 1, "--active")

    def action_switch_field(self) -> None:
        self._field = 1 - self._field
        self._refresh_display()

    def action_decrement(self) -> None:
        if self._field == 0:
            if self._start > 1:
                self._start -= 1
                if self._end < self._start:
                    self._end = self._start
        else:
            if self._end > self._start:
                self._end -= 1
        self._refresh_display()

    def action_increment(self) -> None:
        if self._field == 0:
            if self._start < self._end:
                self._start += 1
        else:
            if self._end < self.max_week:
                self._end += 1
                if self._start > self._end:
                    self._start = self._end
        self._refresh_display()

    def action_confirm(self) -> None:
        self.dismiss((self._start, self._end))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TeamSelectModal(Screen):
    """Modal for selecting a team from a list."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    TeamSelectModal {
        align: center middle;
    }
    #team-select-container {
        width: 50;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #team-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #team-select-list {
        height: auto;
        max-height: 70%;
    }
    #team-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #team-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.options = options  # [(team_key, team_name), ...]

    def compose(self) -> ComposeResult:
        with Vertical(id="team-select-container"):
            yield Static("Select Team", id="team-select-title")
            yield ListView(id="team-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#team-select-list", ListView)
        for key, name in self.options:
            item = ListItem(Label(name))
            item._team_key = key
            lv.mount(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = getattr(event.item, "_team_key", None)
        self.dismiss(key)

    def action_cancel(self) -> None:
        self.dismiss(None)



# --- Position Selection Modal ---


class PositionSelectModal(Screen):
    """Modal for selecting a fantasy position to browse free agents."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    PositionSelectModal {
        align: center middle;
    }
    #pos-select-container {
        width: 40;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #pos-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #pos-select-list {
        height: auto;
        max-height: 70%;
    }
    #pos-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #pos-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    POSITIONS = [
        ("C", "C — Catcher"),
        ("1B", "1B — First Base"),
        ("2B", "2B — Second Base"),
        ("3B", "3B — Third Base"),
        ("SS", "SS — Shortstop"),
        ("LF", "LF — Left Field"),
        ("CF", "CF — Center Field"),
        ("RF", "RF — Right Field"),
        ("SP", "SP — Starting Pitcher"),
        ("RP", "RP — Relief Pitcher"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="pos-select-container"):
            yield Static("Select Position", id="pos-select-title")
            yield ListView(id="pos-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#pos-select-list", ListView)
        for key, label in self.POSITIONS:
            item = ListItem(Label(label))
            item._pos_key = key
            lv.mount(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        key = getattr(event.item, "_pos_key", None)
        self.dismiss(key)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --- Roster Analysis Screen ---


class RosterAnalysisScreen(PlayerCompareMixin, Screen):
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("m", "cycle_team", "Next Team"),
                ("1", "view_season", "Season"), ("2", "view_l14", "L14"),
                ("3", "view_l30", "L30"),
                ("c", "compare", "Compare"),
                ("i", "player_detail", "Player Detail")]
    CSS = """
    #roster-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #roster-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #roto-rank-bar {
        height: 2;
        background: #2A2A2A;
        padding: 0 1;
    }
    #roster-view-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #roster-loading {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }
    .roster-table-section {
        height: 1;
        content-align: left middle;
        background: #2A2A2A;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    #batters-table {
        height: auto;
        max-height: 50%;
        background: $panel;
    }
    #pitchers-table {
        height: auto;
        max-height: 50%;
        background: $panel;
    }
    #roster-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #roster-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #roster-spinner {
        height: 3;
    }
    #roster-scroll {
        height: 1fr;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._team_keys: list[str] = []
        self._team_names: dict[str, str] = {}
        self._team_idx = 0
        self._view = "season"  # "season", "l14", "l30"
        self._all_teams: list[TeamStats] = []
        self._draft_results: dict[str, str] = {}  # player_key -> actual cost
        self._sgp_calc: SGPCalculator | None = None
        self._rank_lookup: dict[str, int] = {}
        self._preseason_rank_lookup: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="roster-header")
        yield Static("", id="roster-controls")
        yield Static("", id="roto-rank-bar")
        yield Static("", id="roster-view-label")
        with Vertical(id="roster-loading-container"):
            yield LoadingIndicator(id="roster-spinner")
            yield Static("Loading team data...", id="roster-loading-status")
        with VerticalScroll(id="roster-scroll"):
            yield Static(" Batters", classes="roster-table-section")
            yield DataTable(id="batters-table")
            yield Static(" Pitchers", classes="roster-table-section")
            yield DataTable(id="pitchers-table")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#roster-header", Static).update(
            f" {self.league.name} — Roster Analysis "
        )
        self.query_one("#roster-scroll").display = False
        # Pre-fetch team list, then prompt user to select
        self.run_worker(self._initial_load)

    async def _show_loading(self, message: str = "Loading...") -> None:
        import asyncio
        container = self.query("#roster-loading-container")
        if container:
            container.first().display = True
        status = self.query("#roster-loading-status")
        if status:
            status.first().update(message)
        scroll = self.query("#roster-scroll")
        if scroll:
            scroll.first().display = False
        # Yield to let the UI repaint before blocking work
        await asyncio.sleep(0)

    def _hide_loading(self) -> None:
        container = self.query("#roster-loading-container")
        if container:
            container.first().display = False
        scroll = self.query("#roster-scroll")
        if scroll:
            scroll.first().display = True

    async def _initial_load(self) -> None:
        cache = self.app.shared_cache
        await cache.ensure_loaded(
            self.api, self.league, self.categories,
            progress_cb=self._show_loading,
        )
        self._all_teams = cache.all_teams
        self._team_keys = cache.team_keys
        self._team_names = cache.team_names
        self._draft_results = cache.draft_results
        self._sgp_calc = cache.sgp_calc
        self._rank_lookup = cache.rank_lookup
        self._preseason_rank_lookup = cache.preseason_rank_lookup

        # Prompt user to select a team
        options = [(k, self._team_names.get(k, k)) for k in self._team_keys]
        self.app.push_screen(
            TeamSelectModal(options),
            callback=self._on_initial_team_selected,
        )

    def _on_initial_team_selected(self, team_key: str | None) -> None:
        if team_key and team_key in self._team_keys:
            self._team_idx = self._team_keys.index(team_key)
            self.run_worker(self._render_roster, group="roster-load", exclusive=True)
        elif self._team_keys:
            # User cancelled — load first team
            self._team_idx = 0
            self.run_worker(self._render_roster, group="roster-load", exclusive=True)

    async def _render_roster(self) -> None:
        if not self._team_keys:
            return

        team_key = self._team_keys[self._team_idx]
        team_name = self._team_names.get(team_key, "?")
        week = self.league.current_week

        # Show loading with team name
        await self._show_loading(f"Loading roster for {team_name}...")

        # Controls
        ctrl = Text()
        ctrl.append(f"[1] Season  [2] L14  [3] L30", style="dim")
        ctrl.append(f"  |  ", style="dim")
        ctrl.append(f"{team_name}", style=f"bold {TEAM_A_COLOR}")
        ctrl.append(f"  ([m] select team)", style="dim")
        self.query_one("#roster-controls", Static).update(ctrl)

        # View label
        view_labels = {"season": "SEASON", "l14": "LAST 14 DAYS", "l30": "LAST 30 DAYS"}
        self.query_one("#roster-view-label", Static).update(
            f" {view_labels[self._view]} STATS — {team_name} "
        )

        # Roto rank bar
        self._render_roto_ranks(team_key)

        # Fetch roster stats based on view
        await self._show_loading(f"Fetching {view_labels[self._view].lower()} stats for {team_name}...")
        if self._view == "l14":
            players = self.api.get_roster_stats_last7(team_key, week)
        elif self._view == "l30":
            players = self.api.get_roster_stats_last30(team_key, week)
        else:
            players = self.api.get_roster_stats_season(team_key, week)

        batting_cats, bat_unscored = build_stat_columns(self.categories, "B")
        pitching_cats, pitch_unscored = build_stat_columns(self.categories, "P")

        batting_positions = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
                             "OF", "Util", "DH", "IF", "BN"}
        batters = [p for p in players if
                   any(pos in batting_positions for pos in p.position.split(","))]
        pitchers = [p for p in players if p not in batters]

        await self._show_loading(f"Loading Statcast data from Baseball Savant...")

        # Pre-fetch statcast data so we can merge it into the main tables
        batter_statcast: dict[str, StatcastBatter] = {}
        mlbam_ids: dict[str, int] = {}  # player name -> mlbam_id
        for p in batters:
            mlbam_id = lookup_mlbam_id(p.name)
            if mlbam_id is not None:
                mlbam_ids[p.name] = mlbam_id
                sc = get_batter_statcast(mlbam_id)
                if sc is not None:
                    batter_statcast[p.name] = sc

        pitcher_statcast: dict[str, StatcastPitcher] = {}
        for p in pitchers:
            mlbam_id = lookup_mlbam_id(p.name)
            if mlbam_id is not None:
                mlbam_ids[p.name] = mlbam_id
                sc = get_pitcher_statcast(mlbam_id)
                if sc is not None:
                    pitcher_statcast[p.name] = sc

        # Bulk fetch player ages and games played from MLB API
        all_ids = list(mlbam_ids.values())
        age_by_id = get_player_ages(all_ids)
        games_by_id = get_player_games(all_ids)
        player_ages: dict[str, int] = {
            name: age_by_id[mid]
            for name, mid in mlbam_ids.items()
            if mid in age_by_id
        }
        # Inject G into player stats dicts so pinned column rendering finds it
        for p in batters + pitchers:
            mid = mlbam_ids.get(p.name)
            if mid and mid in games_by_id and "0" not in p.stats:
                p.stats["0"] = str(games_by_id[mid])

        await self._show_loading(f"Rendering roster tables...")

        def _f(v: float | None, fmt: str = ".1f") -> str:
            return f"{v:{fmt}}" if v is not None else "-"

        def _rate(v: float | None) -> str:
            return f"{v:.1f}" if v is not None else "-"

        # --- Batters table (league stats + statcast) ---
        bat_table = self.query_one("#batters-table", DataTable)
        bat_table.clear(columns=True)
        bat_table.cursor_type = "row"
        bat_table.zebra_stripes = True
        bat_cols: list[str | Text] = ["Player", "Pos".ljust(15), "Team", "Age", "Paid", "Avg$", "SGP", "Y!", "Pre"]
        for cat in batting_cats:
            if cat.stat_id in bat_unscored:
                bat_cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                bat_cols.append(cat.display_name)
        bat_cols.append("│")  # visual divider between league and statcast
        bat_cols.extend([
            "EV", "MaxEV", "LA", "Barrel%", "HardHit%",
            "K%", "BB%", "Whiff%", "xBA", "xSLG", "xwOBA",
        ])
        bat_table.add_columns(*bat_cols)
        bat_table._players = batters  # type: ignore[attr-defined]
        for p in batters:
            actual_cost = self._draft_results.get(p.player_key, "")
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = player_ages.get(p.name)
            row: list[Text] = [
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                Text(f"${actual_cost}" if actual_cost else "-",
                     justify="right"),
                Text(f"${p.draft_cost}" if p.draft_cost else "-", style="dim",
                     justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            ]
            for cat in batting_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in bat_unscored else ""
                row.append(Text(val, style=style, justify="right"))
            # Divider
            row.append(Text("│", style="dim"))
            # Statcast columns
            sc = batter_statcast.get(p.name)
            if sc:
                row.extend([
                    Text(_f(sc.avg_exit_velo), justify="right"),
                    Text(_f(sc.max_exit_velo), justify="right"),
                    Text(_f(sc.avg_launch_angle), justify="right"),
                    Text(_f(sc.barrel_pct), justify="right"),
                    Text(_f(sc.hard_hit_pct), justify="right"),
                    Text(_rate(sc.k_pct), justify="right"),
                    Text(_rate(sc.bb_pct), justify="right"),
                    Text(_rate(sc.whiff_pct), justify="right"),
                    Text(_f(sc.xba, ".3f"), justify="right"),
                    Text(_f(sc.xslg, ".3f"), justify="right"),
                    Text(_f(sc.xwoba, ".3f"), justify="right"),
                ])
            else:
                row.extend([Text("-", style="dim", justify="right")] * 11)
            bat_table.add_row(*row)

        # --- Pitchers table (league stats + statcast) ---
        pitch_table = self.query_one("#pitchers-table", DataTable)
        pitch_table.clear(columns=True)
        pitch_table.cursor_type = "row"
        pitch_table.zebra_stripes = True
        pitch_cols: list[str | Text] = ["Player", "Pos".ljust(15), "Team", "Age", "Paid", "Avg$", "SGP", "Y!", "Pre"]
        for cat in pitching_cats:
            if cat.stat_id in pitch_unscored:
                pitch_cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                pitch_cols.append(cat.display_name)
        pitch_cols.append("│")  # visual divider
        pitch_cols.extend([
            "EV Alw", "Barrel%", "HardHit%",
            "xBA", "xSLG", "xwOBA", "xERA",
            "K%p", "BB%p", "Whiff%p",
        ])
        pitch_table.add_columns(*pitch_cols)
        pitch_table._players = pitchers  # type: ignore[attr-defined]
        for p in pitchers:
            actual_cost = self._draft_results.get(p.player_key, "")
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = player_ages.get(p.name)
            row = [
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                Text(f"${actual_cost}" if actual_cost else "-",
                     justify="right"),
                Text(f"${p.draft_cost}" if p.draft_cost else "-", style="dim",
                     justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            ]
            for cat in pitching_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in pitch_unscored else ""
                row.append(Text(val, style=style, justify="right"))
            # Divider
            row.append(Text("│", style="dim"))
            # Statcast columns
            sc = pitcher_statcast.get(p.name)
            if sc:
                row.extend([
                    Text(_f(sc.avg_exit_velo), justify="right"),
                    Text(_f(sc.barrel_pct), justify="right"),
                    Text(_f(sc.hard_hit_pct), justify="right"),
                    Text(_f(sc.xba, ".3f"), justify="right"),
                    Text(_f(sc.xslg, ".3f"), justify="right"),
                    Text(_f(sc.xwoba, ".3f"), justify="right"),
                    Text(_f(sc.xera, ".2f"), justify="right"),
                    Text(_rate(sc.k_pct), justify="right"),
                    Text(_rate(sc.bb_pct), justify="right"),
                    Text(_rate(sc.whiff_pct), justify="right"),
                ])
            else:
                row.extend([Text("-", style="dim", justify="right")] * 10)
            pitch_table.add_row(*row)

        self._hide_loading()

    def _render_roto_ranks(self, team_key: str) -> None:
        """Show roto ranking for the selected team in each scored category."""
        scored = [c for c in self.categories if not c.is_only_display]
        num_teams = len(self._all_teams)
        team = next((t for t in self._all_teams if t.team_key == team_key), None)
        if not team:
            return

        rank_text = Text()
        rank_text.append(" Roto Rank:  ", style="bold")

        for cat in scored:
            # Rank this team in this category
            vals = []
            for t in self._all_teams:
                try:
                    vals.append((t.team_key, float(t.stats.get(cat.stat_id, "0"))))
                except ValueError:
                    vals.append((t.team_key, 0.0))

            higher_is_better = cat.sort_order == "1"
            vals.sort(key=lambda x: x[1], reverse=higher_is_better)
            rank = next((i + 1 for i, (k, _) in enumerate(vals) if k == team_key), 0)

            # Color based on rank
            if rank <= num_teams * 0.25:
                style = "bold green"
            elif rank <= num_teams * 0.5:
                style = "bold"
            elif rank <= num_teams * 0.75:
                style = "bold yellow"
            else:
                style = "bold red"

            rank_text.append(f" {cat.display_name}:", style="dim")
            rank_text.append(f"{rank}", style=style)

        self.query_one("#roto-rank-bar", Static).update(rank_text)

    def action_cycle_team(self) -> None:
        """Open a team selection modal."""
        if not self._team_keys:
            return
        options = [(k, self._team_names.get(k, k)) for k in self._team_keys]
        self.app.push_screen(
            TeamSelectModal(options),
            callback=self._on_team_selected,
        )

    def _on_team_selected(self, team_key: str | None) -> None:
        if team_key and team_key in self._team_keys:
            self._team_idx = self._team_keys.index(team_key)
            self.run_worker(self._render_roster, group="roster-load", exclusive=True)

    def action_view_season(self) -> None:
        self._view = "season"
        self.run_worker(self._render_roster, group="roster-load", exclusive=True)

    def action_view_l14(self) -> None:
        self._view = "l14"
        self.run_worker(self._render_roster, group="roster-load", exclusive=True)

    def action_view_l30(self) -> None:
        self._view = "l30"
        self.run_worker(self._render_roster, group="roster-load", exclusive=True)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_player_detail(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
        except Exception:
            return
        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return
        p = players[row_idx]
        cache = self.app.shared_cache
        self.app.push_screen(PlayerDetailScreen(
            p.name, p.position, p.team_abbr,
            categories=self.categories,
            all_teams=cache.all_teams if cache.is_loaded else None,
            replacement_by_pos=cache.replacement_by_pos if cache.is_loaded else None,
        ))


# --- Free Agent Browser Screen ---


class FreeAgentScreen(PlayerCompareMixin, Screen):
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("1", "view_season", "Season"),
        ("2", "view_last7", "Last 7"),
        ("3", "view_last30", "Last 30"),
        ("p", "select_position", "Position"),
        ("a", "view_all", "All"),
        ("slash", "focus_search", "Search"),
        ("right", "next_page", "Next Page"),
        ("left", "prev_page", "Prev Page"),
        ("w", "watchlist_toggle", "Watch"),
        ("c", "compare", "Compare"),
        ("i", "player_detail", "Player Detail"),
    ]
    CSS = """
    #fa-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #fa-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #fa-view-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #fa-search {
        height: 3;
        display: none;
        margin: 0 1;
    }
    #fa-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #fa-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #fa-spinner {
        height: 3;
    }
    #fa-scroll {
        height: 1fr;
    }
    #fa-pagination {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    .roster-table-section {
        height: 1;
        content-align: left middle;
        background: #2A2A2A;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    #fa-batters-table, #fa-pitchers-table {
        height: auto;
        max-height: 50%;
        background: $panel;
    }
    .fa-pos-table {
        height: auto;
        max-height: 30%;
        background: $panel;
    }
    """

    STAT_TYPES = {
        "season": ("Season", "SEASON STATS"),
        "lastweek": ("Last 7", "LAST 7 DAYS"),
        "lastmonth": ("Last 30", "LAST 30 DAYS"),
    }

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._stat_type = "season"
        self._position: str | None = None
        self._search: str | None = None
        self._page_start = 0
        self._page_size = 25
        self._current_players: list[PlayerStats] = []
        self._has_next_page = False
        self._draft_results: dict[str, str] = {}
        self._sgp_calc: SGPCalculator | None = None
        self._baselines_loaded = False
        self._rank_lookup: dict[str, int] = {}
        self._preseason_rank_lookup: dict[str, int] = {}
        self._store = RosterDataStore()
        self._player_rows: dict[str, list[PlayerStats]] = {}  # table_id -> players

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="fa-header")
        yield Static("", id="fa-controls")
        yield Static("", id="fa-view-label")
        yield Input(placeholder="Search player name... (Enter to search, Escape to cancel)", id="fa-search")
        with Vertical(id="fa-loading-container"):
            yield LoadingIndicator(id="fa-spinner")
            yield Static("Loading free agents...", id="fa-loading-status")
        yield VerticalScroll(id="fa-scroll")
        yield Static("", id="fa-pagination")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#fa-header", Static).update(
            f" {self.league.name} — Free Agents "
        )
        self.query_one("#fa-scroll").display = False
        self.query_one("#fa-pagination").display = False
        self._update_controls()
        self.run_worker(self._initial_load)

    # --- Loading helpers ---

    async def _show_loading(self, message: str = "Loading...") -> None:
        import asyncio
        container = self.query("#fa-loading-container")
        if container:
            container.first().display = True
        status = self.query("#fa-loading-status")
        if status:
            status.first().update(message)
        scroll = self.query("#fa-scroll")
        if scroll:
            scroll.first().display = False
        await asyncio.sleep(0)

    def _hide_loading(self) -> None:
        container = self.query("#fa-loading-container")
        if container:
            container.first().display = False
        scroll = self.query("#fa-scroll")
        if scroll:
            scroll.first().display = True
        pagination = self.query("#fa-pagination")
        if pagination:
            pagination.first().display = True

    # --- Controls and labels ---

    def _update_controls(self) -> None:
        ctrl = Text()
        for key, (label, _) in self.STAT_TYPES.items():
            num = {"season": "1", "lastweek": "2", "lastmonth": "3"}[key]
            if key == self._stat_type:
                ctrl.append(f" [{num}] {label} ", style="bold on #4A7C59")
            else:
                ctrl.append(f" [{num}] {label} ", style="dim")

        ctrl.append("  |  ", style="dim")
        pos_label = self._position or "ALL"
        ctrl.append("Pos: ", style="dim")
        ctrl.append(pos_label, style="bold")
        ctrl.append(" [p]", style="dim")

        ctrl.append("  |  ", style="dim")
        if self._search:
            ctrl.append(f'Search: "{self._search}"', style="bold")
            ctrl.append(" [/]", style="dim")
        else:
            ctrl.append("[/] Search", style="dim")

        self.query_one("#fa-controls", Static).update(ctrl)

    def _update_view_label(self) -> None:
        _, desc = self.STAT_TYPES[self._stat_type]
        pos_label = self._position or "ALL POSITIONS"
        label = f" {desc} — {pos_label} "
        if self._search:
            label = f' {desc} — Search: "{self._search}" '
        self.query_one("#fa-view-label", Static).update(label)

    def _update_pagination(self) -> None:
        is_default = self._position is None and self._search is None
        pag = Text()
        if is_default:
            pag.append("  Top 15 overall + Top 5 per position  ", style="dim")
            pag.append("  [p] Filter by position  [/] Search", style="dim")
        else:
            page_num = (self._page_start // self._page_size) + 1
            range_start = self._page_start + 1
            range_end = self._page_start + len(self._current_players)
            if self._page_start > 0:
                pag.append(" \u2190 Prev ", style="bold")
            pag.append(f"  Page {page_num}  ({range_start}-{range_end})", style="bold")
            if self._has_next_page:
                pag.append("  Next \u2192 ", style="bold")
            if not self._has_next_page and self._page_start == 0:
                pag.append(f"  ({len(self._current_players)} results)", style="dim")
        self.query_one("#fa-pagination", Static).update(pag)

    # --- Data loading ---

    async def _initial_load(self) -> None:
        """One-time setup: fetch league data, draft results, and SGP baselines."""
        cache = self.app.shared_cache
        await cache.ensure_loaded(
            self.api, self.league, self.categories,
            progress_cb=self._show_loading,
        )
        self._draft_results = cache.draft_results
        self._sgp_calc = cache.sgp_calc
        self._baselines_loaded = True
        self._rank_lookup = cache.rank_lookup
        self._preseason_rank_lookup = cache.preseason_rank_lookup

        # Now load the first page of free agents
        await self._load_free_agents()

    def _is_batter(self, p: PlayerStats) -> bool:
        batting_positions = {
            "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
            "OF", "Util", "DH", "IF", "BN",
        }
        return any(pos in batting_positions for pos in p.position.split(","))

    def _fetch_statcast_and_ages(
        self, players: list[PlayerStats],
    ) -> tuple[dict[str, StatcastBatter], dict[str, StatcastPitcher], dict[str, int]]:
        batter_sc: dict[str, StatcastBatter] = {}
        pitcher_sc: dict[str, StatcastPitcher] = {}
        mlbam_ids: dict[str, int] = {}
        for p in players:
            mlbam_id = lookup_mlbam_id(p.name)
            if mlbam_id is None:
                continue
            mlbam_ids[p.name] = mlbam_id
            if self._is_batter(p):
                sc = get_batter_statcast(mlbam_id)
                if sc is not None:
                    batter_sc[p.name] = sc
            else:
                sc = get_pitcher_statcast(mlbam_id)
                if sc is not None:
                    pitcher_sc[p.name] = sc
        all_ids = list(mlbam_ids.values())
        age_by_id = get_player_ages(all_ids)
        games_by_id = get_player_games(all_ids)
        player_ages = {
            name: age_by_id[mid]
            for name, mid in mlbam_ids.items()
            if mid in age_by_id
        }
        # Inject G into player stats dicts for pinned column rendering
        for p in players:
            mid = mlbam_ids.get(p.name)
            if mid and mid in games_by_id and "0" not in p.stats:
                p.stats["0"] = str(games_by_id[mid])
        return batter_sc, pitcher_sc, player_ages

    async def _load_free_agents(self) -> None:
        _, desc = self.STAT_TYPES[self._stat_type]

        # Clear the scroll container
        scroll = self.query_one("#fa-scroll", VerticalScroll)
        await scroll.remove_children()

        is_default_view = self._position is None and self._search is None

        if is_default_view:
            await self._load_default_view(desc)
        else:
            await self._load_filtered_view(desc)

        self._update_controls()
        self._update_view_label()
        self._update_pagination()
        self._hide_loading()

    async def _load_default_view(self, desc: str) -> None:
        """Default view: top 15 overall + top 5 per position."""
        scroll = self.query_one("#fa-scroll", VerticalScroll)

        # Fetch top 15 overall (mixed batters and pitchers)
        await self._show_loading(f"Fetching top free agents ({desc.lower()})...")
        top_players, _ = self.api.get_free_agents(
            self.league.league_key,
            stat_type=self._stat_type,
            sort="AR", sort_type=self._stat_type,
            count=15,
        )
        self._current_players = top_players
        self._has_next_page = False  # no pagination in default view

        # Fetch top 5 per position
        bat_positions = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
        pitch_positions = ["SP", "RP"]
        pos_players: dict[str, list[PlayerStats]] = {}
        for pos in bat_positions + pitch_positions:
            await self._show_loading(f"Fetching top {pos} free agents...")
            players, _ = self.api.get_free_agents(
                self.league.league_key,
                stat_type=self._stat_type,
                position=pos, sort="AR", sort_type=self._stat_type,
                count=5,
            )
            pos_players[pos] = players

        # Collect all unique players for statcast lookup
        all_players_set: dict[str, PlayerStats] = {}
        for p in top_players:
            all_players_set[p.player_key] = p
        for plist in pos_players.values():
            for p in plist:
                all_players_set[p.player_key] = p

        await self._show_loading("Loading Statcast data from Baseball Savant...")
        batter_sc, pitcher_sc, self._player_ages = self._fetch_statcast_and_ages(
            list(all_players_set.values()))

        await self._show_loading("Rendering tables...")

        # --- Top 15 overall: single unified table ---
        if top_players:
            label = Static(" Top 15 Free Agents", classes="roster-table-section")
            table = DataTable(id="fa-top-table")
            await scroll.mount(label, table)
            self._populate_overview_table(table, top_players)

        # --- Per-position batter tables ---
        for pos in bat_positions:
            players = pos_players.get(pos, [])
            if not players:
                continue
            label = Static(f" Top {pos}", classes="roster-table-section")
            table = DataTable(classes="fa-pos-table")
            await scroll.mount(label, table)
            self._populate_batter_table(table, players, batter_sc)

        # --- Per-position pitcher tables ---
        for pos in pitch_positions:
            players = pos_players.get(pos, [])
            if not players:
                continue
            label = Static(f" Top {pos}", classes="roster-table-section")
            table = DataTable(classes="fa-pos-table")
            await scroll.mount(label, table)
            self._populate_pitcher_table(table, players, pitcher_sc)

    async def _load_filtered_view(self, desc: str) -> None:
        """Position-filtered or search view with pagination."""
        scroll = self.query_one("#fa-scroll", VerticalScroll)

        await self._show_loading(f"Fetching free agents ({desc.lower()})...")
        players, total = self.api.get_free_agents(
            self.league.league_key,
            stat_type=self._stat_type,
            position=self._position,
            search=self._search,
            sort="AR",
            sort_type=self._stat_type,
            start=self._page_start,
            count=self._page_size,
        )
        self._current_players = players
        self._has_next_page = len(players) == self._page_size

        batters = [p for p in players if self._is_batter(p)]
        pitchers = [p for p in players if not self._is_batter(p)]

        await self._show_loading("Loading Statcast data from Baseball Savant...")
        batter_sc, pitcher_sc, self._player_ages = self._fetch_statcast_and_ages(players)

        await self._show_loading("Rendering tables...")

        if batters:
            label = Static(" Batters", classes="roster-table-section")
            table = DataTable(id="fa-batters-table")
            await scroll.mount(label, table)
            self._populate_batter_table(table, batters, batter_sc)

        if pitchers:
            label = Static(" Pitchers", classes="roster-table-section")
            table = DataTable(id="fa-pitchers-table")
            await scroll.mount(label, table)
            self._populate_pitcher_table(table, pitchers, pitcher_sc)

    # --- Table rendering ---

    def _populate_overview_table(
        self,
        table: DataTable,
        players: list[PlayerStats],
    ) -> None:
        """Compact overview table for the top-15 mixed batter/pitcher list."""
        table._players = players  # type: ignore[attr-defined]
        ages = getattr(self, "_player_ages", {})
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Player", "Pos".ljust(15), "Team", "Age", "Avg$", "SGP", "Y!", "Pre")

        for p in players:
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = ages.get(p.name)
            table.add_row(
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                Text(f"${p.draft_cost}" if p.draft_cost else "-", style="dim",
                     justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            )

    def _populate_batter_table(
        self,
        table: DataTable,
        batters: list[PlayerStats],
        batter_statcast: dict[str, StatcastBatter],
    ) -> None:
        table._players = batters  # type: ignore[attr-defined]
        batting_cats, bat_unscored = build_stat_columns(self.categories, "B")
        ages = getattr(self, "_player_ages", {})

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        cols: list[str | Text] = ["Player", "Pos".ljust(15), "Team", "Age", "Avg$", "SGP", "Y!", "Pre"]
        for cat in batting_cats:
            if cat.stat_id in bat_unscored:
                cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                cols.append(cat.display_name)
        cols.append("│")
        cols.extend([
            "EV", "MaxEV", "LA", "Barrel%", "HardHit%",
            "K%", "BB%", "Whiff%", "xBA", "xSLG", "xwOBA",
        ])
        table.add_columns(*cols)

        def _f(v: float | None, fmt: str = ".1f") -> str:
            return f"{v:{fmt}}" if v is not None else "-"

        def _rate(v: float | None) -> str:
            return f"{v:.1f}" if v is not None else "-"

        for p in batters:
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = ages.get(p.name)
            row: list[Text] = [
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                Text(f"${p.draft_cost}" if p.draft_cost else "-", style="dim",
                     justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            ]
            for cat in batting_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in bat_unscored else ""
                row.append(Text(val, style=style, justify="right"))
            # Divider
            row.append(Text("│", style="dim"))
            # Statcast columns
            sc = batter_statcast.get(p.name)
            if sc:
                row.extend([
                    Text(_f(sc.avg_exit_velo), justify="right"),
                    Text(_f(sc.max_exit_velo), justify="right"),
                    Text(_f(sc.avg_launch_angle), justify="right"),
                    Text(_f(sc.barrel_pct), justify="right"),
                    Text(_f(sc.hard_hit_pct), justify="right"),
                    Text(_rate(sc.k_pct), justify="right"),
                    Text(_rate(sc.bb_pct), justify="right"),
                    Text(_rate(sc.whiff_pct), justify="right"),
                    Text(_f(sc.xba, ".3f"), justify="right"),
                    Text(_f(sc.xslg, ".3f"), justify="right"),
                    Text(_f(sc.xwoba, ".3f"), justify="right"),
                ])
            else:
                row.extend([Text("-", style="dim", justify="right")] * 11)
            table.add_row(*row)

    def _populate_pitcher_table(
        self,
        table: DataTable,
        pitchers: list[PlayerStats],
        pitcher_statcast: dict[str, StatcastPitcher],
    ) -> None:
        table._players = pitchers  # type: ignore[attr-defined]
        pitching_cats, pitch_unscored = build_stat_columns(self.categories, "P")
        ages = getattr(self, "_player_ages", {})

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        cols: list[str | Text] = ["Player", "Pos".ljust(15), "Team", "Age", "Avg$", "SGP", "Y!", "Pre"]
        for cat in pitching_cats:
            if cat.stat_id in pitch_unscored:
                cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                cols.append(cat.display_name)
        cols.append("│")
        cols.extend([
            "EV Alw", "Barrel%", "HardHit%",
            "xBA", "xSLG", "xwOBA", "xERA",
            "K%p", "BB%p", "Whiff%p",
        ])
        table.add_columns(*cols)

        def _f(v: float | None, fmt: str = ".1f") -> str:
            return f"{v:{fmt}}" if v is not None else "-"

        def _rate(v: float | None) -> str:
            return f"{v:.1f}" if v is not None else "-"

        for p in pitchers:
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = ages.get(p.name)
            row: list[Text] = [
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                Text(f"${p.draft_cost}" if p.draft_cost else "-", style="dim",
                     justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            ]
            for cat in pitching_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in pitch_unscored else ""
                row.append(Text(val, style=style, justify="right"))
            # Divider
            row.append(Text("│", style="dim"))
            # Statcast columns
            sc = pitcher_statcast.get(p.name)
            if sc:
                row.extend([
                    Text(_f(sc.avg_exit_velo), justify="right"),
                    Text(_f(sc.barrel_pct), justify="right"),
                    Text(_f(sc.hard_hit_pct), justify="right"),
                    Text(_f(sc.xba, ".3f"), justify="right"),
                    Text(_f(sc.xslg, ".3f"), justify="right"),
                    Text(_f(sc.xwoba, ".3f"), justify="right"),
                    Text(_f(sc.xera, ".2f"), justify="right"),
                    Text(_rate(sc.k_pct), justify="right"),
                    Text(_rate(sc.bb_pct), justify="right"),
                    Text(_rate(sc.whiff_pct), justify="right"),
                ])
            else:
                row.extend([Text("-", style="dim", justify="right")] * 10)
            table.add_row(*row)

    # --- Actions ---

    def action_view_season(self) -> None:
        self._stat_type = "season"
        self._page_start = 0
        self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_view_last7(self) -> None:
        self._stat_type = "lastweek"
        self._page_start = 0
        self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_view_last30(self) -> None:
        self._stat_type = "lastmonth"
        self._page_start = 0
        self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_view_all(self) -> None:
        self._position = None
        self._search = None
        self._page_start = 0
        search_input = self.query_one("#fa-search", Input)
        search_input.value = ""
        search_input.display = False
        self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_select_position(self) -> None:
        self.app.push_screen(
            PositionSelectModal(),
            callback=self._on_position_selected,
        )

    def _on_position_selected(self, position: str | None) -> None:
        if position is None:
            return
        self._position = position
        self._page_start = 0
        self._search = None
        search_input = self.query_one("#fa-search", Input)
        search_input.value = ""
        search_input.display = False
        self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_focus_search(self) -> None:
        search_input = self.query_one("#fa-search", Input)
        search_input.display = True
        search_input.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self._search = query
            self._position = None
        else:
            self._search = None
        self._page_start = 0
        event.input.display = False
        self.set_focus(None)
        self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def on_key(self, event) -> None:
        search_input = self.query_one("#fa-search", Input)
        if search_input.has_focus and event.key == "escape":
            search_input.value = ""
            search_input.display = False
            self.set_focus(None)
            event.prevent_default()

    def action_next_page(self) -> None:
        if self._has_next_page:
            self._page_start += self._page_size
            self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_prev_page(self) -> None:
        if self._page_start > 0:
            self._page_start = max(0, self._page_start - self._page_size)
            self.run_worker(self._load_free_agents, group="fa-load", exclusive=True)

    def action_watchlist_toggle(self) -> None:
        """Toggle the focused player on/off the watchlist."""
        # Find the focused DataTable and get the player from its stored list
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
            if not isinstance(table, DataTable):
                return
        except Exception:
            return

        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return

        p = players[row_idx]
        if self._store.is_on_watchlist(self.league.league_key, p.player_key):
            self._store.remove_from_watchlist(self.league.league_key, p.player_key)
            self.notify(f"Removed {p.name} from watchlist")
        else:
            self._store.add_to_watchlist(
                self.league.league_key, p.player_key,
                p.name, p.position, p.team_abbr,
            )
            self.notify(f"Added {p.name} to watchlist")

    def action_player_detail(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
        except Exception:
            return
        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return
        p = players[row_idx]
        cache = self.app.shared_cache
        self.app.push_screen(PlayerDetailScreen(
            p.name, p.position, p.team_abbr,
            categories=self.categories,
            all_teams=cache.all_teams if cache.is_loaded else None,
            replacement_by_pos=cache.replacement_by_pos if cache.is_loaded else None,
        ))

    def action_go_back(self) -> None:
        self.app.pop_screen()


# --- Watchlist Screen ---


class WatchlistScreen(PlayerCompareMixin, Screen):
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("c", "compare", "Compare"),
        ("d", "remove_player", "Remove"),
        ("i", "player_detail", "Player Detail"),
    ]
    CSS = """
    #wl-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #wl-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #wl-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #wl-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #wl-spinner {
        height: 3;
    }
    #wl-scroll {
        height: 1fr;
    }
    .wl-section-header {
        height: 1;
        content-align: left middle;
        background: #2A2A2A;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    .wl-table {
        height: auto;
        max-height: 45%;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._store = RosterDataStore()
        self._sgp_calc: SGPCalculator | None = None
        self._watchlist_players: list[PlayerStats] = []
        self._rank_lookup: dict[str, int] = {}
        self._preseason_rank_lookup: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="wl-header")
        yield Static("", id="wl-controls")
        with Vertical(id="wl-loading-container"):
            yield LoadingIndicator(id="wl-spinner")
            yield Static("Loading watchlist...", id="wl-loading-status")
        yield VerticalScroll(id="wl-scroll")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#wl-header", Static).update(
            f" {self.league.name} — Watchlist "
        )
        ctrl = Text()
        ctrl.append("  [c] Compare to roster  [d] Remove  [Esc] Back", style="dim")
        self.query_one("#wl-controls", Static).update(ctrl)
        self.run_worker(self._initial_load)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    async def _show_loading(self, msg: str) -> None:
        try:
            self.query_one("#wl-loading-status", Static).update(msg)
            self.query_one("#wl-loading-container").display = True
            self.query_one("#wl-scroll").display = False
        except Exception:
            pass

    def _hide_loading(self) -> None:
        try:
            self.query_one("#wl-loading-container").display = False
            self.query_one("#wl-scroll").display = True
        except Exception:
            pass

    async def _initial_load(self) -> None:
        cache = self.app.shared_cache
        await cache.ensure_loaded(
            self.api, self.league, self.categories,
            progress_cb=self._show_loading,
        )
        self._sgp_calc = cache.sgp_calc
        self._rank_lookup = cache.rank_lookup
        self._preseason_rank_lookup = cache.preseason_rank_lookup

        await self._load_watchlist()

    async def _load_watchlist(self) -> None:
        await self._show_loading("Loading watchlist players...")
        watchlist = self._store.get_watchlist(self.league.league_key)
        if not watchlist:
            self._hide_loading()
            scroll = self.query_one("#wl-scroll", VerticalScroll)
            await scroll.remove_children()
            await scroll.mount(Static(
                "  No players on watchlist.\n"
                "  Press [w] on a player in the Free Agents screen to add them.\n",
            ))
            return

        # Fetch current stats for each watchlisted player
        self._watchlist_players = []
        for i, entry in enumerate(watchlist):
            await self._show_loading(
                f"Fetching player stats ({i + 1}/{len(watchlist)})..."
            )
            try:
                results = await asyncio.to_thread(
                    self.api.search_players,
                    self.league.league_key, entry["player_name"], 5,
                )
                for r in results:
                    if r.player_key == entry["player_key"]:
                        self._watchlist_players.append(r)
                        break
                else:
                    self._watchlist_players.append(PlayerStats(
                        player_key=entry["player_key"],
                        name=entry["player_name"],
                        position=entry["player_position"],
                        team_abbr=entry.get("team_abbr", ""),
                    ))
            except Exception:
                self._watchlist_players.append(PlayerStats(
                    player_key=entry["player_key"],
                    name=entry["player_name"],
                    position=entry["player_position"],
                    team_abbr=entry.get("team_abbr", ""),
                ))

        await self._show_loading("Loading Statcast data...")
        batting_positions = {
            "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
            "OF", "Util", "DH", "IF", "BN",
        }
        batters = [p for p in self._watchlist_players
                   if any(pos in batting_positions for pos in p.position.split(","))]
        pitchers = [p for p in self._watchlist_players if p not in batters]

        batter_sc: dict[str, StatcastBatter] = {}
        mlbam_ids: dict[str, int] = {}
        for p in batters:
            mlbam_id = lookup_mlbam_id(p.name)
            if mlbam_id is not None:
                mlbam_ids[p.name] = mlbam_id
                sc = get_batter_statcast(mlbam_id)
                if sc is not None:
                    batter_sc[p.name] = sc

        pitcher_sc: dict[str, StatcastPitcher] = {}
        for p in pitchers:
            mlbam_id = lookup_mlbam_id(p.name)
            if mlbam_id is not None:
                mlbam_ids[p.name] = mlbam_id
                sc = get_pitcher_statcast(mlbam_id)
                if sc is not None:
                    pitcher_sc[p.name] = sc

        all_ids = list(mlbam_ids.values())
        age_by_id = get_player_ages(all_ids)
        games_by_id = get_player_games(all_ids)
        self._player_ages: dict[str, int] = {
            name: age_by_id[mid]
            for name, mid in mlbam_ids.items()
            if mid in age_by_id
        }
        # Inject G into player stats dicts for pinned column rendering
        for p in batters + pitchers:
            mid = mlbam_ids.get(p.name)
            if mid and mid in games_by_id and "0" not in p.stats:
                p.stats["0"] = str(games_by_id[mid])

        await self._show_loading("Rendering watchlist...")
        scroll = self.query_one("#wl-scroll", VerticalScroll)
        await scroll.remove_children()

        if batters:
            label = Static(" Batters", classes="wl-section-header")
            table = DataTable(classes="wl-table")
            await scroll.mount(label, table)
            self._render_batter_table(table, batters, batter_sc)

        if pitchers:
            label = Static(" Pitchers", classes="wl-section-header")
            table = DataTable(classes="wl-table")
            await scroll.mount(label, table)
            self._render_pitcher_table(table, pitchers, pitcher_sc)

        self._hide_loading()

    def _render_batter_table(
        self, table: DataTable, batters: list[PlayerStats],
        batter_sc: dict[str, StatcastBatter],
    ) -> None:
        table._players = batters  # type: ignore[attr-defined]
        batting_cats, bat_unscored = build_stat_columns(self.categories, "B")
        ages = getattr(self, "_player_ages", {})

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        cols: list[str | Text] = ["Player".ljust(20), "Pos".ljust(15), "Team", "Age", "SGP", "Y!", "Pre"]
        for cat in batting_cats:
            if cat.stat_id in bat_unscored:
                cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                cols.append(cat.display_name)
        cols.append("│")
        cols.extend(["EV", "MaxEV", "LA", "Barrel%", "HardHit%",
                      "K%", "BB%", "Whiff%", "xBA", "xSLG", "xwOBA"])
        table.add_columns(*cols)

        def _f(v, fmt=".1f"):
            return f"{v:{fmt}}" if v is not None else "-"
        def _rate(v):
            return f"{v:.1f}" if v is not None else "-"

        for p in batters:
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = ages.get(p.name)
            row: list[Text] = [
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            ]
            for cat in batting_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in bat_unscored else ""
                row.append(Text(val, style=style, justify="right"))
            row.append(Text("│", style="dim"))
            sc = batter_sc.get(p.name)
            if sc:
                row.extend([
                    Text(_f(sc.avg_exit_velo), justify="right"),
                    Text(_f(sc.max_exit_velo), justify="right"),
                    Text(_f(sc.avg_launch_angle), justify="right"),
                    Text(_f(sc.barrel_pct), justify="right"),
                    Text(_f(sc.hard_hit_pct), justify="right"),
                    Text(_rate(sc.k_pct), justify="right"),
                    Text(_rate(sc.bb_pct), justify="right"),
                    Text(_rate(sc.whiff_pct), justify="right"),
                    Text(_f(sc.xba, ".3f"), justify="right"),
                    Text(_f(sc.xslg, ".3f"), justify="right"),
                    Text(_f(sc.xwoba, ".3f"), justify="right"),
                ])
            else:
                row.extend([Text("-", style="dim", justify="right")] * 11)
            table.add_row(*row)

    def _render_pitcher_table(
        self, table: DataTable, pitchers: list[PlayerStats],
        pitcher_sc: dict[str, StatcastPitcher],
    ) -> None:
        table._players = pitchers  # type: ignore[attr-defined]
        pitching_cats, pitch_unscored = build_stat_columns(self.categories, "P")
        ages = getattr(self, "_player_ages", {})

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        cols: list[str | Text] = ["Player".ljust(20), "Pos".ljust(15), "Team", "Age", "SGP", "Y!", "Pre"]
        for cat in pitching_cats:
            if cat.stat_id in pitch_unscored:
                cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                cols.append(cat.display_name)
        cols.append("│")
        cols.extend(["EV Alw", "Barrel%", "HardHit%",
                      "xBA", "xSLG", "xwOBA", "xERA",
                      "K%p", "BB%p", "Whiff%p"])
        table.add_columns(*cols)

        def _f(v, fmt=".1f"):
            return f"{v:{fmt}}" if v is not None else "-"
        def _rate(v):
            return f"{v:.1f}" if v is not None else "-"

        for p in pitchers:
            sgp_val = self._sgp_calc.player_sgp(p) if self._sgp_calc else None
            if sgp_val is not None:
                sgp_style = "bold green" if sgp_val > 0 else "bold red" if sgp_val < 0 else ""
                sgp_text = Text(f"{sgp_val:+.1f}", style=sgp_style, justify="right")
            else:
                sgp_text = Text("N/A", style="dim", justify="right")
            y_rank = self._rank_lookup.get(p.player_key)
            pre_rank = self._preseason_rank_lookup.get(p.player_key)
            age = ages.get(p.name)
            row: list[Text] = [
                Text(p.name[:20].ljust(20), style="bold"),
                Text(p.position.ljust(15), style="dim"),
                Text(p.team_abbr, style="dim"),
                Text(str(age) if age else "-", style="dim", justify="right"),
                sgp_text,
                Text(str(y_rank) if y_rank else "-", style="dim", justify="right"),
                Text(str(pre_rank) if pre_rank else "-", style="dim", justify="right"),
            ]
            for cat in pitching_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in pitch_unscored else ""
                row.append(Text(val, style=style, justify="right"))
            row.append(Text("│", style="dim"))
            sc = pitcher_sc.get(p.name)
            if sc:
                row.extend([
                    Text(_f(sc.avg_exit_velo), justify="right"),
                    Text(_f(sc.barrel_pct), justify="right"),
                    Text(_f(sc.hard_hit_pct), justify="right"),
                    Text(_f(sc.xba, ".3f"), justify="right"),
                    Text(_f(sc.xslg, ".3f"), justify="right"),
                    Text(_f(sc.xwoba, ".3f"), justify="right"),
                    Text(_f(sc.xera, ".2f"), justify="right"),
                    Text(_rate(sc.k_pct), justify="right"),
                    Text(_rate(sc.bb_pct), justify="right"),
                    Text(_rate(sc.whiff_pct), justify="right"),
                ])
            else:
                row.extend([Text("-", style="dim", justify="right")] * 10)
            table.add_row(*row)

    def action_remove_player(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
            if not isinstance(table, DataTable):
                return
        except Exception:
            return

        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return

        p = players[row_idx]
        self._store.remove_from_watchlist(self.league.league_key, p.player_key)
        self.notify(f"Removed {p.name} from watchlist")
        self.run_worker(self._load_watchlist)

    def action_player_detail(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
        except Exception:
            return
        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return
        p = players[row_idx]
        cache = self.app.shared_cache
        self.app.push_screen(PlayerDetailScreen(
            p.name, p.position, p.team_abbr,
            categories=self.categories,
            all_teams=cache.all_teams if cache.is_loaded else None,
            replacement_by_pos=cache.replacement_by_pos if cache.is_loaded else None,
        ))

class ComparisonScreen(Screen):
    """Compare a player against position-matched roster players.

    Two-mode flow:
    - Summary mode (default): list of position-eligible drop candidates with
      ΔSGP, ΔRoto, and ΔWin% per scenario. Enter drills into detail mode.
    - Detail mode: full category impact, roto standings, H2H replay,
      hypothetical, and AI summary for a specific add/drop.
    """
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("1", "view_season", "Season"),
        ("2", "view_l14", "L14"),
        ("3", "view_l30", "L30"),
        ("w", "toggle_watchlist", "Watchlist"),
        ("T", "open_trade_analyzer", "Trade Analyzer"),
    ]
    CSS = """
    #cmp-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #cmp-subheader {
        height: 2;
        padding: 0 1;
        background: $surface;
    }
    #cmp-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #cmp-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #cmp-spinner {
        height: 3;
    }
    #cmp-scroll {
        height: 1fr;
    }
    .cmp-table {
        height: auto;
        background: $panel;
    }
    .cmp-section {
        height: 1;
        text-style: bold;
        background: #2A2A2A;
        padding: 0 1;
        color: $text-muted;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory],
                 watchlist_player: PlayerStats,
                 team_key: str,
                 team_name: str,
                 sgp_calc: SGPCalculator | None) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._wl_player = watchlist_player
        self._wl_player_key = watchlist_player.player_key
        self._wl_player_name = watchlist_player.name
        self._team_key = team_key
        self._team_name = team_name
        self._sgp_calc = sgp_calc
        self._view = "season"
        self._mode = "summary"  # "summary" or "detail"
        self._scenarios: list = []  # list[CompareScenario]
        self._roster: list[PlayerStats] = []
        self._selected_scenario = None  # CompareScenario
        self._store = RosterDataStore()

    @property
    def _is_batter(self) -> bool:
        batting = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "Util", "DH"}
        return any(p in batting for p in self._wl_player.position.split(","))

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="cmp-header")
        yield Static("", id="cmp-subheader")
        with Vertical(id="cmp-loading-container"):
            yield LoadingIndicator(id="cmp-spinner")
            yield Static("Loading comparison...", id="cmp-loading-status")
        yield VerticalScroll(id="cmp-scroll")
        yield WrappingFooter()

    def _update_subheader(self) -> None:
        view_labels = {"season": "Season", "l14": "Last 14 Days", "l30": "Last 30 Days"}
        sub = Text()
        sub.append(f"\n Comparing ", style="dim")
        sub.append(f"{self._wl_player.name}", style="bold #E8A735")
        sub.append(f" ({self._wl_player.position})", style="dim")
        sub.append(f" vs ", style="dim")
        sub.append(f"{self._team_name}", style="bold")
        sub.append(f"  |  {view_labels.get(self._view, self._view)}", style="dim")
        if self._mode == "summary":
            sub.append(f"  [Enter] detail  [1/2/3] view  [w] watch  [T] trade\n", style="dim")
        else:
            sub.append(f"  [Esc] back to list  [w] watch  [T] trade\n", style="dim")
        self.query_one("#cmp-subheader", Static).update(sub)

    def on_mount(self) -> None:
        self.query_one("#cmp-header", Static).update(
            f" {self.league.name} — Player Comparison "
        )
        self._update_subheader()
        self.run_worker(self._load_comparison)

    def action_go_back(self) -> None:
        # If in detail mode, return to the summary list
        if self._mode == "detail":
            self._mode = "summary"
            self._selected_scenario = None
            self._update_subheader()
            self.run_worker(self._render_summary, group="cmp-render", exclusive=True)
            return
        self.app.pop_screen()

    def action_view_season(self) -> None:
        self._view = "season"
        self._update_subheader()
        self.run_worker(self._load_comparison, group="cmp-load", exclusive=True)

    def action_view_l14(self) -> None:
        self._view = "l14"
        self._update_subheader()
        self.run_worker(self._load_comparison, group="cmp-load", exclusive=True)

    def action_view_l30(self) -> None:
        self._view = "l30"
        self._update_subheader()
        self.run_worker(self._load_comparison, group="cmp-load", exclusive=True)

    def action_toggle_watchlist(self) -> None:
        """Toggle the compared player on/off the watchlist."""
        p = self._wl_player
        if self._store.is_on_watchlist(self.league.league_key, p.player_key):
            self._store.remove_from_watchlist(self.league.league_key, p.player_key)
            self.notify(f"Removed {p.name} from watchlist")
        else:
            self._store.add_to_watchlist(
                self.league.league_key, p.player_key,
                p.name, p.position, p.team_abbr,
            )
            self.notify(f"Added {p.name} to watchlist")

    def action_open_trade_analyzer(self) -> None:
        """Open the Trade Analyzer with the compared player pre-selected.

        If the player is on another fantasy team, configures the analyze-trade
        flow with that team pre-selected. Otherwise notifies the user.
        """
        self.run_worker(self._launch_trade_analyzer, group="cmp-ta", exclusive=True)

    async def _launch_trade_analyzer(self) -> None:
        # Find which fantasy team owns the compared player by scanning rosters
        cache = self.app.shared_cache
        await cache.ensure_loaded(self.api, self.league, self.categories)
        import asyncio

        other_teams = [t for t in cache.all_teams if t.team_key != self._team_key]
        owner_team_key: str | None = None
        owner_team_name: str = ""

        # Scan opposing rosters in parallel
        roster_tasks = [
            asyncio.to_thread(
                self.api.get_roster_stats_season, t.team_key, self.league.current_week)
            for t in other_teams
        ]
        rosters = await asyncio.gather(*roster_tasks, return_exceptions=True)
        for team, roster in zip(other_teams, rosters):
            if isinstance(roster, Exception):
                continue
            for p in roster:
                if p.player_key == self._wl_player.player_key:
                    owner_team_key = team.team_key
                    owner_team_name = team.name
                    break
            if owner_team_key:
                break

        if not owner_team_key:
            self.notify(
                f"{self._wl_player.name} is a free agent — Trade Analyzer requires a rostered player.",
                severity="warning",
            )
            return

        # Launch Trade Analyzer with both teams and players pre-configured
        screen = TradeAnalyzerScreen(self.api, self.league, self.categories)
        # Pre-seed state so on_mount skips the team select modals
        screen._team_a_key = self._team_key
        screen._team_a_name = self._team_name
        screen._team_b_key = owner_team_key
        screen._team_b_name = owner_team_name
        screen._skip_auto_select = True  # flag for on_mount
        self.app.push_screen(screen)

    def _get_roster_fetcher(self):
        if self._view == "l14":
            return self.api.get_roster_stats_last7
        elif self._view == "l30":
            return self.api.get_roster_stats_last30
        return self.api.get_roster_stats_season

    async def _load_comparison(self) -> None:
        """Fetch roster + weekly data and compute scenarios, then render summary."""
        try:
            self.query_one("#cmp-loading-status", Static).update(
                "Loading roster for comparison..."
            )
            self.query_one("#cmp-loading-container").display = True
            self.query_one("#cmp-scroll").display = False
        except Exception:
            pass

        fetch = self._get_roster_fetcher()

        # Fetch the team's roster with the selected stat view
        self._roster = await asyncio.to_thread(
            fetch, self._team_key, self.league.current_week,
        )

        # Re-fetch the compared player's stats with the same stat view
        stat_type = {"l14": "lastweek", "l30": "lastmonth"}.get(self._view, "season")
        try:
            found, _ = await asyncio.to_thread(
                self.api.get_free_agents,
                self.league.league_key,
                status=None, stat_type=stat_type,
                search=self._wl_player_name,
                count=5,
            )
            for p in found:
                if p.player_key == self._wl_player_key:
                    self._wl_player = p
                    break
        except Exception:
            pass

        # Compute scenarios with full roto and H2H impact
        try:
            self.query_one("#cmp-loading-status", Static).update(
                "Computing roto and H2H impact for each candidate..."
            )
        except Exception:
            pass

        cache = self.app.shared_cache
        await cache.ensure_loaded(self.api, self.league, self.categories)

        # Fetch weekly data for accurate H2H replay
        weeks = list(range(1, self.league.current_week + 1))
        for w in weeks:
            if w not in cache.week_matchups:
                try:
                    wm = await asyncio.to_thread(
                        self.api.get_scoreboard, self.league.league_key, w)
                    cache.week_matchups[w] = wm
                except Exception:
                    pass

        weekly_roster_target: dict[int, list[PlayerStats]] = {}
        for w in weeks:
            try:
                weekly_roster_target[w] = await asyncio.to_thread(
                    self.api.get_roster_stats, self._team_key, w)
            except Exception:
                pass

        from gkl.trade import compute_compare_scenarios
        self._scenarios = await asyncio.to_thread(
            compute_compare_scenarios,
            self._wl_player,
            self._team_key,
            self._roster,
            cache.all_teams,
            self.categories,
            cache.sgp_calc,
            cache.week_matchups,
            weekly_roster_target,
            self.league.current_week,
        )

        await self._render_summary()

        try:
            self.query_one("#cmp-loading-container").display = False
            self.query_one("#cmp-scroll").display = True
        except Exception:
            pass

    async def _render_summary(self) -> None:
        """Render the summary table of drop-candidate scenarios."""
        self._mode = "summary"
        scroll = self.query_one("#cmp-scroll", VerticalScroll)
        await scroll.remove_children()

        if not self._scenarios:
            await scroll.mount(Static(
                "  No position-eligible players found on this roster.\n",
                classes="cmp-table",
            ))
            return

        # Show the added player header
        cache = self.app.shared_cache
        add_sgp = cache.sgp_calc.player_sgp(self._wl_player) if cache.sgp_calc else None
        header = Text()
        header.append(f" Adding ", style="dim")
        header.append(f"{self._wl_player.name}", style="bold #E8A735")
        header.append(f" ({self._wl_player.position}, {self._wl_player.team_abbr})", style="dim")
        if add_sgp is not None:
            header.append(f"  —  SGP: {add_sgp:+.1f}", style="dim")
        header.append(f"\n Each row shows the impact of dropping that player to make room.",
                      style="dim italic")
        await scroll.mount(Static(header))

        table = DataTable(classes="cmp-table", id="cmp-scenario-table")
        await scroll.mount(table)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table._players = self._scenarios  # type: ignore[attr-defined]
        table.add_columns("Drop Player", "Pos", "Team", "SGP", "ΔSGP", "ΔRoto", "ΔWin%")

        for s in self._scenarios:
            drop = s.drop_player
            sgp_str = f"{s.drop_sgp:+.1f}" if s.drop_sgp is not None else "N/A"

            net_str = f"{s.net_sgp:+.1f}"
            net_style = "bold green" if s.net_sgp > 0 else "bold red" if s.net_sgp < 0 else "dim"

            roto_str = f"{s.roto_delta:+.1f}"
            roto_style = "bold green" if s.roto_delta > 0.1 else "bold red" if s.roto_delta < -0.1 else "dim"

            if abs(s.h2h_win_pct_delta) > 0.001:
                h2h_str = f"{s.h2h_win_pct_delta:+.1%}"
                h2h_style = "bold green" if s.h2h_win_pct_delta > 0 else "bold red"
            else:
                h2h_str = "—"
                h2h_style = "dim"

            table.add_row(
                Text(drop.name[:20], style="bold"),
                Text(drop.position[:12], style="dim"),
                Text(drop.team_abbr, style="dim"),
                Text(sgp_str, justify="right"),
                Text(net_str, style=net_style, justify="right"),
                Text(roto_str, style=roto_style, justify="right"),
                Text(h2h_str, style=h2h_style, justify="right"),
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Drill into detail view when a scenario row is selected."""
        if self._mode != "summary":
            return
        row_idx = event.cursor_row
        if row_idx < 0 or row_idx >= len(self._scenarios):
            return
        scenario = self._scenarios[row_idx]
        self._selected_scenario = scenario
        self._mode = "detail"
        self._update_subheader()
        self.run_worker(self._render_detail, group="cmp-detail", exclusive=True)

    async def _render_detail(self) -> None:
        """Render the full detail analysis for the selected scenario."""
        from gkl.trade import (
            apply_trade_to_team, replay_h2h_with_trade, compute_h2h_hypothetical,
            get_trade_ai_summary,
        )
        from gkl.skipper import DEFAULT_MODEL, load_anthropic_key

        scenario = self._selected_scenario
        if scenario is None:
            return

        cache = self.app.shared_cache
        scored = [c for c in self.categories if not c.is_only_display]

        # Compute trade impact for the selected scenario
        target_team = next(
            (t for t in cache.all_teams if t.team_key == self._team_key), None)
        if target_team is None:
            return

        trade_team = apply_trade_to_team(
            target_team, self._roster,
            players_out=[scenario.drop_player],
            players_in=[self._wl_player],
            categories=self.categories,
        )
        teams_after = [
            trade_team if t.team_key == self._team_key else t
            for t in cache.all_teams
        ]

        # Category impact
        cat_impacts = []
        for cat in scored:
            before_val = target_team.stats.get(cat.stat_id, "0")
            after_val = trade_team.stats.get(cat.stat_id, "0")
            try:
                delta = float(after_val) - float(before_val)
            except (ValueError, TypeError):
                delta = 0.0
            higher_better = cat.sort_order == "1"
            favorable = (delta > 0) if higher_better else (delta < 0)
            cat_impacts.append({
                "cat": cat, "before": before_val, "after": after_val,
                "delta": delta, "favorable": favorable if delta != 0 else True,
            })

        # Roto standings before/after
        roto_before = compute_roto(cache.all_teams, scored)
        roto_after = compute_roto(teams_after, scored)
        bat_cats = [c for c in scored if c.position_type == "B"]
        pit_cats = [c for c in scored if c.position_type == "P"]
        roto_bat_before = compute_roto(cache.all_teams, bat_cats)
        roto_bat_after = compute_roto(teams_after, bat_cats)
        roto_pit_before = compute_roto(cache.all_teams, pit_cats)
        roto_pit_after = compute_roto(teams_after, pit_cats)

        bat_before_by_key = {r["team_key"]: r["total"] for r in roto_bat_before}
        bat_after_by_key = {r["team_key"]: r["total"] for r in roto_bat_after}
        pit_before_by_key = {r["team_key"]: r["total"] for r in roto_pit_before}
        pit_after_by_key = {r["team_key"]: r["total"] for r in roto_pit_after}
        before_ranks = {r["team_key"]: (i, r["total"])
                        for i, r in enumerate(roto_before, 1)}

        # H2H replay + hypothetical
        weeks = list(range(1, self.league.current_week + 1))

        # Ensure weekly matchups are cached
        for w in weeks:
            if w not in cache.week_matchups:
                try:
                    cache.week_matchups[w] = await asyncio.to_thread(
                        self.api.get_scoreboard, self.league.league_key, w)
                except Exception:
                    pass

        weekly_roster_target: dict[int, list[PlayerStats]] = {}
        for w in weeks:
            try:
                weekly_roster_target[w] = await asyncio.to_thread(
                    self.api.get_roster_stats, self._team_key, w)
            except Exception:
                pass

        h2h_replay = None
        h2h_hypo = None
        h2h_error = None
        try:
            h2h_replay = await asyncio.to_thread(
                replay_h2h_with_trade,
                self._team_key, self._team_key,
                {scenario.drop_player.player_key}, {self._wl_player.player_key},
                cache.week_matchups,
                weekly_roster_target,
                {w: [self._wl_player] for w in weekly_roster_target.keys()},
                self.categories, self.league.current_week,
            )
            h2h_hypo = await asyncio.to_thread(
                compute_h2h_hypothetical,
                self._team_key,
                {scenario.drop_player.player_key}, {self._wl_player.player_key},
                cache.week_matchups,
                weekly_roster_target,
                {w: [self._wl_player] for w in weekly_roster_target.keys()},
                self.categories, self.league.current_week,
            )
        except Exception as e:
            h2h_error = str(e)

        # --- Render ---
        scroll = self.query_one("#cmp-scroll", VerticalScroll)
        await scroll.remove_children()

        # Summary line
        summary = Text()
        summary.append(f" Add ", style="dim")
        summary.append(f"{self._wl_player.name}", style="bold #E8A735")
        summary.append(f"  /  Drop ", style="dim")
        summary.append(f"{scenario.drop_player.name}", style=f"bold {TEAM_A_COLOR}")
        await scroll.mount(Static(summary))
        await scroll.mount(Static(""))

        # Category impact table
        await scroll.mount(Static(
            Text(" CATEGORY IMPACT ", style="bold"),
            classes="cmp-section",
        ))
        cat_table = DataTable(classes="cmp-table")
        await scroll.mount(cat_table)
        cat_table.cursor_type = "none"
        cat_table.zebra_stripes = True
        cat_table.add_columns("Category", "Before", "After", "Delta")
        for ci in cat_impacts:
            cat = ci["cat"]
            if ci["delta"] == 0:
                delta_style = "dim"
                delta_str = "—"
            elif ci["favorable"]:
                delta_style = "bold green"
                delta_str = (f"{ci['delta']:+.3f}"
                             if cat.stat_id in RATE_STATS
                             else f"{ci['delta']:+.0f}")
            else:
                delta_style = "bold red"
                delta_str = (f"{ci['delta']:+.3f}"
                             if cat.stat_id in RATE_STATS
                             else f"{ci['delta']:+.0f}")
            cat_table.add_row(
                Text(f" {cat.display_name}", style="bold"),
                Text(ci["before"], justify="right"),
                Text(ci["after"], justify="right"),
                Text(delta_str, style=delta_style, justify="right"),
            )

        await scroll.mount(Static(""))

        # Roto standings table
        await scroll.mount(Static(
            Text(" ROTO STANDINGS (POST-ADD/DROP) ", style="bold"),
            classes="cmp-section",
        ))
        roto_note = Text()
        roto_note.append(
            "  Note: if the added player is rostered on another team, "
            "that team's impact is not modeled here.\n"
            "  Press [T] to open the Trade Analyzer for the full league-wide trade view.",
            style="dim italic",
        )
        await scroll.mount(Static(roto_note))
        roto_table = DataTable(classes="cmp-table")
        await scroll.mount(roto_table)
        roto_table.cursor_type = "none"
        roto_table.zebra_stripes = True
        roto_table.add_columns(
            "", "Team", "Ovr", "Δ", "│", "Bat", "Δ", "│", "Pit", "Δ",
        )
        for i, r in enumerate(roto_after, 1):
            tk = r["team_key"]
            before_rank, before_total = before_ranks.get(tk, (i, r["total"]))
            rank_change = before_rank - i
            bat_delta = bat_after_by_key.get(tk, 0) - bat_before_by_key.get(tk, 0)
            pit_delta = pit_after_by_key.get(tk, 0) - pit_before_by_key.get(tk, 0)

            name_style = f"bold {TEAM_A_COLOR}" if tk == self._team_key else ""
            if rank_change > 0:
                rank_str, rank_style = f"▲{rank_change}", "bold green"
            elif rank_change < 0:
                rank_str, rank_style = f"▼{abs(rank_change)}", "bold red"
            else:
                rank_str, rank_style = "—", "dim"

            bat_d_str = (f"+{bat_delta:.0f}" if bat_delta > 0.1
                         else f"{bat_delta:.0f}" if bat_delta < -0.1 else "—")
            bat_d_style = "green" if bat_delta > 0.1 else "red" if bat_delta < -0.1 else "dim"
            pit_d_str = (f"+{pit_delta:.0f}" if pit_delta > 0.1
                         else f"{pit_delta:.0f}" if pit_delta < -0.1 else "—")
            pit_d_style = "green" if pit_delta > 0.1 else "red" if pit_delta < -0.1 else "dim"

            roto_table.add_row(
                Text(f"#{i}", style="bold" if tk == self._team_key else "dim"),
                Text(r["name"][:18], style=name_style),
                Text(f"{r['total']:.0f}", justify="right", style="bold"),
                Text(rank_str, style=rank_style, justify="right"),
                Text("│", style="dim"),
                Text(f"{bat_after_by_key.get(tk, 0):.0f}", justify="right"),
                Text(bat_d_str, style=bat_d_style, justify="right"),
                Text("│", style="dim"),
                Text(f"{pit_after_by_key.get(tk, 0):.0f}", justify="right"),
                Text(pit_d_str, style=pit_d_style, justify="right"),
            )

        # H2H weekly replay
        await scroll.mount(Static(""))
        await scroll.mount(Static(
            Text(" H2H WEEKLY REPLAY ", style="bold"),
            classes="cmp-section",
        ))
        replay_desc = Text()
        replay_desc.append(
            "  Replays each completed week's actual matchup with the swap applied.",
            style="dim italic",
        )
        await scroll.mount(Static(replay_desc))
        if h2h_replay and h2h_replay.weeks:
            replay_table = DataTable(classes="cmp-table")
            await scroll.mount(replay_table)
            replay_table.cursor_type = "none"
            replay_table.zebra_stripes = True
            replay_table.add_columns("Wk", "Opponent", "Actual", "W/ Swap", "")
            for wr in h2h_replay.weeks:
                actual_str = f"{wr.actual_wins}-{wr.actual_losses}-{wr.actual_ties}"
                trade_str = f"{wr.trade_wins}-{wr.trade_losses}-{wr.trade_ties}"
                if wr.changed:
                    if wr.trade_result == "W" and wr.actual_result != "W":
                        change_str, change_style = "▲ FLIP", "bold green"
                    elif wr.trade_result == "L" and wr.actual_result != "L":
                        change_str, change_style = "▼ FLIP", "bold red"
                    else:
                        change_str, change_style = "~ FLIP", "bold #E8A735"
                else:
                    change_str, change_style = "", "dim"
                replay_table.add_row(
                    Text(f"{wr.week}", justify="right"),
                    Text(wr.opponent_name[:18]),
                    Text(f"{actual_str} {wr.actual_result}", justify="right"),
                    Text(f"{trade_str} {wr.trade_result}", justify="right"),
                    Text(change_str, style=change_style),
                )
            await scroll.mount(Static(""))
            rec_summary = Text()
            rec_summary.append(f"  Season record: ", style="dim")
            rec_summary.append(
                f"{h2h_replay.actual_season_w}-{h2h_replay.actual_season_l}-{h2h_replay.actual_season_t}",
                style="bold",
            )
            rec_summary.append(f"  →  ", style="dim")
            rec_summary.append(
                f"{h2h_replay.trade_season_w}-{h2h_replay.trade_season_l}-{h2h_replay.trade_season_t}",
                style="bold",
            )
            sw_delta = h2h_replay.trade_season_w - h2h_replay.actual_season_w
            if sw_delta > 0:
                rec_summary.append(f"  +{sw_delta}W", style="bold green")
            elif sw_delta < 0:
                rec_summary.append(f"  {sw_delta}W", style="bold red")
            await scroll.mount(Static(rec_summary))
        else:
            msg = Text()
            if h2h_error:
                msg.append(f"  Could not compute replay: {h2h_error}", style="dim italic")
            else:
                msg.append(
                    "  No completed weeks with played games yet — check back after Week 1 has finished.",
                    style="dim italic",
                )
            await scroll.mount(Static(msg))

        # H2H hypothetical
        await scroll.mount(Static(""))
        await scroll.mount(Static(
            Text(" H2H HYPOTHETICAL (ALL OPPONENTS, ALL WEEKS) ", style="bold"),
            classes="cmp-section",
        ))
        if h2h_hypo and (h2h_hypo.before_w + h2h_hypo.before_l + h2h_hypo.before_t) > 0:
            n_matchups = h2h_hypo.before_w + h2h_hypo.before_l + h2h_hypo.before_t
            hypo_desc = Text()
            hypo_desc.append(
                f"  Simulates each completed week vs all other teams ({n_matchups} total matchups).",
                style="dim italic",
            )
            await scroll.mount(Static(hypo_desc))
            hypo_line = Text()
            hypo_line.append(f"  Before: ", style="dim")
            hypo_line.append(f"{h2h_hypo.before_w}-{h2h_hypo.before_l}-{h2h_hypo.before_t}", style="bold")
            b_pct = h2h_hypo.before_w / n_matchups if n_matchups else 0
            hypo_line.append(f" ({b_pct:.1%})", style="dim")
            hypo_line.append(f"   →   After: ", style="dim")
            hypo_line.append(f"{h2h_hypo.after_w}-{h2h_hypo.after_l}-{h2h_hypo.after_t}", style="bold")
            a_pct = h2h_hypo.after_w / n_matchups if n_matchups else 0
            hypo_line.append(f" ({a_pct:.1%})", style="dim")
            hw_delta = h2h_hypo.after_w - h2h_hypo.before_w
            if hw_delta > 0:
                hypo_line.append(f"  +{hw_delta}W", style="bold green")
            elif hw_delta < 0:
                hypo_line.append(f"  {hw_delta}W", style="bold red")
            await scroll.mount(Static(hypo_line))
        else:
            msg = Text()
            msg.append(
                "  No completed weeks with played games yet — check back after Week 1 has finished.",
                style="dim italic",
            )
            await scroll.mount(Static(msg))

        # AI summary
        api_key = load_anthropic_key()
        if api_key:
            await scroll.mount(Static(""))
            await scroll.mount(Static(
                Text(" AI ANALYSIS ", style="bold"),
                classes="cmp-section",
            ))
            ai_content = Static(
                Text("  Generating analysis...", style="dim italic"),
            )
            await scroll.mount(ai_content)
            scroll.scroll_end(animate=False)

            try:
                from gkl.trade import build_compare_summary_prompt
                before_rank, before_pts = before_ranks.get(self._team_key, (0, 0))
                after_rank = next(
                    (i for i, r in enumerate(roto_after, 1)
                     if r["team_key"] == self._team_key), 0,
                )
                after_pts = next(
                    (r["total"] for r in roto_after
                     if r["team_key"] == self._team_key), 0.0,
                )

                # Fetch statcast for both players
                import asyncio
                is_batter = self._is_batter
                add_sc = None
                drop_sc = None
                try:
                    add_mlbam, drop_mlbam = await asyncio.gather(
                        asyncio.to_thread(lookup_mlbam_id, self._wl_player.name),
                        asyncio.to_thread(lookup_mlbam_id, scenario.drop_player.name),
                    )
                    if is_batter:
                        add_sc = (await asyncio.to_thread(get_batter_statcast, add_mlbam)
                                  if add_mlbam else None)
                        drop_sc = (await asyncio.to_thread(get_batter_statcast, drop_mlbam)
                                   if drop_mlbam else None)
                    else:
                        add_sc = (await asyncio.to_thread(get_pitcher_statcast, add_mlbam)
                                  if add_mlbam else None)
                        drop_sc = (await asyncio.to_thread(get_pitcher_statcast, drop_mlbam)
                                   if drop_mlbam else None)
                except Exception:
                    pass

                prompt = build_compare_summary_prompt(
                    team_name=self._team_name,
                    add_player=self._wl_player,
                    drop_player=scenario.drop_player,
                    cat_impacts=cat_impacts,
                    roto_rank_before=before_rank,
                    roto_rank_after=after_rank,
                    roto_points_before=before_pts,
                    roto_points_after=after_pts,
                    h2h_replay=h2h_replay,
                    add_statcast=add_sc,
                    drop_statcast=drop_sc,
                    is_batter=is_batter,
                )
                ai_summary = await get_trade_ai_summary(prompt, api_key, DEFAULT_MODEL)
                ai_content.update(Text(f"  {ai_summary}"))
            except Exception as e:
                ai_content.update(Text(
                    f"  Could not generate AI analysis: {e}", style="dim italic",
                ))
            scroll.scroll_end(animate=False)

# --- Player Explorer Screen ---


class PlayerSearchModal(Screen):
    """Modal for searching and selecting a player."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    #ps-container {
        align: center middle;
        width: 60;
        height: auto;
        max-height: 28;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #ps-input {
        margin: 0 0 1 0;
    }
    #ps-results {
        height: 1fr;
    }
    #ps-info {
        height: auto;
        margin: 1 0 0 0;
        color: $text-muted;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self._store = RosterDataStore()

    def compose(self) -> ComposeResult:
        with Vertical(id="ps-container"):
            yield Static("Search for a player:", id="ps-label")
            yield Input(placeholder="Player name...", id="ps-input")
            yield ListView(id="ps-results")
            yield Static(
                "Roster data is synced daily from Yahoo and cached locally.\n"
                "The first search may take a few minutes while all roster\n"
                "history is downloaded. Subsequent searches load instantly.",
                id="ps-info",
            )

    def on_mount(self) -> None:
        self.query_one("#ps-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        lv = self.query_one("#ps-results", ListView)
        lv.clear()

        # Try Yahoo API first, fall back to local SQLite cache
        results: list[PlayerStats] = []
        try:
            results = self.api.search_players(
                self.league.league_key, query, count=10,
            )
        except Exception:
            pass

        if not results:
            # Fuzzy search from local cache: try each word separately
            cache_results = self._store.search_players(
                self.league.league_key, query,
            )
            if not cache_results:
                # Try each word individually for fuzzy matching
                for word in query.split():
                    if len(word) >= 2:
                        cache_results.extend(
                            self._store.search_players(
                                self.league.league_key, word,
                            )
                        )
                # Deduplicate
                seen: set[str] = set()
                deduped: list[dict] = []
                for r in cache_results:
                    if r["player_key"] not in seen:
                        seen.add(r["player_key"])
                        deduped.append(r)
                cache_results = deduped[:10]

            for r in cache_results:
                p = PlayerStats(
                    player_key=r["player_key"],
                    name=r["player_name"],
                    position=r.get("player_position", ""),
                    team_abbr="",
                )
                item = ListItem(
                    Label(f"{p.name} — {p.position}")
                )
                item._player = p  # type: ignore[attr-defined]
                lv.append(item)
            if not cache_results:
                lv.append(ListItem(Label("No results found.")))
            return

        for p in results:
            item = ListItem(
                Label(f"{p.name} — {p.position} — {p.team_abbr}")
            )
            item._player = p  # type: ignore[attr-defined]
            lv.append(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        player = getattr(event.item, "_player", None)
        if player:
            self.dismiss(player)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlayerExplorerScreen(Screen):
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
    ]
    CSS = """
    #pe-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #pe-player-info {
        height: 3;
        padding: 0 1;
        background: $surface;
    }
    #pe-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #pe-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #pe-spinner {
        height: 3;
    }
    #pe-loading-info {
        height: auto;
        content-align: center middle;
        color: $text-muted;
        margin: 1 0 0 0;
    }
    #pe-scroll {
        height: 1fr;
    }
    .pe-section-header {
        height: 1;
        content-align: left middle;
        background: #2A2A2A;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    .pe-table {
        height: auto;
        max-height: 40%;
        background: $panel;
    }
    .pe-usage-bar {
        height: auto;
        padding: 0 1;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory],
                 player: PlayerStats | None = None) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._player = player
        self._store = RosterDataStore()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="pe-header")
        yield Static("", id="pe-player-info")
        with Vertical(id="pe-loading-container"):
            yield LoadingIndicator(id="pe-spinner")
            yield Static("Loading player data...", id="pe-loading-status")
            yield Static(
                "Roster data is synced daily from Yahoo and cached locally.\n"
                "The first search may take a few minutes while all roster\n"
                "history is downloaded. Subsequent searches load instantly.",
                id="pe-loading-info",
            )
        yield VerticalScroll(id="pe-scroll")
        yield WrappingFooter()

    def on_mount(self) -> None:
        header = self.query_one("#pe-header", Static)
        header.update(f" {self.league.name} — Player Explorer ")
        if self._player:
            self._start_load()
        else:
            self.app.push_screen(
                PlayerSearchModal(self.api, self.league),
                callback=self._on_player_selected,
            )

    def _on_player_selected(self, player: PlayerStats | None) -> None:
        if player is None:
            self.app.pop_screen()
            return
        self._player = player
        self._start_load()

    def _start_load(self) -> None:
        p = self._player
        assert p is not None
        info = self.query_one("#pe-player-info", Static)
        info_text = Text()
        info_text.append(f"\n {p.name}", style="bold")
        info_text.append(f"  {p.position}", style="dim")
        info_text.append(f"  {p.team_abbr}\n", style="dim")
        info.update(info_text)
        self.run_worker(self._load_player_data)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    async def _show_loading(self, msg: str) -> None:
        try:
            self.query_one("#pe-loading-status", Static).update(msg)
            self.query_one("#pe-loading-container").display = True
            self.query_one("#pe-scroll").display = False
        except Exception:
            pass

    def _hide_loading(self) -> None:
        try:
            self.query_one("#pe-loading-container").display = False
            self.query_one("#pe-scroll").display = True
        except Exception:
            pass

    async def _load_player_data(self) -> None:
        p = self._player
        assert p is not None

        # 1. Sync roster data (fetches from Yahoo and caches in SQLite)
        def _progress(msg: str) -> None:
            # Can't await from sync callback, so just update directly
            try:
                self.call_from_thread(self._show_loading, msg)
            except Exception:
                pass

        await self._show_loading("Syncing roster data from Yahoo...")
        synced = await asyncio.to_thread(
            self._store.sync_all_days,
            self.api, self.league,
            progress_callback=_progress,
        )
        if synced > 0:
            await self._show_loading(f"Synced {synced} day(s) of roster data.")

        # 2. Query the cache for this player
        await self._show_loading("Analyzing player data...")

        total_days = self._store.get_total_days(self.league.league_key)

        stints = self._store.get_player_stints(
            self.league.league_key, p.player_key,
        )
        usage = self._store.get_player_usage_summary(
            self.league.league_key, p.player_key, total_days,
        )
        timeline = self._store.get_player_timeline(
            self.league.league_key, p.player_key,
        )

        # 3. Render all sections
        scroll = self.query_one("#pe-scroll", VerticalScroll)
        await scroll.remove_children()

        # --- Usage Summary ---
        label1 = Static(
            " Season Usage Summary with Performance",
            classes="pe-section-header",
        )
        usage_widget = Static("", classes="pe-usage-bar")
        await scroll.mount(label1, usage_widget)
        self._render_usage_summary(usage_widget, usage, total_days)

        # --- Roster Breakdown by Team ---
        label2 = Static(
            " Roster Breakdown by Team",
            classes="pe-section-header",
        )
        table2 = DataTable(classes="pe-table")
        await scroll.mount(label2, table2)
        self._render_roster_breakdown(table2, stints)

        # --- Season Timeline ---
        label3 = Static(
            " Season Timeline",
            classes="pe-section-header",
        )
        timeline_widget = Static("", classes="pe-usage-bar")
        await scroll.mount(label3, timeline_widget)
        self._render_timeline(timeline_widget, timeline)

        self._hide_loading()

    def _render_usage_summary(
        self, widget: Static, usage: dict, total_days: int,
    ) -> None:
        """Render the usage summary with per-category stat breakdowns."""
        scored = [c for c in self.categories if not c.is_only_display]
        bat_cats = [c for c in scored if c.position_type == "B"]

        sections = [
            ("STARTED", usage["started"], "bold green"),
            ("BENCHED", usage["benched"], "bold yellow"),
            ("IL / NA", usage["il"], "bold magenta"),
            ("NOT OWNED", usage["not_owned"], "dim"),
        ]

        # Compute rate stats for each section
        for _, data, _ in sections:
            stats = data.get("stats", {})
            if stats:
                _compute_rates(stats)

        text = Text()
        text.append("\n")

        # Header line: percentage + label + days for each section
        col_width = 28
        for label, data, style in sections:
            days = data["days"]
            pct = round(days / max(1, total_days) * 100)
            cell = f"  {pct}% {label}"
            text.append(cell.ljust(col_width), style=style)
        text.append("\n")

        # Sub-header: days count
        for label, data, style in sections:
            days = data["days"]
            cell = f"  {days} of {total_days} total days"
            text.append(cell.ljust(col_width), style="dim")
        text.append("\n\n")

        # Stat rows: one row per category, columns side by side
        for cat in bat_cats:
            for label, data, style in sections:
                stats = data.get("stats", {})
                val = stats.get(cat.stat_id, "-")
                if not val:
                    val = "-"
                cell = f"  {cat.display_name:8s} {val:>8s}"
                text.append(cell.ljust(col_width))
            text.append("\n")

        text.append("\n")
        widget.update(text)

    def _render_roster_breakdown(
        self, table: DataTable, stints: list[dict],
    ) -> None:
        """Render per-team stats breakdown table."""
        scored = [c for c in self.categories if not c.is_only_display]
        bat_cats = [c for c in scored if c.position_type == "B"]

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        cols = ["Team".ljust(24), "Status", "Dates", "Days"]
        for cat in bat_cats:
            cols.append(cat.display_name)
        table.add_columns(*cols)

        active_positions = {
            "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF",
            "Util", "DH", "SP", "RP", "P",
        }
        il_positions = {"IL", "IL+", "DL", "NA"}

        grand_total_stats: dict[str, str] = {}
        grand_total_days = 0

        for stint in stints:
            days = stint["days"]
            team_name = stint["team_name"]
            if not days:
                continue

            # Compute date range
            first_date = days[0].get("date", "")
            last_date = days[-1].get("date", "")
            from datetime import datetime
            try:
                ds = datetime.strptime(first_date, "%Y-%m-%d").strftime("%b %d")
                de = datetime.strptime(last_date, "%Y-%m-%d").strftime("%b %d")
                date_range = f"{ds} - {de}"
            except (ValueError, TypeError):
                date_range = ""

            # Aggregate stats: total, started, benched
            total_stats: dict[str, str] = {}
            started_stats: dict[str, str] = {}
            benched_stats: dict[str, str] = {}
            started_days = 0
            benched_days = 0

            for dd in days:
                sel_pos = dd.get("selected_position", "BN")
                stats = dd.get("stats", {})

                if sel_pos in active_positions:
                    started_days += 1
                    _acc(started_stats, stats)
                elif sel_pos == "BN":
                    benched_days += 1
                    _acc(benched_stats, stats)
                _acc(total_stats, stats)

            # Compute rate stats
            _compute_rates(total_stats)
            _compute_rates(started_stats)
            _compute_rates(benched_stats)

            num_days = len(days)
            grand_total_days += num_days
            _acc(grand_total_stats, total_stats)

            # Team total row
            row: list[Text] = [
                Text(team_name[:24].ljust(24), style="bold"),
                Text("Total", style="bold"),
                Text(date_range, style="dim"),
                Text(str(num_days), justify="right"),
            ]
            for cat in bat_cats:
                row.append(Text(total_stats.get(cat.stat_id, "-"),
                                justify="right"))
            table.add_row(*row)

            # Started sub-row
            if started_days > 0:
                row_s: list[Text] = [
                    Text(""),
                    Text("  Started", style="dim"),
                    Text(""),
                    Text(str(started_days), justify="right", style="dim"),
                ]
                for cat in bat_cats:
                    row_s.append(Text(
                        started_stats.get(cat.stat_id, "-"),
                        justify="right", style="dim",
                    ))
                table.add_row(*row_s)

            # Benched sub-row
            if benched_days > 0:
                row_b: list[Text] = [
                    Text(""),
                    Text("  Benched", style="dim"),
                    Text(""),
                    Text(str(benched_days), justify="right", style="dim"),
                ]
                for cat in bat_cats:
                    row_b.append(Text(
                        benched_stats.get(cat.stat_id, "-"),
                        justify="right", style="dim",
                    ))
                table.add_row(*row_b)

        # Grand total row
        _compute_rates(grand_total_stats)
        if stints:
            row_t: list[Text] = [
                Text("TOT".ljust(24), style="bold"),
                Text("Total", style="bold"),
                Text(""),
                Text(str(grand_total_days), justify="right", style="bold"),
            ]
            for cat in bat_cats:
                row_t.append(Text(
                    grand_total_stats.get(cat.stat_id, "-"),
                    justify="right", style="bold",
                ))
            table.add_row(*row_t)

    def _render_timeline(
        self, widget: Static, timeline: list[dict],
    ) -> None:
        """Render daily season timeline colored by fantasy team ownership.

        One block per calendar day, colored by the fantasy team that owns
        the player. Each day has its own status and stats from the daily
        roster snapshot. Shows stats and usage summary per month.
        """
        from datetime import datetime, timedelta

        # Assign a unique color to each fantasy team
        team_colors = [
            "bright_cyan", "bright_yellow", "bright_green", "bright_magenta",
            "bright_blue", "dark_orange", "medium_purple", "deep_pink",
            "dodger_blue", "gold1", "spring_green", "coral",
            "turquoise2", "orchid", "chartreuse3", "salmon",
            "steel_blue", "khaki1",
        ]
        team_color_map: dict[str, str] = {}
        color_idx = 0

        # Build day-by-day lookup from daily timeline entries
        day_data: dict[str, dict] = {}

        for entry in timeline:
            date_str = entry.get("date", "")
            if not date_str:
                continue
            status = entry.get("status", "not_owned")
            team_name = entry.get("team_name", "")

            if team_name and team_name not in team_color_map:
                team_color_map[team_name] = team_colors[color_idx % len(team_colors)]
                color_idx += 1

            day_data[date_str] = entry

        if not day_data:
            widget.update(Text("  No timeline data available.\n", style="dim"))
            return

        scored = [c for c in self.categories
                  if not c.is_only_display and c.position_type == "B"]

        all_dates = sorted(day_data.keys())
        first_date = datetime.strptime(all_dates[0], "%Y-%m-%d")
        last_date = datetime.strptime(all_dates[-1], "%Y-%m-%d")

        text = Text()
        text.append("\n")

        # Iterate month by month
        import calendar
        current = first_date.replace(day=1)
        while current <= last_date:
            month_name = current.strftime("%B")
            days_in_month = calendar.monthrange(current.year, current.month)[1]

            text.append(f"  {month_name:12s}", style="bold")

            month_stats: dict[str, str] = {}
            started_count = 0
            benched_count = 0
            il_count = 0
            not_owned_count = 0

            for day in range(1, days_in_month + 1):
                d = current.replace(day=day)
                ds = d.strftime("%Y-%m-%d")

                if d > last_date:
                    text.append("░░", style="dim")
                    continue

                info = day_data.get(ds)
                if not info or info["status"] == "not_owned":
                    text.append("░░", style="#555555")
                    not_owned_count += 1
                else:
                    team = info["team_name"]
                    color = team_color_map.get(team, "dim")
                    text.append("██", style=color)

                    status = info["status"]
                    if status == "started":
                        started_count += 1
                    elif status == "benched":
                        benched_count += 1
                    elif status == "il":
                        il_count += 1

                    # Accumulate daily stats directly
                    day_stats = info.get("stats", {})
                    if day_stats:
                        _acc(month_stats, day_stats)

            text.append("\n")

            # Stats line
            if month_stats:
                _compute_rates(month_stats)
                text.append("              ", style="dim")
                for cat in scored:
                    val = month_stats.get(cat.stat_id, "")
                    if val:
                        text.append(f"{cat.display_name}:{val} ", style="dim")
                # Usage summary
                parts = []
                if started_count:
                    parts.append(f"{started_count} started")
                if benched_count:
                    parts.append(f"{benched_count} benched")
                if il_count:
                    parts.append(f"{il_count} IL")
                if not_owned_count:
                    parts.append(f"{not_owned_count} not owned")
                if parts:
                    text.append("  " + " / ".join(parts), style="dim")
                text.append("\n")

            text.append("\n")

            # Advance to next month
            if current.month == 12:
                current = current.replace(year=current.year + 1, month=1)
            else:
                current = current.replace(month=current.month + 1)

        # Legend: show each team with its color
        text.append("  ")
        for team, color in team_color_map.items():
            text.append("██", style=color)
            text.append(f" {team}  ")
        text.append("░░", style="#555555")
        text.append(" Not Owned  ")
        text.append("░░", style="dim")
        text.append(" Future\n\n")

        widget.update(text)


def _acc(target: dict, source: dict) -> None:
    """Accumulate counting stats from source into target.

    Skips rate stats (3=AVG, 4=OBP) but tracks total bases for SLG
    by computing TB = SLG * AB from each source's raw values.
    """
    # Track total bases for SLG computation
    src_slg = str(source.get("5", ""))
    src_hab = str(source.get("60", "0/0"))
    if src_slg and src_slg not in ("-", "") and "/" in src_hab:
        try:
            src_ab = float(src_hab.split("/")[1])
            tb = float(src_slg) * src_ab
            target["_tb"] = str(float(target.get("_tb", 0)) + tb)
        except (ValueError, IndexError):
            pass

    for sid, val in source.items():
        if sid in ("3", "4", "5"):
            continue
        val = str(val)
        if "/" in val:
            existing = target.get(sid, "0/0")
            e_parts = str(existing).split("/")
            v_parts = val.split("/")
            try:
                target[sid] = f"{int(e_parts[0])+int(v_parts[0])}/{int(e_parts[1])+int(v_parts[1])}"
            except (ValueError, IndexError):
                pass
        else:
            try:
                target[sid] = str(int(target.get(sid, 0)) + int(val))
            except (ValueError, TypeError):
                pass


def _compute_rates(stats: dict) -> None:
    """Compute AVG, OBP, SLG from accumulated counting stats in-place."""
    hab = stats.get("60", "0/0")
    h, ab = 0.0, 0.0
    if "/" in str(hab):
        parts = str(hab).split("/")
        try:
            h = float(parts[0])
            ab = float(parts[1])
        except (ValueError, IndexError):
            pass

    # AVG (stat 3) = H / AB
    stats["3"] = f"{h / ab:.3f}" if ab > 0 else ".000"

    # OBP (stat 4) = (H + BB + HBP) / (AB + BB + HBP + SF)
    bb = float(stats.get("18", 0))
    hbp = float(stats.get("19", 0))
    sf = float(stats.get("20", 0))
    obp_denom = ab + bb + hbp + sf
    stats["4"] = f"{(h + bb + hbp) / obp_denom:.3f}" if obp_denom > 0 else ".000"

    # SLG (stat 5) = Total Bases / AB
    tb = float(stats.get("_tb", 0))
    stats["5"] = f"{tb / ab:.3f}" if ab > 0 else ".000"


# --- Player Detail Screen ---


class PlayerDetailScreen(Screen):
    """Multi-year player stats: tables + per-stat bar charts on one page."""

    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("1", "charts_traditional", "Traditional Charts"),
        ("2", "charts_statcast", "Statcast Charts"),
    ]
    CSS = """
    #pd-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #pd-info {
        height: 2;
        padding: 0 1;
        background: $surface;
    }
    #pd-controls {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #pd-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #pd-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #pd-spinner {
        height: 3;
    }
    #pd-scroll {
        height: 1fr;
    }
    .pd-table {
        height: auto;
        background: $panel;
    }
    .pd-section-label {
        height: 1;
        padding: 0 1;
        color: $accent;
        text-style: bold;
    }
    .pd-chart-row {
        height: 12;
    }
    .pd-chart {
        width: 1fr;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    # 8 chart slots arranged in 2 rows of 4
    _CHART_COUNT = 8

    # Map fantasy stat display names → chart attribute names
    _DISPLAY_TO_ATTR: dict[str, str] = {
        "HR": "hr", "RBI": "rbi", "R": "runs", "SB": "sb",
        "AVG": "avg", "OBP": "obp", "SLG": "slg", "OPS": "ops",
        "BB": "bb", "SO": "so", "H": "hits",
        "W": "wins", "SV": "saves", "K": "so", "IP": "ip",
        "ERA": "era", "WHIP": "whip",
    }
    # Rate stats whose team value is already an average (don't divide by roster)
    _RATE_DISPLAY_NAMES = {"AVG", "OBP", "SLG", "OPS", "ERA", "WHIP", "K/BB"}

    def __init__(
        self,
        player_name: str,
        player_position: str,
        player_team: str,
        categories: list[StatCategory] | None = None,
        all_teams: list[TeamStats] | None = None,
        replacement_by_pos: dict[str, list[PlayerStats]] | None = None,
    ) -> None:
        super().__init__()
        self._player_name = player_name
        self._player_position = player_position
        self._player_team = player_team
        self._categories = categories or []
        self._all_teams = all_teams or []
        self._replacement_by_pos = replacement_by_pos or {}
        self._mlbam_id: int | None = None
        self._is_batter = self._check_is_batter()
        self._chart_mode = "traditional"  # or "statcast"
        # Data storage
        self._batting_stats: dict = {}
        self._pitching_stats: dict = {}
        self._statcast_data: dict = {}
        self._years: list[int] = []
        # Reference lines
        self._mlb_avg: dict[str, float] = {}        # MLB-wide average (statcast)
        self._league_avg: dict[str, float] = {}      # fantasy league rostered avg
        self._repl_avg: dict[str, float] = {}        # replacement level (free agents)

    def _check_is_batter(self) -> bool:
        batting = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "Util", "DH"}
        return any(p.strip() in batting for p in self._player_position.split(","))

    def _compute_fantasy_league_avg(self) -> dict[str, float]:
        """Per-player average across rostered fantasy league teams."""
        if not self._all_teams or not self._categories:
            return {}
        num_teams = len(self._all_teams)
        roster_size = 14 if self._is_batter else 9
        pos_type = "B" if self._is_batter else "P"
        result: dict[str, float] = {}
        for cat in self._categories:
            if cat.is_only_display or cat.position_type != pos_type:
                continue
            attr = self._DISPLAY_TO_ATTR.get(cat.display_name)
            if attr is None:
                continue
            values: list[float] = []
            for team in self._all_teams:
                try:
                    values.append(float(team.stats.get(cat.stat_id, "0")))
                except (ValueError, TypeError):
                    continue
            if not values:
                continue
            total = sum(values)
            if cat.display_name in self._RATE_DISPLAY_NAMES:
                result[attr] = total / len(values)
            else:
                result[attr] = total / (num_teams * roster_size)
        return result

    def _compute_replacement_avg(self) -> dict[str, float]:
        """Per-stat average of top free agents (replacement level)."""
        if not self._replacement_by_pos or not self._categories:
            return {}
        pos_type = "B" if self._is_batter else "P"
        batting_pos = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF"}
        pitching_pos = {"SP", "RP"}
        target_pos = batting_pos if self._is_batter else pitching_pos

        # Collect all replacement players for matching positions
        repl_players: list[PlayerStats] = []
        for pos, players in self._replacement_by_pos.items():
            if pos in target_pos:
                repl_players.extend(players[:5])  # top 5 per position

        if not repl_players:
            return {}

        result: dict[str, float] = {}
        for cat in self._categories:
            if cat.is_only_display or cat.position_type != pos_type:
                continue
            attr = self._DISPLAY_TO_ATTR.get(cat.display_name)
            if attr is None:
                continue
            values: list[float] = []
            for p in repl_players:
                try:
                    values.append(float(p.stats.get(cat.stat_id, "0")))
                except (ValueError, TypeError):
                    continue
            if values:
                result[attr] = sum(values) / len(values)
        return result

    def compose(self) -> ComposeResult:
        from textual_plotext import PlotextPlot
        yield Header()
        yield Static("", id="pd-header")
        yield Static("", id="pd-info")
        yield Static("", id="pd-controls")
        with Vertical(id="pd-loading-container"):
            yield LoadingIndicator(id="pd-spinner")
            yield Static("Loading player data...", id="pd-loading-status")
        with VerticalScroll(id="pd-scroll"):
            yield Static(" Traditional Stats", classes="pd-section-label")
            yield DataTable(id="pd-trad-table", classes="pd-table")
            yield Static(" Statcast", classes="pd-section-label")
            yield DataTable(id="pd-sc-table", classes="pd-table")
            yield Static("", id="pd-chart-section-label", classes="pd-section-label")
            with Horizontal(classes="pd-chart-row"):
                for i in range(4):
                    yield PlotextPlot(id=f"pd-chart-{i}", classes="pd-chart")
            with Horizontal(classes="pd-chart-row"):
                for i in range(4, 8):
                    yield PlotextPlot(id=f"pd-chart-{i}", classes="pd-chart")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#pd-header", Static).update(
            " Player Detail \u2014 3-Year View "
        )
        info = Text()
        info.append(f"\n {self._player_name}", style="bold #E8A735")
        info.append(f"  {self._player_position}", style="dim")
        info.append(f"  {self._player_team}", style="bold")
        info.append(
            f"  ({'Batter' if self._is_batter else 'Pitcher'})\n", style="dim",
        )
        self.query_one("#pd-info", Static).update(info)
        self._update_controls()
        self.query_one("#pd-scroll").display = False
        self.run_worker(self._load_data, group="pd-load", exclusive=True)

    def _update_controls(self) -> None:
        t = Text()
        for key, label, mode in [
            ("1", "Traditional Charts", "traditional"),
            ("2", "Statcast Charts", "statcast"),
        ]:
            t.append("  ")
            if self._chart_mode == mode:
                t.append(f" {key} ", style="bold on #4A7C59")
                t.append(f" {label} ", style="bold #E8A735")
            else:
                t.append(f" {key} ", style="dim")
                t.append(f" {label} ", style="dim")
        self.query_one("#pd-controls", Static).update(t)

    async def _load_data(self) -> None:
        """Progressive load: current year first, then backfill prior years."""
        from datetime import date
        from gkl.statcast import (
            get_batter_statcast_multi_year, get_pitcher_statcast_multi_year,
            get_statcast_league_averages, lookup_mlbam_id,
        )
        from gkl.mlb_api import get_player_batting_stats, get_player_pitching_stats

        try:
            self.query_one("#pd-loading-container").display = True
            self.query_one("#pd-scroll").display = False
        except Exception:
            pass

        current_year = date.today().year
        self._years = [current_year - 2, current_year - 1, current_year]

        try:
            self.query_one("#pd-loading-status", Static).update(
                "Looking up player..."
            )
        except Exception:
            pass
        self._mlbam_id = await asyncio.to_thread(lookup_mlbam_id, self._player_name)

        if self._mlbam_id is None:
            try:
                self.query_one("#pd-loading-status", Static).update(
                    f"Could not find MLBAM ID for {self._player_name}"
                )
                self.query_one("#pd-spinner").display = False
            except Exception:
                pass
            return

        # --- Phase 1: current year (fast — statcast likely already cached) ---
        try:
            self.query_one("#pd-loading-status", Static).update(
                f"Loading {current_year} stats..."
            )
        except Exception:
            pass

        if self._is_batter:
            self._batting_stats = await asyncio.to_thread(
                get_player_batting_stats, self._mlbam_id, [current_year],
            )
            self._statcast_data = await asyncio.to_thread(
                get_batter_statcast_multi_year, self._mlbam_id, [current_year],
            )
        else:
            self._pitching_stats = await asyncio.to_thread(
                get_player_pitching_stats, self._mlbam_id, [current_year],
            )
            self._statcast_data = await asyncio.to_thread(
                get_pitcher_statcast_multi_year, self._mlbam_id, [current_year],
            )

        # Compute reference lines (fantasy league data is instant, MLB avg
        # uses current-year statcast cache that's already loaded)
        self._league_avg = self._compute_fantasy_league_avg()
        self._repl_avg = self._compute_replacement_avg()
        p_type = "batter" if self._is_batter else "pitcher"
        self._mlb_avg = await asyncio.to_thread(
            get_statcast_league_averages, [current_year], p_type,
        )

        # Show what we have so far
        try:
            self.query_one("#pd-loading-container").display = False
            self.query_one("#pd-scroll").display = True
        except Exception:
            pass
        self._render_tables()
        self._render_charts()

        # --- Phase 2: backfill prior years in background ---
        prior_years = [current_year - 1, current_year - 2]
        self.run_worker(
            self._backfill_years(prior_years),
            group="pd-backfill", exclusive=True,
        )

    async def _backfill_years(self, years: list[int]) -> None:
        """Load prior-year data and re-render as each year arrives."""
        from gkl.statcast import (
            get_batter_statcast_multi_year, get_pitcher_statcast_multi_year,
            get_statcast_league_averages,
        )
        from gkl.mlb_api import get_player_batting_stats, get_player_pitching_stats

        if self._mlbam_id is None:
            return

        for year in years:
            # Traditional stats (fast — single API call per year)
            if self._is_batter:
                result = await asyncio.to_thread(
                    get_player_batting_stats, self._mlbam_id, [year],
                )
                self._batting_stats.update(result)
            else:
                result = await asyncio.to_thread(
                    get_player_pitching_stats, self._mlbam_id, [year],
                )
                self._pitching_stats.update(result)

            # Statcast (slower — may download full leaderboard)
            if self._is_batter:
                sc = await asyncio.to_thread(
                    get_batter_statcast_multi_year, self._mlbam_id, [year],
                )
            else:
                sc = await asyncio.to_thread(
                    get_pitcher_statcast_multi_year, self._mlbam_id, [year],
                )
            self._statcast_data.update(sc)

            # Re-render with new data
            self._render_tables()
            self._render_charts()

        # Update MLB avg now that all years are loaded
        p_type = "batter" if self._is_batter else "pitcher"
        self._mlb_avg = await asyncio.to_thread(
            get_statcast_league_averages, self._years, p_type,
        )
        self._render_charts()

    # ---- tables (always visible) ----

    def _render_tables(self) -> None:
        self._render_traditional()
        self._render_statcast()

    def _render_traditional(self) -> None:
        table = self.query_one("#pd-trad-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        if self._is_batter:
            cols = ["Season", "G", "PA", "AB", "H", "HR", "RBI", "R",
                    "SB", "BB", "SO", "AVG", "OBP", "SLG", "OPS"]
            table.add_columns(*cols)
            for year in self._years:
                s = self._batting_stats.get(year)
                if s is None:
                    row = [Text(str(year), style="bold")] + [
                        Text("\u2014", style="dim") for _ in range(len(cols) - 1)
                    ]
                else:
                    row = [
                        Text(str(year), style="bold"),
                        Text(str(s.games)), Text(str(s.pa)), Text(str(s.ab)),
                        Text(str(s.hits)), Text(str(s.hr), style="bold #E8A735"),
                        Text(str(s.rbi)), Text(str(s.runs)),
                        Text(str(s.sb)), Text(str(s.bb)), Text(str(s.so)),
                        Text(f"{s.avg:.3f}"), Text(f"{s.obp:.3f}"),
                        Text(f"{s.slg:.3f}"), Text(f"{s.ops:.3f}", style="bold"),
                    ]
                table.add_row(*row)
        else:
            cols = ["Season", "G", "GS", "IP", "W", "L", "SV", "HLD", "H",
                    "SO", "BB", "ERA", "WHIP", "K/9", "BB/9"]
            table.add_columns(*cols)
            for year in self._years:
                s = self._pitching_stats.get(year)
                if s is None:
                    row = [Text(str(year), style="bold")] + [
                        Text("\u2014", style="dim") for _ in range(len(cols) - 1)
                    ]
                else:
                    row = [
                        Text(str(year), style="bold"),
                        Text(str(s.games)), Text(str(s.games_started)),
                        Text(f"{s.ip:.1f}"),
                        Text(str(s.wins), style="bold #6AAF6E"),
                        Text(str(s.losses)), Text(str(s.saves)),
                        Text(str(s.holds)),
                        Text(str(s.hits)),
                        Text(str(s.so), style="bold #E8A735"),
                        Text(str(s.bb)),
                        Text(f"{s.era:.2f}"), Text(f"{s.whip:.2f}"),
                        Text(f"{s.k_per_9:.1f}"), Text(f"{s.bb_per_9:.1f}"),
                    ]
                table.add_row(*row)

    def _render_statcast(self) -> None:
        table = self.query_one("#pd-sc-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        def _fmt(val: float | None, decimals: int = 1, pct: bool = False) -> Text:
            if val is None:
                return Text("\u2014", style="dim")
            suffix = "%" if pct else ""
            return Text(f"{val:.{decimals}f}{suffix}")

        if self._is_batter:
            cols = ["Season", "PA", "EV", "MaxEV", "LA", "Barrel%",
                    "HardHit%", "K%", "BB%", "Whiff%", "xBA", "xSLG", "xwOBA"]
            table.add_columns(*cols)
            for year in self._years:
                sc = self._statcast_data.get(year)
                if sc is None:
                    row = [Text(str(year), style="bold")] + [
                        Text("\u2014", style="dim") for _ in range(len(cols) - 1)
                    ]
                else:
                    row = [
                        Text(str(year), style="bold"),
                        Text(str(sc.pa)),
                        _fmt(sc.avg_exit_velo), _fmt(sc.max_exit_velo),
                        _fmt(sc.avg_launch_angle),
                        _fmt(sc.barrel_pct, pct=True),
                        _fmt(sc.hard_hit_pct, pct=True),
                        _fmt(sc.k_pct, pct=True), _fmt(sc.bb_pct, pct=True),
                        _fmt(sc.whiff_pct, pct=True),
                        _fmt(sc.xba, 3), _fmt(sc.xslg, 3),
                        _fmt(sc.xwoba, 3),
                    ]
                table.add_row(*row)
        else:
            cols = ["Season", "PA", "EV", "Barrel%", "HardHit%",
                    "xBA", "xSLG", "xwOBA", "xERA",
                    "K%", "BB%", "Whiff%", "Chase%", "Velo"]
            table.add_columns(*cols)
            for year in self._years:
                sc = self._statcast_data.get(year)
                if sc is None:
                    row = [Text(str(year), style="bold")] + [
                        Text("\u2014", style="dim") for _ in range(len(cols) - 1)
                    ]
                else:
                    row = [
                        Text(str(year), style="bold"),
                        Text(str(sc.pa)),
                        _fmt(sc.avg_exit_velo),
                        _fmt(sc.barrel_pct, pct=True),
                        _fmt(sc.hard_hit_pct, pct=True),
                        _fmt(sc.xba, 3), _fmt(sc.xslg, 3),
                        _fmt(sc.xwoba, 3), _fmt(sc.xera, 2),
                        _fmt(sc.k_pct, pct=True), _fmt(sc.bb_pct, pct=True),
                        _fmt(sc.whiff_pct, pct=True),
                        _fmt(sc.chase_pct, pct=True),
                        _fmt(sc.avg_velo),
                    ]
                table.add_row(*row)

    # ---- charts ----

    # Per-chart color palettes — each chart gets a distinct hue so
    # adjacent charts are easy to tell apart at a glance.
    _CHART_PALETTES = [
        ["#E8A735", "#F0C060", "#D49020"],  # amber
        ["#5BA4CF", "#7BBCE0", "#3B8CBF"],  # sky blue
        ["#6AAF6E", "#8CCF8E", "#4A8F4E"],  # green
        ["#C75D5D", "#E07070", "#A04040"],   # red
        ["#B48CC8", "#CCA8E0", "#9470B0"],   # purple
        ["#D4A84B", "#E8C870", "#C09030"],   # gold
        ["#5BC0BE", "#80D8D6", "#3AA09E"],   # teal
        ["#E07B60", "#F09880", "#C06040"],   # coral
    ]

    def _get_chart_specs(self) -> list[tuple[str, str]]:
        """Return (title, attr_or_key) tuples for the current chart mode.

        Each spec produces one small bar chart with 3 bars (one per year).
        Traditional specs pull from _batting_stats / _pitching_stats.
        Statcast specs pull from _statcast_data.
        """
        if self._chart_mode == "traditional":
            if self._is_batter:
                return [
                    ("HR", "hr"), ("RBI", "rbi"),
                    ("R", "runs"), ("SB", "sb"),
                    ("AVG", "avg"), ("OBP", "obp"),
                    ("SLG", "slg"), ("OPS", "ops"),
                ]
            else:
                return [
                    ("W", "wins"), ("SV", "saves"),
                    ("SO", "so"), ("IP", "ip"),
                    ("ERA", "era"), ("WHIP", "whip"),
                    ("K/9", "k_per_9"), ("BB/9", "bb_per_9"),
                ]
        else:  # statcast
            if self._is_batter:
                return [
                    ("EV", "avg_exit_velo"), ("Barrel%", "barrel_pct"),
                    ("HardHit%", "hard_hit_pct"), ("K%", "k_pct"),
                    ("BB%", "bb_pct"), ("Whiff%", "whiff_pct"),
                    ("xBA", "xba"), ("xwOBA", "xwoba"),
                ]
            else:
                return [
                    ("EV", "avg_exit_velo"), ("Barrel%", "barrel_pct"),
                    ("K%", "k_pct"), ("Whiff%", "whiff_pct"),
                    ("xERA", "xera"), ("xwOBA", "xwoba"),
                    ("Chase%", "chase_pct"), ("Velo", "avg_velo"),
                ]

    def _get_stat_value(self, year: int, attr: str) -> float:
        """Pull a single stat value for a year from the right data source."""
        if self._chart_mode == "traditional":
            src = self._batting_stats if self._is_batter else self._pitching_stats
            entry = src.get(year)
        else:
            entry = self._statcast_data.get(year)
        if entry is None:
            return 0.0
        val = getattr(entry, attr, None)
        return float(val) if val is not None else 0.0

    def _render_charts(self) -> None:
        from textual_plotext import PlotextPlot

        specs = self._get_chart_specs()
        year_labels = [str(y) for y in self._years]

        if self._chart_mode == "traditional":
            legend = " Traditional \u2014 Year over Year  (\u2500 League Avg  \u2500 Repl Level)"
        else:
            legend = " Statcast \u2014 Year over Year  (\u2500 MLB Avg)"
        self.query_one("#pd-chart-section-label", Static).update(legend)

        for i in range(self._CHART_COUNT):
            pw = self.query_one(f"#pd-chart-{i}", PlotextPlot)
            plt = pw.plt
            plt.clear_data()
            plt.clear_figure()

            if i < len(specs):
                title, attr = specs[i]
                palette = self._CHART_PALETTES[i % len(self._CHART_PALETTES)]
                values = [self._get_stat_value(y, attr) for y in self._years]
                plt.bar(
                    year_labels,
                    values,
                    color=palette,
                    width=0.6,
                )

                # Collect reference line values so we can set ylim properly
                ref_lines: list[tuple[float, tuple[int, int, int]]] = []
                if self._chart_mode == "traditional":
                    lg = self._league_avg.get(attr)
                    if lg and lg > 0:
                        ref_lines.append((lg, (232, 167, 53)))   # gold
                    rp = self._repl_avg.get(attr)
                    if rp and rp > 0:
                        ref_lines.append((rp, (160, 80, 80)))    # dim red
                else:
                    mlb = self._mlb_avg.get(attr)
                    if mlb and mlb > 0:
                        ref_lines.append((mlb, (180, 180, 180))) # gray

                if ref_lines:
                    max_ref = max(v for v, _ in ref_lines)
                    ceiling = max(max(values, default=0), max_ref) * 1.15
                    plt.ylim(0, ceiling)
                    for val, color in ref_lines:
                        plt.hline(val, color=color)

                plt.title(title)
                plt.theme("dark")
                pw.display = True
            else:
                pw.display = False
            pw.refresh()

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_charts_traditional(self) -> None:
        if not self._years:
            return
        self._chart_mode = "traditional"
        self._update_controls()
        self._render_charts()

    def action_charts_statcast(self) -> None:
        if not self._years:
            return
        self._chart_mode = "statcast"
        self._update_controls()
        self._render_charts()


# --- Transactions Screen ---


class TransactionsScreen(PlayerCompareMixin, Screen):
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("c", "compare", "Compare"),
        ("i", "player_detail", "Player Detail"),
    ]
    CSS = """
    #tx-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #tx-loading-container {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #tx-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #tx-spinner {
        height: 3;
    }
    #tx-scroll {
        height: 1fr;
    }
    .tx-section-header {
        height: 1;
        content-align: left middle;
        background: #2A2A2A;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    .tx-table {
        height: auto;
        max-height: 40%;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._transactions: list[Transaction] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="tx-header")
        with Vertical(id="tx-loading-container"):
            yield LoadingIndicator(id="tx-spinner")
            yield Static("Loading transactions...", id="tx-loading-status")
        yield VerticalScroll(id="tx-scroll")
        yield WrappingFooter()

    def on_mount(self) -> None:
        header = self.query_one("#tx-header", Static)
        header.update(f" {self.league.name} — League Transactions ")
        self.run_worker(self._load)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_player_detail(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            table = focused.first()
        except Exception:
            return
        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return
        p = players[row_idx]
        cache = self.app.shared_cache
        self.app.push_screen(PlayerDetailScreen(
            p.name, p.position, p.team_abbr,
            categories=self.categories,
            all_teams=cache.all_teams if cache.is_loaded else None,
            replacement_by_pos=cache.replacement_by_pos if cache.is_loaded else None,
        ))

    async def _show_loading(self, msg: str) -> None:
        try:
            self.query_one("#tx-loading-status", Static).update(msg)
            container = self.query_one("#tx-loading-container")
            container.display = True
            scroll = self.query_one("#tx-scroll")
            scroll.display = False
        except Exception:
            pass

    def _hide_loading(self) -> None:
        try:
            container = self.query_one("#tx-loading-container")
            container.display = False
            scroll = self.query_one("#tx-scroll")
            scroll.display = True
        except Exception:
            pass

    async def _load(self) -> None:
        await self._show_loading("Fetching league transactions...")
        self._transactions = self.api.get_transactions(
            self.league.league_key, count=100,
        )

        scroll = self.query_one("#tx-scroll", VerticalScroll)
        await scroll.remove_children()

        await self._show_loading("Rendering transactions...")

        # --- Section 1: 10 Most Recent Transactions ---
        label = Static(" 10 Most Recent Transactions", classes="tx-section-header")
        table = DataTable(classes="tx-table")
        await scroll.mount(label, table)
        self._render_recent_transactions(table)

        # --- Section 2: Top 10 Most-Added Players ---
        label2 = Static(" Top 10 Most-Added Players", classes="tx-section-header")
        table2 = DataTable(classes="tx-table")
        await scroll.mount(label2, table2)
        self._render_most_added(table2)

        # --- Section 3: Adds by Position per Team ---
        label3 = Static(" Adds by Position per Team", classes="tx-section-header")
        table3 = DataTable(classes="tx-table")
        await scroll.mount(label3, table3)
        self._render_position_adds(table3)

        self._hide_loading()

    def _render_recent_transactions(self, table: DataTable) -> None:
        """Show the 10 most recent transactions."""
        from datetime import datetime

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Date", "Type", "Player".ljust(20), "Pos".ljust(15),
                          "Team", "Action", "From", "To")

        table._players = []
        recent = self._transactions[:10]
        for tx in recent:
            ts = datetime.fromtimestamp(tx.timestamp).strftime("%m/%d %H:%M")
            for p in tx.players:
                table._players.append(p)
                table.add_row(
                    Text(ts, style="dim"),
                    Text(tx.type, style="bold"),
                    Text(p.name[:20].ljust(20)),
                    Text(p.position.ljust(15), style="dim"),
                    Text(p.team_abbr, style="dim"),
                    Text(p.action, style="bold green" if p.action == "add" else
                         "bold red" if p.action == "drop" else ""),
                    Text(p.from_team[:20] if p.from_team else "-", style="dim"),
                    Text(p.to_team[:20] if p.to_team else "-", style="dim"),
                )

    def _render_most_added(self, table: DataTable) -> None:
        """Show top 10 players added the most times, with all teams they appeared on."""
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Player".ljust(20), "Pos".ljust(15), "Team",
                          "Adds", "Fantasy Teams")

        # Count adds per player and track which fantasy teams
        add_counts: dict[str, int] = {}
        add_teams: dict[str, set[str]] = {}
        player_info: dict[str, tuple[str, str, str]] = {}  # key -> (name, pos, team)

        for tx in self._transactions:
            for p in tx.players:
                if p.action == "add" and p.to_team:
                    add_counts[p.player_key] = add_counts.get(p.player_key, 0) + 1
                    if p.player_key not in add_teams:
                        add_teams[p.player_key] = set()
                    add_teams[p.player_key].add(p.to_team)
                    player_info[p.player_key] = (p.name, p.position, p.team_abbr)

        top_added = sorted(add_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        table._players = []
        for pkey, count in top_added:
            name, pos, team = player_info[pkey]
            teams = ", ".join(sorted(add_teams[pkey]))
            table._players.append(TransactionPlayer(
                player_key=pkey, name=name, position=pos, team_abbr=team,
                action="", from_team="", to_team="",
            ))
            table.add_row(
                Text(name[:20].ljust(20), style="bold"),
                Text(pos.ljust(15), style="dim"),
                Text(team, style="dim"),
                Text(str(count), justify="right"),
                Text(teams, style="dim"),
            )

    def _render_position_adds(self, table: DataTable) -> None:
        """Show per-team count of adds at each league position."""
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        # Get scored positions from league categories
        bat_positions = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "Util"]
        pitch_positions = ["SP", "RP"]
        all_positions = bat_positions + pitch_positions

        # Count adds per team per position
        team_pos_adds: dict[str, dict[str, int]] = {}
        team_totals: dict[str, int] = {}

        for tx in self._transactions:
            for p in tx.players:
                if p.action == "add" and p.to_team:
                    team = p.to_team
                    if team not in team_pos_adds:
                        team_pos_adds[team] = {}
                        team_totals[team] = 0
                    team_totals[team] += 1
                    # Count at each eligible position (total only counts once)
                    player_positions = [pos.strip() for pos in p.position.split(",")]
                    matched = False
                    for pos in player_positions:
                        if pos in all_positions:
                            team_pos_adds[team][pos] = team_pos_adds[team].get(pos, 0) + 1
                            matched = True
                    if not matched and player_positions:
                        pp = player_positions[0]
                        team_pos_adds[team][pp] = team_pos_adds[team].get(pp, 0) + 1

        cols = ["Team".ljust(20)] + all_positions + ["Total"]
        table.add_columns(*cols)

        # Sort teams by total adds descending
        sorted_teams = sorted(team_pos_adds.keys(),
                              key=lambda t: team_totals.get(t, 0), reverse=True)

        for team in sorted_teams:
            pos_counts = team_pos_adds[team]
            total = team_totals[team]
            row: list[Text] = [Text(team[:20].ljust(20), style="bold")]
            for pos in all_positions:
                cnt = pos_counts.get(pos, 0)
                row.append(Text(str(cnt) if cnt else "-", justify="right",
                                style="" if cnt else "dim"))
            row.append(Text(str(total), justify="right", style="bold"))
            table.add_row(*row)


# --- Settings Screen ---


class SettingsScreen(Screen):
    """Global application settings."""
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back")]
    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-container {
        width: 60;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #settings-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #settings-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
        margin-bottom: 1;
    }
    #settings-list {
        height: auto;
        max-height: 20;
    }
    #settings-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #settings-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League) -> None:
        super().__init__()
        self._api = api
        self._league = league

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-container"):
            yield Static("Settings", id="settings-title")
            yield Static("\\[esc] to go back", id="settings-controls")
            yield ListView(id="settings-list")

    def on_mount(self) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        lv = self.query_one("#settings-list", ListView)
        lv.clear()
        current = self.app.store.get_pref("my_team_key")
        current_name = self.app.store.get_pref("my_team_name")
        label = Text()
        label.append("My Fantasy Team: ", style="bold")
        if current and current_name:
            label.append(current_name, style="")
        else:
            label.append("Not set", style="dim")
        item = ListItem(Label(label))
        item._action = "my_team"
        lv.mount(item)
        lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        action = getattr(event.item, "_action", None)
        if action == "my_team":
            self.app.push_screen(
                FantasyTeamPickerScreen(self._api, self._league),
                callback=self._on_team_picked,
            )

    def _on_team_picked(self, result: tuple[str, str] | str) -> None:
        if result == "__clear__":
            self.app.store.set_pref("my_team_key", "")
            self.app.store.set_pref("my_team_name", "")
            self.notify("My Team cleared")
        elif isinstance(result, tuple):
            team_key, team_name = result
            self.app.store.set_pref("my_team_key", team_key)
            self.app.store.set_pref("my_team_name", team_name)
            self.notify(f"My Team set to {team_name}")
        self._refresh_list()

    def action_go_back(self) -> None:
        self.app.pop_screen()


class FantasyTeamPickerScreen(Screen):
    """Pick your fantasy team from the league."""
    BINDINGS = [("escape", "quit", "Cancel"), ("q", "quit", "Cancel")]
    CSS = """
    FantasyTeamPickerScreen {
        align: center middle;
    }
    #team-picker-container {
        width: 60;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #team-picker-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #team-picker-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
        margin-bottom: 1;
    }
    #team-picker-list {
        height: 20;
    }
    #team-picker-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #team-picker-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League) -> None:
        super().__init__()
        self._api = api
        self._league = league

    def compose(self) -> ComposeResult:
        with Vertical(id="team-picker-container"):
            yield Static("Select Your Fantasy Team", id="team-picker-title")
            yield Static("\\[esc] to cancel", id="team-picker-controls")
            yield ListView(id="team-picker-list")

    def on_mount(self) -> None:
        self.run_worker(self._load_teams)

    async def _load_teams(self) -> None:
        teams = self._api.get_team_season_stats(self._league.league_key)
        lv = self.query_one("#team-picker-list", ListView)
        current = self.app.store.get_pref("my_team_key")
        # Clear option
        clear_item = ListItem(Label(Text("(None — clear selection)", style="dim italic")))
        clear_item._team_data = "__clear__"
        await lv.mount(clear_item)
        for team in teams:
            label = Text()
            label.append(f"{team.name}", style="bold")
            label.append(f"  ({team.manager})", style="dim")
            if team.team_key == current:
                label.append("  ★", style="bold #FFD700")
            item = ListItem(Label(label))
            item._team_data = (team.team_key, team.name)
            await lv.mount(item)
        lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        data = getattr(event.item, "_team_data", None)
        if data is not None:
            self.dismiss(data)

    def action_quit(self) -> None:
        self.dismiss("")


# --- MLB Scoreboard Screen ---


class GameCard(Vertical, can_focus=True):
    """A focusable game card widget that stores its MLBGame."""

    def __init__(self, game: MLBGame, card_class: str, card_id: str) -> None:
        super().__init__(classes=card_class, id=card_id)
        self.game = game


class MLBScoreboardScreen(Screen):
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("r", "refresh", "Refresh"),
                ("comma", "prev_day", "< Prev Day"),
                ("full_stop", "next_day", "> Next Day"),
                ("t", "today", "Today"),
                ("m", "mlbtv", "MLB.TV"),
                ("enter", "open_boxscore", "Box Score")]
    CSS = """
    #mlb-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #mlb-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #mlb-loading {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }
    #mlb-games {
        height: 1fr;
        background: $background;
    }
    #game-list {
        height: 1fr;
    }
    #game-list > ListItem {
        height: auto;
        padding: 0;
    }
    #game-list > ListItem.--highlight {
        background: transparent;
    }
    .game-row {
        height: auto;
        width: 100%;
    }
    .game-card {
        height: auto;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: $surface;
        border: solid $primary-lighten-3;
    }
    .game-card-live {
        height: auto;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: #1E2E1E;
        border: solid #4A7C59;
    }
    .game-card-final {
        height: auto;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: #252525;
        border: solid #444444;
    }
    .game-card-roster {
        height: auto;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: $surface;
        border: solid #FFD700;
    }
    .game-card-live-roster {
        height: auto;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: #1E2E1E;
        border: solid #FFD700;
    }
    .game-card-final-roster {
        height: auto;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: #252525;
        border: solid #FFD700;
    }
    .game-card:focus {
        border: solid #FFD700;
    }
    .game-card-live:focus {
        border: solid #FFD700;
    }
    .game-card-final:focus {
        border: solid #FFD700;
    }
    .game-card-roster:focus {
        border: solid #FFFFFF;
    }
    .game-card-live-roster:focus {
        border: solid #FFFFFF;
    }
    .game-card-final-roster:focus {
        border: solid #FFFFFF;
    }
    .game-line {
        height: 1;
        width: 100%;
    }
    .game-status {
        height: 1;
        width: 100%;
        color: $text-muted;
    }
    .linescore-line {
        height: 1;
        width: 100%;
        color: $text-muted;
    }
    .roster-players-line {
        height: 1;
        width: 100%;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League) -> None:
        super().__init__()
        from datetime import date as date_cls
        self._date = date_cls.today()
        self._api = api
        self._league = league
        self._games: list[MLBGame] = []
        self._categories: list = []
        self._roster_mlb_teams: set[str] = set()  # MLB team abbrs on user's roster
        self._roster_players_by_team: dict[str, list[str]] = {}  # MLB abbr -> [player names]
        self._collapsed_games: set[str] = set()
        self._refresh_timer = None
        self._game_index = 0  # currently focused game index

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("MLB Scoreboard", id="mlb-header")
        yield Static("", id="mlb-controls")
        yield Static("Loading...", id="mlb-loading")
        yield VerticalScroll(id="mlb-games")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self._load_roster_teams()
        try:
            self._categories = self._api.get_stat_categories(self._league.league_key)
        except Exception:
            self._categories = []
        self.query_one("#mlb-games").display = False
        self._update_controls()
        self.run_worker(self._load)

    def on_unmount(self) -> None:
        if self._refresh_timer:
            self._refresh_timer.stop()
            self._refresh_timer = None

    def _load_roster_teams(self) -> None:
        """Load the user's fantasy roster and map players to MLB teams."""
        team_key = self.app.store.get_pref("my_team_key")
        if not team_key:
            self._roster_mlb_teams = set()
            self._roster_players_by_team = {}
            return
        try:
            players = self._api.get_roster_stats(
                team_key, self._league.current_week
            )
            by_team: dict[str, list[str]] = {}
            for p in players:
                if p.team_abbr:
                    by_team.setdefault(p.team_abbr, []).append(p.name)
            self._roster_mlb_teams = set(by_team.keys())
            self._roster_players_by_team = by_team
        except Exception:
            self._roster_mlb_teams = set()
            self._roster_players_by_team = {}

    def _update_controls(self) -> None:
        from datetime import date as date_cls
        today = date_cls.today()
        ctrl = Text()
        ctrl.append(f"{self._date.strftime('%A, %B %d, %Y')}", style="bold")
        if self._date == today:
            ctrl.append("  (today)", style="dim")
        ctrl.append("  |  <,> change day  [t] today  [r] refresh", style="dim")
        if self._refresh_timer:
            ctrl.append("  [auto-refreshing]", style="dim #4A7C59")
        self.query_one("#mlb-controls", Static).update(ctrl)

    def _has_roster_players(self, game: MLBGame) -> bool:
        return bool(
            self._roster_mlb_teams
            and (game.away_abbr in self._roster_mlb_teams
                 or game.home_abbr in self._roster_mlb_teams)
        )

    def _get_roster_players_in_game(self, game: MLBGame) -> list[str]:
        """Get names of fantasy roster players in this game."""
        names = []
        for abbr in (game.away_abbr, game.home_abbr):
            names.extend(self._roster_players_by_team.get(abbr, []))
        return names

    async def _load(self) -> None:
        games = get_mlb_scoreboard(self._date)
        self._games = games

        loading = self.query("#mlb-loading")
        if loading:
            loading.first().remove()

        container = self.query_one("#mlb-games", VerticalScroll)
        container.display = True
        await container.remove_children()

        if not games:
            await container.mount(Static("  No games scheduled.", classes="game-line"))
            self._manage_refresh_timer(games)
            return

        # Sort: roster games first within each status group, then live > preview > final
        order = {"Live": 0, "Preview": 1, "Final": 2}
        games.sort(key=lambda g: (
            order.get(g.status, 1),
            0 if self._has_roster_players(g) else 1,
        ))

        # Batch into rows of 4
        cards_per_row = 4
        first_card = None
        for i in range(0, len(games), cards_per_row):
            row = Horizontal(classes="game-row")
            await container.mount(row)
            for game in games[i:i + cards_per_row]:
                card = GameCard(
                    game, self._card_class(game),
                    card_id=f"game-{game.gamePk}",
                )
                if first_card is None:
                    first_card = card
                await row.mount(card)
                await card.mount(Static(self._format_status(game), classes="game-status"))
                await card.mount(Static(self._format_away(game), classes="game-line"))
                await card.mount(Static(self._format_home(game), classes="game-line"))
                # Show roster players in this game
                roster_names = self._get_roster_players_in_game(game)
                if roster_names:
                    names_text = Text()
                    names_text.append(" ★ ", style="#FFD700")
                    names_text.append(", ".join(roster_names), style="dim italic")
                    await card.mount(Static(names_text, classes="roster-players-line"))
                # Show linescore for live/final by default, or if explicitly expanded
                # Show linescore for Live/Final unless user collapsed it
                show_linescore = (
                    game.innings
                    and game.status in ("Live", "Final")
                    and game.gamePk not in self._collapsed_games
                )
                if show_linescore:
                    await self._mount_linescore(card, game)

        if first_card is not None:
            first_card.focus()
        self._manage_refresh_timer(games)

    def _manage_refresh_timer(self, games: list[MLBGame]) -> None:
        has_live = any(g.status == "Live" for g in games)
        if has_live and self._refresh_timer is None:
            self._refresh_timer = self.set_interval(45, self._auto_refresh)
            self._update_controls()
        elif not has_live and self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None
            self._update_controls()

    def _auto_refresh(self) -> None:
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    async def _mount_linescore(self, card: Vertical, game: MLBGame) -> None:
        num_innings = max(len(game.innings), 9)
        # Header row: inning numbers + R H E
        hdr = Text()
        hdr.append("     ", style="dim")  # team abbr padding
        for n in range(1, num_innings + 1):
            hdr.append(f"{n:>3}", style="dim bold")
        hdr.append("   R  H  E", style="dim bold")
        await card.mount(Static(hdr, classes="linescore-line"))

        # Away row
        away_line = Text()
        away_line.append(f" {game.away_abbr:<4}", style="bold" if game.away_score > game.home_score else "")
        for n in range(num_innings):
            if n < len(game.innings) and game.innings[n][0] is not None:
                away_line.append(f"{game.innings[n][0]:>3}")
            else:
                away_line.append("  -", style="dim")
        away_line.append(f"  {game.away_score:>2} {game.away_hits:>2} {game.away_errors:>2}")
        await card.mount(Static(away_line, classes="linescore-line"))

        # Home row
        home_line = Text()
        home_line.append(f" {game.home_abbr:<4}", style="bold" if game.home_score > game.away_score else "")
        for n in range(num_innings):
            if n < len(game.innings) and game.innings[n][1] is not None:
                home_line.append(f"{game.innings[n][1]:>3}")
            else:
                home_line.append("  -", style="dim")
        home_line.append(f"  {game.home_score:>2} {game.home_hits:>2} {game.home_errors:>2}")
        await card.mount(Static(home_line, classes="linescore-line"))

    def _get_cards(self) -> list[GameCard]:
        return list(self.query(GameCard))

    def _focused_card_index(self) -> int | None:
        cards = self._get_cards()
        focused = self.focused
        if focused in cards:
            return cards.index(focused)
        return None

    def key_left(self) -> None:
        cards = self._get_cards()
        idx = self._focused_card_index()
        if idx is not None and idx > 0:
            cards[idx - 1].focus()
        elif idx is None and cards:
            cards[0].focus()

    def key_right(self) -> None:
        cards = self._get_cards()
        idx = self._focused_card_index()
        if idx is not None and idx < len(cards) - 1:
            cards[idx + 1].focus()
        elif idx is None and cards:
            cards[0].focus()

    def key_up(self) -> None:
        cards = self._get_cards()
        idx = self._focused_card_index()
        if idx is not None and idx >= 4:
            cards[idx - 4].focus()
        elif idx is None and cards:
            cards[0].focus()

    def key_down(self) -> None:
        cards = self._get_cards()
        idx = self._focused_card_index()
        if idx is not None and idx + 4 < len(cards):
            cards[idx + 4].focus()
        elif idx is None and cards:
            cards[0].focus()

    def on_click(self, event) -> None:
        """Open box score when a game card is clicked."""
        try:
            widget, _ = self.screen.get_widget_at(event.screen_x, event.screen_y)
        except Exception:
            return
        while widget is not None and widget is not self:
            if isinstance(widget, GameCard):
                self.app.push_screen(
                    BoxScoreScreen(widget.game, self._roster_players_by_team, self._categories)
                )
                return
            widget = widget.parent

    def action_open_boxscore(self) -> None:
        """Open box score for the focused game card."""
        focused = self.focused
        if isinstance(focused, GameCard):
            self.app.push_screen(
                BoxScoreScreen(focused.game, self._roster_players_by_team, self._categories)
            )
            return
        if not self._games:
            return
        # Fallback: first live game, then first final, then first game
        target = None
        for g in self._games:
            if g.status == "Live":
                target = g
                break
        if target is None:
            for g in self._games:
                if g.status == "Final":
                    target = g
                    break
        if target is None:
            target = self._games[0]
        self.app.push_screen(BoxScoreScreen(target, self._roster_players_by_team, self._categories))

    def _card_class(self, game: MLBGame) -> str:
        has_roster = self._has_roster_players(game)
        if game.status == "Live":
            return "game-card-live-roster" if has_roster else "game-card-live"
        elif game.status == "Final":
            return "game-card-final-roster" if has_roster else "game-card-final"
        return "game-card-roster" if has_roster else "game-card"

    def _format_away(self, game: MLBGame) -> Text:
        line = Text()
        winning = game.away_score > game.home_score
        has_roster = game.away_abbr in self._roster_mlb_teams
        style = "bold" if winning else ""
        if has_roster:
            line.append("★ ", style="#FFD700")
            line.append(f"{game.away_abbr:<4}", style=style)
        else:
            line.append(f" {game.away_abbr:<4}", style=style)
        if game.status != "Preview":
            line.append(f" {game.away_score:>2}", style=style)
        return line

    def _format_home(self, game: MLBGame) -> Text:
        line = Text()
        winning = game.home_score > game.away_score
        has_roster = game.home_abbr in self._roster_mlb_teams
        style = "bold" if winning else ""
        if has_roster:
            line.append("★ ", style="#FFD700")
            line.append(f"{game.home_abbr:<4}", style=style)
        else:
            line.append(f" {game.home_abbr:<4}", style=style)
        if game.status != "Preview":
            line.append(f" {game.home_score:>2}", style=style)
            if game.status == "Live":
                r1, r2, r3 = game.runners
                d = f" {'◆' if r3 else '◇'}{'◆' if r2 else '◇'}{'◆' if r1 else '◇'} {game.outs}o"
                line.append(d, style="dim")
        else:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(game.start_time.replace("Z", "+00:00"))
                local = dt.astimezone()
                line.append(f" {local.strftime('%-I:%M %p')}", style="dim")
            except (ValueError, TypeError):
                pass
        return line

    def _format_status(self, game: MLBGame) -> Text:
        status = Text()
        if game.status == "Live":
            status.append(" LIVE ", style="bold on #4A7C59")
            status.append(f"  {game.inning_half} {game.inning_ordinal}")
        elif game.status == "Final":
            status.append(" FINAL ", style="bold on #444444")
            if game.inning > 9:
                status.append(f"  ({game.inning})")
        else:
            status.append(f" {game.detail_status}", style="dim")
        return status

    @staticmethod
    def _format_start_time(utc_iso: str) -> str:
        """Convert UTC ISO time to local time display."""
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
            local = dt.astimezone()
            return local.strftime("%-I:%M %p")
        except (ValueError, TypeError):
            return ""

    def action_refresh(self) -> None:
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    def action_prev_day(self) -> None:
        from datetime import timedelta
        self._date -= timedelta(days=1)
        self._collapsed_games.clear()
        self._update_controls()
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    def action_next_day(self) -> None:
        from datetime import timedelta
        self._date += timedelta(days=1)
        self._collapsed_games.clear()
        self._update_controls()
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    def action_today(self) -> None:
        from datetime import date as date_cls
        self._date = date_cls.today()
        self._collapsed_games.clear()
        self._update_controls()
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_mlbtv(self) -> None:
        if self._games:
            self._show_mlbtv_picker(self._games)
        else:
            self.notify("No games available", severity="information")

    def _show_mlbtv_picker(self, games: list[MLBGame]) -> None:
        def on_game_selected(gamePk: str) -> None:
            if gamePk != "":
                webbrowser.open("https://www.mlb.com/tv/g" + gamePk)

        self.app.push_screen(
            MlbtvSelectScreen(games), callback=on_game_selected
        )


# --- Box Score Screen ---


# Mapping from Yahoo stat display names to MLB boxscore API field names.
# Batting: (game_field, season_field, is_rate, width)
_BATTING_STAT_MAP: dict[str, tuple[str, str, bool, int]] = {
    "AB":  ("atBats", "atBats", False, 4),
    "R":   ("runs", "runs", False, 4),
    "H":   ("hits", "hits", False, 4),
    "HR":  ("homeRuns", "homeRuns", False, 4),
    "RBI": ("rbi", "rbi", False, 4),
    "SB":  ("stolenBases", "stolenBases", False, 4),
    "BB":  ("baseOnBalls", "baseOnBalls", False, 4),
    "K":   ("strikeOuts", "strikeOuts", False, 4),
    "HBP": ("hitByPitch", "hitByPitch", False, 4),
    "SF":  ("sacFlies", "sacFlies", False, 4),
    "2B":  ("doubles", "doubles", False, 4),
    "3B":  ("triples", "triples", False, 4),
    "TB":  ("totalBases", "totalBases", False, 4),
    "CS":  ("caughtStealing", "caughtStealing", False, 4),
    "AVG": ("", "avg", True, 6),
    "OBP": ("", "obp", True, 6),
    "SLG": ("", "slg", True, 6),
    "OPS": ("", "ops", True, 6),
    "PA":  ("plateAppearances", "plateAppearances", False, 4),
    "LOB": ("leftOnBase", "leftOnBase", False, 4),
    "GIDP": ("groundIntoDoublePlay", "groundIntoDoublePlay", False, 5),
    "H/AB": ("", "", False, 0),  # special: skip in box score
    "G":   ("gamesPlayed", "gamesPlayed", False, 0),  # skip game-level
}

# Pitching: (game_field, season_field, is_rate, width)
_PITCHING_STAT_MAP: dict[str, tuple[str, str, bool, int]] = {
    "IP":   ("inningsPitched", "inningsPitched", False, 5),
    "W":    ("wins", "wins", False, 3),
    "L":    ("losses", "losses", False, 3),
    "SV":   ("saves", "saves", False, 3),
    "S":    ("saves", "saves", False, 3),
    "HLD":  ("holds", "holds", False, 4),
    "BS":   ("blownSaves", "blownSaves", False, 3),
    "H":    ("hits", "hits", False, 4),
    "HA":   ("hits", "hits", False, 4),
    "R":    ("runs", "runs", False, 4),
    "ER":   ("earnedRuns", "earnedRuns", False, 4),
    "HR":   ("homeRuns", "homeRuns", False, 4),
    "BB":   ("baseOnBalls", "baseOnBalls", False, 4),
    "K":    ("strikeOuts", "strikeOuts", False, 4),
    "SO":   ("strikeOuts", "strikeOuts", False, 4),
    "HBP":  ("hitBatsmen", "hitBatsmen", False, 4),
    "WP":   ("wildPitches", "wildPitches", False, 3),
    "BK":   ("balks", "balks", False, 3),
    "ERA":  ("", "era", True, 6),
    "WHIP": ("", "whip", True, 6),
    "K/9":  ("", "strikeoutsPer9Inn", True, 5),
    "BB/9": ("", "walksPer9Inn", True, 5),
    "K/BB": ("", "strikeoutWalkRatio", True, 5),
    "QS":   ("", "", False, 0),  # not in boxscore
    "P":    ("pitchesThrown", "pitchesThrown", False, 4),
    "G":    ("gamesPlayed", "gamesPlayed", False, 0),  # skip game-level
    "GS":   ("gamesStarted", "gamesStarted", False, 0),
}


def _get_box_stat(
    stats: dict, season_stats: dict,
    game_field: str, season_field: str, is_rate: bool,
) -> str:
    """Extract a stat value from boxscore data."""
    if is_rate:
        # Use season stats for rate stats
        val = season_stats.get(season_field, "")
        if val and val != ".---" and val != "-.--":
            return str(val)
        return "-"
    if game_field:
        val = stats.get(game_field, 0)
        if isinstance(val, str):
            return val
        return str(val)
    return "-"


class BoxScoreScreen(Screen):
    """Full box score for a single game."""
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("r", "refresh", "Refresh")]
    CSS = """
    #box-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #box-subheader {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #box-loading {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }
    #box-content {
        height: 1fr;
        background: $background;
    }
    .box-section-label {
        height: 1;
        content-align: left middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
        padding: 0 1;
        margin-top: 1;
    }
    .box-table-header {
        height: 1;
        background: #2A2A2A;
        padding: 0 1;
    }
    .box-player-row {
        height: 1;
        padding: 0 1;
    }
    .box-player-row-highlight {
        height: 1;
        padding: 0 1;
        background: #2E3A1E;
    }
    .box-player-row-current {
        height: 1;
        padding: 0 1;
        background: #1E2E1E;
        text-style: bold;
    }
    .box-totals-row {
        height: 1;
        padding: 0 1;
        background: #2A2A2A;
        text-style: bold;
    }
    .box-linescore-line {
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(
        self, game: MLBGame,
        roster_players_by_team: dict[str, list[str]] | None = None,
        categories: list | None = None,
    ) -> None:
        super().__init__()
        self._game = game
        self._roster_names: set[str] = set()
        if roster_players_by_team:
            for abbr in (game.away_abbr, game.home_abbr):
                for name in roster_players_by_team.get(abbr, []):
                    self._roster_names.add(name)
        self._categories = categories or []
        self._boxscore: BoxScore | None = None

        # Build column lists from league categories
        self._bat_cols = self._build_batting_cols()
        self._pitch_cols = self._build_pitching_cols()

    def _build_batting_cols(self) -> list[tuple[str, str, str, bool, int]]:
        """Build batting columns: (display_name, game_field, season_field, is_rate, width)."""
        cols: list[tuple[str, str, str, bool, int]] = []
        seen: set[str] = set()
        # Always show AB first
        cols.append(("AB", "atBats", "atBats", False, 4))
        seen.add("AB")
        # Add league scoring categories
        for cat in self._categories:
            if cat.position_type != "B":
                continue
            name = cat.display_name
            if name in seen:
                continue
            mapping = _BATTING_STAT_MAP.get(name)
            if mapping and mapping[3] > 0:  # has width (not skipped)
                cols.append((name, mapping[0], mapping[1], mapping[2], mapping[3]))
                seen.add(name)
        # If no categories, use defaults
        if len(cols) <= 1:
            for name in ("R", "H", "HR", "RBI", "SB", "BB", "K", "AVG", "OBP", "SLG"):
                mapping = _BATTING_STAT_MAP.get(name)
                if mapping and name not in seen:
                    cols.append((name, mapping[0], mapping[1], mapping[2], mapping[3]))
        return cols

    def _build_pitching_cols(self) -> list[tuple[str, str, str, bool, int]]:
        """Build pitching columns."""
        cols: list[tuple[str, str, str, bool, int]] = []
        seen: set[str] = set()
        # Always show IP first
        cols.append(("IP", "inningsPitched", "inningsPitched", False, 5))
        seen.add("IP")
        # Add league scoring categories
        for cat in self._categories:
            if cat.position_type != "P":
                continue
            name = cat.display_name
            if name in seen:
                continue
            mapping = _PITCHING_STAT_MAP.get(name)
            if mapping and mapping[3] > 0:
                cols.append((name, mapping[0], mapping[1], mapping[2], mapping[3]))
                seen.add(name)
        # Always add pitches at the end
        if "P" not in seen:
            cols.append(("P", "pitchesThrown", "pitchesThrown", False, 4))
        # If no categories, use defaults
        if len(cols) <= 2:
            for name in ("H", "R", "ER", "BB", "K", "HR", "ERA", "WHIP"):
                mapping = _PITCHING_STAT_MAP.get(name)
                if mapping and name not in seen:
                    cols.append((name, mapping[0], mapping[1], mapping[2], mapping[3]))
        return cols

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="box-header")
        yield Static("", id="box-subheader")
        yield Static("Loading box score...", id="box-loading")
        yield VerticalScroll(id="box-content")
        yield WrappingFooter()

    def on_mount(self) -> None:
        g = self._game
        title = Text()
        title.append(f" {g.away_team} @ {g.home_team} ", style="bold")
        self.query_one("#box-header", Static).update(title)

        sub = Text()
        if g.status == "Live":
            sub.append(" LIVE ", style="bold on #4A7C59")
            sub.append(f"  {g.inning_half} {g.inning_ordinal}")
        elif g.status == "Final":
            sub.append(" FINAL ", style="bold on #444444")
            if g.inning > 9:
                sub.append(f"  ({g.inning})")
        sub.append(f"    {g.away_abbr} {g.away_score} - {g.home_abbr} {g.home_score}",
                   style="bold")
        sub.append("    [r] refresh  [esc] back", style="dim")
        self.query_one("#box-subheader", Static).update(sub)

        self.query_one("#box-content").display = False
        self.run_worker(self._load)

    async def _load(self) -> None:
        try:
            boxscore = get_mlb_boxscore(self._game.gamePk)
        except Exception as e:
            loading = self.query("#box-loading")
            if loading:
                loading.first().update(f"Failed to load box score: {e}")
            return

        self._boxscore = boxscore

        loading = self.query("#box-loading")
        if loading:
            loading.first().remove()

        container = self.query_one("#box-content", VerticalScroll)
        container.display = True
        await container.remove_children()

        g = self._game

        # Linescore
        if g.innings:
            await container.mount(Static(
                Text(" Line Score", style="bold"), classes="box-section-label"
            ))
            num_innings = max(len(g.innings), 9)
            hdr = Text()
            hdr.append(f"{'':>20s}")
            for n in range(1, num_innings + 1):
                hdr.append(f"{n:>3}", style="bold")
            hdr.append("   R  H  E", style="bold")
            await container.mount(Static(hdr, classes="box-table-header"))

            away_line = Text()
            away_line.append(f" {g.away_team[:18]:<19s}")
            for n in range(num_innings):
                if n < len(g.innings) and g.innings[n][0] is not None:
                    away_line.append(f"{g.innings[n][0]:>3}")
                else:
                    away_line.append("  -", style="dim")
            away_line.append(f"  {g.away_score:>2} {g.away_hits:>2} {g.away_errors:>2}")
            await container.mount(Static(away_line, classes="box-linescore-line"))

            home_line = Text()
            home_line.append(f" {g.home_team[:18]:<19s}")
            for n in range(num_innings):
                if n < len(g.innings) and g.innings[n][1] is not None:
                    home_line.append(f"{g.innings[n][1]:>3}")
                else:
                    home_line.append("  -", style="dim")
            home_line.append(f"  {g.home_score:>2} {g.home_hits:>2} {g.home_errors:>2}")
            await container.mount(Static(home_line, classes="box-linescore-line"))

        # Batting tables
        for team in (boxscore.away, boxscore.home):
            await self._render_batting(container, team)

        # Pitching tables
        for team in (boxscore.away, boxscore.home):
            await self._render_pitching(container, team)

    async def _render_batting(self, container, team: BoxScoreTeam) -> None:
        await container.mount(Static(
            Text(f" {team.name} — Batting", style="bold"),
            classes="box-section-label",
        ))

        hdr = Text()
        hdr.append(f" {'Batter':<24s}", style="bold")
        hdr.append(f"{'Pos':>4s}", style="bold")
        for name, _, _, is_rate, width in self._bat_cols:
            hdr.append(f"{name:>{width}s}", style="bold")
        await container.mount(Static(hdr, classes="box-table-header"))

        # Track totals for counting stats
        totals: dict[str, int] = {}
        for b in team.batters:
            is_roster = b.name in self._roster_names
            row = Text()
            name_style = "#FFD700 bold" if is_roster else ("bold" if b.is_current else "")
            prefix = "★ " if is_roster else ("► " if b.is_current else "  ")
            row.append(f"{prefix}{b.name[:22]:<22s}", style=name_style)
            row.append(f"{b.position:>4s}", style="dim")
            for col_name, game_field, season_field, is_rate, width in self._bat_cols:
                val = _get_box_stat(b.stats, b.season_stats, game_field, season_field, is_rate)
                style = "dim" if is_rate else ""
                row.append(f"{val:>{width}s}", style=style)
                if not is_rate and game_field:
                    try:
                        totals[col_name] = totals.get(col_name, 0) + int(val)
                    except (ValueError, TypeError):
                        pass
            css_cls = "box-player-row-current" if b.is_current else (
                "box-player-row-highlight" if is_roster else "box-player-row"
            )
            await container.mount(Static(row, classes=css_cls))

        # Totals row
        totals_row = Text()
        totals_row.append(f"  {'Totals':<22s}")
        totals_row.append(f"{'':>4s}")
        for col_name, game_field, _, is_rate, width in self._bat_cols:
            if is_rate or not game_field:
                totals_row.append(f"{'':>{width}s}")
            else:
                totals_row.append(f"{totals.get(col_name, 0):>{width}d}")
        await container.mount(Static(totals_row, classes="box-totals-row"))

    async def _render_pitching(self, container, team: BoxScoreTeam) -> None:
        await container.mount(Static(
            Text(f" {team.name} — Pitching", style="bold"),
            classes="box-section-label",
        ))

        hdr = Text()
        hdr.append(f" {'Pitcher':<24s}", style="bold")
        for name, _, _, is_rate, width in self._pitch_cols:
            hdr.append(f"{name:>{width}s}", style="bold")
        await container.mount(Static(hdr, classes="box-table-header"))

        for p in team.pitchers:
            is_roster = p.name in self._roster_names
            row = Text()
            name_style = "#FFD700 bold" if is_roster else ("bold" if p.is_current else "")
            prefix = "★ " if is_roster else ("► " if p.is_current else "  ")
            name_display = p.name[:20]
            if p.decision:
                name_display += f" ({p.decision})"
            row.append(f"{prefix}{name_display:<22s}", style=name_style)
            for col_name, game_field, season_field, is_rate, width in self._pitch_cols:
                val = _get_box_stat(p.stats, p.season_stats, game_field, season_field, is_rate)
                style = "dim" if is_rate else ""
                row.append(f"{val:>{width}s}", style=style)
            css_cls = "box-player-row-current" if p.is_current else (
                "box-player-row-highlight" if is_roster else "box-player-row"
            )
            await container.mount(Static(row, classes=css_cls))

    def action_refresh(self) -> None:
        self.run_worker(self._load, group="box-load", exclusive=True)

    def action_go_back(self) -> None:
        self.app.pop_screen()


# --- MLB.TV Selection Screen ---


class MlbtvSelectScreen(Screen):
    """Game picker for opening an MLB.TV stream."""
    BINDINGS = [("escape", "quit", "Quit"), ("q", "quit", "Quit")]
    CSS = """
    MlbtvSelectScreen {
        align: center middle;
    }
    #mlbtv-select-container {
        width: 60;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #mlbtv-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #mlbtv-controls {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
        margin-bottom: 1;
    }
    #mlbtv-select-list {
        height: 15;
        max-height: 85%;
    }
    #mlbtv-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #mlbtv-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, games: list[MLBGame]) -> None:
        super().__init__()
        self.games = games

    def compose(self) -> ComposeResult:
        with Vertical(id="mlbtv-select-container"):
            yield Static("Select game to view MLB.TV Stream", id="mlbtv-select-title")
            yield Static("\\[esc] or \\[q] to go back", id="mlbtv-controls")
            yield ListView(id="mlbtv-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#mlbtv-select-list", ListView)
        for game in self.games:
            label = Text()
            label.append(f"{game.away_team} {game.away_score}", style="bold")
            label.append(" @ ", style="dim")
            label.append(f"{game.home_team} {game.home_score}", style="bold")
            if game.status == "Preview":
                label.append(" " + game.detail_status, style="dim")
            elif game.status == "Final":
                label.append(" Final", style="dim")
            else:
                label.append(" " + game.inning_half + " " + game.inning_ordinal, style="bold")
            item = ListItem(Label(label))
            item._gamePk = game.gamePk
            lv.mount(item)
        lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        gamePk = getattr(event.item, "_gamePk", None)
        if gamePk:
            self.dismiss(gamePk)

    def action_quit(self) -> None:
        self.dismiss("")

# --- Ask Skipper (AI Chat) ---


class ApiKeyModal(Screen):
    """Modal for entering an Anthropic API key."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    ApiKeyModal {
        align: center middle;
    }
    #apikey-container {
        width: 70;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #apikey-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
        margin-bottom: 1;
    }
    #apikey-help {
        height: auto;
        color: $text-muted;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="apikey-container"):
            yield Static("Anthropic API Key Required", id="apikey-title")
            yield Static(
                "Ask Skipper uses Claude to answer questions about your league.\n"
                "Enter your Anthropic API key below (starts with sk-ant-).\n"
                "Get one at https://console.anthropic.com/settings/keys",
                id="apikey-help",
            )
            yield Input(
                placeholder="sk-ant-...",
                id="apikey-input",
                password=True,
            )

    def on_mount(self) -> None:
        self.query_one("#apikey-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        key = event.value.strip()
        if key:
            save_anthropic_key(key)
            self.dismiss(key)
        else:
            self.notify("API key cannot be empty.", severity="error")

    def action_cancel(self) -> None:
        self.dismiss(None)


class ModelSelectModal(Screen):
    """Modal for selecting a Claude model."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    ModelSelectModal {
        align: center middle;
    }
    #model-select-container {
        width: 40;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #model-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #model-select-list {
        height: auto;
        max-height: 70%;
    }
    #model-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #model-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, current_model_id: str) -> None:
        super().__init__()
        self._current = current_model_id

    def compose(self) -> ComposeResult:
        with Vertical(id="model-select-container"):
            yield Static("Select Model", id="model-select-title")
            yield ListView(id="model-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#model-select-list", ListView)
        for model_id, label in AVAILABLE_MODELS:
            prefix = "● " if model_id == self._current else "  "
            item = ListItem(Label(prefix + label))
            item._model_id = model_id
            lv.mount(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        model_id = getattr(event.item, "_model_id", None)
        if model_id:
            self.dismiss(model_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskSkipperScreen(Screen):
    """Chat screen for asking Skipper questions about your fantasy league."""
    BINDINGS = [("escape", "go_back", "Back"),
                Binding("f2", "select_model", "Model (F2)", priority=True)]
    CSS = """
    #skipper-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #skipper-model-bar {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #skipper-messages {
        height: 1fr;
        padding: 0 1;
    }
    .skipper-user-msg {
        margin: 1 0 0 0;
        color: #E8A735;
    }
    .skipper-assistant-msg {
        margin: 0 0 1 0;
        color: $foreground;
    }
    .skipper-error-msg {
        margin: 0 0 1 0;
        color: $error;
    }
    #skipper-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
        display: none;
    }
    #skipper-input {
        dock: bottom;
    }
    """

    def __init__(
        self,
        api: YahooFantasyAPI,
        league: League,
        categories: list[StatCategory],
    ) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self.skipper: Skipper | None = None
        self._model_id = DEFAULT_MODEL

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(" Ask Skipper ", id="skipper-header")
        yield Static("", id="skipper-model-bar")
        yield VerticalScroll(id="skipper-messages")
        yield Static("Skipper is thinking...", id="skipper-status")
        yield Input(placeholder="Ask Skipper a question...", id="skipper-input")
        yield WrappingFooter()

    def _update_model_bar(self) -> None:
        label = next(
            (lbl for mid, lbl in AVAILABLE_MODELS if mid == self._model_id),
            self._model_id,
        )
        bar = Text()
        bar.append("Model: ", style="dim")
        bar.append(label, style="bold")
        bar.append("  |  F2 to change", style="dim")
        self.query_one("#skipper-model-bar", Static).update(bar)

    def action_select_model(self) -> None:
        self.app.push_screen(
            ModelSelectModal(self._model_id),
            callback=self._on_model_selected,
        )

    def _on_model_selected(self, model_id: str | None) -> None:
        if model_id is None or model_id == self._model_id:
            return
        self._model_id = model_id
        self._update_model_bar()
        if self.skipper:
            self.skipper.model = model_id
        label = next(
            (lbl for mid, lbl in AVAILABLE_MODELS if mid == model_id),
            model_id,
        )
        self.notify(f"Switched to {label}")
        self.query_one("#skipper-input", Input).focus()

    def on_mount(self) -> None:
        self._update_model_bar()
        key = load_anthropic_key()
        if key:
            self._init_skipper(key)
        else:
            self.app.push_screen(ApiKeyModal(), callback=self._on_api_key)
        self.query_one("#skipper-input", Input).focus()

    def _on_api_key(self, key: str | None) -> None:
        if key:
            self._init_skipper(key)
        else:
            self.app.pop_screen()

    def _init_skipper(self, key: str) -> None:
        try:
            self.skipper = Skipper(self.api, self.league, self.categories, model=self._model_id)
            messages = self.query_one("#skipper-messages", VerticalScroll)
            messages.mount(
                Static(
                    "Skipper here. Ask me anything about your league — "
                    "standings, matchups, rosters, free agents.",
                    classes="skipper-assistant-msg",
                )
            )
        except Exception as e:
            self.notify(f"Failed to initialize Skipper: {e}", severity="error")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or not self.skipper:
            return

        event.input.value = ""
        messages = self.query_one("#skipper-messages", VerticalScroll)
        messages.mount(Static(f"You: {text}", classes="skipper-user-msg"))

        # Show thinking indicator and disable input
        status = self.query_one("#skipper-status", Static)
        status.display = True
        event.input.disabled = True

        self.run_worker(self._get_response(text), group="skipper", exclusive=True)

    async def _get_response(self, text: str) -> None:
        messages = self.query_one("#skipper-messages", VerticalScroll)
        try:
            response = await self.skipper.chat(text)
            await messages.mount(
                Static(f"Skipper: {response}", classes="skipper-assistant-msg")
            )
        except Exception as e:
            await messages.mount(
                Static(f"Error: {e}", classes="skipper-error-msg")
            )

        # Hide thinking indicator and re-enable input
        status = self.query_one("#skipper-status", Static)
        status.display = False
        inp = self.query_one("#skipper-input", Input)
        inp.disabled = False
        inp.focus()
        messages.scroll_end(animate=False)

    def action_go_back(self) -> None:
        self.app.pop_screen()


# --- League Selection Screen ---


class LeagueSelectScreen(Screen):
    """Full-screen league picker shown when user has multiple leagues."""
    BINDINGS = [("escape", "quit", "Quit"), ("q", "quit", "Quit")]
    CSS = """
    LeagueSelectScreen {
        align: center middle;
    }
    #league-select-container {
        width: 60;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #league-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
        margin-bottom: 1;
    }
    #league-select-list {
        height: auto;
        max-height: 70%;
    }
    #league-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #league-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, leagues: list[League], last_league_key: str | None = None) -> None:
        super().__init__()
        self.leagues = leagues
        self.last_league_key = last_league_key

    def compose(self) -> ComposeResult:
        with Vertical(id="league-select-container"):
            yield Static("Select League", id="league-select-title")
            yield ListView(id="league-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#league-select-list", ListView)
        highlight_idx = 0
        for i, league in enumerate(self.leagues):
            label = Text()
            label.append(f"{league.name}", style="bold")
            label.append(f"  {league.season}  •  {league.num_teams} teams", style="dim")
            item = ListItem(Label(label))
            item._league = league
            lv.mount(item)
            if self.last_league_key and league.league_key == self.last_league_key:
                highlight_idx = i
        lv.index = highlight_idx

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        league = getattr(event.item, "_league", None)
        if league:
            self.dismiss(league)

    def action_quit(self) -> None:
        self.app.exit()


# --- Trade Analyzer Screen ---


class TradeModeSelectorModal(Screen):
    """Modal for selecting trade analyzer mode."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    TradeModeSelectorModal {
        align: center middle;
    }
    #trade-mode-container {
        width: 60;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #trade-mode-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #trade-mode-list {
        height: auto;
        max-height: 80%;
    }
    #trade-mode-list > ListItem {
        height: 3;
        padding: 0 1;
    }
    #trade-mode-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="trade-mode-container"):
            yield Static("Trade Analyzer Mode", id="trade-mode-title")
            yield ListView(id="trade-mode-list")

    def on_mount(self) -> None:
        lv = self.query_one("#trade-mode-list", ListView)
        modes = [
            ("analyze", "Analyze Trade\n  Select players from two rosters"),
            ("block", "Trading Block\n  Find targets for a player you want to trade"),
            ("discover", "Trade Discovery\n  Find trades to improve specific categories"),
        ]
        for mode_id, label in modes:
            item = ListItem(Label(label))
            item._mode = mode_id
            lv.mount(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        mode = getattr(event.item, "_mode", None)
        if mode:
            self.dismiss(mode)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CategorySelectModal(Screen):
    """Modal for selecting stat categories to improve (multi-select)."""
    BINDINGS = [("escape", "cancel", "Cancel"), ("d", "confirm", "Done")]
    CSS = """
    CategorySelectModal {
        align: center middle;
    }
    #cat-select-container {
        width: 50;
        height: auto;
        max-height: 80%;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #cat-select-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #cat-select-hint {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    #cat-select-list {
        height: auto;
        max-height: 70%;
    }
    #cat-select-list > ListItem {
        height: 1;
        padding: 0 1;
    }
    #cat-select-list > ListItem.--highlight {
        background: #3A5A3A;
    }
    """

    def __init__(self, categories: list[StatCategory]) -> None:
        super().__init__()
        self._categories = [c for c in categories if not c.is_only_display]
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="cat-select-container"):
            yield Static("Select Categories to Improve", id="cat-select-title")
            yield Static("[Enter] toggle selection  |  [d] done — run discovery  |  [Esc] cancel", id="cat-select-hint")
            yield ListView(id="cat-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#cat-select-list", ListView)
        for cat in self._categories:
            direction = "higher ↑" if cat.sort_order == "1" else "lower ↓"
            ptype = "bat" if cat.position_type == "B" else "pit"
            label = f"  {cat.display_name:<8} ({ptype}, {direction})"
            item = ListItem(Label(label))
            item._stat_id = cat.stat_id
            lv.mount(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        stat_id = getattr(event.item, "_stat_id", None)
        if stat_id is None:
            return
        if stat_id in self._selected:
            self._selected.discard(stat_id)
        else:
            self._selected.add(stat_id)
        self._refresh_labels()

    def _refresh_labels(self) -> None:
        lv = self.query_one("#cat-select-list", ListView)
        for i, cat in enumerate(self._categories):
            direction = "higher ↑" if cat.sort_order == "1" else "lower ↓"
            ptype = "bat" if cat.position_type == "B" else "pit"
            marker = "★" if cat.stat_id in self._selected else " "
            label = f"{marker} {cat.display_name:<8} ({ptype}, {direction})"
            try:
                item = lv.children[i]
                item.query_one(Label).update(label)
            except Exception:
                pass

    def action_confirm(self) -> None:
        if self._selected:
            self.dismiss(list(self._selected))
        else:
            self.notify("Select at least one category", severity="warning")

    def action_cancel(self) -> None:
        self.dismiss(None)


class TradeAnalyzerScreen(Screen):
    """Analyze the impact of a trade between two teams."""
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("b", "select_team_b", "Team B"),
        ("a", "analyze", "Analyze"),
        ("m", "switch_mode", "Mode"),
        ("1", "trade_view_season", "Season"),
        ("2", "trade_view_last30", "L30"),
    ]
    CSS = """
    #trade-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #trade-subheader {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #trade-split {
        height: 1fr;
    }
    #trade-left {
        width: 60%;
        border-right: solid $primary;
    }
    #trade-right {
        width: 40%;
    }
    #trade-left-scroll {
        height: 1fr;
    }
    #trade-right-scroll {
        height: 1fr;
        padding: 0 1;
    }
    .trade-team-label {
        height: 1;
        text-style: bold;
        padding: 0 1;
    }
    .trade-section-label {
        height: 1;
        text-style: bold;
        background: #2A2A2A;
        padding: 0 1;
        color: $text-muted;
    }
    .trade-roster-table {
        height: auto;
        max-height: 45%;
        background: $panel;
    }
    .trade-result-label {
        height: auto;
        padding: 0 1;
    }
    .trade-impact-table {
        height: auto;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    #trade-loading {
        height: auto;
        content-align: center middle;
        padding: 1 0;
    }
    #trade-loading-status {
        height: 1;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League,
                 categories: list[StatCategory]) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.categories = categories
        self._mode = "analyze"  # "analyze", "block", or "discover"
        self._team_a_key: str | None = None
        self._team_a_name: str = ""
        self._team_b_key: str | None = None
        self._team_b_name: str = ""
        self._roster_a: list[PlayerStats] = []
        self._roster_b: list[PlayerStats] = []
        self._selected_a: set[str] = set()  # player_keys checked for trade
        self._selected_b: set[str] = set()
        # Trading Block state
        self._block_player: PlayerStats | None = None
        self._trade_targets: list = []  # list[TradeTarget]
        # Trade Discovery state
        self._discover_cats: list[str] = []
        self._discover_scenarios: list = []  # list[TradeScenario]
        # Stat view for roster tables
        self._trade_view = "season"  # "season" or "last30"
        # When set, skip the initial team select modals (used when pre-configured
        # externally, e.g., from the Compare screen's "open trade analyzer" hotkey)
        self._skip_auto_select = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="trade-header")
        yield Static("", id="trade-subheader")
        with Horizontal(id="trade-split"):
            with Vertical(id="trade-left"):
                yield VerticalScroll(id="trade-left-scroll")
            with Vertical(id="trade-right"):
                with Vertical(id="trade-loading"):
                    yield LoadingIndicator()
                    yield Static("Analyzing trade...", id="trade-loading-status")
                yield VerticalScroll(id="trade-right-scroll")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#trade-header", Static).update(
            f" {self.league.name} — Trade Analyzer "
        )
        self.query_one("#trade-loading").display = False
        self._update_subheader()

        if self._skip_auto_select and self._team_a_key and self._team_b_key:
            # Pre-configured from another screen — load both rosters directly
            self.run_worker(self._load_both_rosters, group="trade-preload", exclusive=True)
            return

        # Auto-select Team A
        teams = self.api.get_team_season_stats(self.league.league_key)
        options = [(t.team_key, t.name) for t in teams]
        self.app.push_screen(
            TeamSelectModal(options),
            callback=self._on_team_a_selected,
        )

    async def _load_both_rosters(self) -> None:
        """Load both team rosters when the screen is pre-configured."""
        import asyncio
        ra, rb = await asyncio.gather(
            asyncio.to_thread(
                self.api.get_roster_stats_season, self._team_a_key, self.league.current_week),
            asyncio.to_thread(
                self.api.get_roster_stats_season, self._team_b_key, self.league.current_week),
        )
        self._roster_a = ra
        self._roster_b = rb
        self._selected_a.clear()
        self._selected_b.clear()
        await self._render_left_pane()
        self._update_subheader()

    def _update_subheader(self) -> None:
        sub = Text()
        mode_labels = {"analyze": "Analyze Trade", "block": "Trading Block", "discover": "Trade Discovery"}
        sub.append(f" [{mode_labels.get(self._mode, self._mode)}] ", style="bold italic")

        if self._mode == "discover":
            if self._discover_cats:
                scored = [c for c in self.categories if not c.is_only_display]
                cat_names = [c.display_name for c in scored if c.stat_id in self._discover_cats]
                sub.append(f" Improving: ", style="dim")
                sub.append(", ".join(cat_names), style=f"bold {TEAM_A_COLOR}")
                sub.append("  |  [m] Mode", style="dim")
            elif self._team_a_key:
                sub.append(f" {self._team_a_name}", style=f"bold {TEAM_A_COLOR}")
                sub.append("  |  Select categories to improve  [m] Mode", style="dim")
            else:
                sub.append(" Select your team...", style="dim")
        elif self._mode == "block":
            if self._block_player:
                sub.append(f" Trading: ", style="dim")
                sub.append(f"{self._block_player.name}", style=f"bold {TEAM_A_COLOR}")
                sub.append(f" ({self._block_player.position})", style="dim")
                sub.append("  |  [Enter] Select target  [m] Mode", style="dim")
            elif self._team_a_key:
                sub.append(f" {self._team_a_name}", style=f"bold {TEAM_A_COLOR}")
                sub.append("  |  [Enter] Select player to trade  [m] Mode", style="dim")
            else:
                sub.append(" Select your team...", style="dim")
        else:
            if self._team_a_key and self._team_b_key:
                sub.append(f" {self._team_a_name}", style=f"bold {TEAM_A_COLOR}")
                sub.append("  ↔  ", style="dim")
                sub.append(f"{self._team_b_name} ", style=f"bold {TEAM_B_COLOR}")
                sub.append("  |  [a] Analyze  [b] Team B  [m] Mode", style="dim")
            elif self._team_a_key:
                sub.append(f" {self._team_a_name}", style=f"bold {TEAM_A_COLOR}")
                sub.append("  |  [b] Select trade partner  [m] Mode", style="dim")
            else:
                sub.append(" Select your team...", style="dim")
        # Show stat view toggle
        if self._team_a_key:
            view_label = "Season" if self._trade_view == "season" else "Last 30"
            sub.append(f"  |  {view_label}", style="bold")
            sub.append(" [1/2]", style="dim")
        self.query_one("#trade-subheader", Static).update(sub)

    def _on_team_a_selected(self, team_key: str | None) -> None:
        if not team_key:
            self.app.pop_screen()
            return
        teams = self.api.get_team_season_stats(self.league.league_key)
        self._team_a_key = team_key
        self._team_a_name = next(
            (t.name for t in teams if t.team_key == team_key), team_key
        )
        self._update_subheader()
        self.run_worker(self._load_roster_a)

    async def _load_roster_a(self) -> None:
        self._roster_a = self.api.get_roster_stats_season(
            self._team_a_key, self.league.current_week
        )
        self._selected_a.clear()
        await self._render_left_pane()

    def action_select_team_b(self) -> None:
        if not self._team_a_key:
            return
        teams = self.api.get_team_season_stats(self.league.league_key)
        options = [(t.team_key, t.name) for t in teams
                   if t.team_key != self._team_a_key]
        self.app.push_screen(
            TeamSelectModal(options),
            callback=self._on_team_b_selected,
        )

    def _on_team_b_selected(self, team_key: str | None) -> None:
        if not team_key:
            return
        teams = self.api.get_team_season_stats(self.league.league_key)
        self._team_b_key = team_key
        self._team_b_name = next(
            (t.name for t in teams if t.team_key == team_key), team_key
        )
        self._update_subheader()
        self.run_worker(self._load_roster_b)

    async def _load_roster_b(self) -> None:
        self._roster_b = self.api.get_roster_stats_season(
            self._team_b_key, self.league.current_week
        )
        self._selected_b.clear()
        await self._render_left_pane()

    async def _render_left_pane(self) -> None:
        scroll = self.query_one("#trade-left-scroll", VerticalScroll)
        await scroll.remove_children()

        if self._roster_a:
            label_a = Text(f" {self._team_a_name} ", style=f"bold {TEAM_A_COLOR}")
            await scroll.mount(Static(label_a, classes="trade-team-label"))
            table_a = DataTable(classes="trade-roster-table", id="roster-table-a")
            await scroll.mount(table_a)
            self._fill_trade_roster(table_a, self._roster_a, self._selected_a)

        if self._roster_b:
            await scroll.mount(Static(""))  # spacer
            label_b = Text(f" {self._team_b_name} ", style=f"bold {TEAM_B_COLOR}")
            await scroll.mount(Static(label_b, classes="trade-team-label"))
            table_b = DataTable(classes="trade-roster-table", id="roster-table-b")
            await scroll.mount(table_b)
            self._fill_trade_roster(table_b, self._roster_b, self._selected_b)

    def _fill_trade_roster(
        self, table: DataTable, roster: list[PlayerStats], selected: set[str],
    ) -> None:
        table.cursor_type = "row"
        table.zebra_stripes = True
        table._players = roster
        table.clear(columns=True)

        scored = [c for c in self.categories if not c.is_only_display]
        batting_cats = [c for c in scored if c.position_type == "B"]
        pitching_cats = [c for c in scored if c.position_type == "P"]
        batting_positions = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
                             "OF", "Util", "DH", "IF", "BN"}

        # Build columns: marker, player, pos, then stat columns
        cols: list[str | Text] = ["", "Player", "Pos"]
        # Use batting cats for all players — pitchers show pitching cats
        # Combine all stat cols since table is shared
        for cat in batting_cats:
            cols.append(cat.display_name)
        cols.append("│")
        for cat in pitching_cats:
            cols.append(cat.display_name)
        table.add_columns(*cols)

        for p in roster:
            is_batter = any(
                pos in batting_positions for pos in p.position.split(",")
            )
            marker = "★" if p.player_key in selected else " "
            marker_style = "bold #FFD700" if p.player_key in selected else "dim"

            row: list[Text] = [
                Text(marker, style=marker_style),
                Text(p.name[:18], style="bold"),
                Text(p.selected_position or p.position[:6], style="dim"),
            ]
            for cat in batting_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "" if is_batter else "dim"
                row.append(Text(str(val), style=style, justify="right"))
            row.append(Text("│", style="dim"))
            for cat in pitching_cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "" if not is_batter else "dim"
                row.append(Text(str(val), style=style, justify="right"))
            table.add_row(*row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on a roster or target table row."""
        table = event.data_table
        table_id = getattr(table, "id", "")
        players = getattr(table, "_players", [])
        row_idx = event.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return

        if self._mode == "discover":
            if table_id == "scenario-table" and row_idx < len(self._discover_scenarios):
                scenario = self._discover_scenarios[row_idx]
                self._run_scenario_analysis(scenario)
            return

        if self._mode == "block":
            if table_id == "roster-table-a":
                # Select the player to trade
                p = players[row_idx]
                self._block_player = p
                self._update_subheader()
                self.run_worker(self._scan_trade_targets, group="trade-scan", exclusive=True)
            elif table_id == "target-table":
                # Select a target → run full analysis
                target = self._trade_targets[row_idx]
                self._run_block_analysis(target)
            return

        # Analyze mode: toggle selection
        p = players[row_idx]
        if table_id == "roster-table-a":
            if p.player_key in self._selected_a:
                self._selected_a.discard(p.player_key)
            else:
                self._selected_a.add(p.player_key)
        elif table_id == "roster-table-b":
            if p.player_key in self._selected_b:
                self._selected_b.discard(p.player_key)
            else:
                self._selected_b.add(p.player_key)

        # Re-render left pane to update markers
        self.run_worker(self._render_left_pane, group="render-left", exclusive=True)

    def action_switch_mode(self) -> None:
        self.app.push_screen(
            TradeModeSelectorModal(),
            callback=self._on_mode_selected,
        )

    def _on_mode_selected(self, mode: str | None) -> None:
        if mode is None or mode == self._mode:
            return
        self._mode = mode
        self._block_player = None
        self._trade_targets = []
        self._discover_cats = []
        self._discover_scenarios = []
        self._selected_a.clear()
        self._selected_b.clear()
        self._team_b_key = None
        self._team_b_name = ""
        self._roster_b = []
        self._update_subheader()
        # Clear right pane
        async def _clear():
            scroll = self.query_one("#trade-right-scroll", VerticalScroll)
            await scroll.remove_children()
            await self._render_left_pane()
        self.run_worker(_clear, group="render-left", exclusive=True)

        if mode == "discover" and self._team_a_key:
            self.app.push_screen(
                CategorySelectModal(self.categories),
                callback=self._on_categories_selected,
            )

    def _on_categories_selected(self, stat_ids: list[str] | None) -> None:
        if not stat_ids:
            return
        self._discover_cats = stat_ids
        self._update_subheader()
        self.run_worker(self._run_discovery, group="trade-discovery", exclusive=True)

    async def _run_discovery(self) -> None:
        from gkl.trade import discover_trades

        self.query_one("#trade-loading").display = True
        self.query_one("#trade-loading-status", Static).update("Fetching all team rosters...")
        self.query_one("#trade-right-scroll").display = False

        try:
            cache = self.app.shared_cache
            await cache.ensure_loaded(self.api, self.league, self.categories)

            import asyncio
            teams = cache.all_teams

            # Fetch season rosters for all teams in parallel
            roster_tasks = [
                asyncio.to_thread(
                    self.api.get_roster_stats_season, t.team_key, self.league.current_week
                )
                for t in teams if t.team_key != self._team_a_key
            ]
            roster_results = await asyncio.gather(*roster_tasks)
            all_rosters: dict[str, list] = {}
            non_my_teams = [t for t in teams if t.team_key != self._team_a_key]
            for t, roster in zip(non_my_teams, roster_results):
                all_rosters[t.team_key] = roster
            all_rosters[self._team_a_key] = self._roster_a

            self.query_one("#trade-loading-status", Static).update(
                "Finding trade scenarios..."
            )
            self._discover_scenarios = await asyncio.to_thread(
                discover_trades,
                self._team_a_key,
                self._discover_cats,
                all_rosters,
                cache.all_teams,
                cache.team_names,
                self.categories,
                cache.sgp_calc,
            )

            await self._render_discovery_results()
        except Exception as e:
            self.notify(f"Discovery failed: {e}", severity="error")
        finally:
            self.query_one("#trade-loading").display = False
            self.query_one("#trade-right-scroll").display = True

    async def _render_discovery_results(self) -> None:
        from gkl.trade import TradeScenario

        scroll = self.query_one("#trade-right-scroll", VerticalScroll)
        await scroll.remove_children()

        if not self._discover_scenarios:
            await scroll.mount(Static(
                Text("  No trade scenarios found for the selected categories.", style="dim"),
            ))
            return

        scored = [c for c in self.categories if not c.is_only_display]
        cat_names = [c.display_name for c in scored if c.stat_id in self._discover_cats]
        header = Text()
        header.append(f" Improving: ", style="dim")
        header.append(", ".join(cat_names), style=f"bold {TEAM_A_COLOR}")
        header.append(f"\n Select a scenario to see full trade analysis.", style="dim italic")
        await scroll.mount(Static(header, classes="trade-result-label"))

        scenario_table = DataTable(classes="trade-impact-table", id="scenario-table")
        await scroll.mount(scenario_table)
        scenario_table.cursor_type = "row"
        scenario_table.zebra_stripes = True
        scenario_table._players = self._discover_scenarios
        scenario_table.add_columns("You Get", "From", "You Send", "ΔSGP", "ΔRoto", "ΔWin%", "Partner")

        for s in self._discover_scenarios:
            net_str = f"{s.net_sgp:+.1f}"
            net_style = "bold green" if s.net_sgp > 0 else "bold red" if s.net_sgp < 0 else "dim"

            roto_str = f"{s.roto_delta:+.1f}"
            roto_style = "bold green" if s.roto_delta > 0.1 else "bold red" if s.roto_delta < -0.1 else "dim"

            if abs(s.h2h_win_pct_delta) > 0.001:
                h2h_str = f"{s.h2h_win_pct_delta:+.1%}"
                h2h_style = "bold green" if s.h2h_win_pct_delta > 0 else "bold red"
            else:
                h2h_str = "—"
                h2h_style = "dim"

            # Partner roto delta — shows if the deal is realistic
            partner_str = f"{s.partner_roto_delta:+.0f}"
            if s.partner_roto_delta > 0.1:
                partner_style = "green"  # partner benefits — good for acceptance
            elif s.partner_roto_delta < -5:
                partner_style = "red"  # partner loses a lot — unlikely to accept
            else:
                partner_style = "dim"

            scenario_table.add_row(
                Text(f"{s.target.name[:16]} ({s.target.position[:5]})", style="bold"),
                Text(s.target_team_name[:12], style="dim"),
                Text(f"{s.offer.name[:16]} ({s.offer.position[:5]})", style=f"{TEAM_A_COLOR}"),
                Text(net_str, style=net_style, justify="right"),
                Text(roto_str, style=roto_style, justify="right"),
                Text(h2h_str, style=h2h_style, justify="right"),
                Text(partner_str, style=partner_style, justify="right"),
            )

    async def _scan_trade_targets(self) -> None:
        """Scan all rosters for trade targets matching the block player."""
        from gkl.trade import find_trade_targets

        self.query_one("#trade-loading").display = True
        self.query_one("#trade-loading-status", Static).update("Fetching all team rosters...")
        self.query_one("#trade-right-scroll").display = False

        try:
            cache = self.app.shared_cache
            await cache.ensure_loaded(self.api, self.league, self.categories)

            import asyncio
            teams = cache.all_teams
            weeks = list(range(1, self.league.current_week + 1))

            # Fetch season rosters for all teams in parallel
            self.query_one("#trade-loading-status", Static).update(
                "Fetching season rosters for all teams..."
            )
            roster_tasks = [
                asyncio.to_thread(
                    self.api.get_roster_stats_season, t.team_key, self.league.current_week
                )
                for t in teams if t.team_key != self._team_a_key
            ]
            roster_results = await asyncio.gather(*roster_tasks)
            all_rosters: dict[str, list] = {}
            non_my_teams = [t for t in teams if t.team_key != self._team_a_key]
            for t, roster in zip(non_my_teams, roster_results):
                all_rosters[t.team_key] = roster
            all_rosters[self._team_a_key] = self._roster_a

            # Fetch weekly matchups
            self.query_one("#trade-loading-status", Static).update(
                "Fetching weekly matchup data..."
            )
            for w in weeks:
                if w not in cache.week_matchups:
                    try:
                        wm = self.api.get_scoreboard(self.league.league_key, week=w)
                        cache.week_matchups[w] = wm
                    except Exception:
                        pass

            # Fetch weekly rosters for team A and all opposing teams
            self.query_one("#trade-loading-status", Static).update(
                "Fetching weekly player stats for all teams..."
            )
            all_weekly_rosters: dict[str, dict[int, list]] = {}
            for tk in all_rosters:
                all_weekly_rosters[tk] = {}

            for w in weeks:
                week_tasks = [
                    asyncio.to_thread(self.api.get_roster_stats, tk, w)
                    for tk in all_rosters
                ]
                week_results = await asyncio.gather(*week_tasks, return_exceptions=True)
                for tk, result in zip(all_rosters.keys(), week_results):
                    if not isinstance(result, Exception):
                        all_weekly_rosters[tk][w] = result

            self.query_one("#trade-loading-status", Static).update(
                "Computing roto and H2H impact for each target..."
            )
            self._trade_targets = await asyncio.to_thread(
                find_trade_targets,
                self._block_player,
                self._team_a_key,
                all_rosters,
                cache.all_teams,
                cache.team_names,
                self.categories,
                cache.sgp_calc,
                cache.week_matchups,
                all_weekly_rosters,
                self.league.current_week,
            )

            await self._render_target_list()
        except Exception as e:
            self.notify(f"Scan failed: {e}", severity="error")
        finally:
            self.query_one("#trade-loading").display = False
            self.query_one("#trade-right-scroll").display = True

    async def _render_target_list(self) -> None:
        """Render the ranked trade target list in the right pane."""
        scroll = self.query_one("#trade-right-scroll", VerticalScroll)
        await scroll.remove_children()

        if not self._trade_targets:
            await scroll.mount(Static(
                Text("  No position-eligible trade targets found.", style="dim"),
            ))
            return

        out_sgp = None
        if self._block_player:
            cache = self.app.shared_cache
            if cache.sgp_calc:
                out_sgp = cache.sgp_calc.player_sgp(self._block_player)

        header = Text()
        header.append(f" Trading: ", style="dim")
        header.append(f"{self._block_player.name}", style=f"bold {TEAM_A_COLOR}")
        if out_sgp is not None:
            header.append(f" (SGP: {out_sgp:+.1f})", style="dim")
        header.append(f"\n Select a target to see full trade analysis.", style="dim italic")
        await scroll.mount(Static(header, classes="trade-result-label"))

        target_table = DataTable(classes="trade-impact-table", id="target-table")
        await scroll.mount(target_table)
        target_table.cursor_type = "row"
        target_table.zebra_stripes = True
        target_table._players = self._trade_targets
        target_table.add_columns("Player", "Pos", "Team", "SGP", "ΔSGP", "ΔRoto", "ΔWin%", "Partner")

        for t in self._trade_targets:
            sgp_str = f"{t.sgp:+.1f}" if t.sgp is not None else "N/A"

            net_str = f"{t.net_sgp:+.1f}"
            net_style = "bold green" if t.net_sgp > 0 else "bold red" if t.net_sgp < 0 else "dim"

            roto_str = f"{t.roto_delta:+.1f}"
            roto_style = "bold green" if t.roto_delta > 0.1 else "bold red" if t.roto_delta < -0.1 else "dim"

            if abs(t.h2h_win_pct_delta) > 0.001:
                h2h_str = f"{t.h2h_win_pct_delta:+.1%}"
                h2h_style = "bold green" if t.h2h_win_pct_delta > 0 else "bold red"
            else:
                h2h_str = "—"
                h2h_style = "dim"

            partner_str = f"{t.partner_roto_delta:+.0f}"
            if t.partner_roto_delta > 0.1:
                partner_style = "green"
            elif t.partner_roto_delta < -5:
                partner_style = "red"
            else:
                partner_style = "dim"

            target_table.add_row(
                Text(t.player.name[:20], style="bold"),
                Text(t.player.position[:12], style="dim"),
                Text(t.team_name[:15], style="dim"),
                Text(sgp_str, justify="right"),
                Text(net_str, style=net_style, justify="right"),
                Text(roto_str, style=roto_style, justify="right"),
                Text(h2h_str, style=h2h_style, justify="right"),
                Text(partner_str, style=partner_style, justify="right"),
            )

    def _run_block_analysis(self, target) -> None:
        """Run full trade analysis for a Trading Block target selection."""
        from gkl.trade import TradeTarget
        target: TradeTarget

        # Set up as if it were an Analyze Trade
        self._team_b_key = target.team_key
        self._team_b_name = target.team_name
        self._selected_a = {self._block_player.player_key}
        self._selected_b = {target.player.player_key}

        # Fetch team B roster, update left pane, and run analysis
        async def _load_and_analyze():
            self._roster_b = self.api.get_roster_stats_season(
                target.team_key, self.league.current_week
            )
            # Switch to analyze mode view for the left pane
            self._mode = "analyze"
            self._update_subheader()
            await self._render_left_pane()
            await self._run_analysis()

        self.run_worker(_load_and_analyze, group="trade-analysis", exclusive=True)

    def _run_scenario_analysis(self, scenario) -> None:
        """Run full trade analysis for a Trade Discovery scenario."""
        from gkl.trade import TradeScenario
        scenario: TradeScenario

        self._team_b_key = scenario.target_team_key
        self._team_b_name = scenario.target_team_name
        self._selected_a = {scenario.offer.player_key}
        self._selected_b = {scenario.target.player_key}

        async def _load_and_analyze():
            self._roster_b = self.api.get_roster_stats_season(
                scenario.target_team_key, self.league.current_week
            )
            self._mode = "analyze"
            self._update_subheader()
            await self._render_left_pane()
            await self._run_analysis()

        self.run_worker(_load_and_analyze, group="trade-analysis", exclusive=True)

    def action_analyze(self) -> None:
        if not self._selected_a or not self._selected_b:
            self.notify("Select players from both teams first", severity="warning")
            return
        if not self._team_a_key or not self._team_b_key:
            self.notify("Select both teams first", severity="warning")
            return
        self.run_worker(self._run_analysis, group="trade-analysis", exclusive=True)

    async def _run_analysis(self) -> None:
        from gkl.trade import TradeSide, compute_trade_impact

        # Show loading
        self.query_one("#trade-loading").display = True
        self.query_one("#trade-right-scroll").display = False

        try:
            cache = self.app.shared_cache
            await cache.ensure_loaded(self.api, self.league, self.categories)

            players_out_a = [p for p in self._roster_a if p.player_key in self._selected_a]
            players_out_b = [p for p in self._roster_b if p.player_key in self._selected_b]

            side_a = TradeSide(self._team_a_key, self._team_a_name, players_out_a)
            side_b = TradeSide(self._team_b_key, self._team_b_name, players_out_b)

            import asyncio
            impact = await asyncio.to_thread(
                compute_trade_impact,
                cache.all_teams,
                self._roster_a,
                self._roster_b,
                side_a, side_b,
                self.categories,
            )

            # Fetch weekly matchups and per-player rosters for H2H replay
            self.query_one("#trade-loading-status", Static).update(
                "Loading weekly data for H2H replay..."
            )
            weeks = list(range(1, self.league.current_week + 1))
            # Fetch weekly matchups
            for w in weeks:
                if w not in cache.week_matchups:
                    try:
                        wm = self.api.get_scoreboard(self.league.league_key, week=w)
                        cache.week_matchups[w] = wm
                    except Exception:
                        pass

            # Fetch per-player weekly rosters for both teams (parallel per week)
            weekly_roster_a: dict[int, list] = {}
            weekly_roster_b: dict[int, list] = {}
            for w in weeks:
                try:
                    ra, rb = await asyncio.gather(
                        asyncio.to_thread(
                            self.api.get_roster_stats, self._team_a_key, w),
                        asyncio.to_thread(
                            self.api.get_roster_stats, self._team_b_key, w),
                    )
                    weekly_roster_a[w] = ra
                    weekly_roster_b[w] = rb
                except Exception:
                    pass

            from gkl.trade import replay_h2h_with_trade, compute_h2h_hypothetical
            side_a_keys = {p.player_key for p in players_out_a}
            side_b_keys = {p.player_key for p in players_out_b}
            h2h_replay = await asyncio.to_thread(
                replay_h2h_with_trade,
                self._team_a_key,
                self._team_b_key,
                side_a_keys, side_b_keys,
                cache.week_matchups,
                weekly_roster_a, weekly_roster_b,
                self.categories,
                self.league.current_week,
            )
            h2h_hypo = await asyncio.to_thread(
                compute_h2h_hypothetical,
                self._team_a_key,
                side_a_keys, side_b_keys,
                cache.week_matchups,
                weekly_roster_a, weekly_roster_b,
                self.categories,
                self.league.current_week,
            )

            await self._render_results(impact, h2h_replay, h2h_hypo)

            # AI summary if API key is available
            api_key = load_anthropic_key()
            if api_key:
                await self._render_ai_summary(impact, h2h_replay, api_key)
        except Exception as e:
            self.notify(f"Analysis failed: {e}", severity="error")
        finally:
            self.query_one("#trade-loading").display = False
            self.query_one("#trade-right-scroll").display = True

    async def _render_results(self, impact, h2h_replay=None, h2h_hypo=None) -> None:
        from gkl.trade import TradeImpact, H2HReplay, H2HHypothetical
        impact: TradeImpact

        scroll = self.query_one("#trade-right-scroll", VerticalScroll)
        await scroll.remove_children()

        # --- Stat Impact Table ---
        await scroll.mount(Static(
            Text(" CATEGORY IMPACT ", style="bold"),
            classes="trade-section-label",
        ))

        cat_table = DataTable(classes="trade-impact-table")
        await scroll.mount(cat_table)
        cat_table.cursor_type = "none"
        cat_table.zebra_stripes = True
        cat_table.add_columns("Category", "Before", "After", "Delta")

        for ci in impact.cat_impacts:
            if ci.delta == 0:
                delta_style = "dim"
                delta_str = "—"
            elif ci.favorable:
                delta_style = "bold green"
                # Format delta based on stat type
                if ci.stat_id in RATE_STATS:
                    delta_str = f"{ci.delta:+.3f}"
                else:
                    delta_str = f"{ci.delta:+.0f}"
            else:
                delta_style = "bold red"
                if ci.stat_id in RATE_STATS:
                    delta_str = f"{ci.delta:+.3f}"
                else:
                    delta_str = f"{ci.delta:+.0f}"

            cat_table.add_row(
                Text(f" {ci.display_name}", style="bold"),
                Text(ci.before, justify="right"),
                Text(ci.after, justify="right"),
                Text(delta_str, style=delta_style, justify="right"),
            )

        await scroll.mount(Static(""))  # spacer

        # --- Roto Standings Table ---
        await scroll.mount(Static(
            Text(" ROTO STANDINGS (POST-TRADE) ", style="bold"),
            classes="trade-section-label",
        ))

        from gkl.trade import RotoEntry
        roto_table = DataTable(classes="trade-impact-table")
        await scroll.mount(roto_table)
        roto_table.cursor_type = "none"
        roto_table.zebra_stripes = True
        roto_table.add_columns(
            "", "Team",
            "Ovr", "Δ",
            "│",
            "Bat", "Δ",
            "│",
            "Pit", "Δ",
        )

        # Build before lookup
        before_by_key = {r.team_key: r for r in impact.roto_standings_before}

        # Iterate in after-trade order (already sorted by compute_roto)
        for ra in impact.roto_standings_after:
            rb = before_by_key.get(ra.team_key, ra)
            rank_change = rb.rank - ra.rank  # positive = improved

            # Highlight the two traded teams
            is_team_a = ra.team_key == self._team_a_key
            is_team_b = ra.team_key == self._team_b_key
            if is_team_a:
                name_style = f"bold {TEAM_A_COLOR}"
            elif is_team_b:
                name_style = f"bold {TEAM_B_COLOR}"
            else:
                name_style = ""

            # Rank movement indicator
            if rank_change > 0:
                rank_str = f"▲{rank_change}"
                rank_style = "bold green"
            elif rank_change < 0:
                rank_str = f"▼{abs(rank_change)}"
                rank_style = "bold red"
            else:
                rank_str = "—"
                rank_style = "dim"

            # Batting delta
            bat_delta = ra.batting - rb.batting
            if bat_delta > 0.1:
                bat_d_str = f"+{bat_delta:.0f}"
                bat_d_style = "green"
            elif bat_delta < -0.1:
                bat_d_str = f"{bat_delta:.0f}"
                bat_d_style = "red"
            else:
                bat_d_str = "—"
                bat_d_style = "dim"

            # Pitching delta
            pitch_delta = ra.pitching - rb.pitching
            if pitch_delta > 0.1:
                pitch_d_str = f"+{pitch_delta:.0f}"
                pitch_d_style = "green"
            elif pitch_delta < -0.1:
                pitch_d_str = f"{pitch_delta:.0f}"
                pitch_d_style = "red"
            else:
                pitch_d_str = "—"
                pitch_d_style = "dim"

            roto_table.add_row(
                Text(f"#{ra.rank}", style="bold" if is_team_a or is_team_b else "dim"),
                Text(ra.name[:18], style=name_style),
                Text(f"{ra.total:.0f}", justify="right", style="bold"),
                Text(rank_str, style=rank_style, justify="right"),
                Text("│", style="dim"),
                Text(f"{ra.batting:.0f}", justify="right"),
                Text(bat_d_str, style=bat_d_style, justify="right"),
                Text("│", style="dim"),
                Text(f"{ra.pitching:.0f}", justify="right"),
                Text(pitch_d_str, style=pitch_d_style, justify="right"),
            )

        # --- H2H Weekly Replay ---
        if h2h_replay and h2h_replay.weeks:
            await scroll.mount(Static(""))  # spacer

            await scroll.mount(Static(
                Text(" H2H WEEKLY REPLAY ", style="bold"),
                classes="trade-section-label",
            ))

            replay_desc = Text()
            replay_desc.append(
                "  Replays each completed week's actual matchup with the trade\n"
                "  applied to your team's weekly stats.",
                style="dim italic",
            )
            await scroll.mount(Static(replay_desc, classes="trade-result-label"))

            replay_table = DataTable(classes="trade-impact-table")
            await scroll.mount(replay_table)
            replay_table.cursor_type = "none"
            replay_table.zebra_stripes = True
            replay_table.add_columns("Wk", "Opponent", "Actual", "W/ Trade", "")

            for wr in h2h_replay.weeks:
                actual_str = f"{wr.actual_wins}-{wr.actual_losses}-{wr.actual_ties}"
                trade_str = f"{wr.trade_wins}-{wr.trade_losses}-{wr.trade_ties}"

                if wr.changed:
                    if wr.trade_result == "W" and wr.actual_result != "W":
                        change_str = "▲ FLIP"
                        change_style = "bold green"
                    elif wr.trade_result == "L" and wr.actual_result != "L":
                        change_str = "▼ FLIP"
                        change_style = "bold red"
                    else:
                        change_str = "~ FLIP"
                        change_style = "bold #E8A735"
                else:
                    change_str = ""
                    change_style = "dim"

                replay_table.add_row(
                    Text(f"{wr.week}", justify="right"),
                    Text(wr.opponent_name[:18]),
                    Text(f"{actual_str} {wr.actual_result}", justify="right"),
                    Text(f"{trade_str} {wr.trade_result}", justify="right"),
                    Text(change_str, style=change_style),
                )

            # Season summary
            await scroll.mount(Static(""))
            summary = Text()
            summary.append(f"  Season record: ", style="dim")
            summary.append(
                f"{h2h_replay.actual_season_w}-{h2h_replay.actual_season_l}-{h2h_replay.actual_season_t}",
                style="bold",
            )
            summary.append(f"  →  ", style="dim")
            summary.append(
                f"{h2h_replay.trade_season_w}-{h2h_replay.trade_season_l}-{h2h_replay.trade_season_t}",
                style="bold",
            )
            sw_delta = h2h_replay.trade_season_w - h2h_replay.actual_season_w
            if sw_delta > 0:
                summary.append(f"  +{sw_delta}W", style="bold green")
            elif sw_delta < 0:
                summary.append(f"  {sw_delta}W", style="bold red")
            await scroll.mount(Static(summary, classes="trade-result-label"))

        # --- H2H Hypothetical (per-week vs all opponents) ---
        if h2h_hypo:
            await scroll.mount(Static(""))  # spacer

            await scroll.mount(Static(
                Text(" H2H HYPOTHETICAL (ALL OPPONENTS, ALL WEEKS) ", style="bold"),
                classes="trade-section-label",
            ))

            n_matchups = h2h_hypo.before_w + h2h_hypo.before_l + h2h_hypo.before_t
            n_opponents = self.league.num_teams - 1
            n_weeks = self.league.current_week
            hypo_desc = Text()
            hypo_desc.append(
                f"  For each of the {n_weeks} completed weeks, simulates your\n"
                f"  weekly stats vs all {n_opponents} other teams ({n_matchups} total matchups).",
                style="dim italic",
            )
            await scroll.mount(Static(hypo_desc, classes="trade-result-label"))

            hypo_before = Text()
            hypo_before.append(f"  Before: ", style="dim")
            hypo_before.append(f"{h2h_hypo.before_w}-{h2h_hypo.before_l}-{h2h_hypo.before_t}", style="bold")
            b_pct = h2h_hypo.before_w / n_matchups if n_matchups else 0
            hypo_before.append(f" ({b_pct:.1%})", style="dim")
            await scroll.mount(Static(hypo_before, classes="trade-result-label"))

            hypo_after = Text()
            hypo_after.append(f"  After:  ", style="dim")
            hypo_after.append(f"{h2h_hypo.after_w}-{h2h_hypo.after_l}-{h2h_hypo.after_t}", style="bold")
            a_pct = h2h_hypo.after_w / n_matchups if n_matchups else 0
            hypo_after.append(f" ({a_pct:.1%})", style="dim")
            hw_delta = h2h_hypo.after_w - h2h_hypo.before_w
            if hw_delta > 0:
                hypo_after.append(f"  +{hw_delta}W", style="bold green")
            elif hw_delta < 0:
                hypo_after.append(f"  {hw_delta}W", style="bold red")
            await scroll.mount(Static(hypo_after, classes="trade-result-label"))

        await scroll.mount(Static(""))  # spacer

        # --- Trade Partner Impact ---
        await scroll.mount(Static(
            Text(f" IMPACT ON {self._team_b_name.upper()} ", style="bold"),
            classes="trade-section-label",
        ))

        partner_roto = Text()
        b_rank_delta = impact.roto_rank_before_b - impact.roto_rank_after_b
        partner_roto.append(f"  Roto: #{impact.roto_rank_before_b} → #{impact.roto_rank_after_b}", style="dim")
        if b_rank_delta > 0:
            partner_roto.append(f"  ▲ {b_rank_delta}", style="green")
        elif b_rank_delta < 0:
            partner_roto.append(f"  ▼ {abs(b_rank_delta)}", style="red")
        await scroll.mount(Static(partner_roto, classes="trade-result-label"))

        partner_h2h = Text()
        partner_h2h.append(f"  H2H: {impact.h2h_before_b.record_str} → {impact.h2h_after_b.record_str}", style="dim")
        b_win_delta = impact.h2h_after_b.total_wins - impact.h2h_before_b.total_wins
        if b_win_delta > 0:
            partner_h2h.append(f"  +{b_win_delta}W", style="green")
        elif b_win_delta < 0:
            partner_h2h.append(f"  {b_win_delta}W", style="red")
        await scroll.mount(Static(partner_h2h, classes="trade-result-label"))

    async def _render_ai_summary(self, impact, h2h_replay, api_key: str) -> None:
        from gkl.trade import build_trade_summary_prompt, get_trade_ai_summary
        from gkl.skipper import DEFAULT_MODEL

        scroll = self.query_one("#trade-right-scroll", VerticalScroll)

        await scroll.mount(Static(""))
        await scroll.mount(Static(
            Text(" AI ANALYSIS ", style="bold"),
            classes="trade-section-label",
        ))

        ai_content = Static(
            Text("  Generating analysis...", style="dim italic"),
            classes="trade-result-label",
        )
        await scroll.mount(ai_content)
        scroll.scroll_end(animate=False)

        try:
            players_out_a = [p for p in self._roster_a if p.player_key in self._selected_a]
            players_out_b = [p for p in self._roster_b if p.player_key in self._selected_b]

            prompt = build_trade_summary_prompt(
                impact, self._team_a_name, self._team_b_name,
                players_out_a, players_out_b, h2h_replay,
            )
            summary = await get_trade_ai_summary(prompt, api_key, DEFAULT_MODEL)
            ai_content.update(Text(f"  {summary}"))
        except Exception as e:
            err_msg = str(e)
            # Include more detail for API errors
            if hasattr(e, 'message'):
                err_msg = e.message
            elif hasattr(e, 'body'):
                err_msg = f"{e} — {e.body}"
            ai_content.update(Text(f"  Could not generate AI analysis: {err_msg}", style="dim italic"))
        scroll.scroll_end(animate=False)

    def action_trade_view_season(self) -> None:
        if self._trade_view == "season":
            return
        self._trade_view = "season"
        self.run_worker(self._reload_trade_rosters, group="trade-view", exclusive=True)

    def action_trade_view_last30(self) -> None:
        if self._trade_view == "last30":
            return
        self._trade_view = "last30"
        self.run_worker(self._reload_trade_rosters, group="trade-view", exclusive=True)

    async def _reload_trade_rosters(self) -> None:
        """Reload rosters with the current stat view and re-render."""
        import asyncio
        if self._trade_view == "last30":
            fetch = self.api.get_roster_stats_last30
        else:
            fetch = self.api.get_roster_stats_season

        if self._team_a_key:
            self._roster_a = await asyncio.to_thread(
                fetch, self._team_a_key, self.league.current_week)
        if self._team_b_key:
            self._roster_b = await asyncio.to_thread(
                fetch, self._team_b_key, self.league.current_week)
        await self._render_left_pane()

    def action_go_back(self) -> None:
        self.app.pop_screen()


# --- Scoreboard Screen (3-pane layout) ---


class ScoreboardScreen(PlayerCompareMixin, Screen):
    BINDINGS = [("q", "quit", "Quit" if not is_web_mode() else "Logout"),
                ("r", "refresh", "Refresh"),
                ("s", "standings", "League Standings"),
                ("h", "h2h_sim", "H2H Sim"),
                ("g", "mlb_scores", "MLB Scores"),
                ("t", "roster", "Roster"),
                ("f", "free_agents", "Free Agents"),
                ("x", "transactions", "Transactions"),
                ("p", "player_explorer", "Player Explorer"),
                ("l", "watchlist", "Watchlist"),
                Binding("w", "view_weekly", "Weekly", show=False),
                Binding("d", "view_daily", "Daily", show=False),
                Binding("n", "view_season", "Season", show=False),
                Binding("comma", "prev_date", "< Prev Day", show=False),
                Binding("full_stop", "next_date", "> Next Day", show=False),
                ("left", "prev_week", "Prev Week"),
                ("right", "next_week", "Next Week"),
                ("e", "select_week", "Select Week"),
                ("L", "switch_league", "Switch League"),
                ("c", "compare", "Compare"),
                ("i", "player_detail", "Player Detail"),
                ("a", "ask_skipper", "Ask Skipper"),
                ("T", "trade_analyzer", "Trade Analyzer"),
                ("C", "settings", "Config")]
    CSS = """
    #board-header {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
    }
    #board-subheader {
        height: 1;
        content-align: center middle;
        background: $surface;
        color: $text-muted;
    }
    #main-split {
        height: 60%;
    }
    #left-pane {
        width: 1fr;
        min-width: 40;
        max-width: 50%;
    }
    #right-pane {
        width: 1fr;
        border-left: solid $primary;
    }
    #right-pane-inner {
        height: 1fr;
    }
    ListView {
        height: 1fr;
        background: $background;
    }
    ListView > ListItem {
        height: 3;
        padding: 0 1;
        background: $surface;
    }
    ListView > ListItem.--highlight {
        background: #2E3E2E;
    }
    .matchup-row {
        height: 1;
        width: 100%;
    }
    .matchup-divider {
        height: 1;
        color: $primary-lighten-2;
    }
    #loading-msg {
        height: 3;
        content-align: center middle;
        color: $text-muted;
    }
    .stat-header {
        height: 1;
        content-align: center middle;
        background: #2A2A2A;
        text-style: bold;
    }
    .stat-score {
        height: 1;
        content-align: center middle;
        background: #1E1E1E;
        text-style: bold;
    }
    .stat-tally {
        height: 1;
        content-align: center middle;
        background: $surface;
        text-style: bold;
    }
    .section-label {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
    }
    #stat-empty {
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    #right-pane-inner DataTable {
        height: auto;
        max-height: 50%;
        background: $panel;
    }
    #bottom-pane {
        height: 40%;
        border-top: solid $primary;
    }
    #player-view-header {
        height: 1;
        content-align: center middle;
        background: #3A4A3A;
        color: $foreground;
        text-style: bold;
        dock: top;
    }
    #player-scroll-a {
        width: 1fr;
        height: 1fr;
    }
    #player-scroll-b {
        width: 1fr;
        height: 1fr;
        border-left: solid $primary;
    }
    .team-roster-label {
        height: 1;
        content-align: center middle;
        text-style: bold;
    }
    .roster-section-label {
        height: 1;
        content-align: left middle;
        background: #2A2A2A;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    #bottom-pane DataTable {
        height: auto;
        background: $panel;
    }
    DataTable > .datatable--cursor {
        background: #3A5A3A;
        color: #E8E4DF;
    }
    """

    def __init__(self, api: YahooFantasyAPI, league: League) -> None:
        super().__init__()
        self.api = api
        self.league = league
        self.matchups: list[Matchup] = []
        self.categories: list[StatCategory] = []
        self._selected_idx: int | None = None
        self._viewing_week: int | None = None  # None = current week
        self._player_view = "weekly"  # "weekly", "daily", "season"
        self._daily_date: str = ""  # current date for daily view
        self._matchup_dates: list[str] = []  # available dates for current matchup
        self._roto_rank: dict[str, int] = {}
        self._h2h_record: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="board-header")
        yield Static("", id="board-subheader")
        with Horizontal(id="main-split"):
            with Vertical(id="left-pane"):
                yield Static("Loading...", id="loading-msg")
                yield ListView(id="matchup-list")
            with Vertical(id="right-pane"):
                yield VerticalScroll(id="right-pane-inner")
        with Vertical(id="bottom-pane"):
            yield Static("", id="player-view-header")
            with Horizontal():
                yield VerticalScroll(id="player-scroll-a")
                yield VerticalScroll(id="player-scroll-b")
        yield WrappingFooter()

    def on_mount(self) -> None:
        self.query_one("#matchup-list", ListView).display = False
        self.query_one("#bottom-pane").display = False
        self.run_worker(self._load)
        self._auto_refresh_timer = self.set_interval(60, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self.league:
            self.run_worker(self._refresh_data)

    async def _load(self) -> None:
        self.categories = self.api.get_stat_categories(self.league.league_key)
        week = self._viewing_week if self._viewing_week is not None else None
        self.matchups = self.api.get_scoreboard(self.league.league_key, week=week)

        self._update_header()
        loading = self.query("#loading-msg")
        if loading:
            loading.first().remove()
        self._populate_matchups()

        # Start background prefetch for secondary screens
        self.run_worker(self._prefetch_background, group="prefetch", exclusive=True)

    async def _prefetch_background(self) -> None:
        """Prefetch expensive data while user browses the scoreboard."""
        if not self.league:
            return

        try:
            cache = self.app.shared_cache

            # 1. SGP setup (benefits Roster Analysis, Free Agents, Watchlist)
            await cache.ensure_loaded(self.api, self.league, self.categories)

            # 2. Statcast cache warm-up (benefits Roster Analysis, Watchlist)
            from gkl.statcast import _ensure_cache
            await asyncio.to_thread(_ensure_cache)

            # 3. Weekly team stats (benefits Roto Standings, H2H Simulator)
            weeks = list(range(1, self.league.current_week + 1))
            await cache.prefetch_weeks(self.api, self.league.league_key, weeks)
        except Exception:
            pass

    def _is_future_week(self) -> bool:
        if self._viewing_week is None:
            return False
        return self._viewing_week > self.league.current_week

    def _project_weekly_stats(self, matchups: list[Matchup]) -> None:
        """Replace empty future-week stats with projections from season averages.

        Also computes roto ranks and H2H records for display.
        """
        season_teams = self.api.get_team_season_stats(self.league.league_key)
        season_by_key = {t.team_key: t for t in season_teams}
        weeks_played = max(self.league.current_week - 1, 1)

        scored_cats = [c for c in self.categories if not c.is_only_display]

        for m in matchups:
            for team in (m.team_a, m.team_b):
                season = season_by_key.get(team.team_key)
                if not season:
                    continue
                for cat in scored_cats:
                    season_val = season.stats.get(cat.stat_id, "")
                    if not season_val or season_val in ("-", "/"):
                        continue
                    try:
                        val_f = float(season_val)
                    except (ValueError, TypeError):
                        continue
                    if cat.stat_id in RATE_STATS:
                        team.stats[cat.stat_id] = season_val
                    else:
                        projected = val_f / weeks_played
                        if projected == int(projected):
                            team.stats[cat.stat_id] = str(int(projected))
                        else:
                            team.stats[cat.stat_id] = f"{projected:.1f}"

        # Compute roto ranks from season stats
        roto_results = compute_roto(season_teams, scored_cats)
        self._roto_rank = {e["team_key"]: r for r, e in enumerate(roto_results, 1)}

        # Compute H2H records from past matchups
        self._h2h_record: dict[str, dict] = {}
        cache = self.app.shared_cache
        for w in range(1, self.league.current_week + 1):
            week_matchups = cache.week_matchups.get(w)
            if not week_matchups:
                try:
                    week_matchups = self.api.get_scoreboard(self.league.league_key, week=w)
                    cache.week_matchups[w] = week_matchups
                except Exception:
                    continue
            for wm in week_matchups:
                if wm.status == "preevent":
                    continue
                for tk in (wm.team_a.team_key, wm.team_b.team_key):
                    if tk not in self._h2h_record:
                        self._h2h_record[tk] = {"wins": 0, "losses": 0, "ties": 0}
                pa, pb = wm.team_a.points, wm.team_b.points
                if pa > pb:
                    self._h2h_record[wm.team_a.team_key]["wins"] += 1
                    self._h2h_record[wm.team_b.team_key]["losses"] += 1
                elif pb > pa:
                    self._h2h_record[wm.team_a.team_key]["losses"] += 1
                    self._h2h_record[wm.team_b.team_key]["wins"] += 1
                else:
                    self._h2h_record[wm.team_a.team_key]["ties"] += 1
                    self._h2h_record[wm.team_b.team_key]["ties"] += 1

    async def _refresh_data(self) -> None:
        if not self.league:
            return
        week = self._viewing_week if self._viewing_week is not None else None
        self.matchups = self.api.get_scoreboard(self.league.league_key, week=week)
        if self._is_future_week():
            await asyncio.to_thread(self._project_weekly_stats, self.matchups)
        self._update_header()
        self._populate_matchups()
        if self._selected_idx is not None and self._selected_idx < len(self.matchups):
            await self._show_matchup_detail(self._selected_idx)

    def _update_header(self) -> None:
        if not self.league:
            return
        self.query_one("#board-header", Static).update(
            f" {self.league.name} "
        )
        display_week = self._viewing_week if self._viewing_week is not None else self.league.current_week
        sub = Text()
        sub.append(f"Week {display_week}", style="bold")
        if self._is_future_week():
            sub.append("  (projected)", style="bold italic #E8A735")
        sub.append(f"  (←→ week, [e] select)", style="dim")
        sub.append(f"  |  {self.league.season} Season  |  {self.league.num_teams} Teams")
        if len(getattr(self.app, "_leagues", [])) > 1:
            sub.append("  |  [L] Switch League", style="dim")
        self.query_one("#board-subheader", Static).update(sub)

    def _compute_projected_record(self, m: Matchup) -> tuple[int, int, int]:
        """Compute projected W/L/T for a matchup based on projected stats."""
        scored_cats = [c for c in self.categories if not c.is_only_display]
        a_wins = b_wins = ties = 0
        for cat in scored_cats:
            a_val = m.team_a.stats.get(cat.stat_id, "-")
            b_val = m.team_b.stats.get(cat.stat_id, "-")
            winner = who_wins(a_val, b_val, cat.sort_order)
            if winner == "a":
                a_wins += 1
            elif winner == "b":
                b_wins += 1
            else:
                ties += 1
        return a_wins, b_wins, ties

    def _populate_matchups(self) -> None:
        lv = self.query_one("#matchup-list", ListView)
        lv.clear()
        lv.display = True
        is_future = self._is_future_week()

        if is_future:
            # Column headers — fixed widths matching data rows
            hdr = Text()
            hdr.append(f"   {'':18}  {'':18}", style="dim")
            hdr.append("  proj   ", style="italic dim #E8A735")
            hdr.append(" roto  ", style="dim")
            hdr.append("   h2h    ", style="dim")
            hdr_item = ListItem(Label(hdr, classes="matchup-row"))
            hdr_item._matchup_index = None
            lv.mount(hdr_item)

        for i, m in enumerate(self.matchups):
            num = str(i + 1) if i < 9 else "0" if i == 9 else ""
            score_line = Text()
            score_line.append(f"{num:>2} ", style="bold dim")
            score_line.append(f"{m.team_a.name[:18]:<18}", style=f"bold {TEAM_A_COLOR}")
            score_line.append("  ")
            score_line.append(f"{m.team_b.name[:18]:<18}", style=f"bold {TEAM_B_COLOR}")
            if is_future:
                aw, bw, t = self._compute_projected_record(m)
                # Projected record — 9 char column
                score_line.append(f"{aw:>3}", style=f"bold {TEAM_A_COLOR}")
                score_line.append("-", style="dim")
                score_line.append(f"{bw}", style=f"bold {TEAM_B_COLOR}")
                score_line.append("-", style="dim")
                score_line.append(f"{t:<3}", style="dim")

                # Roto rank — 6 char column
                roto_a = self._roto_rank.get(m.team_a.team_key, "-")
                roto_b = self._roto_rank.get(m.team_b.team_key, "-")
                score_line.append(f"{roto_a:>3}", style=f"{TEAM_A_COLOR}")
                score_line.append("/", style="dim")
                score_line.append(f"{roto_b:<3}", style=f"{TEAM_B_COLOR}")

                # H2H record — 10 char column
                h2h_a = self._h2h_record.get(m.team_a.team_key, {})
                h2h_b = self._h2h_record.get(m.team_b.team_key, {})
                rec_a = f"{h2h_a.get('wins', 0)}-{h2h_a.get('losses', 0)}"
                rec_b = f"{h2h_b.get('wins', 0)}-{h2h_b.get('losses', 0)}"
                score_line.append(f"{rec_a:>5}", style=f"{TEAM_A_COLOR}")
                score_line.append("/", style="dim")
                score_line.append(f"{rec_b:<5}", style=f"{TEAM_B_COLOR}")
            else:
                score_line.append(f"{m.team_a.points:>5.0f}", style=f"{TEAM_A_COLOR}")
                score_line.append("  ")
                score_line.append(f"{m.team_b.points:>5.0f}", style=f"{TEAM_B_COLOR}")

            mgr_line = Text()
            mgr_line.append(f"   {m.team_a.manager[:18]:<23}", style="dim")
            mgr_line.append(f"{m.team_b.manager[:18]:<18}", style="dim")

            item = ListItem(
                Label(score_line, classes="matchup-row"),
                Label(mgr_line, classes="matchup-row"),
                Label("─" * 70, classes="matchup-divider"),
            )
            item._matchup_index = i
            lv.mount(item)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Load matchup detail + player stats when Enter is pressed."""
        idx = getattr(event.item, "_matchup_index", None)
        if idx is not None and idx < len(self.matchups):
            self._select_matchup(idx)

    def _select_matchup(self, idx: int) -> None:
        """Select a matchup by index and load its details."""
        if idx < 0 or idx >= len(self.matchups):
            return
        self._selected_idx = idx
        async def _update(i: int = idx) -> None:
            await self._show_matchup_detail(i)
            if self._is_future_week():
                await self._load_future_player_stats(i)
            else:
                await self._load_player_stats(i)
        self.run_worker(_update, group="matchup-detail", exclusive=True)

    def on_key(self, event) -> None:
        """Handle numeric keys 1-9,0 for quick matchup selection."""
        if event.character and event.character in "1234567890":
            idx = int(event.character) - 1
            if event.character == "0":
                idx = 9
            if 0 <= idx < len(self.matchups):
                self._select_matchup(idx)
                event.prevent_default()

    async def _show_matchup_detail(self, idx: int) -> None:
        """Populate the right pane with matchup stat comparison."""
        self._selected_idx = idx
        m = self.matchups[idx]
        a = m.team_a
        b = m.team_b

        container = self.query_one("#right-pane-inner", VerticalScroll)
        await container.remove_children()

        # Header
        header = Text()
        header.append(f" {a.name} ", style=f"bold {TEAM_A_COLOR}")
        header.append("  vs  ")
        header.append(f" {b.name} ", style=f"bold {TEAM_B_COLOR}")
        await container.mount(Static(header, classes="stat-header"))

        if self._is_future_week():
            score = Text()
            score.append(" Projected based on season stats", style="italic dim #E8A735")
            await container.mount(Static(score, classes="stat-score"))
        else:
            score = Text()
            score.append(f" {a.points:.0f} ", style=f"bold {TEAM_A_COLOR}")
            score.append(" - ")
            score.append(f" {b.points:.0f} ", style=f"bold {TEAM_B_COLOR}")
            await container.mount(Static(score, classes="stat-score"))

        scored_cats = [c for c in self.categories if not c.is_only_display]
        batting_cats = [c for c in scored_cats if c.position_type == "B"]
        pitching_cats = [c for c in scored_cats if c.position_type == "P"]

        a_wins = 0
        b_wins = 0
        ties = 0

        if batting_cats:
            await container.mount(Static(" BATTING ", classes="section-label"))
            bat_table = DataTable()
            await container.mount(bat_table)
            aw, bw, t = self._fill_stat_table(bat_table, batting_cats, a, b)
            a_wins += aw
            b_wins += bw
            ties += t

        if pitching_cats:
            await container.mount(Static(" PITCHING ", classes="section-label"))
            pitch_table = DataTable()
            await container.mount(pitch_table)
            aw, bw, t = self._fill_stat_table(pitch_table, pitching_cats, a, b)
            a_wins += aw
            b_wins += bw
            ties += t

        tally = Text()
        tally.append(f" {a.name[:15]}: {a_wins} ", style=f"bold {TEAM_A_COLOR}")
        tally.append(f"  Tied: {ties}  ", style="dim")
        tally.append(f" {b.name[:15]}: {b_wins} ", style=f"bold {TEAM_B_COLOR}")
        await container.mount(Static(tally, classes="stat-tally"))

    def _fill_stat_table(
        self, table: DataTable, cats: list[StatCategory],
        a: TeamStats, b: TeamStats,
    ) -> tuple[int, int, int]:
        table.cursor_type = "none"
        table.zebra_stripes = False
        a_label = Text(a.name[:14], style=f"bold {TEAM_A_COLOR}")
        b_label = Text(b.name[:14], style=f"bold {TEAM_B_COLOR}")
        table.add_columns("Stat", a_label, b_label)

        a_wins = b_wins = ties = 0
        for cat in cats:
            a_val = a.stats.get(cat.stat_id, "-")
            b_val = b.stats.get(cat.stat_id, "-")
            winner = who_wins(a_val, b_val, cat.sort_order)
            if winner == "a":
                bg = TEAM_A_BG
                a_wins += 1
            elif winner == "b":
                bg = TEAM_B_BG
                b_wins += 1
            else:
                bg = TIED_BG
                ties += 1
            table.add_row(
                Text(f" {cat.display_name}", style=f"bold on {bg}"),
                Text(f"{a_val} ", justify="right", style=f"on {bg}"),
                Text(f"{b_val} ", justify="right", style=f"on {bg}"),
            )
        return a_wins, b_wins, ties

    def _compute_matchup_dates(self, matchup: Matchup) -> list[str]:
        """Generate the list of dates within a matchup's week."""
        from datetime import date, timedelta
        try:
            start = date.fromisoformat(matchup.week_start)
            end = date.fromisoformat(matchup.week_end)
            # Don't go past today
            today = date.today()
            if end > today:
                end = today
            dates = []
            d = start
            while d <= end:
                dates.append(d.isoformat())
                d += timedelta(days=1)
            return dates
        except (ValueError, TypeError):
            return []

    def _update_player_view_header(self, matchup: Matchup) -> None:
        """Update the player view header with current view mode."""
        header = Text()
        if self._player_view == "weekly":
            header.append(f" WEEKLY ", style="bold on #4A7C59")
            header.append(f"  [d] Daily  [n] Season", style="dim")
            header.append(f"  |  Week {matchup.week}", style="bold")
        elif self._player_view == "daily":
            header.append(f"  [w] Weekly  ", style="dim")
            header.append(f" DAILY ", style="bold on #4A7C59")
            header.append(f"  [n] Season", style="dim")
            header.append(f"  |  {self._daily_date}", style="bold")
            if len(self._matchup_dates) > 1:
                header.append(f"  (<,> to cycle days)", style="dim")
        else:
            header.append(f"  [w] Weekly  [d] Daily  ", style="dim")
            header.append(f" SEASON ", style="bold on #4A7C59")
        self.query_one("#player-view-header", Static).update(header)

    async def _load_player_stats(self, idx: int) -> None:
        """Load player-level stats for both teams in a matchup."""
        if not self.league:
            return
        m = self.matchups[idx]
        week = m.week or self.league.current_week

        # Compute available dates for this matchup
        self._matchup_dates = self._compute_matchup_dates(m)
        if self._daily_date not in self._matchup_dates and self._matchup_dates:
            self._daily_date = self._matchup_dates[-1]  # default to most recent

        # Fetch based on current view
        if self._player_view == "daily" and self._daily_date:
            players_a = self.api.get_roster_stats_daily(
                m.team_a.team_key, week, self._daily_date)
            players_b = self.api.get_roster_stats_daily(
                m.team_b.team_key, week, self._daily_date)
        elif self._player_view == "season":
            players_a = self.api.get_roster_stats_season(m.team_a.team_key, week)
            players_b = self.api.get_roster_stats_season(m.team_b.team_key, week)
        else:
            players_a = self.api.get_roster_stats(m.team_a.team_key, week)
            players_b = self.api.get_roster_stats(m.team_b.team_key, week)

        self.query_one("#bottom-pane").display = True
        self._update_player_view_header(m)

        batting_cats, bat_unscored = build_stat_columns(self.categories, "B")
        pitching_cats, pitch_unscored = build_stat_columns(self.categories, "P")

        batting_positions = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
                             "OF", "Util", "DH", "IF", "BN"}

        for container_id, team, players, color in [
            ("#player-scroll-a", m.team_a, players_a, TEAM_A_COLOR),
            ("#player-scroll-b", m.team_b, players_b, TEAM_B_COLOR),
        ]:
            container = self.query_one(container_id, VerticalScroll)
            await container.remove_children()

            await container.mount(
                Static(Text(f" {team.name} ", style=f"bold {color}"),
                       classes="team-roster-label")
            )

            batters = [p for p in players if
                       any(pos in batting_positions for pos in p.position.split(","))]
            pitchers = [p for p in players if p not in batters]

            if batters:
                await container.mount(
                    Static(" Batters", classes="roster-section-label"))
                bat_table = DataTable()
                await container.mount(bat_table)
                self._fill_player_table(bat_table, batters, batting_cats, bat_unscored)

            if pitchers:
                await container.mount(
                    Static(" Pitchers", classes="roster-section-label"))
                pitch_table = DataTable()
                await container.mount(pitch_table)
                self._fill_player_table(pitch_table, pitchers, pitching_cats, pitch_unscored)

    async def _load_future_player_stats(self, idx: int) -> None:
        """Load season-long player stats for both teams in a future week matchup."""
        if not self.league:
            return
        m = self.matchups[idx]
        week = self.league.current_week

        players_a = self.api.get_roster_stats_season(m.team_a.team_key, week)
        players_b = self.api.get_roster_stats_season(m.team_b.team_key, week)

        self.query_one("#bottom-pane").display = True

        # Update player view header to indicate season stats
        header = Text()
        header.append(" Season Stats ", style="bold")
        header.append(" (projected week — showing season totals)", style="dim")
        self.query_one("#player-view-header", Static).update(header)

        batting_cats, bat_unscored = build_stat_columns(self.categories, "B")
        pitching_cats, pitch_unscored = build_stat_columns(self.categories, "P")

        batting_positions = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
                             "OF", "Util", "DH", "IF", "BN"}

        for container_id, team, players, color in [
            ("#player-scroll-a", m.team_a, players_a, TEAM_A_COLOR),
            ("#player-scroll-b", m.team_b, players_b, TEAM_B_COLOR),
        ]:
            container = self.query_one(container_id, VerticalScroll)
            await container.remove_children()

            await container.mount(
                Static(Text(f" {team.name} ", style=f"bold {color}"),
                       classes="team-roster-label")
            )

            batters = [p for p in players if
                       any(pos in batting_positions for pos in p.position.split(","))]
            pitchers = [p for p in players if p not in batters]

            if batters:
                await container.mount(
                    Static(" Batters", classes="roster-section-label"))
                bat_table = DataTable()
                await container.mount(bat_table)
                self._fill_player_table(bat_table, batters, batting_cats, bat_unscored)

            if pitchers:
                await container.mount(
                    Static(" Pitchers", classes="roster-section-label"))
                pitch_table = DataTable()
                await container.mount(pitch_table)
                self._fill_player_table(pitch_table, pitchers, pitching_cats, pitch_unscored)

    def _fill_player_table(
        self, table: DataTable, players: list[PlayerStats],
        cats: list[StatCategory],
        unscored_ids: set[str] | None = None,
    ) -> None:
        table.cursor_type = "row"
        table.zebra_stripes = True
        table._players = players  # type: ignore[attr-defined]
        unscored = unscored_ids or set()

        cols: list[str | Text] = ["Player", "Pos"]
        for cat in cats:
            if cat.stat_id in unscored:
                cols.append(Text(f"({cat.display_name})", style="dim italic"))
            else:
                cols.append(cat.display_name)
        table.add_columns(*cols)

        for p in players:
            row: list[str | Text] = [
                Text(p.name[:18], style="bold"),
                Text(p.position, style="dim"),
            ]
            for cat in cats:
                val = get_stat_value(p.stats, cat.stat_id, cat.display_name)
                style = "dim italic" if cat.stat_id in unscored else ""
                row.append(Text(str(val), style=style, justify="right"))
            table.add_row(*row)

    def _reload_players(self) -> None:
        if self._selected_idx is not None and self._selected_idx < len(self.matchups):
            async def _do(i: int = self._selected_idx) -> None:
                await self._load_player_stats(i)
            self.run_worker(_do, group="player-reload", exclusive=True)

    def action_view_weekly(self) -> None:
        self._player_view = "weekly"
        self._reload_players()

    def action_view_daily(self) -> None:
        self._player_view = "daily"
        self._reload_players()

    def action_view_season(self) -> None:
        self._player_view = "season"
        self._reload_players()

    def action_prev_date(self) -> None:
        if self._player_view != "daily" or not self._matchup_dates:
            return
        idx = self._matchup_dates.index(self._daily_date) if self._daily_date in self._matchup_dates else 0
        if idx > 0:
            self._daily_date = self._matchup_dates[idx - 1]
            self._reload_players()

    def action_next_date(self) -> None:
        if self._player_view != "daily" or not self._matchup_dates:
            return
        idx = self._matchup_dates.index(self._daily_date) if self._daily_date in self._matchup_dates else 0
        if idx < len(self._matchup_dates) - 1:
            self._daily_date = self._matchup_dates[idx + 1]
            self._reload_players()

    def action_refresh(self) -> None:
        if self.league:
            self.run_worker(self._refresh_data)

    def action_player_detail(self) -> None:
        try:
            focused = self.query("DataTable:focus")
            if not focused:
                return
            table = focused.first()
        except Exception:
            return
        players = getattr(table, "_players", [])
        row_idx = table.cursor_row
        if row_idx < 0 or row_idx >= len(players):
            return
        p = players[row_idx]
        cache = self.app.shared_cache
        self.app.push_screen(PlayerDetailScreen(
            p.name, p.position, p.team_abbr,
            categories=self.categories,
            all_teams=cache.all_teams if cache.is_loaded else None,
            replacement_by_pos=cache.replacement_by_pos if cache.is_loaded else None,
        ))

    def action_standings(self) -> None:
        if self.league:
            self.app.push_screen(
                LeagueStandingsScreen(self.api, self.league, self.categories)
            )

    def action_h2h_sim(self) -> None:
        if self.league:
            self.app.push_screen(
                H2HSimulatorScreen(self.api, self.league, self.categories)
            )

    def action_roster(self) -> None:
        if self.league:
            self.app.push_screen(
                RosterAnalysisScreen(self.api, self.league, self.categories)
            )

    def action_free_agents(self) -> None:
        if self.league:
            self.app.push_screen(
                FreeAgentScreen(self.api, self.league, self.categories)
            )

    def action_transactions(self) -> None:
        if self.league:
            self.app.push_screen(
                TransactionsScreen(self.api, self.league, self.categories)
            )

    def action_player_explorer(self) -> None:
        if self.league:
            self.app.push_screen(
                PlayerExplorerScreen(self.api, self.league, self.categories)
            )

    def action_watchlist(self) -> None:
        if self.league:
            self.app.push_screen(
                WatchlistScreen(self.api, self.league, self.categories)
            )

    def action_ask_skipper(self) -> None:
        if self.league:
            self.app.push_screen(
                AskSkipperScreen(self.api, self.league, self.categories)
            )

    def action_trade_analyzer(self) -> None:
        if self.league:
            self.app.push_screen(
                TradeAnalyzerScreen(self.api, self.league, self.categories)
            )

    def action_mlb_scores(self) -> None:
        self.app.push_screen(MLBScoreboardScreen(self.api, self.league))

    def action_settings(self) -> None:
        self.app.push_screen(SettingsScreen(self.api, self.league))

    def _load_week(self, week: int) -> None:
        """Switch to viewing a specific week's matchups."""
        self._viewing_week = week
        self._selected_idx = None
        self.query_one("#bottom-pane").display = False
        self._update_header()
        self.run_worker(self._refresh_data)

    def action_prev_week(self) -> None:
        if not self.league:
            return
        current = self._viewing_week if self._viewing_week is not None else self.league.current_week
        if current > 1:
            self._load_week(current - 1)

    def action_next_week(self) -> None:
        if not self.league:
            return
        current = self._viewing_week if self._viewing_week is not None else self.league.current_week
        if current < self.league.end_week:
            self._load_week(current + 1)

    def action_select_week(self) -> None:
        if not self.league:
            return
        current = self._viewing_week if self._viewing_week is not None else self.league.current_week
        self.app.push_screen(
            WeekSelectModal(self.league.end_week, current, league_week=self.league.current_week),
            callback=self._on_week_selected,
        )

    def _on_week_selected(self, week: int | None) -> None:
        if week is not None:
            self._load_week(week)

    def action_switch_league(self) -> None:
        leagues = getattr(self.app, "_leagues", [])
        if len(leagues) > 1:
            self.app.pop_screen()
            self.app._show_league_picker(leagues, self.league.league_key)
        else:
            self.notify("Only one league available", severity="information")

    def action_quit(self) -> None:
        if is_web_mode():
            self.app.open_url("/logout")
        else:
            self.app.exit()


# --- App ---


class GklApp(App):
    TITLE = "GKL — Fantasy Baseball Command Center"
    CSS = """
    Screen {
        background: $background;
    }
    """

    def __init__(self, api: YahooFantasyAPI) -> None:
        super().__init__()
        self.api = api
        self.store = RosterDataStore()
        self._leagues: list[League] = []
        self.shared_cache = SharedDataCache()

    def on_mount(self) -> None:
        self.register_theme(BASEBALL_THEME)
        self.theme = "baseball"
        self.run_worker(self._init_league_selection)
        if not is_web_mode():
            self.run_worker(self._check_for_updates)

    async def _check_for_updates(self) -> None:
        cleanup_old_binary()
        info = check_for_update()
        if info is None:
            return
        should_update = await self.app.push_screen_wait(UpdateModal(info))
        if not should_update:
            return
        try:
            self.notify("Downloading update…")
            new_binary = download_update(info.asset_url)
            apply_update(new_binary)
            self.notify(
                f"Updated to v{info.latest_version}. Restart the app to use it.",
                severity="information",
                timeout=10,
            )
        except Exception:
            self.notify("Update failed. Try again later.", severity="error")

    async def _init_league_selection(self) -> None:
        leagues = self.api.get_user_leagues()
        self._leagues = leagues
        if not leagues:
            self.notify("No MLB leagues found.", severity="error")
            return
        if len(leagues) == 1:
            self.push_screen(ScoreboardScreen(self.api, leagues[0]))
        else:
            last_key = self.store.get_pref("last_league_key")
            self._show_league_picker(leagues, last_key)

    def _show_league_picker(
        self, leagues: list[League], last_key: str | None
    ) -> None:
        def on_league_selected(league: League) -> None:
            self.store.set_pref("last_league_key", league.league_key)
            self.push_screen(ScoreboardScreen(self.api, league))

        self.push_screen(
            LeagueSelectScreen(leagues, last_key), callback=on_league_selected
        )


def main() -> None:
    saved = load_credentials()
    if saved:
        client_id, client_secret = saved
    else:
        if is_web_mode():
            print(
                "GKL_YAHOO_CLIENT_ID and GKL_YAHOO_CLIENT_SECRET must be set "
                "in web mode.",
                file=sys.stderr,
            )
            sys.exit(1)

        client_id = os.environ.get("YAHOO_CLIENT_ID")
        client_secret = os.environ.get("YAHOO_CLIENT_SECRET")

        if not client_id or not client_secret:
            print(
                "No saved credentials found.\n"
                "Create an app at https://developer.yahoo.com/apps/create/\n"
                "  - Application Name: Choose a name for your application\n"
                "  - Description: A terminal application for managing my fantasy baseball roster\n"
                "  - Homepage URL: Your personal website if you have one, otherwise choose\n"
                "    a website. It must be formatted as https://www.yourwebsitename.com\n"
                "  - Redirect URI: https://localhost:8080\n"
                "  - OAuth Client Type: Confidential Client\n"
                "  - API Permissions: Fantasy Sports (Read)\n",
                file=sys.stderr,
            )
            client_id = input("Yahoo Client ID: ").strip()
            client_secret = input("Yahoo Client Secret: ").strip()
            if not client_id or not client_secret:
                print("Client ID and Secret are required.", file=sys.stderr)
                sys.exit(1)

        save_credentials(client_id, client_secret)

    auth = YahooAuth(client_id=client_id, client_secret=client_secret)
    token = auth.get_token()
    if not is_web_mode():
        print("Authenticated successfully. Launching app...\n")

    api = YahooFantasyAPI(auth)
    app = GklApp(api)
    app.run()


if __name__ == "__main__":
    main()
