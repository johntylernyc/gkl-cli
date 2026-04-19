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
from gkl.yahoo_api import PlayerStats, StatCategory, TeamStats


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

    # Roto rankings
    roto_before = compute_roto(teams_before, scored)
    roto_after = compute_roto(teams_after, scored)

    def _roto_rank(results: list[dict], team_key: str) -> tuple[int, float]:
        for i, r in enumerate(results, 1):
            if r["team_key"] == team_key:
                return i, r["total"]
        return 0, 0.0

    rank_before_a, pts_before_a = _roto_rank(roto_before, side_a.team_key)
    rank_after_a, pts_after_a = _roto_rank(roto_after, side_a.team_key)
    rank_before_b, pts_before_b = _roto_rank(roto_before, side_b.team_key)
    rank_after_b, pts_after_b = _roto_rank(roto_after, side_b.team_key)

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
    )
