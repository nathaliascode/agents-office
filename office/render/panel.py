"""Side panel, top bar pieces and modals."""

import base64
import time
from datetime import datetime
from html import escape
from pathlib import Path

from ..board import clip
from ..config import AGENTS, AGENTS_BY_KEY, CONFIG, THRESHOLDS
from ..i18n import Translator
from ..models import Agent, Note, OfficeState, Session, Stage, Usage, UsageWindow
from ..state import current_phase
from .labels import (
    agent_stage,
    agent_task,
    history_html,
    iso,
    phase_name,
    phase_text,
    readable,
    source_text,
    stage_text,
    tokens_html,
)
from .pixel import character_svg

STATUSES = ("working", "paused", "blocked", "idle")
COORDINATOR_NAME = AGENTS_BY_KEY[CONFIG["coordinator"]].name
# Pill colour for each stage status in the plan modal.
STAGE_PILL = {"done": "working", "working": "paused", "paused": "paused", "blocked": "blocked", "pending": "idle"}


def avatar_css(avatars: str | None) -> str:
    """Photos go once in the CSS; small avatars only reference the class."""
    rules = []
    for agent in AGENTS:
        path = Path(avatars) / f"{agent.key}.png" if avatars else None
        if path and path.is_file():
            data = base64.b64encode(path.read_bytes()).decode()
            rules.append(f".avatar-{agent.key}{{background-image:url(data:image/png;base64,{data})}}")
    return "\n".join(rules)


def mini_avatar(agent: Agent, avatars: str | None, tr: Translator) -> str:
    path = Path(avatars) / f"{agent.key}.png" if avatars else None
    if path and path.is_file():
        return f'<span class="mini-avatar photo avatar-{agent.key}"></span>'
    return f'<span class="mini-avatar">{character_svg(agent.role, tr("room.character", role=agent.role))}</span>'


def _time_attributes(agent: Agent, tr: Translator) -> str:
    attributes = f' data-since="{iso(agent.since)}" data-status="{agent.status}"'
    if agent.time_key:
        attributes += f' data-template="{escape(tr(f"time_prefix.{agent.time_key}"))}"'
    if agent.empty_key:
        attributes += f' data-empty="{escape(tr(f"session.{agent.empty_key}"))}"'
    return attributes


def _pill(status: str, tr: Translator) -> str:
    return f'<span class="pill pill-{status}"><span class="dot"></span>{tr(f"status.{status}")}</span>'


# ---------------------------------------------------------------- agents
def agent_row(agent: Agent, avatars: str | None, view: str, tr: Translator) -> str:
    task = agent_task(agent, tr)
    shown = escape(task) if task else tr("agent.no_task_assigned")
    if task and agent.status == "idle":
        shown = f'{tr("agent.last")} {shown}'
    title = (escape(task) if task else tr("agent.no_task")) + (
        tr("agent.since", date=readable(agent.since)) if agent.since else ""
    )
    return (
        f'<li><button type="button" class="agent-row status-{agent.status}" data-modal="agent-{agent.key}-{view}" '
        f'title="{title}">{mini_avatar(agent, avatars, tr)}<span class="agent-row-text">'
        f'<span class="agent-row-head"><b>{escape(agent.name)}</b><span class="role">{escape(agent.role)}</span>'
        f'{_pill(agent.status, tr)}</span><span class="agent-row-task">{shown}</span></span></button></li>'
    )


def agent_modal(agent: Agent, avatars: str | None, view: str, tr: Translator) -> str:
    modal_id = f"agent-{agent.key}-{view}"
    task = agent_task(agent, tr)
    stage = agent_stage(agent, tr)
    history = (
        "".join(history_html(item, THRESHOLDS["high_tokens"], tr) for item in agent.history)
        or f'<li class="empty">{tr("agent.no_history")}</li>'
    )
    return (
        f'<dialog id="{modal_id}" class="modal" aria-labelledby="{modal_id}-title">'
        f'<header>{mini_avatar(agent, avatars, tr)}<h4 id="{modal_id}-title">{escape(agent.name)} '
        f'<span class="role">{escape(agent.role)}</span></h4>{close_button(tr)}</header>'
        f'<div class="summary">{_pill(agent.status, tr)}'
        f'<p class="task">{escape(task) if task else tr("agent.no_task_assigned")}</p>'
        + (f'<span class="stage-tag">{escape(stage)}</span>' if stage else "")
        + f'<span class="elapsed"{_time_attributes(agent, tr)}>—</span>'
        + tokens_html(agent.tokens, THRESHOLDS["high_tokens"], tr)
        + "</div>"
        f'<h5>{tr("agent.history")}</h5><ol>{history}</ol></dialog>'
    )


def working_now(agents: list[Agent], avatars: str | None, view: str, tr: Translator) -> str:
    active = [agent for agent in agents if agent.status == "working"]
    if not active:
        return ""
    chips = "".join(
        f'<button type="button" class="working-now-chip" data-modal="agent-{agent.key}-{view}" '
        f'title="{escape(agent_task(agent, tr))}">{mini_avatar(agent, avatars, tr)}<span><b>{escape(agent.name)}</b> '
        f'<span class="role">{escape(agent.role)}</span><small>{escape(clip(agent_task(agent, tr), 34))}</small></span>'
        "</button>"
        for agent in active
    )
    return (
        f'<div class="working-now"><span class="working-now-title"><i class="working-now-dot"></i>'
        f'{tr("room.working_now", n=len(active))}</span>{chips}</div>'
    )


def counter(agents: list[Agent], tr: Translator) -> str:
    counts = {status: sum(1 for agent in agents if agent.status == status) for status in STATUSES}
    blocked = "counter.blocked_one" if counts["blocked"] == 1 else "counter.blocked_many"
    texts = {"working": "counter.working", "paused": "counter.paused", "blocked": blocked, "idle": "counter.idle"}
    items = "".join(
        f'<li class="pill-{status}"><span class="dot"></span>{tr(texts[status], n=counts[status])}</li>'
        for status in STATUSES
    )
    return f'<ul class="counter" aria-label="{tr("counter.aria")}">{items}</ul>'


# ------------------------------------------------------- phases and plan
def phase_chips(state: OfficeState, tr: Translator) -> str:
    current = current_phase(state.phases)
    chips = []
    for number, phase in enumerate(state.phases, 1):
        date = f" · {readable(phase.date)}" if phase.date else ""
        chips.append(
            f'<li class="chip phase-{phase.status}{" current" if phase.key == current else ""}" '
            f'title="{escape(phase_text(phase, tr))}{date}"><b>{number}</b>'
            f"<span>{escape(phase_name(phase.key, tr))}</span></li>"
        )
    return "".join(chips)


def stage_chips(stages: list[Stage], tr: Translator) -> str:
    return "".join(
        f'<li class="chip phase-{stage.status}" title="{escape(stage.description)} — {escape(stage_text(stage, tr))}">'
        f'<b>{tr("room.stage_short", n=stage.number)}</b><span>{escape(clip(stage.description, 28))}</span>'
        f"<small>{escape(clip(stage_text(stage, tr), 26))}</small></li>"
        for stage in stages
    )


def plan_modal(stages: list[Stage], plan_file: str | None, view: str, tr: Translator) -> str:
    items = (
        "".join(
            f'<li class="{"current" if stage.status == "working" else ""}">'
            f'<b>{tr("room.stage_short", n=stage.number)}</b>'
            f"<span>{escape(stage.description)}</span>"
            f'<span class="pill pill-{STAGE_PILL.get(stage.status, "idle")}"><span class="dot"></span>'
            f"{escape(stage_text(stage, tr))}</span></li>"
            for stage in stages
        )
        or f'<li class="empty">{tr("room.no_plan")}</li>'
    )
    file = (
        f'<p class="plan-file plan-file-modal">{tr("panel.plan_file")} <code>{escape(plan_file)}</code></p>'
        if plan_file
        else ""
    )
    return (
        f'<dialog id="plan-{view}" class="modal"><header><h4>{tr("plan.title")}</h4>{close_button(tr)}</header>'
        f'{file}<ol class="plan-list">{items}</ol></dialog>'
    )


# ------------------------------------------------------ waiting and notes
def waiting_item(session: Session, tr: Translator) -> Note:
    text = tr(f"session.waiting_{session.waiting.kind}", title=display_title(session, tr), coordinator=COORDINATOR_NAME)
    return Note(text, "session", session.waiting.since)


def note_list(notes: list[Note], css_class: str, tr: Translator) -> str:
    def source(note: Note) -> str:
        if note.agent_label:
            return tr("source.board_agent", agent=note.agent_label)
        return source_text(note.source, tr)

    return "".join(
        f'<li class="{css_class}"><p>{escape(note.text)}</p><small>{escape(source(note))}'
        f'{" · " + readable(note.date) if note.date else ""}</small></li>'
        for note in notes
    )


def blocker_notes(state: OfficeState, tr: Translator) -> list[Note]:
    return [
        Note(
            f"{agent.name} {agent.role}: {agent_task(agent, tr)}" + (f" ({agent.note})" if agent.note else ""),
            "board",
            agent.since,
        )
        for agent in state.blockers
    ]


def close_button(tr: Translator) -> str:
    return f'<button type="button" class="close" aria-label="{tr("agent.close")}">×</button>'


# --------------------------------------------------------------- sessions
def display_title(session: Session, tr: Translator) -> str:
    if session.title:
        return session.title
    where = {"cli": "session.in_terminal", "claude-vscode": "session.in_vscode"}.get(session.origin or "")
    title = tr("session.new") + (" " + tr(where) if where else "")
    return title + (f" ({session.started:%H:%M})" if session.started else "")


def session_option(session: Session, view: str, tr: Translator) -> str:
    title = display_title(session, tr)
    if session.waiting:
        chip, icon = (
            f'<span class="view-option-chip waiting">{tr("menu.waiting_reply")}</span>',
            '<span class="view-option-icon waiting">!</span>',
        )
    elif session.working_count:
        chip, icon = (
            f'<span class="view-option-chip ok">{tr("menu.working", n=session.working_count)}</span>',
            '<span class="view-option-icon live">●</span>',
        )
    else:
        chip, icon = (
            f'<span class="view-option-chip">{tr("menu.no_activity")}</span>',
            '<span class="view-option-icon">●</span>',
        )
    plan = f" · {escape(session.plan_file)}" if session.plan_file else tr("menu.no_plan_file")
    tooltip = tr("menu.subagents_tooltip", title=title, n=session.subagent_count)
    return (
        f'<button type="button" class="view-option" role="option" data-view="{view}" data-name="{escape(title)}" '
        f'title="{escape(tooltip)}">{icon}<span class="view-option-text"><b>{escape(clip(title, 34))}</b>'
        f"<small>{session.last_activity:%H:%M}{plan}</small></span>{chip}</button>"
    )


def board_option(view: str, working: int, tr: Translator) -> str:
    chip = f'<span class="view-option-chip ok">{tr("menu.working", n=working)}</span>' if working else ""
    return (
        f'<button type="button" class="view-option" role="option" data-view="{view}" data-name="{tr("menu.board")}">'
        f'<span class="view-option-icon">▤</span><span class="view-option-text"><b>{tr("menu.board")}</b>'
        f'<small>{tr("menu.board_detail")}</small></span>{chip}</button>'
    )


def view_menu(options: list[str], start: str, session_count: int, waiting: int, tr: Translator) -> str:
    count = tr("menu.one_session" if session_count == 1 else "menu.many_sessions", n=session_count)
    alert = f'<span class="view-menu-alert" title="{tr("menu.waiting_tooltip")}">{waiting}</span>' if waiting else ""
    return (
        f'<div class="view-menu" data-start="{start}">'
        '<button type="button" id="view-menu-button" class="view-menu-button" aria-haspopup="listbox" '
        'aria-expanded="false" aria-controls="view-menu-list">'
        f'<span class="view-menu-text"><small>{tr("menu.viewing")}</small><b id="view-menu-current">—</b></span>'
        f'{alert}<span class="view-menu-count">{count}</span><span class="view-menu-arrow" aria-hidden="true"></span>'
        f'</button><div class="view-menu-list" id="view-menu-list" role="listbox" aria-label="{tr("menu.choose")}" '
        f'hidden>{"".join(options)}</div></div>'
    )


# ------------------------------------------------------------------ usage
def usage_meters(usage: Usage | None, tr: Translator) -> str:
    if not usage:
        return ""
    now = time.time()
    stale = now - (usage.updated or 0) > THRESHOLDS["usage_stale_minutes"] * 60

    def meter(name: str, window: UsageWindow | None, with_day: bool) -> str:
        if not window:
            return ""
        percent = max(0.0, min(100.0, float(window.used)))
        level = (
            "red"
            if percent >= THRESHOLDS["usage_red_percent"]
            else "amber" if percent >= THRESHOLDS["usage_amber_percent"] else "ok"
        )
        resets = ""
        if window.resets_at:
            moment = datetime.fromtimestamp(int(window.resets_at))
            if moment.timestamp() < now:
                resets = tr("usage.already_reset")
            else:
                when = f'{tr("usage.days")[moment.weekday()]} {moment:%H:%M}' if with_day else f"{moment:%H:%M}"
                resets = tr("usage.resets", when=when)
        tooltip = tr("usage.tooltip", name=name, pct=f"{percent:.0f}", resets=resets)
        return (
            f'<div class="usage-meter level-{level}" title="{tooltip}"><span class="usage-name">{name}</span>'
            f'<span class="usage-bar"><i style="width:{percent:.1f}%"></i></span>'
            f'<span class="usage-pct">{percent:.0f}%</span><span class="usage-reset">{resets}</span></div>'
        )

    body = meter(tr("usage.five_hour"), usage.five_hour, False) + meter(tr("usage.week"), usage.week, True)
    if not body:
        return ""
    note = tr("usage.stale", min=THRESHOLDS["usage_stale_minutes"]) if stale else tr("usage.refresh")
    updated = ""
    if usage.updated:
        updated = (
            f'<span class="usage-time">'
            f'{tr("usage.updated", time=datetime.fromtimestamp(usage.updated).strftime("%H:%M"))}</span>'
        )
    return f'<div class="usage{" stale" if stale else ""}" title="{note}">{body}{updated}</div>'
