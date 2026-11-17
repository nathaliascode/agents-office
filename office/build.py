"""Reads every source and writes the page in every language."""

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .board import read_board, read_plan_files
from .config import CODE_VERSION, DEFAULT_LANGUAGE, LANGUAGES, USAGE_FILE
from .i18n import all_translators
from .models import OfficeState, Session
from .render.page import PageInput, render_page
from .sessions import read_sessions
from .state import apply_session_work, build_state


@dataclass
class Options:
    module: str
    board: str
    plans: str
    sessions: str | None
    avatars: str | None
    output: str
    watch: int
    tokens: dict[str, int]


@dataclass
class Result:
    state: OfficeState
    sessions: list[Session]


def output_files(output: str) -> dict[str, Path]:
    """office.html for the default language, office.<lang>.html for the others."""
    base = Path(output)
    return {
        language: base if language == DEFAULT_LANGUAGE else base.with_name(f"{base.stem}.{language}{base.suffix}")
        for language in LANGUAGES
    }


def sources_signature(options: Options) -> tuple:
    """Changes when anything the page shows may have changed."""
    paths = [Path(options.board), *Path(options.plans).glob("*.md"), USAGE_FILE]
    if options.avatars and Path(options.avatars).is_dir():
        paths += list(Path(options.avatars).glob("*.png"))
    if options.sessions and Path(options.sessions).is_dir():
        paths += list(Path(options.sessions).glob("*.jsonl"))
        paths += list(Path(options.sessions).glob("*/subagents/agent-*"))
    return tuple(sorted((str(path), path.stat().st_mtime) for path in paths if path.exists()))


def version_of(signature: tuple) -> str:
    """Includes the program version so open pages also notice design changes."""
    return hashlib.sha1((repr(signature) + CODE_VERSION).encode()).hexdigest()[:12]


def _write(path: Path, html: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, path)  # never serve a half-written file


def build(options: Options, version: str = "") -> Result:
    rows = read_board(options.board, options.module)
    plan = read_plan_files(options.plans, options.module)
    state = build_state(rows, plan, options.tokens)
    sessions = read_sessions(options.sessions, options.plans, datetime.now())
    for session in sessions:
        if session.plan_file:
            session.state = build_state(
                read_board(options.board, session.plan_file), read_plan_files(options.plans, session.plan_file)
            )
            apply_session_work(session.state, session)
    files = output_files(options.output)
    data = PageInput(
        state,
        options.module,
        sessions,
        options.avatars,
        options.watch,
        version,
        {language: path.name for language, path in files.items()},
    )
    for language, translator in all_translators().items():
        _write(files[language], render_page(data, translator))
    return Result(state, sessions)
