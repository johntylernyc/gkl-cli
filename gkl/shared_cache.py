"""App-level shared data cache for expensive cross-screen computations."""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from gkl.stats import SGPCalculator
from gkl.yahoo_api import (
    League, Matchup, PlayerStats, StatCategory, TeamStats, YahooFantasyAPI,
)

POSITIONS = ("C", "1B", "2B", "3B", "SS", "OF", "SP", "RP")


class SharedDataCache:
    """Caches SGP baselines, rank lookups, and draft results across screens.

    Only one load is performed per session.  If a second screen calls
    ``ensure_loaded`` while the first is still fetching, it awaits the
    same ``asyncio.Event`` instead of issuing duplicate API calls.
    """

    def __init__(self) -> None:
        self.sgp_calc: SGPCalculator | None = None
        self.all_teams: list[TeamStats] = []
        self.team_keys: list[str] = []
        self.team_names: dict[str, str] = {}
        self.draft_results: dict[str, str] = {}
        self.rank_lookup: dict[str, int] = {}
        self.preseason_rank_lookup: dict[str, int] = {}
        self.replacement_by_pos: dict[str, list[PlayerStats]] = {}

        # Week-level caches shared across Roto/H2H/Scoreboard prefetch
        self.week_team_stats: dict[int, list[TeamStats]] = {}
        self.week_matchups: dict[int, list[Matchup]] = {}

        self._loading = False
        self._loaded = asyncio.Event()

    @property
    def is_loaded(self) -> bool:
        return self._loaded.is_set()

    async def ensure_loaded(
        self,
        api: YahooFantasyAPI,
        league: League,
        categories: list[StatCategory],
        progress_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        """Load SGP data once; concurrent callers await the same event."""
        if self._loaded.is_set():
            return
        if self._loading:
            await self._loaded.wait()
            return

        self._loading = True
        try:
            await self._do_load(api, league, categories, progress_cb)
        finally:
            self._loaded.set()
            self._loading = False

    async def _do_load(
        self,
        api: YahooFantasyAPI,
        league: League,
        categories: list[StatCategory],
        progress_cb: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        async def _report(msg: str) -> None:
            if progress_cb:
                await progress_cb(msg)

        await _report("Fetching league data...")
        self.all_teams = await asyncio.to_thread(
            api.get_team_season_stats, league.league_key,
        )
        self.team_keys = [t.team_key for t in self.all_teams]
        self.team_names = {t.team_key: t.name for t in self.all_teams}

        self.draft_results = await asyncio.to_thread(
            api.get_draft_results, league.league_key,
        )

        # Fetch free agents per position for SGP replacement baselines
        # — all 8 positions in parallel
        await _report("Computing SGP baselines...")
        replacement_by_pos: dict[str, list[PlayerStats]] = {}

        async def _fetch_pos(pos: str) -> tuple[str, list[PlayerStats]]:
            players, _ = await asyncio.to_thread(
                api.get_free_agents,
                league.league_key, stat_type="season",
                position=pos, sort="AR", sort_type="season", count=25,
            )
            return pos, players

        results = await asyncio.gather(*[_fetch_pos(p) for p in POSITIONS])
        for pos, players in results:
            replacement_by_pos[pos] = players

        self.replacement_by_pos = replacement_by_pos
        self.sgp_calc = SGPCalculator(
            self.all_teams, categories, replacement_by_pos,
        )

        # Build rank lookups — paginate in parallel batches of 4
        await _report("Fetching Yahoo rankings...")
        self.rank_lookup = await _build_rank_lookup_parallel(
            api, league.league_key, "AR",
        )

        await _report("Loading pre-season rankings...")
        self.preseason_rank_lookup = await asyncio.to_thread(
            api.get_preseason_ranks, league.league_key,
        )

    async def get_week_teams(
        self, api: YahooFantasyAPI, league_key: str, week: int,
    ) -> list[TeamStats]:
        """Get team stats for a week, using shared cache."""
        if week not in self.week_team_stats:
            self.week_team_stats[week] = await asyncio.to_thread(
                api.get_team_week_stats, league_key, week,
            )
        return self.week_team_stats[week]

    async def get_week_matchups(
        self, api: YahooFantasyAPI, league_key: str, week: int,
    ) -> list[Matchup]:
        """Get matchups for a week, using shared cache."""
        if week not in self.week_matchups:
            self.week_matchups[week] = await asyncio.to_thread(
                api.get_scoreboard, league_key, week,
            )
        return self.week_matchups[week]

    async def prefetch_weeks(
        self, api: YahooFantasyAPI, league_key: str, weeks: list[int],
    ) -> None:
        """Prefetch multiple weeks of team stats in parallel."""
        missing = [w for w in weeks if w not in self.week_team_stats]
        if not missing:
            return
        results = await asyncio.gather(*[
            asyncio.to_thread(api.get_team_week_stats, league_key, w)
            for w in missing
        ])
        for w, data in zip(missing, results):
            self.week_team_stats[w] = data


async def _build_rank_lookup_parallel(
    api: YahooFantasyAPI,
    league_key: str,
    sort: str,
    max_players: int = 1000,
    batch_size: int = 4,
    page_size: int = 25,
) -> dict[str, int]:
    """Paginate player rankings in parallel batches."""
    lookup: dict[str, int] = {}
    start = 0

    while start < max_players:
        # Fetch up to batch_size pages concurrently
        offsets = list(range(start, min(start + batch_size * page_size, max_players), page_size))

        async def _fetch_page(offset: int) -> tuple[int, list[PlayerStats]]:
            players, _ = await asyncio.to_thread(
                api.get_free_agents,
                league_key, status=None,
                stat_type="season", sort=sort, sort_type="season",
                start=offset, count=page_size,
            )
            return offset, players

        results = await asyncio.gather(*[_fetch_page(o) for o in offsets])
        results_sorted = sorted(results, key=lambda r: r[0])

        hit_end = False
        for offset, players in results_sorted:
            for i, p in enumerate(players):
                lookup[p.player_key] = offset + i + 1
            if len(players) < page_size:
                hit_end = True
                break

        if hit_end:
            break
        start = offsets[-1] + page_size

    return lookup
