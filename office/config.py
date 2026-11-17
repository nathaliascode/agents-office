"""Configuration, translations and program paths."""

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "office"
STATIC = PACKAGE / "static"
I18N = ROOT / "i18n"
CONFIG_FILE = ROOT / "config.json"
USAGE_FILE = ROOT / "usage.json"
EXAMPLE = ROOT / "example"
# The office usually lives inside the log folder: <log>/office/ next to <log>/tablero.md.
LOG_DIR = ROOT.parent


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        sys.exit(f"Could not read {path}: {error}")


@dataclass(frozen=True)
class AgentDef:
    key: str
    name: str
    role: str
    zone: str


CONFIG = load_json(CONFIG_FILE)
THRESHOLDS = CONFIG["thresholds"]
AGENTS = [AgentDef(**agent) for agent in CONFIG["agents"]]
AGENTS_BY_KEY = {agent.key: agent for agent in AGENTS}
PHASES = [(phase["key"], phase["match"]) for phase in CONFIG["phases"]]
LANGUAGES = CONFIG["languages"]
DEFAULT_LANGUAGE = CONFIG["language"]


def load_texts(language: str) -> dict:
    return load_json(I18N / f"{language}.json")


def program_files() -> list[Path]:
    """Every file whose change must restart the live server and reload open pages."""
    files = [*PACKAGE.rglob("*.py"), *STATIC.glob("*"), *I18N.glob("*.json"), CONFIG_FILE]
    return sorted(path for path in files if path.is_file())


def program_signature() -> tuple:
    return tuple((str(path), path.stat().st_mtime) for path in program_files())


CODE_VERSION = hashlib.sha1(b"".join(path.read_bytes() for path in program_files())).hexdigest()[:10]
