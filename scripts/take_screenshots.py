#!/usr/bin/env python3
"""Automated screenshot capture for README documentation.

Navigates through each screen of the GKL app, waits for data to load,
and saves SVG screenshots to docs/screenshots/.

Usage:
    uv run python scripts/take_screenshots.py
"""

import asyncio
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gkl.yahoo_auth import YahooAuth, load_credentials
from gkl.yahoo_api import YahooFantasyAPI
from gkl.app import GklApp

SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"


async def take_screenshots():
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    saved = load_credentials()
    if not saved:
        print("No Yahoo credentials found. Run 'uv run gkl' first to set up.", file=sys.stderr)
        sys.exit(1)
    client_id, client_secret = saved

    auth = YahooAuth(client_id=client_id, client_secret=client_secret)
    auth.get_token()
    api = YahooFantasyAPI(auth)
    app = GklApp(api)

    async with app.run_test(size=(160, 48)) as pilot:
        # Wait for initial load (league selection + scoreboard)
        await asyncio.sleep(10)

        # 1. Main Scoreboard
        app.save_screenshot("scoreboard.svg", path=str(SCREENSHOT_DIR))
        print("✓ scoreboard.svg")

        # 2. League Standings — press 's'
        await pilot.press("s")
        await asyncio.sleep(8)
        app.save_screenshot("roto-standings.svg", path=str(SCREENSHOT_DIR))
        print("✓ roto-standings.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 3. H2H Simulator — press 'h'
        await pilot.press("h")
        await asyncio.sleep(10)
        app.save_screenshot("h2h-simulator.svg", path=str(SCREENSHOT_DIR))
        print("✓ h2h-simulator.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 4. Roster Analysis — press 't', wait for team modal, select first team
        await pilot.press("t")
        # Wait for shared cache to load + team select modal to appear
        for _ in range(20):
            await asyncio.sleep(1)
            try:
                modal = app.screen_stack
                if any("TeamSelect" in type(s).__name__ for s in modal):
                    break
            except Exception:
                pass
        await asyncio.sleep(1)
        await pilot.press("enter")
        await asyncio.sleep(10)
        app.save_screenshot("roster-analysis.svg", path=str(SCREENSHOT_DIR))
        print("✓ roster-analysis.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 5. Free Agents — press 'f'
        await pilot.press("f")
        await asyncio.sleep(8)
        app.save_screenshot("free-agents.svg", path=str(SCREENSHOT_DIR))
        print("✓ free-agents.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 6. Transactions — press 'x'
        await pilot.press("x")
        await asyncio.sleep(8)
        app.save_screenshot("transactions.svg", path=str(SCREENSHOT_DIR))
        print("✓ transactions.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 7. Player Explorer — press 'p'
        await pilot.press("p")
        await asyncio.sleep(3)
        app.save_screenshot("player-explorer.svg", path=str(SCREENSHOT_DIR))
        print("✓ player-explorer.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 8. Watchlist — press 'l'
        await pilot.press("l")
        await asyncio.sleep(8)
        app.save_screenshot("watchlist.svg", path=str(SCREENSHOT_DIR))
        print("✓ watchlist.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        # 9. MLB Scoreboard — press 'g'
        await pilot.press("g")
        await asyncio.sleep(6)
        app.save_screenshot("mlb-scoreboard.svg", path=str(SCREENSHOT_DIR))
        print("✓ mlb-scoreboard.svg")
        await pilot.press("escape")
        await asyncio.sleep(1)

        print(f"\nAll screenshots saved to {SCREENSHOT_DIR}")


if __name__ == "__main__":
    asyncio.run(take_screenshots())
