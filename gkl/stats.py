"""Stat aggregation and simulation utilities."""

from __future__ import annotations

from dataclasses import dataclass, field

from gkl.yahoo_api import PlayerStats, StatCategory, TeamStats

# Rate stats that need special aggregation (not just summing).
# Values are skipped during counting-stat summation and instead
# accumulated as weighted averages (see _RATE_WEIGHTS).
RATE_STATS = {
    "3": ("60",),   # AVG = H/AB (stat 60 is "H/AB" like "31/103")
    "4": None,      # OBP — needs H, BB, HBP, AB, SF (complex)
    "5": None,      # SLG — needs TB, AB
    "26": ("50",),  # ERA = ER*9/IP (stat 50 is IP)
    "27": ("50",),  # WHIP = (W+H)/IP
    "56": None,     # K/BB ratio
}

# Weight stat and decimal places for rate-stat weighted averaging.
# Batting rates are weighted by AB (stat 6); pitching rates by IP (stat 50).
_RATE_WEIGHTS: dict[str, tuple[str, int]] = {
    "3":  ("6", 3),   # AVG weighted by AB
    "4":  ("6", 3),   # OBP weighted by AB (approximate — denom is PA, not AB)
    "5":  ("6", 3),   # SLG weighted by AB
    "26": ("50", 2),  # ERA weighted by IP
    "27": ("50", 2),  # WHIP weighted by IP
    "56": ("50", 2),  # K/BB weighted by IP (approximate)
}

# Stats pinned to always display regardless of league scoring configuration.
# (display_name, position_type, fallback_stat_id)
_PINNED_STATS: list[tuple[str, str, str]] = [
    ("G", "B", "0"),
    ("AB", "B", "6"),
    ("IP", "P", "50"),
]


def build_stat_columns(
    categories: list[StatCategory],
    position_type: str,
) -> tuple[list[StatCategory], set[str]]:
    """Build stat columns with pinned unscored stats before scored stats.

    Returns (ordered_cats, unscored_ids) where ordered_cats has pinned
    unscored stats first, then scored stats.  unscored_ids contains the
    stat_ids of pinned stats that aren't league scoring categories.
    """
    scored = [c for c in categories if not c.is_only_display and c.position_type == position_type]
    scored_names = {c.display_name for c in scored}

    pinned: list[StatCategory] = []
    unscored_ids: set[str] = set()

    for name, ptype, fallback_id in _PINNED_STATS:
        if ptype != position_type:
            continue
        if name in scored_names:
            continue  # already a scoring category — appears normally
        # Try to find in league categories (may be display-only)
        existing = next(
            (c for c in categories
             if c.display_name == name and c.position_type == position_type),
            None,
        )
        stat_id = existing.stat_id if existing else fallback_id
        pinned.append(StatCategory(
            stat_id=stat_id,
            display_name=name,
            sort_order="1",
            position_type=position_type,
            is_only_display=True,
        ))
        unscored_ids.add(stat_id)

    return pinned + scored, unscored_ids


def get_stat_value(stats: dict[str, str], stat_id: str, display_name: str) -> str:
    """Get a stat value with fallback for derived stats like AB."""
    val = stats.get(stat_id)
    if val:
        return val
    # Fallback: extract AB from H/AB (stat 60)
    if display_name == "AB":
        hab = stats.get("60", "")
        if "/" in hab:
            try:
                return hab.split("/")[1]
            except IndexError:
                pass
    return "-"


def compute_roto(
    teams: list[TeamStats],
    categories: list[StatCategory],
) -> list[dict]:
    """Compute roto points for each team across the given categories.

    Each team is ranked per category (ties get averaged ranks).
    Returns a list of dicts sorted by total roto points (highest first),
    each containing: name, manager, team_key, total, and per-category rank
    keyed by stat_id, plus raw values keyed as raw_{stat_id}.
    """
    results: list[dict] = []
    for t in teams:
        results.append({
            "name": t.name,
            "manager": t.manager,
            "team_key": t.team_key,
            "total": 0.0,
        })

    scored = [c for c in categories if not c.is_only_display]
    for cat in scored:
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


def aggregate_weekly_stats(
    weekly_data: list[list[TeamStats]],
    categories: list[StatCategory],
) -> list[TeamStats]:
    """Aggregate multiple weeks of team stats into a single set.

    For counting stats: sum across weeks.
    For rate stats: compute weighted averages (batting rates by AB,
    pitching rates by IP).
    """
    if not weekly_data:
        return []
    if len(weekly_data) == 1:
        return weekly_data[0]

    # Build a map of team_key -> aggregated stats
    team_map: dict[str, TeamStats] = {}
    for week_teams in weekly_data:
        for t in week_teams:
            if t.team_key not in team_map:
                team_map[t.team_key] = TeamStats(
                    team_key=t.team_key,
                    name=t.name,
                    manager=t.manager,
                    points=0.0,
                    projected_points=0.0,
                    stats={},
                )
            agg = team_map[t.team_key]
            agg.points += t.points

            for stat_id, val in t.stats.items():
                if stat_id == "60":
                    # H/AB — aggregate components separately
                    _add_hab(agg, val)
                elif stat_id in RATE_STATS:
                    # Rate stats: skip counting, accumulated below
                    continue
                else:
                    # Counting stat: sum
                    _add_numeric(agg, stat_id, val)

            # Accumulate rate stats as weighted sums for proper averaging.
            # Batting rates weighted by AB, pitching rates weighted by IP.
            ab = _get_ab(t)
            ip = _parse_ip(t.stats.get("50", "0"))

            for stat_id, (weight_stat, _decimals) in _RATE_WEIGHTS.items():
                if stat_id not in t.stats:
                    continue
                try:
                    val_f = float(t.stats[stat_id])
                except (ValueError, TypeError):
                    continue
                weight = ab if weight_stat == "6" else ip
                if weight <= 0:
                    continue
                wk = f"_rw_{stat_id}"
                wtk = f"_rwt_{stat_id}"
                agg.stats[wk] = str(
                    float(agg.stats.get(wk, "0")) + val_f * weight)
                agg.stats[wtk] = str(
                    float(agg.stats.get(wtk, "0")) + weight)

    # Compute final rate stat values from weighted sums
    for agg in team_map.values():
        _compute_rates(agg)

    return list(team_map.values())


def _get_ab(t: TeamStats) -> float:
    """Get at-bats from stat 6, falling back to parsing H/AB (stat 60)."""
    try:
        ab = float(t.stats.get("6", "0"))
        if ab > 0:
            return ab
    except (ValueError, TypeError):
        pass
    # Fallback: parse AB from H/AB string like "31/103"
    hab = t.stats.get("60", "")
    if "/" in hab:
        try:
            return float(hab.split("/")[1])
        except (ValueError, IndexError):
            pass
    return 0.0


def _add_hab(agg: TeamStats, val: str) -> None:
    """Parse and accumulate H/AB (e.g., '31/103')."""
    try:
        parts = val.split("/")
        h = int(parts[0])
        ab = int(parts[1])
        agg.stats["_h"] = str(int(agg.stats.get("_h", "0")) + h)
        agg.stats["_ab"] = str(int(agg.stats.get("_ab", "0")) + ab)
        agg.stats["60"] = f"{agg.stats['_h']}/{agg.stats['_ab']}"
    except (ValueError, IndexError):
        pass


def _add_numeric(agg: TeamStats, stat_id: str, val: str) -> None:
    """Sum a numeric stat."""
    try:
        existing = float(agg.stats.get(stat_id, "0"))
        agg.stats[stat_id] = str(existing + float(val))
        # Clean up: if it's a whole number, drop the decimal
        if agg.stats[stat_id].endswith(".0"):
            agg.stats[stat_id] = agg.stats[stat_id][:-2]
    except (ValueError, TypeError):
        pass


def _compute_rates(agg: TeamStats) -> None:
    """Compute rate stats from weighted sums accumulated during aggregation."""
    for stat_id, (_weight_stat, decimals) in _RATE_WEIGHTS.items():
        wk = f"_rw_{stat_id}"
        wtk = f"_rwt_{stat_id}"
        weight = float(agg.stats.get(wtk, "0"))
        if weight > 0:
            val = float(agg.stats.get(wk, "0")) / weight
            agg.stats[stat_id] = f"{val:.{decimals}f}"


def _parse_ip(val: str) -> float:
    """Parse innings pitched (e.g., '34.1' means 34 and 1/3)."""
    try:
        f = float(val)
        whole = int(f)
        frac = f - whole
        # Yahoo represents 1/3 as .1 and 2/3 as .2
        if abs(frac - 0.1) < 0.05:
            return whole + 1 / 3
        elif abs(frac - 0.2) < 0.05:
            return whole + 2 / 3
        return f
    except (ValueError, TypeError):
        return 0.0


# --- H2H Simulation ---


def who_wins(a_val: str, b_val: str, sort_order: str) -> str:
    """Returns 'a', 'b', or 'tie'."""
    try:
        a_f = float(a_val)
        b_f = float(b_val)
    except (ValueError, TypeError):
        return "tie"
    if a_f == b_f:
        return "tie"
    if (a_f > b_f) == (sort_order == "1"):
        return "a"
    return "b"


@dataclass
class H2HResult:
    """Result of one team vs another across all categories."""
    wins: int = 0
    losses: int = 0
    ties: int = 0
    # Per-category result: list of (display_name, "w"/"l"/"t")
    cat_results: list[tuple[str, str]] = field(default_factory=list)

    @property
    def result(self) -> str:
        if self.wins > self.losses:
            return "WIN"
        elif self.losses > self.wins:
            return "LOSS"
        return "TIE"

    @property
    def record_str(self) -> str:
        return f"{self.wins}-{self.losses}-{self.ties}"


@dataclass
class TeamH2HSummary:
    """A team's hypothetical record against all opponents."""
    team_key: str
    name: str
    manager: str
    total_wins: int = 0
    total_losses: int = 0
    total_ties: int = 0

    @property
    def win_pct(self) -> float:
        total = self.total_wins + self.total_losses + self.total_ties
        return self.total_wins / total if total > 0 else 0.0

    @property
    def record_str(self) -> str:
        return f"{self.total_wins}-{self.total_losses}-{self.total_ties}"


def simulate_h2h(
    teams: list[TeamStats],
    categories: list[StatCategory],
) -> dict[str, dict[str, H2HResult]]:
    """Simulate every pairwise matchup for a set of teams.

    Returns: result[team_a_key][team_b_key] = H2HResult from team_a's perspective.
    """
    scored = [c for c in categories if not c.is_only_display]
    results: dict[str, dict[str, H2HResult]] = {}

    for a in teams:
        results[a.team_key] = {}
        for b in teams:
            if a.team_key == b.team_key:
                continue
            r = H2HResult()
            for cat in scored:
                a_val = a.stats.get(cat.stat_id, "0")
                b_val = b.stats.get(cat.stat_id, "0")
                w = who_wins(a_val, b_val, cat.sort_order)
                if w == "a":
                    r.wins += 1
                    r.cat_results.append((cat.display_name, "w"))
                elif w == "b":
                    r.losses += 1
                    r.cat_results.append((cat.display_name, "l"))
                else:
                    r.ties += 1
                    r.cat_results.append((cat.display_name, "t"))
            results[a.team_key][b.team_key] = r

    return results


def compute_power_rankings(
    h2h_results: dict[str, dict[str, H2HResult]],
    teams: list[TeamStats],
) -> list[TeamH2HSummary]:
    """Compute each team's hypothetical record against all others."""
    summaries: list[TeamH2HSummary] = []
    for t in teams:
        s = TeamH2HSummary(team_key=t.team_key, name=t.name, manager=t.manager)
        for opp_key, result in h2h_results.get(t.team_key, {}).items():
            if result.result == "WIN":
                s.total_wins += 1
            elif result.result == "LOSS":
                s.total_losses += 1
            else:
                s.total_ties += 1
        summaries.append(s)
    summaries.sort(key=lambda s: (s.win_pct, s.total_wins), reverse=True)
    return summaries


def aggregate_h2h_season(
    weekly_rankings: list[list[TeamH2HSummary]],
) -> list[TeamH2HSummary]:
    """Aggregate power rankings across multiple weeks."""
    agg: dict[str, TeamH2HSummary] = {}
    for week in weekly_rankings:
        for s in week:
            if s.team_key not in agg:
                agg[s.team_key] = TeamH2HSummary(
                    team_key=s.team_key, name=s.name, manager=s.manager)
            a = agg[s.team_key]
            a.total_wins += s.total_wins
            a.total_losses += s.total_losses
            a.total_ties += s.total_ties
    result = list(agg.values())
    result.sort(key=lambda s: (s.win_pct, s.total_wins), reverse=True)
    return result


# --- Standings Gain Points (SGP) ---

# Batting positions used to classify players as batters vs pitchers
_BATTING_POSITIONS = {
    "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "Util", "DH", "IF", "BN",
}

# Typical roster sizes used for the N-1 rate stat baseline
_HITTERS_PER_TEAM = 14
_PITCHERS_PER_TEAM = 9

# Minimum sample sizes for meaningful SGP calculation
_MIN_AB = 30
_MIN_IP = 10.0

# Number of qualified free agents to average per position for replacement level
_REPL_POOL_SIZE = 3


def _is_batter(player: PlayerStats) -> bool:
    return any(pos in _BATTING_POSITIONS for pos in player.position.split(","))


def _player_ab(player: PlayerStats) -> float:
    """Extract at-bats from a player's H/AB stat (stat 60)."""
    hab = player.stats.get("60", "0/0")
    try:
        return float(hab.split("/")[1])
    except (ValueError, IndexError):
        return 0.0


def _player_ip(player: PlayerStats) -> float:
    """Extract innings pitched from a player's IP stat (stat 50)."""
    return _parse_ip(player.stats.get("50", "0"))


def _has_sufficient_sample(player: PlayerStats) -> bool:
    """Check if a player has enough playing time for meaningful SGP."""
    if _is_batter(player):
        return _player_ab(player) >= _MIN_AB
    return _player_ip(player) >= _MIN_IP


class SGPCalculator:
    """Compute Standings Gain Points for individual players.

    SGP measures how many roto standings points a player's production
    generates.  Denominators come from the spread of team totals in
    each scoring category; replacement level is computed per position
    from the best qualified free agents at that position.
    """

    def __init__(
        self,
        all_teams: list[TeamStats],
        categories: list[StatCategory],
        replacement_by_pos: dict[str, list[PlayerStats]],
    ) -> None:
        self._all_teams = all_teams
        self._num_teams = len(all_teams)
        self._scored = [c for c in categories if not c.is_only_display]
        self._batting_cats = [c for c in self._scored if c.position_type == "B"]
        self._pitching_cats = [c for c in self._scored if c.position_type == "P"]

        self._denominators = self._compute_denominators()
        self._league_avgs = self._compute_league_averages()
        self._rate_baselines = self._compute_rate_baselines()
        self._repl_by_pos, self._repl_batting, self._repl_pitching = (
            self._compute_replacement_sgp(replacement_by_pos)
        )

    # --- Denominators ---

    def _compute_denominators(self) -> dict[str, float]:
        """Compute the SGP denominator for each scored category.

        denominator = (1st_place - last_place) / (num_teams - 1)
        """
        denoms: dict[str, float] = {}
        if self._num_teams < 2:
            return denoms
        for cat in self._scored:
            vals: list[float] = []
            for t in self._all_teams:
                try:
                    vals.append(float(t.stats.get(cat.stat_id, "0")))
                except ValueError:
                    vals.append(0.0)
            vals.sort()
            spread = vals[-1] - vals[0]
            denom = spread / (self._num_teams - 1)
            # For inverse stats (lower is better), denominator is negative
            if cat.sort_order == "0":
                denom = -denom
            denoms[cat.stat_id] = denom
        return denoms

    # --- League averages ---

    def _compute_league_averages(self) -> dict[str, float]:
        """Average of each stat across all teams."""
        avgs: dict[str, float] = {}
        if not self._all_teams:
            return avgs
        for cat in self._scored:
            total = 0.0
            count = 0
            for t in self._all_teams:
                try:
                    total += float(t.stats.get(cat.stat_id, "0"))
                    count += 1
                except ValueError:
                    pass
            avgs[cat.stat_id] = total / count if count else 0.0
        return avgs

    # --- Rate stat baselines (N-1 model) ---

    def _compute_rate_baselines(self) -> dict[str, dict[str, float]]:
        """Compute N-1 team baselines for rate stat categories.

        Returns dict[stat_id] -> {component_key: avg_value} representing
        the rest-of-team (N-1 roster slots) baseline components.
        """
        baselines: dict[str, dict[str, float]] = {}
        if not self._all_teams:
            return baselines

        n = self._num_teams

        # AVG (stat 3): baseline from H and AB components
        if any(c.stat_id == "3" for c in self._scored):
            total_h, total_ab = 0.0, 0.0
            for t in self._all_teams:
                hab = t.stats.get("60", "0/0")
                try:
                    parts = hab.split("/")
                    total_h += float(parts[0])
                    total_ab += float(parts[1])
                except (ValueError, IndexError):
                    pass
            avg_h = total_h / n if n else 0
            avg_ab = total_ab / n if n else 0
            h_per_player = avg_h / _HITTERS_PER_TEAM if _HITTERS_PER_TEAM else 0
            ab_per_player = avg_ab / _HITTERS_PER_TEAM if _HITTERS_PER_TEAM else 0
            baselines["3"] = {
                "h": h_per_player * (_HITTERS_PER_TEAM - 1),
                "ab": ab_per_player * (_HITTERS_PER_TEAM - 1),
            }

        # OBP (stat 4): baseline from H, BB, HBP, AB, SF
        if any(c.stat_id == "4" for c in self._scored):
            total_h, total_ab = 0.0, 0.0
            total_bb, total_hbp, total_sf = 0.0, 0.0, 0.0
            for t in self._all_teams:
                hab = t.stats.get("60", "0/0")
                try:
                    parts = hab.split("/")
                    total_h += float(parts[0])
                    total_ab += float(parts[1])
                except (ValueError, IndexError):
                    pass
                try:
                    total_bb += float(t.stats.get("18", "0"))
                except ValueError:
                    pass
                try:
                    total_hbp += float(t.stats.get("19", "0"))
                except ValueError:
                    pass
                try:
                    total_sf += float(t.stats.get("20", "0"))
                except ValueError:
                    pass
            n_minus_1 = _HITTERS_PER_TEAM - 1
            baselines["4"] = {
                "h": (total_h / n / _HITTERS_PER_TEAM) * n_minus_1 if n else 0,
                "ab": (total_ab / n / _HITTERS_PER_TEAM) * n_minus_1 if n else 0,
                "bb": (total_bb / n / _HITTERS_PER_TEAM) * n_minus_1 if n else 0,
                "hbp": (total_hbp / n / _HITTERS_PER_TEAM) * n_minus_1 if n else 0,
                "sf": (total_sf / n / _HITTERS_PER_TEAM) * n_minus_1 if n else 0,
            }

        # ERA (stat 26): baseline from ER and IP
        if any(c.stat_id == "26" for c in self._scored):
            total_er, total_ip = 0.0, 0.0
            for t in self._all_teams:
                try:
                    total_er += float(t.stats.get("40", "0"))
                except ValueError:
                    pass
                total_ip += _parse_ip(t.stats.get("50", "0"))
            n_minus_1 = _PITCHERS_PER_TEAM - 1
            baselines["26"] = {
                "er": (total_er / n / _PITCHERS_PER_TEAM) * n_minus_1 if n else 0,
                "ip": (total_ip / n / _PITCHERS_PER_TEAM) * n_minus_1 if n else 0,
            }

        # WHIP (stat 27): baseline from BB allowed, H allowed, IP
        if any(c.stat_id == "27" for c in self._scored):
            total_bb, total_ha, total_ip = 0.0, 0.0, 0.0
            for t in self._all_teams:
                try:
                    total_bb += float(t.stats.get("39", "0"))
                except ValueError:
                    pass
                try:
                    total_ha += float(t.stats.get("35", "0"))
                except ValueError:
                    pass
                total_ip += _parse_ip(t.stats.get("50", "0"))
            n_minus_1 = _PITCHERS_PER_TEAM - 1
            baselines["27"] = {
                "bb": (total_bb / n / _PITCHERS_PER_TEAM) * n_minus_1 if n else 0,
                "ha": (total_ha / n / _PITCHERS_PER_TEAM) * n_minus_1 if n else 0,
                "ip": (total_ip / n / _PITCHERS_PER_TEAM) * n_minus_1 if n else 0,
            }

        return baselines

    # --- Player SGP calculation ---

    def _raw_player_sgp(self, player: PlayerStats) -> float | None:
        """Compute raw (pre-replacement) SGP for a player.

        Returns None if the player has insufficient sample size.
        """
        if not _has_sufficient_sample(player):
            return None

        is_batter = _is_batter(player)
        cats = self._batting_cats if is_batter else self._pitching_cats
        total = 0.0

        for cat in cats:
            denom = self._denominators.get(cat.stat_id, 0.0)
            if denom == 0:
                continue

            if cat.stat_id in RATE_STATS:
                total += self._rate_stat_sgp(player, cat, denom)
            else:
                # Counting stat
                try:
                    val = float(player.stats.get(cat.stat_id, "0"))
                except ValueError:
                    continue
                total += val / denom

        return total

    def _rate_stat_sgp(
        self, player: PlayerStats, cat: StatCategory, denom: float,
    ) -> float:
        """Compute SGP for a rate stat using the N-1 team baseline model."""
        league_avg = self._league_avgs.get(cat.stat_id, 0.0)
        baseline = self._rate_baselines.get(cat.stat_id)
        if baseline is None:
            # Fallback: treat like a counting stat (rough approximation)
            try:
                val = float(player.stats.get(cat.stat_id, "0"))
            except ValueError:
                return 0.0
            return (val - league_avg) / denom

        if cat.stat_id == "3":  # AVG
            return self._avg_sgp(player, baseline, league_avg, denom)
        elif cat.stat_id == "4":  # OBP
            return self._obp_sgp(player, baseline, league_avg, denom)
        elif cat.stat_id == "26":  # ERA
            return self._era_sgp(player, baseline, league_avg, denom)
        elif cat.stat_id == "27":  # WHIP
            return self._whip_sgp(player, baseline, league_avg, denom)
        else:
            # Other rate stats (SLG, K/BB): rough approximation
            try:
                val = float(player.stats.get(cat.stat_id, "0"))
            except ValueError:
                return 0.0
            return (val - league_avg) / denom

    def _avg_sgp(
        self, player: PlayerStats, baseline: dict[str, float],
        league_avg: float, denom: float,
    ) -> float:
        """AVG SGP: insert player H/AB into N-1 baseline team."""
        hab = player.stats.get("60", "")
        try:
            parts = hab.split("/")
            p_h, p_ab = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            return 0.0
        if p_ab == 0:
            return 0.0
        team_h = baseline["h"] + p_h
        team_ab = baseline["ab"] + p_ab
        team_avg = team_h / team_ab if team_ab else 0
        return (team_avg - league_avg) / denom

    def _obp_sgp(
        self, player: PlayerStats, baseline: dict[str, float],
        league_avg: float, denom: float,
    ) -> float:
        """OBP SGP: insert player into N-1 baseline."""
        hab = player.stats.get("60", "")
        try:
            parts = hab.split("/")
            p_h, p_ab = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            return 0.0
        try:
            p_bb = float(player.stats.get("18", "0"))
        except ValueError:
            p_bb = 0.0
        try:
            p_hbp = float(player.stats.get("19", "0"))
        except ValueError:
            p_hbp = 0.0
        try:
            p_sf = float(player.stats.get("20", "0"))
        except ValueError:
            p_sf = 0.0

        team_h = baseline["h"] + p_h
        team_ab = baseline["ab"] + p_ab
        team_bb = baseline["bb"] + p_bb
        team_hbp = baseline["hbp"] + p_hbp
        team_sf = baseline["sf"] + p_sf
        denom_obp = team_ab + team_bb + team_hbp + team_sf
        if denom_obp == 0:
            return 0.0
        team_obp = (team_h + team_bb + team_hbp) / denom_obp
        return (team_obp - league_avg) / denom

    def _era_sgp(
        self, player: PlayerStats, baseline: dict[str, float],
        league_avg: float, denom: float,
    ) -> float:
        """ERA SGP: insert player ER/IP into N-1 baseline team."""
        try:
            p_er = float(player.stats.get("40", "0"))
        except ValueError:
            return 0.0
        p_ip = _parse_ip(player.stats.get("50", "0"))
        if p_ip == 0:
            return 0.0
        team_er = baseline["er"] + p_er
        team_ip = baseline["ip"] + p_ip
        team_era = (team_er * 9 / team_ip) if team_ip else 0
        return (team_era - league_avg) / denom

    def _whip_sgp(
        self, player: PlayerStats, baseline: dict[str, float],
        league_avg: float, denom: float,
    ) -> float:
        """WHIP SGP: insert player (BB+H allowed)/IP into N-1 baseline."""
        try:
            p_bb = float(player.stats.get("39", "0"))
        except ValueError:
            p_bb = 0.0
        try:
            p_ha = float(player.stats.get("35", "0"))
        except ValueError:
            p_ha = 0.0
        p_ip = _parse_ip(player.stats.get("50", "0"))
        if p_ip == 0:
            return 0.0
        team_bb = baseline["bb"] + p_bb
        team_ha = baseline["ha"] + p_ha
        team_ip = baseline["ip"] + p_ip
        team_whip = (team_bb + team_ha) / team_ip if team_ip else 0
        return (team_whip - league_avg) / denom

    # --- Replacement level ---

    def _compute_replacement_sgp(
        self, replacement_by_pos: dict[str, list[PlayerStats]],
    ) -> tuple[dict[str, float], float, float]:
        """Compute per-position replacement-level SGP.

        For each position, filters to players with sufficient sample size,
        computes raw SGP, and averages the top few as the baseline.

        Returns (per_position_dict, batting_fallback, pitching_fallback).
        """
        repl: dict[str, float] = {}
        bat_vals: list[float] = []
        pitch_vals: list[float] = []

        for pos, players in replacement_by_pos.items():
            qualified_sgps: list[float] = []
            for p in players:
                sgp = self._raw_player_sgp(p)
                if sgp is not None:
                    qualified_sgps.append(sgp)

            if not qualified_sgps:
                continue

            qualified_sgps.sort(reverse=True)
            top_n = qualified_sgps[:_REPL_POOL_SIZE]
            pos_repl = max(0.0, sum(top_n) / len(top_n))
            repl[pos] = pos_repl

            if pos in _BATTING_POSITIONS:
                bat_vals.append(pos_repl)
            else:
                pitch_vals.append(pos_repl)

        repl_bat = sum(bat_vals) / len(bat_vals) if bat_vals else 0.0
        repl_pitch = sum(pitch_vals) / len(pitch_vals) if pitch_vals else 0.0
        return repl, repl_bat, repl_pitch

    # --- Public API ---

    def player_sgp(self, player: PlayerStats) -> float | None:
        """Compute marginal SGP for a player (raw SGP minus replacement level).

        Returns None if the player has insufficient sample size.
        """
        raw = self._raw_player_sgp(player)
        if raw is None:
            return None

        # Find replacement level: try the player's specific position first,
        # then fall back to the batter/pitcher average
        is_batter = _is_batter(player)
        repl = self._repl_batting if is_batter else self._repl_pitching
        for pos in player.position.split(","):
            pos = pos.strip()
            if pos in self._repl_by_pos:
                repl = self._repl_by_pos[pos]
                break

        return raw - repl
