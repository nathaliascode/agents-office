"""Readers for the project log: tablero.md (board) and planes/<module>.md (plan files).

The log is written in Spanish by the orchestrator, so the words this module looks
for (column names, section titles, statuses) are Spanish on purpose. This is the
only place where they live.
"""

import re
from datetime import datetime
from pathlib import Path

from .config import AGENTS, PHASES
from .models import BoardRow, LogEntry, Note, PhaseRecord, PlanFile, StageDef

# Column names of the board and of the phase table.
COL_TASK, COL_STAGE, COL_AGENT, COL_STATUS = "tarea", "etapa", "agente", "estado"
COL_UPDATED, COL_NOTE, COL_PHASE, COL_DATE = "actualizado", "nota", "fase", "fecha"

# Section titles of a plan file.
SECTION_LOG = ("dónde quedé", "donde quede")
SECTION_PHASES = "avance"
SECTION_STAGES = "etapas"
STAGES_LINE = re.compile(r"^Etapas:\s*(.+)$", re.M)
PREVIOUS_ENTRY = "(Entrada anterior:)"
ENTRY_PREFIX = re.compile(r"^\((Entrada anterior|Estado previo)[^)]*\)\s*")

# Sentences that ask for a decision from the user.
DECISION = re.compile(
    r"decisi[oó]n(es)? pendiente|pendiente(s)? de (su|la usuaria)|"
    r"esperan? (su )?(decisi|respuesta|visto bueno)|esperando (decisi|respuesta|visto bueno|su)|"
    r"falta (su )?visto bueno|pregunta(r)? a la usuaria",
    re.I,
)


# ------------------------------------------------------------------ text
def clean_markdown(text: str) -> str:
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, length: int) -> str:
    """Cut at a word boundary and add an ellipsis."""
    text = clean_markdown(text or "")
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def clip(text: str, length: int) -> str:
    """Cut at an exact length (for tight labels)."""
    text = clean_markdown(text or "")
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def parse_date(text: str) -> datetime | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?", text or "")
    if not match:
        return None
    try:
        return datetime.fromisoformat(f"{match.group(1)} {match.group(2) or '00:00'}")
    except ValueError:
        return None


def markdown_tables(text: str) -> list[tuple[list[str], list[dict]]]:
    """Every Markdown table as (header, rows), with lower-case headers."""
    tables, header, rows = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line[1:-1].split("|")]
            if header is None:
                header, rows = [cell.lower() for cell in cells], []
            elif not all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                cells += [""] * (len(header) - len(cells))
                rows.append(dict(zip(header, cells, strict=False)))  # extra cells are ignored
        elif header is not None:
            tables.append((header, rows))
            header = None
    if header is not None:
        tables.append((header, rows))
    return tables


def sections(text: str) -> list[tuple[str, str]]:
    """Level-2 sections as (title, body), in order."""
    found, title, body = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if title is not None:
                found.append((title, "\n".join(body)))
            title, body = line[3:].strip(), []
        elif title is not None:
            body.append(line)
    if title is not None:
        found.append((title, "\n".join(body)))
    return found


# ------------------------------------------------------------- matching
def agent_in(text: str) -> str | None:
    """Key of the first agent named in the text."""
    best = None
    for agent in AGENTS:
        match = re.search(rf"\b{agent.name}\b", text or "", re.I)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), agent.key)
    return best[1] if best else None


def phase_in(text: str) -> str | None:
    for key, pattern in PHASES:
        if re.search(pattern, text or "", re.I):
            return key
    return None


def agent_status(status_text: str, note: str = "") -> str:
    status, note = (status_text or "").lower(), (note or "").lower()
    if "bloque" in status:
        return "paused" if "paus" in note else "blocked"
    if "curso" in status:
        return "working"
    if any(word in status for word in ("paus", "diferid", "pendiente", "espera")):
        return "paused"
    return "idle"


def phase_status(status_text: str) -> str:
    status = (status_text or "").lower()
    if "bloque" in status:
        return "blocked"
    if "curso" in status:
        return "working"
    if any(word in status for word in ("listo", "entregad", "hecho")):
        return "done"
    if any(word in status for word in ("diferid", "paus", "espera")):
        return "paused"
    return "pending"


# ---------------------------------------------------------------- board
def read_board(path: str | Path, module: str) -> list[BoardRow]:
    """Rows of the module, newest first."""
    rows = []
    for header, table in markdown_tables(read_text(path)):
        if COL_TASK not in header or COL_AGENT not in header:
            continue
        for cells in table:
            task = cells.get(COL_TASK, "")
            if task != module and not task.startswith(module + ":"):
                continue
            rows.append(
                BoardRow(
                    task=task[len(module) :].lstrip(": ").strip(),
                    stage=cells.get(COL_STAGE, ""),
                    agent=agent_in(cells.get(COL_AGENT, "")),
                    agent_label=cells.get(COL_AGENT, ""),
                    status=agent_status(cells.get(COL_STATUS, ""), cells.get(COL_NOTE, "")),
                    status_text=cells.get(COL_STATUS, ""),
                    note=cells.get(COL_NOTE, ""),
                    updated=parse_date(cells.get(COL_UPDATED, "")),
                )
            )
    rows.sort(key=lambda row: row.updated or datetime.min, reverse=True)
    return rows


def decision_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚ¿*-])|\s+-\s+", clean_markdown(text))
    return [shorten(part, 260) for part in parts if DECISION.search(part)]


# ----------------------------------------------------------- plan files
def read_plan_files(folder: str | Path, module: str) -> PlanFile:
    """The module's plan file plus its companions (<module>.<part>.md)."""
    main = Path(folder) / f"{module}.md"
    plan = PlanFile()
    for path in [main, *sorted(Path(folder).glob(f"{module}.*.md"))]:
        text = read_text(path)
        if not text:
            continue
        if path == main:
            plan.title = clean_markdown(text.splitlines()[0].lstrip("# "))
        for title, body in sections(text):
            title = title.lower()
            if title.startswith(SECTION_LOG):
                plan.entries += _log_entries(body, path.name)
            if SECTION_PHASES in title:
                plan.phases.update(_phase_records(body))
            if SECTION_STAGES in title and not plan.stages:
                plan.stages = _stages(body)
    plan.entries.sort(key=lambda entry: entry.date or datetime.min, reverse=True)
    if plan.entries:
        # Only the newest entry is current; older ones are history.
        newest = plan.entries[0]
        plan.decisions = [
            Note(text=sentence, source=newest.source, date=newest.date)
            for sentence in decision_sentences(newest.full_text)
        ]
    return plan


def _log_entries(body: str, source: str) -> list[LogEntry]:
    blocks, current = [], []
    for raw in body.splitlines():
        line = raw.strip()
        if ENTRY_PREFIX.match(line) and current:
            blocks.append(current)
            current = []
        if line and line != PREVIOUS_ENTRY:
            current.append(line)
    if current:
        blocks.append(current)
    entries = []
    for block in blocks:
        text = ENTRY_PREFIX.sub("", " ".join(block))
        head = re.match(r"\*\*(.+?)\*\*", text)
        summary = head.group(1) if head else text
        entries.append(
            LogEntry(
                date=parse_date(summary) or parse_date(text),
                agent=agent_in(summary.split("—")[0]) or agent_in(summary),
                text=shorten(re.sub(r"^\d{4}-\d{2}-\d{2}\s*(\([^)]*\)\s*[—-]\s*)?", "", summary), 220),
                full_text=text,
                source=source,
            )
        )
    return entries


def _phase_records(body: str) -> dict[str, PhaseRecord]:
    records = {}
    for header, table in markdown_tables(body):
        if COL_PHASE not in header or COL_STATUS not in header:
            continue
        for cells in table:
            key = phase_in(cells.get(COL_PHASE, ""))
            if key:
                records[key] = PhaseRecord(
                    cells.get(COL_STATUS, ""), parse_date(cells.get(COL_DATE, "")), cells.get(COL_AGENT, "")
                )
    return records


def _stages(body: str) -> list[StageDef]:
    match = STAGES_LINE.search(body)
    if not match:
        return []
    found = re.findall(r"(\d+)\s+(.+?)(?=;\s*\d+\s|$)", match.group(1).rstrip("."))
    return [StageDef(int(number), shorten(description, 400)) for number, description in found]


def mentioned_stage(row: BoardRow, number: int) -> bool:
    return re.search(rf"\bEtapa\s+{number}\b", row.task, re.I) is not None
