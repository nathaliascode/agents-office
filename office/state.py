"""Builds what the office shows from the log, and updates it with live session work."""

import re
from datetime import datetime

from .board import DECISION, agent_in, mentioned_stage, phase_in, phase_status, shorten
from .config import AGENTS, AGENTS_BY_KEY, CONFIG, PHASES
from .models import Agent, BoardRow, HistoryItem, Note, OfficeState, Phase, PlanFile, Session, Stage

HISTORY_LENGTH = 6
PHASE_BY_ROLE = CONFIG.get("phase_by_role", {})


def build_state(rows: list[BoardRow], plan: PlanFile, tokens: dict[str, int] | None = None) -> OfficeState:
    agents = [_agent_from_log(agent_def, rows, plan, (tokens or {}).get(agent_def.key)) for agent_def in AGENTS]
    return OfficeState(
        agents=agents,
        phases=[_phase(key, rows, plan) for key, _match in PHASES],
        stages=[_stage(stage.number, stage.description, rows) for stage in plan.stages],
        notes=plan.decisions + _board_notes(rows),
        blockers=[agent for agent in agents if agent.status == "blocked"],
    )


def empty_state() -> OfficeState:
    return OfficeState(agents=[], phases=[Phase(key) for key, _match in PHASES], stages=[], notes=[], blockers=[])


def _agent_from_log(agent_def, rows: list[BoardRow], plan: PlanFile, tokens: int | None) -> Agent:
    own = [row for row in rows if row.agent == agent_def.key]
    history = [
        HistoryItem(row.updated, f"{row.task} — {row.stage}".strip(" —"), row.status_text, row.note, "board")
        for row in own
    ]
    history += [
        HistoryItem(entry.date, entry.text, source=entry.source)
        for entry in plan.entries
        if entry.agent == agent_def.key
    ]
    history.sort(key=lambda item: item.date or datetime.min, reverse=True)
    agent = Agent(
        agent_def.key, agent_def.name, agent_def.role, agent_def.zone, history=history[:HISTORY_LENGTH], tokens=tokens
    )
    if own:
        latest = own[0]
        agent.status, agent.task, agent.stage = latest.status, latest.task, latest.stage
        agent.note, agent.since, agent.whole_module = latest.note, latest.updated, not latest.task
    return agent


def _phase(key: str, rows: list[BoardRow], plan: PlanFile) -> Phase:
    phase = Phase(key)
    record = plan.phases.get(key)
    if record:
        phase.status_text, phase.date, phase.agent = record.status_text, record.date, agent_in(record.agent_label)
    newest = next((row for row in rows if phase_in(row.stage) == key), None)
    if newest and (not record or (newest.updated or datetime.min) > (record.date or datetime.min)):
        phase.status_text, phase.date, phase.agent = newest.status_text, newest.updated, newest.agent
    if phase.status_text:
        phase.status = phase_status(phase.status_text)
    return phase


def _stage(number: int, description: str, rows: list[BoardRow]) -> Stage:
    stage = Stage(number, description)
    row = next((row for row in rows if mentioned_stage(row, number)), None)
    if row:
        stage.status = phase_status(row.status_text)
        stage.status_text = f"{row.stage} · {row.status_text}"
        stage.date = row.updated
    return stage


def _board_notes(rows: list[BoardRow]) -> list[Note]:
    latest_by_agent = {}
    for row in rows:
        latest_by_agent.setdefault(row.agent, row)
    return [
        Note(shorten(f"{row.task}: {row.note or row.status_text}", 260), "board", row.updated, row.agent_label)
        for row in latest_by_agent.values()
        if DECISION.search(f"{row.status_text} {row.note}")
    ]


def current_phase(phases: list[Phase]) -> str | None:
    """Newest phase in progress; otherwise the newest paused or blocked one."""
    for statuses in (("working",), ("blocked", "paused")):
        candidates = [phase for phase in phases if phase.status in statuses]
        if candidates:
            return max(candidates, key=lambda phase: phase.date or datetime.min).key
    return None


# ------------------------------------------------------- live session work
def stages_in(task: str) -> set[int]:
    match = re.search(r"etapas?\s+([\d\s,y-]+)", task or "", re.I)
    return {int(number) for number in re.findall(r"\d+", match.group(1))} if match else set()


def apply_session_work(state: OfficeState, session: Session) -> None:
    """What the session's subagents are doing now wins over the log, which the
    coordinator may not have updated yet."""
    work = [(key, item) for key, items in session.assignments.items() for item in items]
    running = {number for _key, item in work if item.status == "working" for number in stages_in(item.task)}
    touched = {number for _key, item in work for number in stages_in(item.task)}
    top = max(running | touched, default=0)
    for stage in state.stages:
        if stage.number in running:
            stage.status, stage.live = "working", "working"
        elif stage.number in touched and stage.number < top and stage.status != "done":
            stage.status, stage.live = "done", "done"

    active_phases = set()
    for key, item in work:
        phase_key = PHASE_BY_ROLE.get(AGENTS_BY_KEY[key].role) if key in AGENTS_BY_KEY else None
        if item.status != "working" or not phase_key:
            continue
        active_phases.add(phase_key)
        for phase in state.phases:
            if phase.key == phase_key and (phase.date or datetime.min) <= (item.started or datetime.max):
                phase.status, phase.live_task, phase.date, phase.agent = "working", item.task, item.started, key
    if active_phases:
        for phase in state.phases:
            if phase.status == "working" and phase.key not in active_phases:
                phase.status = "done"
