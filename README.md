# GKL CLI — Fantasy Baseball Command Center

A terminal-based application for Yahoo Fantasy Baseball leagues. Combines league data, advanced analytics, and real-time MLB scores into a single interface you can run from any terminal.

Built with [Textual](https://github.com/Textualize/textual) for the terminal UI and the Yahoo Fantasy Sports API.

## Features

### Matchup Scoreboard

The main screen shows head-to-head matchups for the current week with live scoring across all stat categories. Switch between weekly, daily, and full season views. Navigate weeks with arrow keys or jump to any week directly.

**Key bindings:** `w` weekly | `d` daily | `n` season | `←→` change week | `e` select week

![Matchup Scoreboard](docs/screenshots/scoreboard.png)

### Roto Standings

Full roto standings with per-category rankings and point totals across all teams. Includes a cumulative roto rank chart showing how each team's position has changed week over week throughout the season.

**Key binding:** `s` from main screen

![Roto Standings](docs/screenshots/roto-standings.png)

### Head-to-Head Simulator

Simulates your roster against every other team in the league for any given week. Shows projected win/loss/tie record and identifies which categories you'd win or lose against each opponent. Includes season-long power rankings aggregated across all weeks.

**Key binding:** `h` from main screen

![H2H Simulator](docs/screenshots/h2h-simulator.png)

### Roster Analysis

Detailed breakdown of your team's roster with league stats alongside advanced Statcast metrics from Baseball Savant. Includes:

- **SGP (Standings Gain Points):** Measures each player's contribution to your roto standings relative to replacement level
- **Yahoo Rankings:** Current season rank (Y!) and pre-season rank (Pre) for every player
- **Statcast Data:** Exit velocity, barrel rate, hard hit %, expected stats (xBA, xSLG, xwOBA), K%, BB%, and whiff rate
- **Roto Rank Bar:** Your team's ranking in each scoring category at a glance
- **Auction Values:** What you paid vs. average draft cost

Switch between season, last 14 days, and last 30 days. View any team in the league.

**Key binding:** `t` from main screen

![Roster Analysis](docs/screenshots/roster-analysis.png)

### Free Agent Browser

Browse available free agents with the same stats + Statcast data as the roster view. Features:

- **Default view:** Top 15 overall free agents + top 5 at each position (C, 1B, 2B, 3B, SS, LF, CF, RF, SP, RP)
- **Position filter:** Press `p` to filter by a specific position
- **Search:** Press `/` to search by player name
- **Stat views:** Season, last 7 days, or last 30 days
- **Rankings:** Yahoo current and pre-season rankings alongside SGP values

**Key binding:** `f` from main screen

![Free Agents](docs/screenshots/free-agents.png)

### League Transactions

Explore transaction activity across the league with three views:

- **Recent Transactions:** The 10 most recent adds, drops, and add/drops with timestamps and team details
- **Most-Added Players:** Top 10 players by add count, showing every fantasy team they've appeared on
- **Adds by Position per Team:** Matrix showing how many times each team has added players at each position — useful for spotting roster construction trends

**Key binding:** `x` from main screen

![Transactions](docs/screenshots/transactions.png)

### MLB Live Scoreboard

Real-time MLB game scores and linescore data pulled from the MLB Stats API. Shows today's games with current inning, score, and pitching matchups.

**Key binding:** `g` from main screen

![MLB Scoreboard](docs/screenshots/mlb-scoreboard.png)

## Installation

Download the latest release for your operating system from the [Releases page](https://github.com/johntylernyc/gkl-cli/releases).

### macOS

```bash
cd ~/Downloads
chmod +x gkl-macos-arm64
./gkl-macos-arm64
```

> If macOS shows a security warning, right-click the file in Finder and select **Open**, or run: `xattr -d com.apple.quarantine gkl-macos-arm64`

### Windows

1. Download `gkl-windows-amd64.exe`
2. Open **Command Prompt** or **PowerShell**
3. Navigate to the download folder and run:

```
cd %USERPROFILE%\Downloads
gkl-windows-amd64.exe
```

### Linux

```bash
cd ~/Downloads
chmod +x gkl-linux-amd64
./gkl-linux-amd64
```

## First-Time Setup

On the first run, the app will guide you through creating Yahoo API credentials:

1. Go to https://developer.yahoo.com/apps/create/
2. Fill in the following:
   - **Application Name:** Choose a name for your application
   - **Description:** A terminal application for managing my fantasy baseball roster
   - **Homepage URL:** Your personal website, or any website formatted as `https://www.example.com`
   - **Redirect URI:** `https://localhost:8080`
   - **OAuth Client Type:** Confidential Client
   - **API Permissions:** Fantasy Sports (Read)
3. After creating the app, Yahoo will provide a **Client ID** and **Client Secret** — paste these into the app when prompted
4. The app will open your browser to authorize access to your Yahoo Fantasy league — approve the request and paste the verification code back into the app

Your credentials are saved locally (`~/.config/gkl/`) so you only need to do this once.

## Keyboard Shortcuts

From the main scoreboard:

| Key | Action |
|-----|--------|
| `s` | Roto Standings |
| `h` | H2H Simulator |
| `t` | Roster Analysis |
| `f` | Free Agents |
| `x` | Transactions |
| `g` | MLB Scoreboard |
| `w` | Weekly stats view |
| `d` | Daily stats view |
| `n` | Season stats view |
| `←` `→` | Previous/next week |
| `e` | Select specific week |
| `r` | Refresh data |
| `q` | Quit |

All sub-screens: `Escape` or `q` to go back.

## Data Sources

- **League data:** [Yahoo Fantasy Sports API](https://developer.yahoo.com/fantasysports/guide/)
- **Advanced metrics:** [Baseball Savant / Statcast](https://baseballsavant.mlb.com/)
- **Live scores:** [MLB Stats API](https://statsapi.mlb.com/)

## Development

Requires Python 3.12+.

```bash
# Clone and install
git clone https://github.com/johntylernyc/gkl-cli.git
cd gkl-cli
uv sync

# Run
uv run gkl

# Run tests
uv run pytest
```
