"""Display texts that depend on the language: dates, numbers and status labels."""

from datetime import datetime
from html import escape

from ..board import shorten
from ..i18n import Translator
from ..models import Agent, HistoryItem, Phase, Stage, Tokens


def iso(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M") if moment else ""


def readable(moment: datetime | None) -> str:
    if not moment:
        return "—"
    return moment.strftime("%d/%m/%Y" if (moment.hour, moment.minute) == (0, 0) else "%d/%m/%Y %H:%M")


def compact_number(value: int, tr: Translator) -> str:
    decimal = tr("number.decimal")
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".replace(".", decimal) + " M"
    if value >= 1000:
        return f"{round(value / 1000)} k"
    return str(value)


def tokens_html(tokens: Tokens | int | None, high: int, tr: Translator) -> str:
    if tokens is None:
        return ""
    if isinstance(tokens, int):
        number = f"{tokens:,}".replace(",", tr("number.thousands"))
        return f'<div class="tokens" title="{tr("tokens.manual_tooltip")}">{tr("tokens.manual", n=number)}</div>'
    level = " high" if tokens.context >= high else ""
    text = tr("tokens.session", context=compact_number(tokens.context, tr), output=compact_number(tokens.output, tr))
    return f'<div class="tokens{level}" title="{tr("tokens.session_tooltip")}">{text}</div>'


def phase_name(key: str, tr: Translator) -> str:
    return tr(f"phases.{key}")


def phase_text(phase: Phase, tr: Translator) -> str:
    if phase.live_task:
        return tr("session.phase_in_progress", task=phase.live_task)
    return phase.status_text or tr("agent.no_record")


def stage_text(stage: Stage, tr: Translator) -> str:
    if stage.live:
        return tr("session.stage_in_progress" if stage.live == "working" else "session.stage_done")
    return stage.status_text or tr("agent.pending")


def agent_task(agent: Agent, tr: Translator) -> str:
    """Task to show; "" when there is none."""
    if agent.whole_module:
        return tr("agent.whole_module")
    return agent.task


def agent_stage(agent: Agent, tr: Translator) -> str:
    return tr(f"session.{agent.stage_key}") if agent.stage_key else agent.stage


def source_text(source: str, tr: Translator) -> str:
    if source in ("board", "session", "subagent"):
        return tr(f"source.{source}")
    return tr("source.log", file=source)


def history_html(item: HistoryItem, high: int, tr: Translator) -> str:
    text = tr("session.start", title=item.text) if item.source == "session" else item.text
    status = tr(f"assignment.{item.assignment_status}") if item.assignment_status else item.status
    note = item.note
    if item.subagent_count is not None:
        note = tr("session.subagents_total", n=item.subagent_count)
    elif item.tokens:
        note = tr(
            "tokens.history",
            context=compact_number(item.tokens.context, tr),
            output=compact_number(item.tokens.output, tr),
        )
    return (
        f"<li><time>{readable(item.date)}</time><div>{escape(text)}"
        + (f' <span class="muted">· {escape(status)}</span>' if status else "")
        + (f'<div class="muted">{escape(shorten(note, 160))}</div>' if note else "")
        + f'<div class="source">{escape(source_text(item.source, tr))}</div></div></li>'
    )
