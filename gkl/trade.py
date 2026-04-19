"""Trade analysis engine — computes the impact of player swaps on team standings."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from gkl.stats import (
    RATE_STATS,
    _parse_ip,
    compute_roto,
    simulate_h2h,
    compute_power_rankings,
    who_wins,
    TeamH2HSummary,
)
from gkl.stats import SGPCalculator
from gkl.yahoo_api import PlayerStats, StatCategory, TeamStats


@dataclass
class TradeTarget:
    """A candidate player to acquire in a trade."""
    player: PlayerStats
    team_key: str
    team_name: str
    sgp: float | None          # target player's SGP
    net_sgp: float             # target SGP − outgoing SGP (positive = upgrade)
    roto_delta: float = 0.0    # change in roto points for team A
    h2h_win_pct_before: float = 0.0  # baseline win % vs all opponents
    h2h_win_pct_after: float = 0.0   # post-trade win % vs all opponents
    h2h_win_pct_delta: float = 0.0   # change in win %


@dataclass
class TradeScenario:
    """A discovery result: target player + suggested trade piece."""
    target: PlayerStats         # player to acquire
    target_team_key: str
    target_team_name: str
    target_sgp: float | None
    offer: PlayerStats          # suggested player to trade away
    offer_sgp: float | None
    net_sgp: float              # target SGP − offer SGP
    roto_delta: float = 0.0
    partner_roto_delta: float = 0.0  # roto impact on trade partner (positive = they benefit)
    h2h_win_pct_delta: float = 0.0   # H2H win% change for user


@dataclass
class TradeSide:
    team_key: str
    team_name: str
    players: list[PlayerStats]


@dataclass
class CatImpact:
    stat_id: str
    display_name: str
    before: str
    after: str
    delta: float
    favorable: bool  # True if the change is good for team A


@dataclass
class WeekReplayResult:
    """Result of replaying one week's matchup with the trade applied."""
    week: int
    opponent_name: str
    actual_wins: int    # categories won in the actual matchup
    actual_losses: int
    actual_ties: int
    actual_result: str  # "W", "L", "T"
    trade_wins: int     # categories won with the trade applied
    trade_losses: int
    trade_ties: int
    trade_result: str   # "W", "L", "T"
    changed: bool       # True if the matchup result flipped


@dataclass
class H2HReplay:
    """Full season H2H replay with a trade applied."""
    weeks: list[WeekReplayResult]
    actual_season_w: int
    actual_season_l: int
    actual_season_t: int
    trade_season_w: int
    trade_season_l: int
    trade_season_t: int


@dataclass
class H2HHypothetical:
    """Per-week hypothetical: team A's trade-adjusted stats vs every opponent."""
    # Before trade: W-L-T across all weeks vs all opponents
    before_w: int
    before_l: int
    before_t: int
    # After trade
    after_w: int
    after_l: int
    after_t: int


@dataclass
class RotoEntry:
    """One team's roto ranking with batting/pitching breakdown."""
    team_key: str
    name: str
    rank: int
    total: float
    batting: float = 0.0
    pitching: float = 0.0

@dataclass
class TradeImpact:
    # Team A perspective
    team_a_before: TeamStats
    team_a_after: TeamStats
    roto_rank_before_a: int
    roto_rank_after_a: int
    roto_points_before_a: float
    roto_points_after_a: float
    h2h_before_a: TeamH2HSummary
    h2h_after_a: TeamH2HSummary
    # Team B perspective
    team_b_before: TeamStats
    team_b_after: TeamStats
    roto_rank_before_b: int
    roto_rank_after_b: int
    roto_points_before_b: float
    roto_points_after_b: float
    h2h_before_b: TeamH2HSummary
    h2h_after_b: TeamH2HSummary
    # Full league roto standings before/after
    roto_standings_before: list[RotoEntry] = field(default_factory=list)
    roto_standings_after: list[RotoEntry] = field(default_factory=list)
    # Per-category impact for team A
    cat_impacts: list[CatImpact] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Player stat extraction helpers
# ---------------------------------------------------------------------------

def _player_h_ab(p: PlayerStats) -> tuple[int, int]:
    """Extract hits and at-bats from stat 60 (H/AB string like '28/88')."""
    hab = p.stats.get("60", "")
    if "/" in hab:
        parts = hab.split("/")
        try:
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            pass
    return 0, 0


def _player_ip(p: PlayerStats) -> float:
    """Get innings pitched as a float."""
    return _parse_ip(p.stats.get("50", "0"))


def _player_era_components(p: PlayerStats) -> tuple[float, float]:
    """Derive earned runs and IP from ERA and IP stats."""
    ip = _player_ip(p)
    try:
        era = float(p.stats.get("26", "0"))
    except (ValueError, TypeError):
        era = 0.0
    er = era * ip / 9.0 if ip > 0 else 0.0
    return er, ip


def _player_whip_components(p: PlayerStats) -> tuple[float, float]:
    """Derive (BB+H) and IP from WHIP and IP stats."""
    ip = _player_ip(p)
    try:
        whip = float(p.stats.get("27", "0"))
    except (ValueError, TypeError):
        whip = 0.0
    bbh = whip * ip if ip > 0 else 0.0
    return bbh, ip


def _is_pitcher(p: PlayerStats) -> bool:
    positions = {pos.strip() for pos in p.position.split(",")}
    return bool(positions & {"SP", "RP", "P"})


# ---------------------------------------------------------------------------
# Core: apply a trade to a team's stats
# ---------------------------------------------------------------------------

def apply_trade_to_team(
    team: TeamStats,
    roster: list[PlayerStats],
    players_out: list[PlayerStats],
    players_in: list[PlayerStats],
    categories: list[StatCategory],
) -> TeamStats:
    """Return a new TeamStats reflecting a trade (players_out leave, players_in arrive).

    For counting stats: adjust the team total by the player deltas.
    For rate stats: decompose into components across the full roster,
    swap the players, and recompute the rate.
    """
    result = TeamStats(
        team_key=team.team_key,
        name=team.name,
        manager=team.manager,
        points=team.points,
        projected_points=team.projected_points,
        stats=dict(team.stats),
    )

    out_keys = {p.player_key for p in players_out}
    in_keys = {p.player_key for p in players_in}

    # Build the post-trade roster for component-based recomputation
    new_roster = [p for p in roster if p.player_key not in out_keys] + list(players_in)

    scored = [c for c in categories if not c.is_only_display]

    for cat in scored:
        if cat.stat_id in RATE_STATS:
            # Rate stats: compute component delta from traded players,
            # then adjust the team's rate stat accordingly.
            result.stats[cat.stat_id] = _adjust_rate_stat(
                cat.stat_id, team, roster, players_out, players_in
            )
        else:
            # Counting stat: subtract outgoing, add incoming
            try:
                current = float(team.stats.get(cat.stat_id, "0"))
            except (ValueError, TypeError):
                continue

            delta = 0.0
            for p in players_out:
                try:
                    delta -= float(p.stats.get(cat.stat_id, "0"))
                except (ValueError, TypeError):
                    pass
            for p in players_in:
                try:
                    delta += float(p.stats.get(cat.stat_id, "0"))
                except (ValueError, TypeError):
                    pass

            new_val = current + delta
            if new_val == int(new_val):
                result.stats[cat.stat_id] = str(int(new_val))
            else:
                result.stats[cat.stat_id] = f"{new_val:.1f}"

    return result


def _sum_components_for_players(
    players: list[PlayerStats], stat_id: str,
) -> tuple[float, float]:
    """Sum the numerator and denominator components for a rate stat across players.

    Returns (numerator, denominator) where rate = numerator / denominator.
    """
    num = 0.0
    denom = 0.0

    for p in players:
        if stat_id in ("3", "4", "5"):  # batting rate stats
            if _is_pitcher(p):
                continue
            h, ab = _player_h_ab(p)
            if stat_id == "3":  # AVG = H / AB
                num += h
                denom += ab
            elif stat_id == "4":  # OBP ≈ weighted by AB
                try:
                    obp = float(p.stats.get("4", "0"))
                except (ValueError, TypeError):
                    obp = 0.0
                num += obp * ab
                denom += ab
            elif stat_id == "5":  # SLG ≈ weighted by AB
                try:
                    slg = float(p.stats.get("5", "0"))
                except (ValueError, TypeError):
                    slg = 0.0
                num += slg * ab
                denom += ab
        else:  # pitching rate stats
            if not _is_pitcher(p):
                continue
            ip = _player_ip(p)
            if stat_id == "26":  # ERA = ER*9/IP
                er, _ = _player_era_components(p)
                num += er
                denom += ip
            elif stat_id == "27":  # WHIP = (BB+H)/IP
                bbh, _ = _player_whip_components(p)
                num += bbh
                denom += ip
            elif stat_id == "56":  # K/BB ≈ weighted by IP
                try:
                    kbb = float(p.stats.get("56", "0"))
                except (ValueError, TypeError):
                    kbb = 0.0
                num += kbb * ip
                denom += ip

    return num, denom


def _adjust_rate_stat(
    stat_id: str,
    team: TeamStats,
    roster: list[PlayerStats],
    players_out: list[PlayerStats],
    players_in: list[PlayerStats],
) -> str:
    """Adjust a team's rate stat by applying only the traded-player delta.

    Derives the team's baseline components from the team-level rate stat
    and a known denominator, then swaps the traded players' components.
    """
    # Get the team's current rate value and denominator
    try:
        team_rate = float(team.stats.get(stat_id, "0"))
    except (ValueError, TypeError):
        team_rate = 0.0

    if stat_id in ("3", "4", "5"):
        # Batting rate stats: denominator is team AB
        # Sum AB from ALL roster batters as approximation of team AB
        team_denom = 0.0
        for p in roster:
            if not _is_pitcher(p):
                _, ab = _player_h_ab(p)
                team_denom += ab
        if team_denom <= 0:
            return team.stats.get(stat_id, "0")
        team_num = team_rate * team_denom
    elif stat_id in ("26", "27", "56"):
        # Pitching rate stats: denominator is team IP
        team_denom = _parse_ip(team.stats.get("50", "0"))
        if team_denom <= 0:
            return team.stats.get(stat_id, "0")
        if stat_id == "26":  # ERA = ER*9/IP → ER = ERA*IP/9
            team_num = team_rate * team_denom / 9.0
        else:  # WHIP, K/BB: rate = num/IP
            team_num = team_rate * team_denom
    else:
        return team.stats.get(stat_id, "0")

    # Compute delta from traded players
    out_num, out_denom = _sum_components_for_players(players_out, stat_id)
    in_num, in_denom = _sum_components_for_players(players_in, stat_id)

    new_num = team_num - out_num + in_num
    new_denom = team_denom - out_denom + in_denom

    if new_denom <= 0:
        return team.stats.get(stat_id, "0")

    if stat_id in ("3", "4", "5"):  # AVG, OBP, SLG
        val = new_num / new_denom
        formatted = f"{val:.3f}"
        # Match Yahoo format: .282 not 0.282
        if formatted.startswith("0."):
            formatted = formatted[1:]
        return formatted
    elif stat_id == "26":  # ERA = ER * 9 / IP
        return f"{new_num * 9 / new_denom:.2f}"
    elif stat_id in ("27", "56"):  # WHIP, K/BB
        return f"{new_num / new_denom:.2f}"

    return team.stats.get(stat_id, "0")


# ---------------------------------------------------------------------------
# Full trade impact computation
# ---------------------------------------------------------------------------

def compute_trade_impact(
    all_teams: list[TeamStats],
    roster_a: list[PlayerStats],
    roster_b: list[PlayerStats],
    side_a: TradeSide,
    side_b: TradeSide,
    categories: list[StatCategory],
) -> TradeImpact:
    """Compute the full impact of a trade on roto standings and H2H power rankings.

    side_a.players = players leaving team A (going to team B)
    side_b.players = players leaving team B (going to team A)
    """
    scored = [c for c in categories if not c.is_only_display]

    # Find the original TeamStats for both teams
    team_a_orig = next(t for t in all_teams if t.team_key == side_a.team_key)
    team_b_orig = next(t for t in all_teams if t.team_key == side_b.team_key)

    # Compute post-trade stats
    team_a_after = apply_trade_to_team(
        team_a_orig, roster_a,
        players_out=side_a.players,
        players_in=side_b.players,
        categories=categories,
    )
    team_b_after = apply_trade_to_team(
        team_b_orig, roster_b,
        players_out=side_b.players,
        players_in=side_a.players,
        categories=categories,
    )

    # Build before and after team lists for league-wide simulation
    teams_before = list(all_teams)
    teams_after = []
    for t in all_teams:
        if t.team_key == side_a.team_key:
            teams_after.append(team_a_after)
        elif t.team_key == side_b.team_key:
            teams_after.append(team_b_after)
        else:
            teams_after.append(t)

    # Roto rankings — overall, batting, pitching
    roto_before = compute_roto(teams_before, scored)
    roto_after = compute_roto(teams_after, scored)

    bat_cats = [c for c in scored if c.position_type == "B"]
    pitch_cats = [c for c in scored if c.position_type == "P"]
    roto_bat_before = compute_roto(teams_before, bat_cats)
    roto_bat_after = compute_roto(teams_after, bat_cats)
    roto_pitch_before = compute_roto(teams_before, pitch_cats)
    roto_pitch_after = compute_roto(teams_after, pitch_cats)

    def _roto_rank(results: list[dict], team_key: str) -> tuple[int, float]:
        for i, r in enumerate(results, 1):
            if r["team_key"] == team_key:
                return i, r["total"]
        return 0, 0.0

    def _roto_pts(results: list[dict], team_key: str) -> float:
        for r in results:
            if r["team_key"] == team_key:
                return r["total"]
        return 0.0

    rank_before_a, pts_before_a = _roto_rank(roto_before, side_a.team_key)
    rank_after_a, pts_after_a = _roto_rank(roto_after, side_a.team_key)
    rank_before_b, pts_before_b = _roto_rank(roto_before, side_b.team_key)
    rank_after_b, pts_after_b = _roto_rank(roto_after, side_b.team_key)

    # Build full league roto standings ordered by AFTER-trade total
    # Include batting/pitching subtotals
    bat_before_by_key = {r["team_key"]: r["total"] for r in roto_bat_before}
    bat_after_by_key = {r["team_key"]: r["total"] for r in roto_bat_after}
    pitch_before_by_key = {r["team_key"]: r["total"] for r in roto_pitch_before}
    pitch_after_by_key = {r["team_key"]: r["total"] for r in roto_pitch_after}
    before_by_key = {r["team_key"]: r["total"] for r in roto_before}

    standings_before = [
        RotoEntry(
            team_key=r["team_key"], name=r["name"], rank=i, total=r["total"],
            batting=bat_before_by_key.get(r["team_key"], 0),
            pitching=pitch_before_by_key.get(r["team_key"], 0),
        )
        for i, r in enumerate(roto_before, 1)
    ]
    # After standings sorted by after-trade total (compute_roto already sorts)
    standings_after = [
        RotoEntry(
            team_key=r["team_key"], name=r["name"], rank=i, total=r["total"],
            batting=bat_after_by_key.get(r["team_key"], 0),
            pitching=pitch_after_by_key.get(r["team_key"], 0),
        )
        for i, r in enumerate(roto_after, 1)
    ]

    # H2H power rankings
    h2h_before = simulate_h2h(teams_before, scored)
    h2h_after = simulate_h2h(teams_after, scored)
    pr_before = compute_power_rankings(h2h_before, teams_before)
    pr_after = compute_power_rankings(h2h_after, teams_after)

    def _find_summary(rankings: list[TeamH2HSummary], team_key: str) -> TeamH2HSummary:
        for s in rankings:
            if s.team_key == team_key:
                return s
        return TeamH2HSummary(team_key=team_key, name="", manager="")

    h2h_before_a = _find_summary(pr_before, side_a.team_key)
    h2h_after_a = _find_summary(pr_after, side_a.team_key)
    h2h_before_b = _find_summary(pr_before, side_b.team_key)
    h2h_after_b = _find_summary(pr_after, side_b.team_key)

    # Per-category impact for team A
    cat_impacts: list[CatImpact] = []
    for cat in scored:
        before_val = team_a_orig.stats.get(cat.stat_id, "0")
        after_val = team_a_after.stats.get(cat.stat_id, "0")
        try:
            delta = float(after_val) - float(before_val)
        except (ValueError, TypeError):
            delta = 0.0
        higher_better = cat.sort_order == "1"
        favorable = (delta > 0) if higher_better else (delta < 0)
        cat_impacts.append(CatImpact(
            stat_id=cat.stat_id,
            display_name=cat.display_name,
            before=before_val,
            after=after_val,
            delta=delta,
            favorable=favorable if delta != 0 else True,
        ))

    return TradeImpact(
        team_a_before=team_a_orig,
        team_a_after=team_a_after,
        roto_rank_before_a=rank_before_a,
        roto_rank_after_a=rank_after_a,
        roto_points_before_a=pts_before_a,
        roto_points_after_a=pts_after_a,
        h2h_before_a=h2h_before_a,
        h2h_after_a=h2h_after_a,
        team_b_before=team_b_orig,
        team_b_after=team_b_after,
        roto_rank_before_b=rank_before_b,
        roto_rank_after_b=rank_after_b,
        roto_points_before_b=pts_before_b,
        roto_points_after_b=pts_after_b,
        h2h_before_b=h2h_before_b,
        h2h_after_b=h2h_after_b,
        cat_impacts=cat_impacts,
        roto_standings_before=standings_before,
        roto_standings_after=standings_after,
    )


# ---------------------------------------------------------------------------
# Phase 2: Weekly H2H Replay
# ---------------------------------------------------------------------------

def replay_h2h_with_trade(
    team_a_key: str,
    team_b_key: str,
    side_a_player_keys: set[str],
    side_b_player_keys: set[str],
    week_matchups: dict[int, list['Matchup']],
    weekly_roster_a: dict[int, list[PlayerStats]],
    weekly_roster_b: dict[int, list[PlayerStats]],
    categories: list[StatCategory],
    current_week: int,
) -> H2HReplay:
    """Replay each completed week's H2H matchup with the trade applied.

    Uses per-player weekly stats to correctly compute the delta for each
    week. For each week, finds the traded players' weekly contributions,
    applies the swap to team A's weekly team stats, and re-simulates.

    Args:
        team_a_key: The team whose perspective we're analyzing.
        team_b_key: The trade partner's team key.
        side_a_player_keys: Player keys leaving team A.
        side_b_player_keys: Player keys leaving team B (coming to team A).
        week_matchups: Per-week matchup data from cache.
        weekly_roster_a: Per-week player rosters for team A.
        weekly_roster_b: Per-week player rosters for team B.
        categories: League scoring categories.
        current_week: The current week of the season.
    """
    from gkl.yahoo_api import Matchup

    scored = [c for c in categories if not c.is_only_display]
    results: list[WeekReplayResult] = []
    actual_w = actual_l = actual_t = 0
    trade_w = trade_l = trade_t = 0

    for week in range(1, current_week + 1):
        matchups = week_matchups.get(week, [])
        roster_a_week = weekly_roster_a.get(week, [])
        roster_b_week = weekly_roster_b.get(week, [])

        if not matchups:
            continue

        # Find team A's matchup this week
        my_matchup: Matchup | None = None
        am_team_a_side = True
        for m in matchups:
            if m.status == "preevent":
                continue
            if m.team_a.team_key == team_a_key:
                my_matchup = m
                am_team_a_side = True
                break
            elif m.team_b.team_key == team_a_key:
                my_matchup = m
                am_team_a_side = False
                break

        if my_matchup is None:
            continue

        my_team = my_matchup.team_a if am_team_a_side else my_matchup.team_b
        opp_team = my_matchup.team_b if am_team_a_side else my_matchup.team_a

        # Actual result
        a_wins = a_losses = a_ties = 0
        for cat in scored:
            w = who_wins(
                my_team.stats.get(cat.stat_id, "0"),
                opp_team.stats.get(cat.stat_id, "0"),
                cat.sort_order,
            )
            if w == "a":
                a_wins += 1
            elif w == "b":
                a_losses += 1
            else:
                a_ties += 1

        if a_wins > a_losses:
            actual_result = "W"
            actual_w += 1
        elif a_losses > a_wins:
            actual_result = "L"
            actual_l += 1
        else:
            actual_result = "T"
            actual_t += 1

        # Get traded players' WEEKLY stats
        players_out_week = [p for p in roster_a_week if p.player_key in side_a_player_keys]
        players_in_week = [p for p in roster_b_week if p.player_key in side_b_player_keys]

        if not roster_a_week:
            # No per-player data — can't replay, keep as-is
            results.append(WeekReplayResult(
                week=week, opponent_name=opp_team.name,
                actual_wins=a_wins, actual_losses=a_losses, actual_ties=a_ties,
                actual_result=actual_result,
                trade_wins=a_wins, trade_losses=a_losses, trade_ties=a_ties,
                trade_result=actual_result, changed=False,
            ))
            continue

        # Apply trade using this week's player stats
        trade_team_a = apply_trade_to_team(
            my_team, roster_a_week,
            players_out=players_out_week,
            players_in=players_in_week,
            categories=categories,
        )

        # Re-simulate with traded stats vs same opponent
        t_wins = t_losses = t_ties = 0
        for cat in scored:
            w = who_wins(
                trade_team_a.stats.get(cat.stat_id, "0"),
                opp_team.stats.get(cat.stat_id, "0"),
                cat.sort_order,
            )
            if w == "a":
                t_wins += 1
            elif w == "b":
                t_losses += 1
            else:
                t_ties += 1

        if t_wins > t_losses:
            trade_result = "W"
            trade_w += 1
        elif t_losses > t_wins:
            trade_result = "L"
            trade_l += 1
        else:
            trade_result = "T"
            trade_t += 1

        results.append(WeekReplayResult(
            week=week, opponent_name=opp_team.name,
            actual_wins=a_wins, actual_losses=a_losses, actual_ties=a_ties,
            actual_result=actual_result,
            trade_wins=t_wins, trade_losses=t_losses, trade_ties=t_ties,
            trade_result=trade_result,
            changed=(actual_result != trade_result),
        ))

    return H2HReplay(
        weeks=results,
        actual_season_w=actual_w, actual_season_l=actual_l, actual_season_t=actual_t,
        trade_season_w=trade_w, trade_season_l=trade_l, trade_season_t=trade_t,
    )


def compute_h2h_hypothetical(
    team_a_key: str,
    side_a_player_keys: set[str],
    side_b_player_keys: set[str],
    week_matchups: dict[int, list['Matchup']],
    weekly_roster_a: dict[int, list[PlayerStats]],
    weekly_roster_b: dict[int, list[PlayerStats]],
    categories: list[StatCategory],
    current_week: int,
) -> H2HHypothetical:
    """Compute a hypothetical H2H record by replaying every completed week
    against every opponent using actual weekly stats.

    For each week, applies the trade to team A's weekly stats, then
    simulates category matchups against ALL other teams' actual weekly
    stats (not just the scheduled opponent). Sums across all weeks for
    a comprehensive hypothetical W-L-T record.
    """
    from gkl.yahoo_api import Matchup

    scored = [c for c in categories if not c.is_only_display]
    before_w = before_l = before_t = 0
    after_w = after_l = after_t = 0

    for week in range(1, current_week + 1):
        matchups = week_matchups.get(week, [])
        roster_a_week = weekly_roster_a.get(week, [])
        roster_b_week = weekly_roster_b.get(week, [])

        if not matchups:
            continue

        # Extract all teams' weekly stats from matchups
        all_weekly_teams: dict[str, TeamStats] = {}
        for m in matchups:
            if m.status == "preevent":
                continue
            all_weekly_teams[m.team_a.team_key] = m.team_a
            all_weekly_teams[m.team_b.team_key] = m.team_b

        my_team = all_weekly_teams.get(team_a_key)
        if my_team is None:
            continue

        # Compute trade-adjusted team stats for this week
        players_out_week = [p for p in roster_a_week if p.player_key in side_a_player_keys]
        players_in_week = [p for p in roster_b_week if p.player_key in side_b_player_keys]

        if roster_a_week:
            trade_team_a = apply_trade_to_team(
                my_team, roster_a_week,
                players_out=players_out_week,
                players_in=players_in_week,
                categories=categories,
            )
        else:
            trade_team_a = my_team

        # Simulate vs every other team this week
        for opp_key, opp_team in all_weekly_teams.items():
            if opp_key == team_a_key:
                continue

            # Before trade: my actual stats vs opponent
            b_wins = b_losses = b_ties = 0
            for cat in scored:
                w = who_wins(
                    my_team.stats.get(cat.stat_id, "0"),
                    opp_team.stats.get(cat.stat_id, "0"),
                    cat.sort_order,
                )
                if w == "a":
                    b_wins += 1
                elif w == "b":
                    b_losses += 1
                else:
                    b_ties += 1

            if b_wins > b_losses:
                before_w += 1
            elif b_losses > b_wins:
                before_l += 1
            else:
                before_t += 1

            # After trade: adjusted stats vs opponent
            a_wins = a_losses = a_ties = 0
            for cat in scored:
                w = who_wins(
                    trade_team_a.stats.get(cat.stat_id, "0"),
                    opp_team.stats.get(cat.stat_id, "0"),
                    cat.sort_order,
                )
                if w == "a":
                    a_wins += 1
                elif w == "b":
                    a_losses += 1
                else:
                    a_ties += 1

            if a_wins > a_losses:
                after_w += 1
            elif a_losses > a_wins:
                after_l += 1
            else:
                after_t += 1

    return H2HHypothetical(
        before_w=before_w, before_l=before_l, before_t=before_t,
        after_w=after_w, after_l=after_l, after_t=after_t,
    )


# ---------------------------------------------------------------------------
# Phase 3: Trading Block — find trade targets
# ---------------------------------------------------------------------------

def find_trade_targets(
    outgoing_player: PlayerStats,
    my_team_key: str,
    all_rosters: dict[str, list[PlayerStats]],
    all_teams: list[TeamStats],
    team_names: dict[str, str],
    categories: list[StatCategory],
    sgp_calc: SGPCalculator | None,
    week_matchups: dict[int, list] | None = None,
    all_weekly_rosters: dict[str, dict[int, list[PlayerStats]]] | None = None,
    current_week: int = 1,
    max_results: int = 25,
) -> list[TradeTarget]:
    """Find the best trade targets for a player you want to trade away.

    Scans all opposing rosters for position-eligible players. For each
    candidate, computes:
    - Net SGP (target SGP − outgoing SGP)
    - Roto points delta (how the trade changes your roto total)
    - H2H win % delta (per-week hypothetical vs all opponents when
      weekly data is available, otherwise season-aggregate)

    Returns a ranked list sorted by roto points delta.
    """
    from gkl.yahoo_api import Matchup

    outgoing_positions = {pos.strip() for pos in outgoing_player.position.split(",")}
    outgoing_sgp = sgp_calc.player_sgp(outgoing_player) if sgp_calc else None

    scored = [c for c in categories if not c.is_only_display]
    my_roster = all_rosters.get(my_team_key, [])
    my_team = next((t for t in all_teams if t.team_key == my_team_key), None)
    if my_team is None:
        return []

    has_weekly = bool(week_matchups and all_weekly_rosters)
    weekly_roster_a = all_weekly_rosters.get(my_team_key, {}) if all_weekly_rosters else {}

    # Compute baseline roto for my team (before any trade)
    baseline_roto = compute_roto(all_teams, scored)
    baseline_pts = 0.0
    for r in baseline_roto:
        if r["team_key"] == my_team_key:
            baseline_pts = r["total"]
            break

    # Compute baseline H2H actual record (before any trade)
    baseline_replay = None
    if has_weekly:
        # Use empty trade (no players swapped) to get actual record
        baseline_replay = replay_h2h_with_trade(
            my_team_key, my_team_key,
            set(), set(),  # no players traded
            week_matchups, weekly_roster_a, {},
            categories, current_week,
        )
        total_games = baseline_replay.actual_season_w + baseline_replay.actual_season_l + baseline_replay.actual_season_t
        baseline_win_pct = baseline_replay.actual_season_w / total_games if total_games else 0.0
    else:
        baseline_win_pct = 0.0

    candidates: list[TradeTarget] = []

    for team_key, roster in all_rosters.items():
        if team_key == my_team_key:
            continue

        team_name = team_names.get(team_key, team_key)

        for player in roster:
            if player.selected_position in ("IL", "IL+", "NA"):
                continue

            player_positions = {pos.strip() for pos in player.position.split(",")}
            if not (outgoing_positions & player_positions):
                continue

            player_sgp = sgp_calc.player_sgp(player) if sgp_calc else None

            if outgoing_sgp is not None and player_sgp is not None:
                net = player_sgp - outgoing_sgp
            elif player_sgp is not None:
                net = player_sgp
            else:
                net = 0.0

            candidates.append(TradeTarget(
                player=player,
                team_key=team_key,
                team_name=team_name,
                sgp=player_sgp,
                net_sgp=net,
            ))

    # Pre-filter to top candidates by SGP before running expensive simulations
    candidates.sort(key=lambda t: t.net_sgp, reverse=True)
    candidates = candidates[:max_results * 2]  # keep extra for re-ranking

    # Compute roto and H2H deltas for each candidate
    for target in candidates:
        # Apply trade to my team
        trade_team = apply_trade_to_team(
            my_team, my_roster,
            players_out=[outgoing_player],
            players_in=[target.player],
            categories=categories,
        )

        # Build modified team list
        teams_after = []
        for t in all_teams:
            if t.team_key == my_team_key:
                teams_after.append(trade_team)
            else:
                teams_after.append(t)

        # Roto delta
        roto_after = compute_roto(teams_after, scored)
        for r in roto_after:
            if r["team_key"] == my_team_key:
                target.roto_delta = r["total"] - baseline_pts
                break

        # H2H win % delta — replay actual weekly matchups with trade applied
        target.h2h_win_pct_before = baseline_win_pct
        if has_weekly:
            weekly_roster_b = all_weekly_rosters.get(target.team_key, {})
            replay = replay_h2h_with_trade(
                my_team_key, target.team_key,
                {outgoing_player.player_key}, {target.player.player_key},
                week_matchups,
                weekly_roster_a, weekly_roster_b,
                categories, current_week,
            )
            total = replay.trade_season_w + replay.trade_season_l + replay.trade_season_t
            target.h2h_win_pct_after = replay.trade_season_w / total if total else 0.0
            target.h2h_win_pct_delta = target.h2h_win_pct_after - baseline_win_pct

    # Sort by roto delta descending
    candidates.sort(key=lambda t: t.roto_delta, reverse=True)
    return candidates[:max_results]


# ---------------------------------------------------------------------------
# Phase 4: AI Trade Summary
# ---------------------------------------------------------------------------

def build_trade_summary_prompt(
    impact: TradeImpact,
    side_a_name: str,
    side_b_name: str,
    side_a_players: list[PlayerStats],
    side_b_players: list[PlayerStats],
    h2h_replay: H2HReplay | None = None,
) -> str:
    """Build a prompt for Claude to analyze a trade."""
    lines = [
        "You are a fantasy baseball analyst. Analyze this trade concisely.",
        "",
        f"**{side_a_name}** sends: {', '.join(f'{p.name} ({p.position})' for p in side_a_players)}",
        f"**{side_b_name}** sends: {', '.join(f'{p.name} ({p.position})' for p in side_b_players)}",
        "",
        "Category impact for " + side_a_name + ":",
    ]
    for ci in impact.cat_impacts:
        if ci.delta != 0:
            direction = "+" if ci.favorable else "-"
            lines.append(f"  {ci.display_name}: {ci.before} → {ci.after} ({direction})")

    lines.append("")
    lines.append(f"Roto standings: #{impact.roto_rank_before_a} → #{impact.roto_rank_after_a} "
                 f"({impact.roto_points_before_a:.1f} → {impact.roto_points_after_a:.1f} pts)")

    if h2h_replay:
        lines.append(f"H2H record: {h2h_replay.actual_season_w}-{h2h_replay.actual_season_l}-{h2h_replay.actual_season_t}"
                     f" → {h2h_replay.trade_season_w}-{h2h_replay.trade_season_l}-{h2h_replay.trade_season_t}")
        flips = [w for w in h2h_replay.weeks if w.changed]
        if flips:
            lines.append(f"Matchup flips: {len(flips)} weeks changed outcome")

    lines.append(f"\nTrade partner ({side_b_name}):")
    lines.append(f"  Roto: #{impact.roto_rank_before_b} → #{impact.roto_rank_after_b}")

    lines.append("")
    lines.append("Provide:")
    lines.append("1. **Verdict**: One sentence — is this a good trade for " + side_a_name + "?")
    lines.append("2. **Pros** (2-3 bullets)")
    lines.append("3. **Cons** (2-3 bullets)")
    lines.append("4. **Pitch**: 2-3 sentences to sell this deal to " + side_b_name)
    lines.append("5. **Counter**: If declining, 2-3 sentences suggesting what you'd want instead")
    lines.append("")
    lines.append("Be specific — reference actual stat changes and standings impact. Keep it under 200 words total.")

    return "\n".join(lines)


async def get_trade_ai_summary(
    prompt: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Call Claude for a trade analysis summary."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Phase 5: Trade Discovery
# ---------------------------------------------------------------------------

def _player_category_value(player: PlayerStats, stat_id: str) -> float:
    """Get a player's value for a specific stat category."""
    val = player.stats.get(stat_id, "0")
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _find_best_offer(
    my_roster: list[PlayerStats],
    target: PlayerStats,
    target_sgp: float | None,
    sgp_calc: SGPCalculator | None,
    target_stat_ids: list[str] | None = None,
) -> PlayerStats | None:
    """Find the best player to offer from my roster for a given target.

    Strategy: offer a player from a DIFFERENT position group than the
    target categories, so the trade makes sense for both sides. E.g.,
    if targeting a pitcher, offer a batter — the other manager fills
    a batting need, you fill a pitching need.

    Prefers players with SGP close to the target's value (realistic deal).
    Avoids offering players at the same position as the target (which
    would devastate the partner's roster at that position).
    """
    batting_positions = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF",
                         "OF", "Util", "DH"}
    target_is_pitcher = _is_pitcher(target)

    candidates: list[tuple[PlayerStats, float, float]] = []
    for p in my_roster:
        if p.selected_position in ("IL", "IL+", "NA", "BN"):
            continue
        p_sgp = sgp_calc.player_sgp(p) if sgp_calc else None
        if p_sgp is None:
            continue

        p_is_pitcher = _is_pitcher(p)

        # Prefer cross-type trades: if target is pitcher, offer a batter and vice versa
        # This creates trades where both sides fill different needs
        if target_is_pitcher == p_is_pitcher:
            # Same type — penalize to push these down the ranking
            gap = abs(p_sgp - (target_sgp or 0)) + 50
        else:
            gap = abs(p_sgp - (target_sgp or 0))

        candidates.append((p, p_sgp, gap))

    if not candidates:
        return None

    # Sort by gap — closest value match (with cross-type bonus) first
    candidates.sort(key=lambda x: x[2])
    return candidates[0][0]


def discover_trades(
    my_team_key: str,
    target_stat_ids: list[str],
    all_rosters: dict[str, list[PlayerStats]],
    all_teams: list[TeamStats],
    team_names: dict[str, str],
    categories: list[StatCategory],
    sgp_calc: SGPCalculator | None,
    max_results: int = 20,
) -> list[TradeScenario]:
    """Discover trade scenarios that improve specific stat categories.

    Scans all opposing rosters for players who are strong in the target
    categories, pairs each with a suggested trade offer from the user's
    roster, and ranks by roto points improvement.

    Args:
        my_team_key: The user's team key.
        target_stat_ids: Stat IDs the user wants to improve.
        all_rosters: {team_key: roster} for all teams.
        all_teams: Season team stats for all teams.
        team_names: {team_key: name} mapping.
        categories: League scoring categories.
        sgp_calc: SGP calculator.
        max_results: Max scenarios to return.
    """
    scored = [c for c in categories if not c.is_only_display]
    my_roster = all_rosters.get(my_team_key, [])
    my_team = next((t for t in all_teams if t.team_key == my_team_key), None)
    if my_team is None:
        return []

    # Score each opposing player by how strong they are in target categories
    # relative to the league. Use percentile rank within their position group.
    candidates: list[tuple[PlayerStats, str, str, float, float]] = []
    # (player, team_key, team_name, category_score, sgp)

    for team_key, roster in all_rosters.items():
        if team_key == my_team_key:
            continue
        team_name = team_names.get(team_key, team_key)
        for player in roster:
            if player.selected_position in ("IL", "IL+", "NA"):
                continue
            p_sgp = sgp_calc.player_sgp(player) if sgp_calc else None

            # Score: sum of the player's values in target categories
            # For "higher is better" stats, raw value. For "lower is better", negate.
            cat_score = 0.0
            cat_values: dict[str, float] = {}
            for sid in target_stat_ids:
                cat = next((c for c in scored if c.stat_id == sid), None)
                if not cat:
                    continue
                val = _player_category_value(player, sid)
                cat_values[sid] = val
                if cat.sort_order == "1":
                    cat_score += val
                else:
                    cat_score -= val  # lower is better → high negative = good

            if cat_score == 0:
                continue

            candidates.append((player, team_key, team_name, cat_score, p_sgp or 0.0))

    # Sort by category score descending — players strongest in target cats first
    candidates.sort(key=lambda x: x[3], reverse=True)
    # Keep top candidates
    candidates = candidates[:max_results * 3]

    # For each candidate, find a trade offer and compute roto delta
    scenarios: list[TradeScenario] = []
    baseline_roto = compute_roto(all_teams, scored)
    baseline_pts = 0.0
    for r in baseline_roto:
        if r["team_key"] == my_team_key:
            baseline_pts = r["total"]
            break

    # Also compute baseline partner roto and H2H for filtering
    partner_baseline_pts: dict[str, float] = {}
    for r in baseline_roto:
        partner_baseline_pts[r["team_key"]] = r["total"]

    baseline_h2h = simulate_h2h(all_teams, scored)
    baseline_pr = compute_power_rankings(baseline_h2h, all_teams)
    baseline_win_pct = 0.0
    for s in baseline_pr:
        if s.team_key == my_team_key:
            baseline_win_pct = s.win_pct
            break

    for player, team_key, team_name, cat_score, p_sgp in candidates:
        offer = _find_best_offer(my_roster, player, p_sgp, sgp_calc, target_stat_ids)
        if offer is None:
            continue

        offer_sgp = sgp_calc.player_sgp(offer) if sgp_calc else None
        net = (p_sgp - (offer_sgp or 0)) if p_sgp else 0.0

        # Compute roto delta for both sides
        trade_team_a = apply_trade_to_team(
            my_team, my_roster,
            players_out=[offer],
            players_in=[player],
            categories=categories,
        )
        partner_team = next((t for t in all_teams if t.team_key == team_key), None)
        partner_roster = all_rosters.get(team_key, [])
        if partner_team:
            trade_team_b = apply_trade_to_team(
                partner_team, partner_roster,
                players_out=[player],
                players_in=[offer],
                categories=categories,
            )
        else:
            trade_team_b = None

        teams_after = []
        for t in all_teams:
            if t.team_key == my_team_key:
                teams_after.append(trade_team_a)
            elif t.team_key == team_key and trade_team_b:
                teams_after.append(trade_team_b)
            else:
                teams_after.append(t)

        roto_after = compute_roto(teams_after, scored)
        roto_delta = 0.0
        partner_roto_delta = 0.0
        for r in roto_after:
            if r["team_key"] == my_team_key:
                roto_delta = r["total"] - baseline_pts
            elif r["team_key"] == team_key:
                partner_roto_delta = r["total"] - partner_baseline_pts.get(team_key, 0)

        # Skip deals where the partner loses too much — unrealistic offers
        if partner_roto_delta < -15:
            continue

        # H2H win% delta
        h2h_after = simulate_h2h(teams_after, scored)
        pr_after = compute_power_rankings(h2h_after, teams_after)
        h2h_win_pct_delta = 0.0
        for s in pr_after:
            if s.team_key == my_team_key:
                h2h_win_pct_delta = s.win_pct - baseline_win_pct
                break

        scenarios.append(TradeScenario(
            target=player,
            target_team_key=team_key,
            target_team_name=team_name,
            target_sgp=p_sgp if p_sgp else None,
            offer=offer,
            offer_sgp=offer_sgp,
            net_sgp=net,
            roto_delta=roto_delta,
            partner_roto_delta=partner_roto_delta,
            h2h_win_pct_delta=h2h_win_pct_delta,
        ))

    # Sort by roto delta
    scenarios.sort(key=lambda s: s.roto_delta, reverse=True)
    return scenarios[:max_results]
