"""Claude Code sessions: metadata only, never the conversation text.

A session is a transcript in ~/.claude/projects/<project>/<id>.jsonl; its
subagents live in <id>/subagents/agent-*.jsonl with a .meta.json beside them.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .board import agent_in
from .config import AGENTS_BY_KEY, CONFIG, LOG_DIR, THRESHOLDS
from .models import Agent, Assignment, HistoryItem, Session, Tokens, Transcript, Waiting

COORDINATOR = CONFIG["coordinator"]
QUESTION_TOOLS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}
INTERRUPTED = "[Request interrupted"


def default_sessions_dir() -> Path:
    project = re.sub(r"[^A-Za-z0-9]", "-", str(LOG_DIR.parent))
    return Path.home() / ".claude" / "projects" / project


def _local_time(stamp: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return moment.astimezone().replace(tzinfo=None, second=0, microsecond=0)


def _modified(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime)


def _content(message: dict) -> list:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _text_of(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def read_transcript(path: Path, plan_pattern: re.Pattern) -> Transcript | None:
    """Times, title, tokens, how the last turn ended and which plan files it touched."""
    info = Transcript()
    usage_by_message = {}
    own_stop = None
    try:
        lines = path.open(encoding="utf-8")
    except OSError:
        return None
    with lines:
        for line in lines:
            if '"timestamp"' not in line and '"aiTitle"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            kind = record.get("type")
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if kind == "ai-title" and record.get("aiTitle"):
                info.title = record["aiTitle"]
            info.origin = info.origin or record.get("entrypoint")
            moment = _local_time(record.get("timestamp"))
            if moment:
                info.started = info.started or moment
                info.ended = moment
            if kind == "assistant":
                own_stop = message.get("stop_reason")
                if message.get("id") and isinstance(message.get("usage"), dict):
                    usage_by_message[message["id"]] = message["usage"]
            elif kind == "user" and not record.get("isMeta"):
                # The only content read: the technical marker left when an agent is stopped.
                own_stop = "interrupted" if _text_of(message).strip().startswith(INTERRUPTED) else None
            if kind in ("user", "assistant") and not record.get("isSidechain"):
                _follow_main_thread(info, record, message, moment, plan_pattern)
    info.finished_turn = own_stop in ("end_turn", "interrupted")
    if usage_by_message:
        last = list(usage_by_message.values())[-1]
        info.tokens = Tokens(
            context=sum(
                last.get(name) or 0
                for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
            ),
            output=sum(item.get("output_tokens") or 0 for item in usage_by_message.values()),
        )
    return info


def _follow_main_thread(info: Transcript, record: dict, message: dict, moment, plan_pattern: re.Pattern) -> None:
    parts = [part for part in _content(message) if isinstance(part, dict)]
    tools = [part.get("name") for part in parts if part.get("type") == "tool_use"]
    if (
        record["type"] == "user"
        and "tool_result" not in {part.get("type") for part in parts}
        and not record.get("isMeta")
    ):
        info.worked_this_turn = False  # a new message from the user starts a new turn
    if tools:
        info.worked_this_turn = True
    info.last_message = {"type": record["type"], "stop": message.get("stop_reason"), "tools": tools, "time": moment}
    for part in parts:
        if part.get("type") != "tool_use":
            continue
        tool_input = part.get("input") if isinstance(part.get("input"), dict) else {}
        paths = f'{tool_input.get("file_path", "")} {tool_input.get("command", "")}'
        for name in plan_pattern.findall(paths):
            info.plan_files[name] = info.plan_files.get(name, 0) + 1


def fix_accents(text: str | None) -> str | None:
    """Titles are generated without accents sometimes; fix the common words for display."""
    if not text:
        return text
    accents = CONFIG.get("title_accents", {})

    def fix(match):
        word = match.group(0)
        right = accents.get(word.lower())
        if not right:
            return word
        return right.capitalize() if word[0].isupper() else right

    return re.sub(r"[A-Za-zÁÉÍÓÚáéíóúñÑ]+", fix, text)


def read_sessions(folder: str | Path | None, plans_dir: str | Path, now: datetime) -> list[Session]:
    if not folder or not Path(folder).is_dir():
        return []
    plan_pattern = re.compile(re.escape(Path(plans_dir).name) + r"/([A-Za-z0-9_]+)(?:\.[a-z]+)?\.md")
    sessions = [_session(path, plan_pattern, now) for path in Path(folder).glob("*.jsonl")]
    sessions = [session for session in sessions if session]
    sessions.sort(key=lambda session: session.last_activity, reverse=True)
    return sessions


def _session(path: Path, plan_pattern: re.Pattern, now: datetime) -> Session | None:
    stale = timedelta(hours=THRESHOLDS["stale_subagent_hours"])
    subagents = _subagent_files(path, now, stale)
    last_activity = max([_modified(path), *(item[2] for item in subagents)])
    running = any(unfinished and now - modified < stale for _meta, _file, modified, unfinished in subagents)
    if now - last_activity > timedelta(minutes=THRESHOLDS["active_session_minutes"]) and not running:
        return None

    main = read_transcript(path, plan_pattern) or Transcript()
    plan_files = dict(main.plan_files)
    assignments: dict[str, list[Assignment]] = {}
    for meta, file, modified, unfinished in subagents:
        description = meta.get("description", "")
        key = agent_in(description.split(":")[0]) or agent_in(description)
        if not key:
            continue
        agent = AGENTS_BY_KEY[key]
        task = re.sub(rf"^\s*{agent.name}\s+(\d+\s+)?{agent.role}\s*:?\s*", "", description, flags=re.I).strip()
        transcript = read_transcript(file, plan_pattern) or Transcript()
        if transcript.finished_turn:
            unfinished = False  # it delivered its report even without the «finished» mark
        for name, count in transcript.plan_files.items():
            plan_files[name] = plan_files.get(name, 0) + count
        status = "idle" if not unfinished else ("working" if now - modified < stale else "paused")
        assignments.setdefault(key, []).append(
            Assignment(task or description, status, transcript.started, transcript.ended or modified, transcript.tokens)
        )

    return Session(
        id=path.stem,
        title=fix_accents(main.title),
        origin=main.origin,
        started=main.started,
        last_activity=last_activity,
        active_now=now - _modified(path) < timedelta(minutes=THRESHOLDS["coordinator_active_minutes"]),
        transcript=main,
        assignments=assignments,
        subagent_count=len(subagents),
        plan_file=max(plan_files, key=plan_files.get) if plan_files else None,
        waiting=_waiting(main, assignments),
    )


def _subagent_files(path: Path, now: datetime, stale: timedelta) -> list:
    folder = path.with_suffix("") / "subagents"
    found = []
    for meta_file in sorted(folder.glob("agent-*.meta.json")) if folder.is_dir() else []:
        transcript = meta_file.with_name(meta_file.name.replace(".meta.json", ".jsonl"))
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        modified = _modified(transcript) if transcript.exists() else _modified(meta_file)
        found.append((meta, transcript, modified, not meta.get("finished")))
    return found


def _waiting(main: Transcript, assignments: dict[str, list[Assignment]]) -> Waiting | None:
    """The coordinator needs the user to answer before it can go on:
    - it left a question with options or a plan to approve, or
    - it worked (used tools), ended its turn and has no agent running.
    A plain reply with no work (for example, answering «hola») does not count."""
    last = main.last_message or {}
    if last.get("type") != "assistant":
        return None
    for tool, kind in QUESTION_TOOLS.items():
        if tool in (last.get("tools") or []):
            return Waiting(last.get("time"), kind)
    agents_running = any(item.status == "working" for items in assignments.values() for item in items)
    if last.get("stop") == "end_turn" and main.worked_this_turn and not agents_running:
        return Waiting(last.get("time"), "turn")
    return None


def session_agents(session: Session) -> list[Agent]:
    """One Agent per team member, as seen in this session."""
    agents = []
    for key, definition in AGENTS_BY_KEY.items():
        agent = Agent(key, definition.name, definition.role, definition.zone, empty_key="no_assignments")
        if key == COORDINATOR:
            _as_coordinator(agent, session)
        else:
            _with_assignments(agent, session.assignments.get(key, []))
        agents.append(agent)
    return agents


def _as_coordinator(agent: Agent, session: Session) -> None:
    waiting = session.waiting
    agent.status = "paused" if waiting or not session.active_now else "working"
    agent.task = session.title or ""
    agent.stage_key = "waiting_reply" if waiting else "this_session"
    agent.since = (waiting.since if waiting else None) or session.last_activity
    agent.time_key = "waiting" if waiting else "activity"
    agent.tokens = session.transcript.tokens
    agent.history = [
        HistoryItem(session.started, session.title or "", source="session", subagent_count=session.subagent_count)
    ]


def _with_assignments(agent: Agent, items: list[Assignment]) -> None:
    items = sorted(items, key=lambda item: item.started or datetime.min, reverse=True)
    current = next((item for item in items if item.status != "idle"), items[0] if items else None)
    if current:
        agent.status, agent.task, agent.tokens = current.status, current.task, current.tokens
        agent.since = current.started if current.status == "working" else current.finished
    agent.history = [
        HistoryItem(item.started, item.task, source="subagent", assignment_status=item.status, tokens=item.tokens)
        for item in items[:6]
    ]
