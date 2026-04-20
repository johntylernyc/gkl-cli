# GKL Podcast

Overview: 
The goal of this feature is to use the available data and insights we've been able to create in our application, and generate a podcast that discusses the league. 

Capabilities:
- Use the data capabilities of our application to retrieve the relevant datasets from the various APIs we use.
- Prepare and pre-process this data in a way that it can be provided to an endpoint in a clear and well documented manner. 
- Leverage the Google Podcast API to send this data to and generate the audio podcast. 
- Update our application to create a "News" page in our application, navigated to from the homepage like any of the other features in our appplication, where users can select a link to stream the podcast online in their browser.

Requirements:

We think this will expand over time to have all sorts of different versions of the podcast, we can think of each of these versions as "Segments". Each segment will have a general theme and cadence at which it runs. For example: 

- Weekly recap: a segment that runs on Mondays that recaps the matchups from the week prior. The podcast quickly summarizes each matchup's results, and then jumps into the big stories from that week. Which teams surged or tanked, standout performances from individual teams or players, key transactions, and a brief commentary on the week ahead and what to expect. Available once weekly on Monday 7am est, 8-10 minutes in length. 
- Daily dive: a daily episode that goes over yesterday's team performances and each of their matchups in that week of the fantasy season. Available daily at 8am est, 4-6 minutes in length.
- League standings: a zoomed out view of the league, and each team's roto, power rankings, and official h2h standings. How things have changed week-over-week. A discussion of specific manager's and their team's needs or strengths. And, from week 15 forward, commentary on the emerging playoff picture for that league. Available once weekly on Tuesday at 7am est, 8-10 minutes in length.
- The wire: a discussion of available free agents and potential rosters they could add value. Available once weekly on Sunday night at 5pm est, 4-6 minutes in length. 

For the initial version, we just want to get one variant of the podcast working and thoroughly refine. We'll add to the publishing schedule afterward. We will begin with the Weekly recap.

We want each segment to have distinct host personalities that are consistent episode over episode. The hosts should have names and characteristics that persist. We are a bit indifferent to the specifics here provided the requirement is met. The podcasts described above will target a league-wide audience for now. The hosts should take the role of traditional sports analysts and news media personalities. The personalities can be both complimentary and critical of individual teams and decisions in the league. They should mostly remain professional (but not cold, we want them to be engaging). 

We will use Google Podcast API, or a suitable alternative. The general idea is that we wil prepare data packs using the calls we make in our application elsewhere (e.g., h2h sim, standings, etc) and that these will become data packs for the Podcast API. We think it might be beneficial if we define the types of questions or analyses we want to be a recurring part of each segment so the Podcast API can produce something that feels consistent over episodes. Of course, like any broadcast, there should be some variation to keep things feeling fresh. 

Pre-requisites: 

In addition to the holistic data packs we want to provide the podcast API, we want to re-use some of the functionality in ask-skipper.py to get some insights about the league and more interesting datasets than the base data that is being returned when we make API calls. This will also ensure some consistency between the podcast content and the analysis provided by our application llm. Thus, before we can begin work on this feature, we would like to revisit our current ask-skipper.py functionality, and specifically we need to update the skiills, tools, and scripts to reflect our current application and recent improvements: 

1) we've recently added a trade analyzer with 3 trade modes that should be used to improve our analyze trade capabilities in ask-skipper,
2) we've recently incorporated learnings from our trade analyzer into our "compare player" functionality, and in cases where free agents are being evaluated against a team's current roster this analytical view should be taken into consideration
3) we've expanded the capabilities of our mlb game feature and so questions about MLB game outcomes as they relate to fantasy team performance and otherwise should be able to leverage these
4) we generally want to review the work on ask-skipper.py to date to see where we can improve the agent's ability to retrieve data and answer user questions. 

Note: We don't want to overly constrain the podcast API to the ask-skipper capabilities, as we generally believe an API like Google's NotebookLM will out-perform our agent's naive capabilities. The goal here is to incrementally improve ask-skipper.py while doing this work to create some alignment in the narrative and analyses being shown to users, while generally leveraging the 3rd party podcast api as fully as possible to get the best outcomes. 

Skipper refactor complete — takeaways from v0.6.2:

The skipper refactor shipped as v0.6.2 and lays the groundwork for podcast seeding. Tools Skipper can now call: `get_league_standings`, `get_h2h_standings`, `analyze_strength_of_schedule`, `get_matchup_scoreboard`, `get_weekly_recap`, `get_team_roster`, `find_trade_targets`, `analyze_trade`, `discover_trade_scenarios`, `compare_add_drop`, `get_mlb_scoreboard`, `get_mlb_boxscore`, `get_statcast_profile`, `get_free_agents`. These are the same tools that should back the "suggested topics" half of each podcast data pack.

Hardening learnings worth preserving when we build the podcast pipeline:

- Player availability must be tagged explicitly. Every player listed by Skipper tools now carries one of `[ACTIVE]`, `[BENCH — active]`, `[BENCH — healthy; SPs rotate through bench on rest days]`, `[INJURED/IL]`, `[NOT-ACTIVE …]`, or `[FREE AGENT]`. The podcast data packs should carry the same tags so the podcast API doesn't speculate about injuries or mistake a bench SP on a rest day for a drop candidate.
- NA players don't count against max roster size. This is easy to miss; Skipper had to be taught it explicitly. Podcast hosts commenting on roster moves should inherit the same rule.
- Every named current-season claim has to be grounded in data just pulled by a tool call — no characterizing a player from memory. The podcast pipeline should pass the actual current-year line, not rely on the model's prior.
- Statcast (xBA, xSLG, xERA, Barrel%, HardHit%) is required context before endorsing a pickup or trade target. Podcast segments that discuss "standout performances" or "who to watch" should include Statcast regression signals in the data pack so the hosts can distinguish sustainable performance from luck.
- Prompt formatting rules (no markdown tables, no heavy `**bold**` / `###` / `---`, no ΔRoto-style shorthand): these were tuned for TUI consumption. For podcasts, the analog is "the hosts should speak in flowing analyst language," and the data pack should avoid shipping pre-formatted prose that the hosts will awkwardly read verbatim.

Skipper will keep improving independently of the podcast — we expect more tools and better prompts over time. The podcast implementation should treat Skipper's toolkit as a dependency we can call into, not a fixed API.

Data pack structure:

Each podcast data pack (one per episode) has two halves:

1. Raw datasets — the large, objective views the podcast API can analyze directly. These are the outputs of the same tools Skipper uses, serialized without editorial commentary:
   - League roto standings (all teams, all categories, points and ranks)
   - H2H records and power rankings
   - Strength-of-schedule table (actual record vs. power-ranking record vs. luck factor)
   - Weekly matchup scoreboards for the segment's time window
   - Full rosters for all teams with availability tags, season stats, and trailing-window stats (last7/last30)
   - Transactions log (trades, adds, drops) for the window
   - Statcast profiles for players the data pack highlights (not every player — just the ones the segment calls out)
   - MLB game context where relevant (who won/lost, key player lines)
   The podcast API consumes these datasets and does its own pattern-finding and analysis.

2. Suggested topics — a shorter file produced by running Skipper against the segment's prompt template. This gives the podcast API a narrative spine to lean on: which stories stood out, which teams surged or tanked, which transactions mattered, which matchups are worth highlighting. The suggested-topics output grounds the podcast in the same analytical framing users see in Ask Skipper, so the narrative feels consistent with the app.

Model policy for Skipper-seeded podcast generation:

When Skipper is invoked to produce the suggested-topics half of a data pack, it must use `claude-opus-4-6`, not the default Sonnet. Podcast generation runs infrequently (one segment per schedule slot) and is worth the stronger reasoning. The default Skipper model in the interactive "Ask Skipper" feature remains `claude-sonnet-4-6` — this rule only applies to the podcast seeding code path. Expose this as a constant (e.g. `SKIPPER_PODCAST_MODEL = "claude-opus-4-6"`) so it's easy to audit and swap later.

Open questions: 

This application runs both locally and is hosted on the web via railway.com; We're not yet sure the best way to have these generated and served to users. For the initial version, we're ok with not having these episodes generated on a schedule but instead triggered on-demand. I think the long-term version is a scheduled series of programs where past podcasts act as input to future ones, but right now we just want to be able to programmatically generate the podcast and prove the concept. We'll need options to consider how best to incrementally build this. 

Confirm access to Google's NotebookLM API to create engaging podcasts programmatically. If this isn't available, discovery to find a suitable alternative. 

The data pack formats that are provided to the podcast api should be informed by the service we use and it's recommendations for best practices in providing data to use.

How best to get each of the segments to maintain a consistent theme/structure while not being overly formulaic. The implementation plan will need to take on a producer role to decide the format for each segment and give named recurring segments and some guardrails without overly constraining the llm generation the podcast. The segments, structures, personalities of the hosts, and other guardrails should be defined in an artifact in our project so that we can come back to and refine these over-time. We should maintain seperate files for each segment.

How to store the generated mp3s for historic retrieval and to surface a broadcast library.

How much will this cost? We'll want to monitor cost during development as well.

Assumptions:

We can build this in a way that works for both the terminal application running locally as well as the deployed version on gklbaseball.com using railway.com as the infrastructure provisioning provider (as is currently the case). When we get to serving this through the publicly hosted site, the podcasts should require a login/league auth to listen and a user should only be able to listen to episodes related to their league.

MVP:

A single example of the weekly recap using data provided by the application that can be used to refine and iterate the podcast generation process before moving onto figuring out how to do this in a repeatable way.

Housekeeping:

This .md file will act as a place to store all decisions made in the course of developing this capability, as well as a project tracker. The implementation plan should be captured at the start in this file and this file should be regularly updated throughout development. trade-analyzer.md is a good example of what "good" looks like for using the markdown to update progress throughout development. At the end of development, we want to capture any future opportunities or work that was not completed as issues in our remote repository (similar to how we did issues #10 an #11 in our remote repo after work on trade-analyzer.md wrapped). 