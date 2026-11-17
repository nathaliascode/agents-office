"""Assembles one self-contained HTML page in one language."""

import json
import random
from dataclasses import dataclass
from html import escape

from ..config import CODE_VERSION, LANGUAGES, STATIC
from ..i18n import Translator
from ..models import Agent, Note, OfficeState, Session
from ..sessions import session_agents
from ..state import empty_state
from ..usage import read_usage
from . import panel
from .pixel import decor_css
from .room import defs_svg, room_svg

THEME_TOGGLE_ICONS = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3.2" fill="#f6b73c"/>'
    '<g stroke="#f6b73c" stroke-width="1.6" stroke-linecap="round"><path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6'
    'M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"/></g></svg>'
    '<svg viewBox="0 0 16 16" aria-hidden="true">'
    '<path d="M10.8 2.2a6 6 0 1 0 3 9.6A5 5 0 0 1 10.8 2.2z" fill="#b39ddb"/>'
    '</svg><span class="knob"></span>'
)
BOARD_VIEW = "board"


@dataclass
class View:
    id: str
    agents: list[Agent]
    state: OfficeState
    plan_file: str | None
    waiting: list[Session]
    session: Session | None = None


@dataclass
class PageInput:
    state: OfficeState
    module: str
    sessions: list[Session]
    avatars: str | None
    watch_seconds: int
    version: str
    files: dict[str, str]  # language -> file name, for the static page


def _petals(count: int = 16) -> str:
    rng = random.Random(42)
    return (
        '<div class="petals" aria-hidden="true">'
        + "".join(
            f'<i style="left:{rng.uniform(0, 98):.1f}%;animation-duration:{rng.uniform(11, 22):.1f}s;'
            f'animation-delay:-{rng.uniform(0, 22):.1f}s;transform:scale({rng.uniform(.7, 1.2):.2f})"></i>'
            for _ in range(count)
        )
        + "</div>"
    )


def _views(data: PageInput) -> list[View]:
    """Sessions waiting for the user first, then those with agents working, then the rest; the board last."""

    def priority(session: Session):
        return (0 if session.waiting else 1, 0 if session.working_count else 1, -session.last_activity.timestamp())

    views = [
        View(
            session.id[:8],
            session_agents(session),
            session.state or empty_state(),
            session.plan_file,
            [session] if session.waiting else [],
            session,
        )
        for session in sorted(data.sessions, key=priority)
    ]
    waiting = [session for session in data.sessions if session.waiting]
    views.append(View(BOARD_VIEW, data.state.agents, data.state, data.module, waiting))
    return views


def _view_html(view: View, data: PageInput, start: str, tr: Translator) -> str:
    state, avatars = view.state, data.avatars
    waiting_notes = [panel.waiting_item(session, tr) for session in view.waiting]
    waiting = panel.note_list(waiting_notes, "decision", tr) or f'<li class="empty">{tr("panel.nobody_waiting")}</li>'
    written: list[Note] = state.notes + panel.blocker_notes(state, tr)
    notes = panel.note_list(state.notes, "note", tr) + panel.note_list(
        panel.blocker_notes(state, tr), "note blocker", tr
    )
    notes_html = (
        f'<h3 class="notes-heading">{tr("panel.logged")}</h3><ul class="note-list notes">{notes}</ul>'
        if written
        else ""
    )
    plan_file = (
        f'<p class="plan-file">{tr("panel.plan_file")} <code>{escape(view.plan_file)}</code></p>'
        if view.plan_file
        else f'<p class="plan-file">{tr("panel.no_plan_file")}</p>'
    )
    stages = panel.stage_chips(state.stages, tr)
    notice = (
        tr(
            "session.notice",
            n=view.session.subagent_count,
            time=f"{view.session.last_activity:%H:%M}",
            coordinator=panel.COORDINATOR_NAME,
        )
        if view.session
        else ""
    )
    count = len(view.waiting)
    return (
        f'<div class="view" data-view="{view.id}"{"" if view.id == start else " hidden"}>'
        f'<section class="scene" aria-label="{tr("room.aria")}">{panel.working_now(view.agents, avatars, view.id, tr)}'
        f"{room_svg(view.agents, state, view.id, count, tr)}</section>"
        f'<aside class="panel">'
        f'<div class="card"><div class="view-notice" title="{escape(notice)}">{panel.counter(view.agents, tr)}</div>'
        f'<h2>{tr("panel.agents")}</h2><ul class="agent-list">'
        + "".join(panel.agent_row(agent, avatars, view.id, tr) for agent in view.agents)
        + "</ul></div>"
        f'<div class="card"><h2>{tr("panel.phases")}</h2>{plan_file}'
        f'<ol class="chips chips-phases">{panel.phase_chips(state, tr)}</ol>'
        + (f'<h2>{tr("panel.stages")}</h2><ol class="chips">{stages}</ol>' if stages else "")
        + f'</div><div class="card"><h2>{tr("panel.waiting")} '
        f'<span class="badge{"" if count else " zero"}">{count}</span>'
        f'</h2><ul class="note-list">{waiting}</ul>{notes_html}</div></aside>'
        + "".join(panel.agent_modal(agent, avatars, view.id, tr) for agent in view.agents)
        + panel.plan_modal(state.stages, view.plan_file, view.id, tr)
        + f'<dialog id="waiting-{view.id}" class="modal"><header><h4>{tr("panel.waiting")}</h4>{panel.close_button(tr)}'
        f'</header><ul class="note-list">{waiting}</ul>{notes_html}</dialog></div>'
    )


def _menu(views: list[View], data: PageInput, tr: Translator) -> str:
    options = []
    for view in views:
        if view.session:
            if not options:
                options.append(f'<p class="menu-heading">{tr("menu.active_sessions", n=len(data.sessions))}</p>')
            options.append(panel.session_option(view.session, view.id, tr))
        else:
            if data.sessions:
                options.append('<hr class="menu-divider">')
            working = sum(1 for agent in view.agents if agent.status == "working")
            options.append(panel.board_option(view.id, working, tr))
    if not data.sessions:
        options.insert(0, f'<p class="menu-heading">{tr("menu.no_sessions")}</p>')
    waiting = sum(1 for session in data.sessions if session.waiting)
    return panel.view_menu(options, views[0].id, len(data.sessions), waiting, tr)


def _language_switch(current: str, data: PageInput, tr: Translator) -> str:
    buttons = "".join(
        f'<button type="button" class="language-option{" active" if language == current else ""}" '
        f'data-language="{language}" data-file="{data.files.get(language, "")}" '
        f'aria-pressed="{str(language == current).lower()}"'
        f' title="{tr(f"language.{language}")}">{language.upper()}</button>'
        for language in LANGUAGES
    )
    return f'<div class="language" role="group" aria-label="{tr("language.label")}">{buttons}</div>'


def render_page(data: PageInput, tr: Translator) -> str:
    views = _views(data)
    start = views[0].id
    css = (STATIC / "office.css").read_text(encoding="utf-8")
    script = (STATIC / "office.js").read_text(encoding="utf-8")
    settings = json.dumps({"language": tr.language, "time": tr.group("time")}, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="{tr.language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{tr("title")}</title>
<style>{css}
{decor_css()}
{panel.avatar_css(data.avatars)}</style>
</head>
<body data-version="{data.version}" data-interval="{data.watch_seconds}" data-code="{CODE_VERSION}">
{defs_svg(data.avatars, tr)}
{_petals()}
<header class="topbar">
  <h1><span class="title-flower" aria-hidden="true"></span>{tr("title")}</h1>
  {panel.usage_meters(read_usage(), tr)}
  <div class="topbar-right">
    {_menu(views, data, tr)}
    {_language_switch(tr.language, data, tr)}
    <button type="button" id="theme-toggle" class="theme-toggle"
            aria-label="{tr("theme")}">{THEME_TOGGLE_ICONS}</button>
  </div>
</header>
<main class="main">{"".join(_view_html(view, data, start, tr) for view in views)}</main>
<script>window.OFFICE={settings};</script>
<script>{script}</script>
</body>
</html>"""
