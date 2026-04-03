"""Auto-update support for gkl-cli."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Static

from gkl._version import __version__

GITHUB_API_URL = "https://api.github.com/repos/johntylernyc/gkl-tui/releases/latest"
CHECK_INTERVAL_HOURS = 24
CONFIG_DIR = Path.home() / ".config" / "gkl"
CHECK_FILE = CONFIG_DIR / "update_check.json"

ASSET_MAP = {
    ("darwin", "arm64"): "gkl-macos-arm64",
    ("darwin", "x86_64"): "gkl-macos-arm64",  # Rosetta fallback
    ("linux", "x86_64"): "gkl-linux-amd64",
    ("linux", "amd64"): "gkl-linux-amd64",
    ("win32", "amd64"): "gkl-windows-amd64.exe",
    ("win32", "x86_64"): "gkl-windows-amd64.exe",
}


def _parse_version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.lstrip("v").split("."))


@dataclass
class UpdateInfo:
    latest_version: str
    asset_url: str
    release_notes: str


def _should_check() -> bool:
    """Return True if enough time has passed since the last check."""
    try:
        data = json.loads(CHECK_FILE.read_text())
        last = datetime.fromisoformat(data["last_check"])
        elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        return elapsed >= CHECK_INTERVAL_HOURS
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return True


def _record_check() -> None:
    """Record that we just performed an update check."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CHECK_FILE.write_text(
        json.dumps({"last_check": datetime.now(timezone.utc).isoformat()})
    )


def _get_asset_name() -> str | None:
    """Return the expected release asset name for this platform."""
    machine = platform.machine().lower()
    key = (sys.platform, machine)
    return ASSET_MAP.get(key)


def check_for_update() -> UpdateInfo | None:
    """Check GitHub for a newer release. Returns None if no update or on any error."""
    if not getattr(sys, "frozen", False):
        return None
    if not _should_check():
        return None

    try:
        resp = httpx.get(GITHUB_API_URL, timeout=5, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    finally:
        _record_check()

    tag = data.get("tag_name", "")
    if not tag:
        return None

    try:
        if _parse_version(tag) <= _parse_version(__version__):
            return None
    except (ValueError, TypeError):
        return None

    asset_name = _get_asset_name()
    if not asset_name:
        return None

    asset_url = None
    for asset in data.get("assets", []):
        if asset.get("name") == asset_name:
            asset_url = asset.get("browser_download_url")
            break

    if not asset_url:
        return None

    return UpdateInfo(
        latest_version=tag.lstrip("v"),
        asset_url=asset_url,
        release_notes=data.get("body", ""),
    )


def download_update(asset_url: str) -> Path:
    """Download the update binary to a temp file next to the current executable."""
    current = Path(sys.executable)
    temp_path = current.with_suffix(".update_tmp")

    with httpx.stream("GET", asset_url, timeout=60, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(temp_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=8192):
                f.write(chunk)

    return temp_path


def apply_update(new_binary: Path) -> None:
    """Replace the current executable with the downloaded update."""
    current = Path(sys.executable)

    if sys.platform == "win32":
        old = current.with_suffix(current.suffix + ".old")
        old.unlink(missing_ok=True)
        current.rename(old)
        new_binary.rename(current)
    else:
        os.chmod(new_binary, 0o755)
        if sys.platform == "darwin":
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", str(new_binary)],
                capture_output=True,
            )
        os.replace(new_binary, current)


def cleanup_old_binary() -> None:
    """Remove leftover .old file from a previous Windows update."""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        old = Path(sys.executable).with_suffix(".exe.old")
        old.unlink(missing_ok=True)


# --- Textual UI ---


class UpdateModal(Screen):
    """Modal screen prompting the user to install an available update."""

    BINDINGS = [("escape", "skip", "Skip")]

    CSS = """
    UpdateModal {
        align: center middle;
    }
    #update-container {
        width: 60;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
    }
    #update-title {
        height: 1;
        content-align: center middle;
        text-style: bold;
        background: $primary;
        color: $foreground;
        margin-bottom: 1;
    }
    #update-body {
        height: auto;
        margin-bottom: 1;
    }
    #update-buttons {
        height: 3;
        align: center middle;
    }
    #update-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, info: UpdateInfo) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        with Vertical(id="update-container"):
            yield Static("Update Available", id="update-title")
            yield Static(
                f"Version {self.info.latest_version} is available "
                f"(current: {__version__}).\n\n"
                "Would you like to update now?",
                id="update-body",
            )
            with Horizontal(id="update-buttons"):
                yield Button("Update Now", variant="success", id="update-yes")
                yield Button("Skip", variant="default", id="update-skip")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "update-yes")

    def action_skip(self) -> None:
        self.dismiss(False)
