"""GKL CLI — Fantasy Baseball Command Center."""

from __future__ import annotations

import asyncio
import os
import sys

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
from gkl.yahoo_auth import YahooAuth, load_credentials, save_credentials
from gkl.stats import (
    who_wins, simulate_h2h, compute_power_rankings, aggregate_h2h_season,
    H2HResult, TeamH2HSummary, SGPCalculator,
    build_stat_columns, get_stat_value,
)
from gkl.datastore import RosterDataStore
from gkl.mlb_api import MLBGame, get_mlb_scoreboard, get_player_ages, get_player_games
from gkl.statcast import (
    get_batter_statcast, get_pitcher_statcast, lookup_mlbam_id,
    StatcastBatter, StatcastPitcher,
)

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


def _compute_roto(
    teams: list[TeamStats],
    categories: list[StatCategory],
) -> list[dict]:
    """Compute roto points for each team across the given categories."""
    results: list[dict] = []
    for t in teams:
        results.append({
            "name": t.name,
            "manager": t.manager,
            "team_key": t.team_key,
            "total": 0.0,
        })

    for cat in categories:
        vals: list[tuple[int, float]] = []
        for i, t in enumerate(teams):
            raw = t.stats.get(cat.stat_id, "0")
            results[i][f"raw_{cat.stat_id}"] = raw
            try:
                vals.append((i, float(raw)))
            except (ValueError, TypeError):
                vals.append((i, 0.0))

        higher_is_better = cat.sort_order == "1"
        vals.sort(key=lambda x: x[1], reverse=not higher_is_better)

        rank = 1
        i = 0
        while i < len(vals):
            j = i
            while j < len(vals) and vals[j][1] == vals[i][1]:
                j += 1
            avg_rank = sum(range(rank, rank + j - i)) / (j - i)
            for k in range(i, j):
                idx = vals[k][0]
                results[idx][cat.stat_id] = avg_rank
                results[idx]["total"] += avg_rank
            rank += j - i
            i = j

    results.sort(key=lambda r: r["total"], reverse=True)
    return results


# --- Week Range Modal ---


class WeekRangeModal(Screen):
    """Modal for selecting a start and end week range."""
    BINDINGS = [("escape", "cancel", "Cancel")]
    CSS = """
    WeekRangeModal {
        align: center middle;
    }
    #wr-container {
        width: 40;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #wr-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
        margin-bottom: 1;
    }
    .wr-label {
        height: 1;
        margin-top: 1;
    }
    #wr-start, #wr-end {
        width: 100%;
    }
    #wr-submit {
        margin-top: 1;
        width: 100%;
        content-align: center middle;
    }
    """

    def __init__(self, max_week: int, current_start: int, current_end: int) -> None:
        super().__init__()
        self.max_week = max_week
        self.current_start = current_start
        self.current_end = current_end

    def compose(self) -> ComposeResult:
        from textual.widgets import Button
        with Vertical(id="wr-container"):
            yield Static("Set Week Range", id="wr-title")
            yield Static(f"Start week (1-{self.max_week}):", classes="wr-label")
            yield Input(str(self.current_start), id="wr-start", type="integer")
            yield Static(f"End week (1-{self.max_week}):", classes="wr-label")
            yield Input(str(self.current_end), id="wr-end", type="integer")
            yield Button("Apply", id="wr-submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#wr-start", Input).focus()

    def on_button_pressed(self, event) -> None:
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wr-start":
            self.query_one("#wr-end", Input).focus()
        else:
            self._submit()

    def _submit(self) -> None:
        try:
            start = int(self.query_one("#wr-start", Input).value)
            end = int(self.query_one("#wr-end", Input).value)
        except ValueError:
            return
        start = max(1, min(start, self.max_week))
        end = max(1, min(end, self.max_week))
        if start > end:
            start, end = end, start
        self.dismiss((start, end))

    def action_cancel(self) -> None:
        self.dismiss(None)


# --- League Standings Screen ---


class LeagueStandingsScreen(Screen):
    """Combined league standings: H2H record on top, full roto table on bottom."""
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("1", "show_overall", "Overall"), ("2", "show_batting", "Batting"),
                ("3", "show_pitching", "Pitching"),
                ("w", "set_weeks", "Set Weeks")]
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
        self._week_end = max(1, league.current_week)
        self._max_week = max(1, league.current_week)

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
        yield Footer()

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

        # Compute actual H2H records from matchup results
        num_scored_cats = len([c for c in self.categories if not c.is_only_display])
        h2h_records: dict[str, dict] = {}
        for w in all_weeks:
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
                cat_ties = int(num_scored_cats - pa - pb)
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
                # Category-level totals
                h2h_records[m.team_a.team_key]["cat_wins"] += int(pa)
                h2h_records[m.team_a.team_key]["cat_losses"] += int(pb)
                h2h_records[m.team_a.team_key]["cat_ties"] += cat_ties
                h2h_records[m.team_b.team_key]["cat_wins"] += int(pb)
                h2h_records[m.team_b.team_key]["cat_losses"] += int(pa)
                h2h_records[m.team_b.team_key]["cat_ties"] += cat_ties

        # Compute roto rank summaries for the H2H table
        season_teams = await asyncio.to_thread(
            self.api.get_team_season_stats, self.league.league_key)

        scored = [c for c in self.categories if not c.is_only_display]
        bat_scored = [c for c in scored if c.position_type == "B"]
        pitch_scored = [c for c in scored if c.position_type == "P"]

        overall_roto = _compute_roto(season_teams, scored)
        batting_roto = _compute_roto(season_teams, bat_scored)
        pitching_roto = _compute_roto(season_teams, pitch_scored)

        overall_rank = {e["team_key"]: r for r, e in enumerate(overall_roto, 1)}
        batting_rank = {e["team_key"]: r for r, e in enumerate(batting_roto, 1)}
        pitching_rank = {e["team_key"]: r for r, e in enumerate(pitching_roto, 1)}

        # Build combined data sorted by H2H standings
        team_info = {t.team_key: t for t in season_teams}
        standings = []
        for team_key, rec in h2h_records.items():
            total = rec["wins"] + rec["losses"] + rec["ties"]
            pct = rec["wins"] / total if total > 0 else 0.0
            team = team_info.get(team_key)
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
                "overall_rank": overall_rank.get(team_key, 0),
                "batting_rank": batting_rank.get(team_key, 0),
                "pitching_rank": pitching_rank.get(team_key, 0),
            })
        standings.sort(key=lambda s: (s["win_pct"], s["wins"]), reverse=True)

        # Remove loading indicator, show both panels
        loading = self.query("#ls-loading")
        if loading:
            loading.first().remove()
        self.query_one("#ls-top").display = True
        self.query_one("#ls-bottom").display = True

        # Render H2H standings table
        self.query_one("#ls-top-label", Static).update(" H2H Standings ")
        table = self.query_one("#ls-table", DataTable)
        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Team", "Manager", "H2H Record", "Win %",
                          "Total Record", "Total Win %", "Roto Overall",
                          "Roto Batting", "Roto Pitching")

        num_teams = len(standings)
        for rank, s in enumerate(standings, 1):
            pct = s["win_pct"]
            pct_style = "bold green" if pct >= 0.6 else "bold red" if pct < 0.4 else ""

            cat_total = s["cat_wins"] + s["cat_losses"] + s["cat_ties"]
            cat_pct = s["cat_wins"] / cat_total if cat_total > 0 else 0.0
            cat_pct_style = "bold green" if cat_pct >= 0.6 else "bold red" if cat_pct < 0.4 else ""

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
                Text(f"{s['wins']}-{s['losses']}-{s['ties']}", justify="center"),
                Text(f"{pct:.1%}", style=pct_style, justify="right"),
                Text(f"{s['cat_wins']}-{s['cat_losses']}-{s['cat_ties']}",
                     justify="center"),
                Text(f"{cat_pct:.1%}", style=cat_pct_style, justify="right"),
                Text(str(s["overall_rank"]), style=_roto_style(s["overall_rank"]),
                     justify="center"),
                Text(str(s["batting_rank"]), style=_roto_style(s["batting_rank"]),
                     justify="center"),
                Text(str(s["pitching_rank"]), style=_roto_style(s["pitching_rank"]),
                     justify="center"),
            )

        # Initial roto table load (full season)
        self._roto_teams = season_teams
        self._render_roto_table()

    async def _fetch_roto(self) -> None:
        """Fetch roto stats for the selected week range and re-render."""
        from gkl.stats import aggregate_weekly_stats

        cache = self.app.shared_cache
        needed = list(range(self._week_start, self._week_end + 1))
        await cache.prefetch_weeks(self.api, self.league.league_key, needed)

        if self._week_start == 1 and self._week_end == self._max_week:
            self._roto_teams = await asyncio.to_thread(
                self.api.get_team_season_stats, self.league.league_key)
        else:
            weekly_data = [cache.week_team_stats[w] for w in needed]
            self._roto_teams = aggregate_weekly_stats(weekly_data, self.categories)

        self._render_roto_table()

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
        self.run_worker(self._fetch_roto, group="roto-fetch", exclusive=True)

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
        self._week = league.current_week
        self._max_week = league.current_week
        self._season_mode = False
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
        yield Footer()

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

        if self._season_mode:
            await self._render_season(teams, selected_key, selected_team)
        else:
            await self._render_week(teams, selected_key, selected_team)

    @staticmethod
    def _week_is_preevent(matchups: list[Matchup]) -> bool:
        """Check if all matchups for a week are preevent (no games started)."""
        return bool(matchups) and all(m.status == "preevent" for m in matchups)

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
        ctrl = Text()
        ctrl.append(f"SEASON", style="bold")
        ctrl.append(f"  (←→ week)  |  ", style="dim")
        ctrl.append(f"Manager: {selected_team.name}", style=f"bold {TEAM_A_COLOR}")
        ctrl.append(f"  (Enter on rankings to select)  |  [a] Week View", style="dim")
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

        # Aggregate across all weeks (skip preevent weeks)
        all_rankings: list[list[TeamH2HSummary]] = []
        all_h2h: dict[str, dict[str, H2HResult]] = {}
        for w in all_weeks:
            w_matchups = cache.week_matchups[w]
            if self._week_is_preevent(w_matchups):
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
            f" Season H2H — {selected_team.name} — Matchup W/L vs Each Opponent "
        )
        self.query_one("#h2h-bottom-label", Static).update(
            " Season Power Rankings — All Weeks Combined "
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
        if self._season_mode:
            self._season_mode = False
        if self._week > 1:
            self._week -= 1
            self.run_worker(self._render_all, group="h2h-load", exclusive=True)

    def action_next_week(self) -> None:
        if self._season_mode:
            self._season_mode = False
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
        self._season_mode = not self._season_mode
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

    def __init__(self, max_week: int, current_week: int) -> None:
        super().__init__()
        self.max_week = max_week
        self.current_week = current_week

    def compose(self) -> ComposeResult:
        with Vertical(id="week-select-container"):
            yield Static("Select Week", id="week-select-title")
            yield ListView(id="week-select-list")

    def on_mount(self) -> None:
        lv = self.query_one("#week-select-list", ListView)
        for w in range(1, self.max_week + 1):
            label = f"Week {w}"
            if w == self.current_week:
                label += "  (current)"
            item = ListItem(Label(label))
            item._week = w
            lv.mount(item)
        lv.index = self.current_week - 1

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


class RosterAnalysisScreen(Screen):
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("m", "cycle_team", "Next Team"),
                ("1", "view_season", "Season"), ("2", "view_l14", "L14"),
                ("3", "view_l30", "L30"),
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
        yield Footer()

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


class FreeAgentScreen(Screen):
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
        yield Footer()

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


class WatchlistScreen(Screen):
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
        yield Footer()

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

        # Get team list for selection
        teams = self.api.get_team_season_stats(self.league.league_key)
        self._team_names = {t.team_key: t.name for t in teams}
        options = [(t.team_key, t.name) for t in teams]
        self.app.push_screen(
            TeamSelectModal(options),
            callback=self._on_team_selected,
        )

    def _on_team_selected(self, team_key: str | None) -> None:
        if team_key is None or not hasattr(self, "_compare_player"):
            return
        p = self._compare_player
        team_name = self._team_names.get(team_key, team_key)
        self.app.push_screen(
            ComparisonScreen(
                self.api, self.league, self.categories,
                p, team_key, team_name, self._sgp_calc,
            )
        )


class ComparisonScreen(Screen):
    """Compare a watchlisted player against position-matched roster players."""
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
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
        self._team_key = team_key
        self._team_name = team_name
        self._sgp_calc = sgp_calc

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
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cmp-header", Static).update(
            f" {self.league.name} — Player Comparison "
        )
        sub = Text()
        sub.append(f"\n Comparing ", style="dim")
        sub.append(f"{self._wl_player.name}", style="bold #E8A735")
        sub.append(f" ({self._wl_player.position})", style="dim")
        sub.append(f" vs ", style="dim")
        sub.append(f"{self._team_name}\n", style="bold")
        self.query_one("#cmp-subheader", Static).update(sub)
        self.run_worker(self._load_comparison)

    def action_go_back(self) -> None:
        self.app.pop_screen()

    async def _load_comparison(self) -> None:
        try:
            self.query_one("#cmp-loading-status", Static).update(
                "Loading roster for comparison..."
            )
            self.query_one("#cmp-loading-container").display = True
            self.query_one("#cmp-scroll").display = False
        except Exception:
            pass

        # Fetch the team's roster
        roster = await asyncio.to_thread(
            self.api.get_roster_stats_season,
            self._team_key, self.league.current_week,
        )

        # Position-match: find roster players with overlapping positions
        wl_positions = {pos.strip() for pos in self._wl_player.position.split(",")}
        matched = []
        for rp in roster:
            rp_positions = {pos.strip() for pos in rp.position.split(",")}
            if wl_positions & rp_positions:
                matched.append(rp)

        # Fetch Statcast for all players in comparison
        try:
            self.query_one("#cmp-loading-status", Static).update(
                "Loading Statcast data..."
            )
        except Exception:
            pass
        all_players = [self._wl_player] + matched
        batter_sc: dict[str, StatcastBatter] = {}
        pitcher_sc: dict[str, StatcastPitcher] = {}
        for p in all_players:
            mlbam_id = await asyncio.to_thread(lookup_mlbam_id, p.name)
            if mlbam_id is not None:
                if self._is_batter:
                    sc = await asyncio.to_thread(get_batter_statcast, mlbam_id)
                    if sc is not None:
                        batter_sc[p.name] = sc
                else:
                    sc = await asyncio.to_thread(get_pitcher_statcast, mlbam_id)
                    if sc is not None:
                        pitcher_sc[p.name] = sc

        # Build comparison table
        scroll = self.query_one("#cmp-scroll", VerticalScroll)
        await scroll.remove_children()

        if not matched:
            await scroll.mount(Static(
                "  No position-matched players found on this roster.\n",
            ))
        else:
            table = DataTable(classes="cmp-table")
            await scroll.mount(table)
            if self._is_batter:
                self._render_comparison(table, matched, batter_sc=batter_sc)
            else:
                self._render_comparison(table, matched, pitcher_sc=pitcher_sc)

        try:
            self.query_one("#cmp-loading-container").display = False
            self.query_one("#cmp-scroll").display = True
        except Exception:
            pass

    def _render_comparison(
        self, table: DataTable, roster_players: list[PlayerStats],
        batter_sc: dict[str, StatcastBatter] | None = None,
        pitcher_sc: dict[str, StatcastPitcher] | None = None,
    ) -> None:
        scored = [c for c in self.categories if not c.is_only_display]
        is_batter = self._is_batter
        cats = [c for c in scored if c.position_type == ("B" if is_batter else "P")]

        table.clear(columns=True)
        table.cursor_type = "row"
        table.zebra_stripes = True

        cols = ["Player".ljust(20), "Pos".ljust(15), "Team", "SGP"]
        for cat in cats:
            cols.append(cat.display_name)
        cols.append("│")
        if is_batter:
            sc_cols = ["EV", "MaxEV", "LA", "Barrel%", "HardHit%",
                       "K%", "BB%", "Whiff%", "xBA", "xSLG", "xwOBA"]
        else:
            sc_cols = ["EV Alw", "Barrel%", "HardHit%",
                       "xBA", "xSLG", "xwOBA", "xERA",
                       "K%p", "BB%p", "Whiff%p"]
        cols.extend(sc_cols)
        table.add_columns(*cols)

        def _f(v, fmt=".1f"):
            return f"{v:{fmt}}" if v is not None else "-"
        def _rate(v):
            return f"{v:.1f}" if v is not None else "-"

        def _get_sc_vals(name: str) -> list[str]:
            """Get Statcast values as strings for a player."""
            if is_batter and batter_sc:
                sc = batter_sc.get(name)
                if sc:
                    return [_f(sc.avg_exit_velo), _f(sc.max_exit_velo),
                            _f(sc.avg_launch_angle), _f(sc.barrel_pct),
                            _f(sc.hard_hit_pct), _rate(sc.k_pct),
                            _rate(sc.bb_pct), _rate(sc.whiff_pct),
                            _f(sc.xba, ".3f"), _f(sc.xslg, ".3f"),
                            _f(sc.xwoba, ".3f")]
            elif not is_batter and pitcher_sc:
                sc = pitcher_sc.get(name)
                if sc:
                    return [_f(sc.avg_exit_velo), _f(sc.barrel_pct),
                            _f(sc.hard_hit_pct), _f(sc.xba, ".3f"),
                            _f(sc.xslg, ".3f"), _f(sc.xwoba, ".3f"),
                            _f(sc.xera, ".2f"), _rate(sc.k_pct),
                            _rate(sc.bb_pct), _rate(sc.whiff_pct)]
            return ["-"] * len(sc_cols)

        # Watchlist player row (highlighted)
        wl_sgp = self._sgp_calc.player_sgp(self._wl_player) if self._sgp_calc else None
        wl_row: list[Text] = [
            Text(("★ " + self._wl_player.name)[:20].ljust(20), style="bold #E8A735"),
            Text(self._wl_player.position.ljust(15), style="dim"),
            Text(self._wl_player.team_abbr, style="dim"),
            Text(f"{wl_sgp:+.1f}" if wl_sgp is not None else "N/A",
                 style="bold #E8A735", justify="right"),
        ]
        for cat in cats:
            wl_row.append(Text(
                self._wl_player.stats.get(cat.stat_id, "-"),
                justify="right", style="#E8A735",
            ))
        wl_row.append(Text("│", style="dim"))
        wl_sc_vals = _get_sc_vals(self._wl_player.name)
        for v in wl_sc_vals:
            wl_row.append(Text(v, justify="right", style="#E8A735"))
        table.add_row(*wl_row)

        # For each roster player: their stats + delta row
        for rp in roster_players:
            rp_sgp = self._sgp_calc.player_sgp(rp) if self._sgp_calc else None

            # Roster player row
            rp_row: list[Text] = [
                Text(rp.name[:20].ljust(20), style="bold"),
                Text(rp.position.ljust(15), style="dim"),
                Text(rp.team_abbr, style="dim"),
                Text(f"{rp_sgp:+.1f}" if rp_sgp is not None else "N/A",
                     justify="right"),
            ]
            for cat in cats:
                rp_row.append(Text(rp.stats.get(cat.stat_id, "-"), justify="right"))
            rp_row.append(Text("│", style="dim"))
            rp_sc_vals = _get_sc_vals(rp.name)
            for v in rp_sc_vals:
                rp_row.append(Text(v, justify="right"))
            table.add_row(*rp_row)

            # Delta row
            delta_row: list[Text] = [
                Text("  DELTA".ljust(20), style="italic dim"),
                Text(""),
                Text(""),
                Text("", justify="right"),
            ]

            # SGP delta
            if wl_sgp is not None and rp_sgp is not None:
                sgp_delta = wl_sgp - rp_sgp
                delta_style = "bold green" if sgp_delta > 0 else "bold red" if sgp_delta < 0 else "dim"
                delta_row[3] = Text(f"{sgp_delta:+.1f}", style=delta_style, justify="right")

            for cat in cats:
                wl_val = self._wl_player.stats.get(cat.stat_id, "")
                rp_val = rp.stats.get(cat.stat_id, "")
                delta_text = self._compute_delta(wl_val, rp_val, cat)
                delta_row.append(delta_text)

            # Statcast deltas
            delta_row.append(Text("│", style="dim"))
            # For batters: higher EV/MaxEV/LA/Barrel/HardHit/BB%/xBA/xSLG/xwOBA = better
            #              lower K%/Whiff% = better
            # For pitchers: lower EV/Barrel/HardHit/xBA/xSLG/xwOBA/xERA = better
            #               higher K%/Whiff% = better, lower BB% = better
            if is_batter:
                sc_higher_better = [True, True, True, True, True, False, True, False,
                                    True, True, True]
            else:
                sc_higher_better = [False, False, False, False, False, False, False,
                                    True, False, True]

            for i, sc_col in enumerate(sc_cols):
                wv = wl_sc_vals[i]
                rv = rp_sc_vals[i]
                try:
                    wf = float(wv)
                    rf = float(rv)
                    d = wf - rf
                    higher = sc_higher_better[i] if i < len(sc_higher_better) else True
                    favorable = (d > 0) if higher else (d < 0)
                    if d == 0:
                        delta_row.append(Text("0", style="dim", justify="right"))
                    else:
                        style = "bold green" if favorable else "bold red"
                        delta_row.append(Text(f"{d:+.1f}" if abs(d) >= 1 else f"{d:+.3f}",
                                              style=style, justify="right"))
                except (ValueError, TypeError):
                    delta_row.append(Text("-", style="dim", justify="right"))

            table.add_row(*delta_row)

    def _compute_delta(
        self, wl_val: str, rp_val: str, cat: StatCategory,
    ) -> Text:
        """Compute and format the delta between watchlist and roster values."""
        try:
            # Handle H/AB format
            if "/" in wl_val and "/" in rp_val:
                return Text("-", style="dim", justify="right")

            wl_f = float(wl_val)
            rp_f = float(rp_val)
        except (ValueError, TypeError):
            return Text("-", style="dim", justify="right")

        # For stats where higher is better (sort_order == "1"), positive delta = good
        # For stats where lower is better (sort_order == "0"), negative delta = good
        raw_delta = wl_f - rp_f
        if cat.sort_order == "0":
            # Lower is better (ERA, WHIP) — flip for coloring
            favorable = raw_delta < 0
        else:
            favorable = raw_delta > 0

        if raw_delta == 0:
            return Text("0", style="dim", justify="right")

        # Format based on stat type
        if cat.stat_id in ("3", "4", "5", "26", "27"):
            # Rate stat — show 3 decimal places
            formatted = f"{raw_delta:+.3f}"
        elif "." in wl_val or "." in rp_val:
            formatted = f"{raw_delta:+.2f}"
        else:
            formatted = f"{raw_delta:+.0f}"

        style = "bold green" if favorable else "bold red"
        return Text(formatted, style=style, justify="right")


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
        yield Footer()

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
        yield Footer()

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
            cols = ["Season", "G", "GS", "W", "L", "SV", "IP", "H",
                    "ER", "BB", "SO", "ERA", "WHIP", "K/9", "BB/9"]
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
                        Text(str(s.wins), style="bold #6AAF6E"),
                        Text(str(s.losses)), Text(str(s.saves)),
                        Text(f"{s.ip:.1f}"), Text(str(s.hits)),
                        Text(str(s.er)), Text(str(s.bb)),
                        Text(str(s.so), style="bold #E8A735"),
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


class TransactionsScreen(Screen):
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
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
        yield Footer()

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


# --- MLB Scoreboard Screen ---


class MLBScoreboardScreen(Screen):
    BINDINGS = [("escape", "go_back", "Back"), ("q", "go_back", "Back"),
                ("r", "refresh", "Refresh"),
                ("comma", "prev_day", "< Prev Day"),
                ("full_stop", "next_day", "> Next Day"),
                ("t", "today", "Today")]
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
    .game-row {
        height: auto;
        width: 100%;
    }
    .game-card {
        height: 5;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: $surface;
        border: solid $primary-lighten-3;
    }
    .game-card-live {
        height: 5;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: #1E2E1E;
        border: solid #4A7C59;
    }
    .game-card-final {
        height: 5;
        width: 1fr;
        margin: 0 0 1 1;
        padding: 0 1;
        background: #252525;
        border: solid #444444;
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
    """

    def __init__(self) -> None:
        super().__init__()
        from datetime import date as date_cls
        self._date = date_cls.today()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("MLB Scoreboard", id="mlb-header")
        yield Static("", id="mlb-controls")
        yield Static("Loading...", id="mlb-loading")
        yield VerticalScroll(id="mlb-games")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#mlb-games").display = False
        self._update_controls()
        self.run_worker(self._load)

    def _update_controls(self) -> None:
        from datetime import date as date_cls
        today = date_cls.today()
        ctrl = Text()
        ctrl.append(f"{self._date.strftime('%A, %B %d, %Y')}", style="bold")
        if self._date == today:
            ctrl.append("  (today)", style="dim")
        ctrl.append("  |  <,> change day  [t] today  [r] refresh", style="dim")
        self.query_one("#mlb-controls", Static).update(ctrl)

    async def _load(self) -> None:
        games = get_mlb_scoreboard(self._date)

        loading = self.query("#mlb-loading")
        if loading:
            loading.first().remove()

        container = self.query_one("#mlb-games", VerticalScroll)
        container.display = True
        await container.remove_children()

        if not games:
            await container.mount(Static("  No games scheduled.", classes="game-line"))
            return

        # Sort: live first, then scheduled, then final
        order = {"Live": 0, "Preview": 1, "Final": 2}
        games.sort(key=lambda g: order.get(g.status, 1))

        # Batch into rows of 4
        cards_per_row = 4
        for i in range(0, len(games), cards_per_row):
            row = Horizontal(classes="game-row")
            await container.mount(row)
            for game in games[i:i + cards_per_row]:
                card = Vertical(classes=self._card_class(game))
                await row.mount(card)
                await card.mount(Static(self._format_status(game), classes="game-status"))
                await card.mount(Static(self._format_away(game), classes="game-line"))
                await card.mount(Static(self._format_home(game), classes="game-line"))

    @staticmethod
    def _card_class(game: MLBGame) -> str:
        if game.status == "Live":
            return "game-card-live"
        elif game.status == "Final":
            return "game-card-final"
        return "game-card"

    @staticmethod
    def _format_away(game: MLBGame) -> Text:
        line = Text()
        winning = game.away_score > game.home_score
        style = "bold" if winning else ""
        line.append(f" {game.away_abbr:<4}", style=style)
        if game.status != "Preview":
            line.append(f" {game.away_score:>2}", style=style)
        return line

    @staticmethod
    def _format_home(game: MLBGame) -> Text:
        line = Text()
        winning = game.home_score > game.away_score
        style = "bold" if winning else ""
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
        self._update_controls()
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    def action_next_day(self) -> None:
        from datetime import timedelta
        self._date += timedelta(days=1)
        self._update_controls()
        self.run_worker(self._load, group="mlb-load", exclusive=True)

    def action_today(self) -> None:
        from datetime import date as date_cls
        self._date = date_cls.today()
        self._update_controls()
        self.run_worker(self._load, group="mlb-load", exclusive=True)

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


# --- Scoreboard Screen (3-pane layout) ---


class ScoreboardScreen(Screen):
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh"),
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
                ("i", "player_detail", "Player Detail")]
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
        yield Footer()

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

    async def _refresh_data(self) -> None:
        if not self.league:
            return
        week = self._viewing_week if self._viewing_week is not None else None
        self.matchups = self.api.get_scoreboard(self.league.league_key, week=week)
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
        if self._viewing_week is not None and self._viewing_week != self.league.current_week:
            sub.append(f"  (←→ week, [e] select)", style="dim")
        else:
            sub.append(f"  (←→ week, [e] select)", style="dim")
        sub.append(f"  |  {self.league.season} Season  |  {self.league.num_teams} Teams")
        if len(getattr(self.app, "_leagues", [])) > 1:
            sub.append("  |  [L] Switch League", style="dim")
        self.query_one("#board-subheader", Static).update(sub)

    def _populate_matchups(self) -> None:
        lv = self.query_one("#matchup-list", ListView)
        lv.clear()
        lv.display = True
        for i, m in enumerate(self.matchups):
            num = str(i + 1) if i < 9 else "0" if i == 9 else ""
            score_line = Text()
            score_line.append(f"{num:>2} ", style="bold dim")
            score_line.append(f"{m.team_a.name[:18]:<18}", style=f"bold {TEAM_A_COLOR}")
            score_line.append(f"{m.team_a.points:>5.0f}", style=f"{TEAM_A_COLOR}")
            score_line.append("  ")
            score_line.append(f"{m.team_b.name[:18]:<18}", style=f"bold {TEAM_B_COLOR}")
            score_line.append(f"{m.team_b.points:>5.0f}", style=f"{TEAM_B_COLOR}")

            mgr_line = Text()
            mgr_line.append(f"   {m.team_a.manager[:18]:<23}", style="dim")
            mgr_line.append(f"{m.team_b.manager[:18]:<18}", style="dim")

            item = ListItem(
                Label(score_line, classes="matchup-row"),
                Label(mgr_line, classes="matchup-row"),
                Label("─" * 52, classes="matchup-divider"),
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

    def action_mlb_scores(self) -> None:
        self.app.push_screen(MLBScoreboardScreen())

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
        if current < self.league.current_week:
            self._load_week(current + 1)

    def action_select_week(self) -> None:
        if not self.league:
            return
        current = self._viewing_week if self._viewing_week is not None else self.league.current_week
        self.app.push_screen(
            WeekSelectModal(self.league.current_week, current),
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
    print("Authenticated successfully. Launching app...\n")

    api = YahooFantasyAPI(auth)
    app = GklApp(api)
    app.run()


if __name__ == "__main__":
    main()
