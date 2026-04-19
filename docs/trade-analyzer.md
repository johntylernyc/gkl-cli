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

### Phase 2: H2H Weekly Replay (in progress)

**Goal:** For each completed week, fetch weekly team stats, apply the trade retroactively, re-simulate that week's actual H2H matchup, and show how the season W/L/T record would have changed.

**Approach:**
- Use `shared_cache.week_team_stats` and `shared_cache.week_matchups` to get per-week data
- For each week, apply `apply_trade_to_team()` to modify the two traded teams' weekly stats
- Re-run `who_wins()` on each category for that week's actual matchup opponents
- Show a week-by-week table: week #, opponent, actual result, projected result, change
- Summarize with total season W-L-T before vs after

### Phase 3: Trade Discovery & Trading Block (not started)

**Goal:** User selects categories/positions to improve; system finds suitable trade targets and partners across the league.

### Phase 4: AI Summary (not started)

**Goal:** Claude-powered narrative analysis of the trade — pros/cons, talking points to sell the deal, counter-offer suggestions. 