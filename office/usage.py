"""Usage limits (5-hour window and week) from Claude Code's /usage command.

/usage is a built-in command: it does not call the model, uses no tokens and,
with --no-session-persistence, leaves no session behind.
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import USAGE_FILE
from .models import Usage, UsageWindow

WORK_DIR = Path.home() / ".cache" / "agents-office"
MONTHS = {
    name: number
    for number, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
RESET = re.compile(r"([A-Za-z]{3})\w*\s+(\d{1,2}),?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.I)


def _reset_time(text: str) -> int | None:
    """'Nov 17, 6:39pm' -> local epoch seconds."""
    match = RESET.match(text.strip())
    if not match or not MONTHS.get(match.group(1).lower()):
        return None
    hour = int(match.group(3)) % 12 + (12 if match.group(5).lower() == "pm" else 0)
    today = datetime.now()
    moment = datetime(today.year, MONTHS[match.group(1).lower()], int(match.group(2)), hour, int(match.group(4) or 0))
    if moment < today - timedelta(days=200):
        moment = moment.replace(year=today.year + 1)
    return int(moment.timestamp())


def _window(output: str, label: str) -> UsageWindow | None:
    match = re.search(label + r"[^:]*:\s*(\d+(?:\.\d+)?)%\s*used\s*·\s*resets\s*([^(\n]+)", output, re.I)
    return UsageWindow(float(match.group(1)), _reset_time(match.group(2))) if match else None


def refresh_usage() -> bool:
    """Ask Claude Code and save the result to usage.json."""
    executable = shutil.which("claude")
    if not executable:
        return False
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [executable, "-p", "/usage", "--no-session-persistence"],
            cwd=WORK_DIR,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    five_hour = _window(result.stdout or "", "Current session")
    week = _window(result.stdout or "", "Current week")
    if not (five_hour or week):
        return False
    data = {"five_hour": five_hour and vars(five_hour), "week": week and vars(week), "updated": int(time.time())}
    temporary = USAGE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, USAGE_FILE)
    return True


def read_usage() -> Usage | None:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    def window(name: str) -> UsageWindow | None:
        item = data.get(name)
        return UsageWindow(item["used"], item.get("resets_at")) if item and item.get("used") is not None else None

    return Usage(window("five_hour"), window("week"), data.get("updated"))


def current_account() -> str | None:
    """Only the id of the signed-in account (never credentials)."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (data.get("oauthAccount") or {}).get("accountUuid")
