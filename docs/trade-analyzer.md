# Trade Analyzer

Overview: The purpose of trade analyzer is to help users both analyze trades that have been proposed to them as well as find potential trade partnes and model deals.

Capabilities:
- Analyze a trade that has been proposed to you by another manager.
- Find a potential trading partner to propose a trade to another manager.
- Contextualize the merits of potential trades both in their raw stat performance comparatively but also in the context of how the trade will affect the team's roto and h2h performance in their league.

As a manager, when I'm evaluating the trade market, I frequently do these things: 
1) Identify a position, category, or set of categories I would like to improve. Often this includes: 
- Looking at my team's stat performance during the past 14-30 days 
- Scanning the Y! ranks for each position on my roster 
- Looking at my roto performance in a given category
- Looking at my H2H performance in a given category

And then deciding "it would be good if I could improve performance at [position, stat category, etc]"

Note: As I can't necessarily compete in all 18 categories, often I'm looking for those areas where an improvement my meaningfully improve my place in the H2H standings by ensuring I'm more often able to control the outcome of that specific category. You can think of this as wanting to be able to get to a point where most weeks, comparing my roster to the team I'm facing, I can expect to have a good chance at winning 9-12 categories. 

2) Once I've identified a position or set of categories I want to improve, I start to brose other managers rosters to understand which managers might be good trading partners. Generally, I'm looking for some mixture of the following: 
- A manager with an obvious need (positionally, statistically), 
- That has a desirable player at the position or stat categories I'm trying to improve
- And has either some inherent exisiting roster depth at that position/within the set of categories, or
- There is a suitable replacement level player at that position in the free agent pool. 

3) Using this knowledge of what teams I might be able to create a deal with, I need to determine which positions or players on my team I'm in the best position to trade to further narrow down the list of possible trading partners and trading targets based on a deal that would have a chance of being accepted. When thinking about this, I consider: 

- The needs of the team I intend to orchestrate a trade, 
- Offensive positions that I have more than 1 eligible player (ie, if I trade one player, I won't have an empty hole in that position as a result),  
- Players I have that are performing very well that could be 'sell high' candidates because they lack a historical track record or their statcast advanced metrics don't support their standard category performance season to date (ie, often in the first part of the season I'll attempt to trade a surging player showing promise of a breakout in favor of a more trusted player with consistent year over year production in the 'peak' age window 27-32),
- Depth at free agency in a given position and how I could backfill a position on my team if I were willing to make a deal that left a hole in my lineup. 

Much of what I've described is a heuristic driven process where I click through the league pages to try to spot a potential deal. Some of the insights that would help me do this more efficiently are if I could understand if I were to make a given trade, how would that: 
a) have affected my stats season to date, 
b) what would my place in the roto standings be as a result,
c) how would my h2h matchups have changed 
d) what will this do to my projected roto ranking through the end of the season (using a simple projection forward of each player's stats) 
e) how will this change my projected w/l/t record for the remaining h2h matchups using the h2h score projection we have in place to view future matchup weeks

Generally, this feature needs to be able to: 
1) handle trade discovery --> the user selects a position, stat(s) that they want to improve and the feature finds suitable trade targets and presents analysis.
2) handle trading block --> the user selects a specific player they want to trade and the feature finds the best possible trade targets to improve their roster. 
3) analyze a proposed trade --> the user can select specific players from any two rosters and get an analysis on the trade

Lastly, it would be good if the user has entered an anthropic key, if we could get a quick summary beyond the stats views we've requested above that act as an overview of the potential trade, pros, cons, as well as 2-3 quick sentences for 1) how to sell the deal to another manager in cases where the user wants to perform the trade 2) how to decline or possibly counter the manager if the user was proposed the trade and isn't interested in that particular deal but might consider something else.

This is going to be a fairly complex feature, so think through it well. While developing this feature, this document acts as a master project file: all decisions need to be logged here, all tasks need to be listed, the feature isn't complete until all tasks are completed and the changes have been accepted.

---

## Implementation Log

### Phase 1: Analyze Proposed Trade (completed 2026-04-19)

**Status:** Implemented on `feature/trade-analyzer` branch.

**Key files:**
- `gkl/trade.py` — computation engine (new)
- `gkl/app.py` — `TradeAnalyzerScreen` UI

**Decisions:**

1. **Rate stat computation uses delta approach, not full recomputation.** Team-level stats include historical contributions from players no longer on the roster (previously dropped/traded players). Summing current roster players' components does not match the team total. Solution: derive the team's baseline components from the team-level rate stat + denominator (e.g., ERA × IP / 9 = ER), then apply only the traded players' component deltas. This preserves the team's actual baseline while correctly adjusting for the swap.

2. **Roto impact is shown as a full league standings table.** Roto is a zero-sum ranking system — when one team improves in a category, every team they pass loses a rank point. A simple "your rank changed" display was confusing when the rank dropped despite points increasing. The full 18-team table with before/after points and rank arrows makes the ripple effect transparent.

3. **H2H "Season-Aggregate Hypothetical" is a single simulation, not per-week replay.** The current implementation takes each team's full season stats and simulates "if this were one matchup, who wins each category?" This is useful as a power-ranking signal but does NOT show how actual weekly matchups would have changed. Labeled clearly as season-aggregate with a note pointing to the weekly replay section (Phase 2).

4. **Player selection uses DataTable with star markers.** Enter toggles selection. Selected players show ★ in the first column. Tracked via `set[str]` of player_keys.

5. **Keybinding is `T` (shift+T) from the main scoreboard** to avoid conflict with `t` (roster).

### Tasks

| # | Task | Status |
|---|------|--------|
| 1 | Create `gkl/trade.py` computation engine | ✅ Done |
| 2 | Add `TradeAnalyzerScreen` UI | ✅ Done |
| 3 | Wire to scoreboard with `T` keybinding | ✅ Done |
| 4 | Test end-to-end trade analysis | ✅ Done |
| 5 | Fix `display` kwarg error in compose | ✅ Done |
| 6 | Fix rate stat recomputation (delta approach) | ✅ Done |
| 7 | Show full league roto standings table | ✅ Done |
| 8 | Clarify H2H section labeling | ✅ Done |
| 9 | Update this document with decisions | ✅ Done |

### Phase 2: H2H Replay & Hypothetical (completed 2026-04-19)

**Status:** Implemented on `feature/trade-analyzer` branch.

**Decisions:**

6. **H2H Weekly Replay uses per-player weekly stats, not season stats.** The initial implementation incorrectly applied season-long player stats as the delta for each week's team stats. This produced wrong results for rate stats (e.g., subtracting a player's season ERA components from a single week's team ERA). Fix: fetch per-player weekly roster stats via `get_roster_stats(team_key, week)` for both teams for each completed week, find the traded players' actual weekly contributions, and use those as the delta. Trade-off: 2 additional API calls per week (2 teams × N weeks), but accuracy is critical.

7. **H2H Hypothetical replays every week against every opponent.** Instead of one hypothetical using season-aggregate stats, the new approach replays each completed week against ALL opponents using actual weekly team stats with the trade applied. For a 4-week season with 18 teams, that's 68 hypothetical matchups (4 × 17). This captures weekly variance rather than averaging it away, and produces a more representative hypothetical record.

8. **Roto standings table ordered by post-trade rank.** The table was initially ordered by before-trade rank, making it unclear what the new standings would be. Now ordered by after-trade total so the table reads as "here are the standings if this trade happened." Includes batting and pitching roto subtotal columns with deltas to show where the impact comes from.

9. **Roto is zero-sum — other teams' points change even though their stats don't.** Roto points are relative rankings per category (1-18). When a traded team improves in a category, they pass other teams, causing those teams to lose a rank point in that category. This is correct and expected — the full league table makes this visible.

### Tasks

| # | Task | Status |
|---|------|--------|
| 10 | Implement `replay_h2h_with_trade()` | ✅ Done |
| 11 | Implement `compute_h2h_hypothetical()` | ✅ Done |
| 12 | Fix replay to use per-player weekly stats | ✅ Done |
| 13 | Sort roto table by post-trade rank | ✅ Done |
| 14 | Add batting/pitching roto subtotal columns | ✅ Done |
| 15 | Move weekly replay above trade partner impact | ✅ Done |
| 16 | Relabel H2H sections for clarity | ✅ Done |

### Phase 3: Trading Block (completed 2026-04-19)

**Status:** Implemented on `feature/trade-analyzer` branch.

**Decisions:**

10. **Three metrics per trade target, not just SGP.** SGP alone is a black box. Each target now shows: ΔSGP (player-level value swap), ΔRoto (actual roto points change from full league re-ranking), and ΔWin% (actual H2H record change). These three independent signals give confidence when they agree and flag caution when they diverge.

11. **H2H win% uses actual weekly replay, not season-aggregate simulation.** For each candidate, the system runs `replay_h2h_with_trade` using per-player weekly stats from every completed week. This means fetching weekly rosters for all 18 teams × all weeks — expensive in API calls but produces the true change in H2H record rather than an approximation.

12. **Pre-filter by SGP before running expensive simulations.** Position-eligible candidates are first sorted by net SGP, then the top 50 are kept for full roto + H2H simulation. This limits the computation cost while ensuring the best candidates aren't missed.

13. **Target list sorted by roto points delta.** After computing all three metrics, candidates are ranked by ΔRoto (most impactful trades first). ΔRoto is the most holistic measure since it reflects actual standings movement.

14. **Selecting a target transitions to full analyze mode.** When a target is selected from the trading block list, the screen switches to analyze mode with both rosters shown in the left pane (traded players starred) and the full Phase 1 analysis in the right pane.

15. **IL/NA players excluded from scan.** Players on the injured list or not active are filtered out — they can't contribute immediately and would distort the analysis.

### Tasks

| # | Task | Status |
|---|------|--------|
| 17 | Add `find_trade_targets()` to `trade.py` | ✅ Done |
| 18 | Add `TradeModeSelectorModal` | ✅ Done |
| 19 | Add Trading Block mode UI flow | ✅ Done |
| 20 | Add roto delta + H2H win% to target scoring | ✅ Done |
| 21 | Use actual weekly replay for H2H win% | ✅ Done |
| 22 | Fix `all_teams` undefined variable | ✅ Done |
| 23 | Show both rosters when selecting a target | ✅ Done |

### Phase 4: AI Summary (in progress)

**Goal:** When an Anthropic API key is available, provide a Claude-powered narrative analysis of the trade:
- Pros and cons summary
- 2-3 sentences on how to sell the deal to the other manager
- 2-3 sentences on how to decline or counter if you were proposed the trade

**Approach:**
- Check `load_anthropic_key()` for API key availability
- Format the `TradeImpact` data (category deltas, roto movement, H2H replay results) into a structured prompt
- Use the Anthropic SDK directly (lightweight call, not full Skipper tool suite)
- Display the AI summary in a section at the bottom of the analysis results
- Use the model from `DEFAULT_MODEL` in `skipper.py`

### Phase 5: Trade Discovery (not started)

**Goal:** User selects categories/positions to improve. System scans all rosters for players who would improve those areas and identifies which of the user's players could be reasonable trade currency. 