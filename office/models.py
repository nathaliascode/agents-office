"""Plain data structures shared by readers and renderers.

Statuses use a small fixed vocabulary:
- agents and assignments: "working", "paused", "blocked", "idle";
- phases and stages: "done", "working", "paused", "blocked", "pending".
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BoardRow:
    """One row of tablero.md that belongs to the module."""

    task: str  # without the module prefix; "" means the whole module
    stage: str  # free text: "Implementación", "Pruebas"…
    agent: str | None  # agent key
    agent_label: str  # agent cell as written
    status: str  # agent status
    status_text: str  # status cell as written
    note: str
    updated: datetime | None


@dataclass
class LogEntry:
    """One entry of the «Dónde quedé» section of a plan file."""

    date: datetime | None
    agent: str | None
    text: str
    full_text: str
    source: str  # plan file name


@dataclass
class PhaseRecord:
    status_text: str
    date: datetime | None
    agent_label: str


@dataclass
class StageDef:
    number: int
    description: str


@dataclass
class PlanFile:
    title: str = ""
    entries: list[LogEntry] = field(default_factory=list)
    phases: dict[str, PhaseRecord] = field(default_factory=dict)
    stages: list[StageDef] = field(default_factory=list)
    decisions: list["Note"] = field(default_factory=list)


@dataclass
class Note:
    """Something written in the log that may need the user's attention."""

    text: str
    source: str  # "board", "session" or a plan file name
    date: datetime | None
    agent_label: str = ""  # agent as written on the board, for board notes


@dataclass
class HistoryItem:
    date: datetime | None
    text: str
    status: str = ""  # as written in the log
    note: str = ""
    source: str = ""  # "board", "session", "subagent" or a plan file name
    assignment_status: str = ""  # set for subagent assignments
    tokens: "Tokens | None" = None
    subagent_count: int | None = None  # set for the session start item


@dataclass
class Tokens:
    context: int  # tokens of the last request
    output: int  # total output tokens


@dataclass
class Agent:
    key: str
    name: str
    role: str
    zone: str
    status: str = "idle"
    task: str = ""
    stage: str = ""  # as written in the log
    stage_key: str = ""  # translated label instead of stage ("this_session", "waiting_reply")
    note: str = ""
    since: datetime | None = None
    history: list[HistoryItem] = field(default_factory=list)
    tokens: Tokens | int | None = None
    time_key: str = ""  # translated elapsed-time prefix ("waiting", "activity")
    empty_key: str = ""  # translated text when there is no date ("no_assignments")
    whole_module: bool = False  # the task covers the whole module


@dataclass
class Phase:
    key: str
    status: str = "pending"
    status_text: str = ""  # as written in the log; "" means no record
    date: datetime | None = None
    agent: str | None = None
    live_task: str | None = None  # set when a running subagent of the session drives it


@dataclass
class Stage:
    number: int
    description: str
    status: str = "pending"
    status_text: str = ""
    date: datetime | None = None
    live: str | None = None  # "working" or "done" when the session drives it


@dataclass
class OfficeState:
    agents: list[Agent]
    phases: list[Phase]
    stages: list[Stage]
    notes: list[Note]
    blockers: list[Agent]


@dataclass
class Transcript:
    """Metadata of a Claude Code transcript. Never the conversation text."""

    started: datetime | None = None
    ended: datetime | None = None
    title: str | None = None
    origin: str | None = None
    tokens: Tokens | None = None
    last_message: dict | None = None
    plan_files: dict[str, int] = field(default_factory=dict)
    finished_turn: bool = False
    worked_this_turn: bool = False


@dataclass
class Assignment:
    task: str
    status: str
    started: datetime | None
    finished: datetime | None
    tokens: Tokens | None


@dataclass
class Waiting:
    since: datetime | None
    kind: str  # "question", "plan" or "turn"


@dataclass
class Session:
    id: str
    title: str | None
    origin: str | None
    started: datetime | None
    last_activity: datetime
    active_now: bool
    transcript: Transcript
    assignments: dict[str, list[Assignment]]
    subagent_count: int
    plan_file: str | None
    waiting: Waiting | None
    state: OfficeState | None = None

    @property
    def working_count(self) -> int:
        return sum(1 for items in self.assignments.values() for item in items if item.status == "working")


@dataclass
class UsageWindow:
    used: float
    resets_at: int | None


@dataclass
class Usage:
    five_hour: UsageWindow | None
    week: UsageWindow | None
    updated: int | None
