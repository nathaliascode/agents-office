"""The isometric room: walls with phases and the plan, rugs, desks, agents and meeting table."""

import base64
from dataclasses import dataclass
from html import escape
from pathlib import Path

from ..board import clip
from ..config import AGENTS, AGENTS_BY_KEY, CONFIG
from ..i18n import Translator
from ..models import Agent, OfficeState, Phase, Stage
from ..state import current_phase
from .geometry import (
    LEFT_WALL,
    RIGHT_WALL,
    ROOM_DEPTH,
    ROOM_WIDTH,
    SVG_HEIGHT,
    SVG_WIDTH,
    TILE,
    WALL_HEIGHT,
    box,
    cylinder,
    polygon,
    project,
)
from .labels import agent_task, phase_name, readable, stage_text
from .pixel import DECOR, character_svg, mix, shirt_color

DESKS = {key: tuple(value) for key, value in CONFIG["room"]["desks"].items()}
MEETING_TABLE = tuple(CONFIG["room"]["meeting_table"])
RUGS = {key: tuple(value) for key, value in CONFIG["room"]["rugs"].items()}
PLANTS = ((0.5, 0.5), (0.6, 14.4))
COORDINATOR_NAME = AGENTS_BY_KEY[CONFIG["coordinator"]].name

# Phase cards: same gap between every card and before the plan board.
CARD_WIDTH, CARD_HEIGHT, CARD_TOP, CARD_GAP = 92, 86, -116, 10
CARD_STEP = (CARD_WIDTH + CARD_GAP) / TILE
RIGHT_WALL_START = 0.4
BOARD_WIDTH, BOARD_FRAME = 176, 5
BOARD_CENTRE = RIGHT_WALL_START + 3 * CARD_STEP + (CARD_WIDTH + CARD_GAP + BOARD_FRAME + BOARD_WIDTH / 2) / TILE
SEATS_PER_SIDE = 4


# ---------------------------------------------------------- figures
def torso_svg(cx: float, base: float, height: float, shirt: str) -> str:
    """Chibi body: round shoulders, side shade, V collar, buttons, arms and hands."""
    dark, light, skin = mix(shirt, "#000000", 0.22), mix(shirt, "#ffffff", 0.35), "#f6d3b0"
    half = height * 0.55
    top = base - height
    body = (
        f"M{cx - half:.1f} {base:.1f} L{cx - half:.1f} {top + 10:.1f} "
        f"Q{cx - half:.1f} {top:.1f} {cx - half + 9:.1f} {top:.1f} L{cx + half - 9:.1f} {top:.1f} "
        f"Q{cx + half:.1f} {top:.1f} {cx + half:.1f} {top + 10:.1f} L{cx + half:.1f} {base:.1f} Z"
    )
    shade = (
        f"M{cx + half * .35:.1f} {top + 1:.1f} L{cx + half - 9:.1f} {top:.1f} "
        f"Q{cx + half:.1f} {top:.1f} {cx + half:.1f} {top + 10:.1f} L{cx + half:.1f} {base:.1f} "
        f"L{cx + half * .35:.1f} {base:.1f} Z"
    )
    collar = f"M{cx - 6:.1f} {top:.1f} L{cx:.1f} {top + 8:.1f} L{cx + 6:.1f} {top:.1f} Z"

    def arm(side: int) -> str:
        x = cx + side * (half + 1)
        return (
            f'<rect x="{x - (5 if side > 0 else 1):.1f}" y="{top + 6:.1f}" width="6" height="{height * .62:.1f}" '
            f'rx="3" fill="{dark}"/>'
            f'<circle cx="{x + (-2 if side > 0 else 2):.1f}" cy="{top + 6 + height * .62:.1f}" r="3.4" fill="{skin}"/>'
        )

    return (
        f'<g class="torso">{arm(-1)}{arm(1)}'
        f'<path d="{body}" fill="{shirt}"/><path d="{shade}" fill="{dark}" opacity=".45"/>'
        f'<path d="{collar}" fill="{light}"/>'
        f'<circle cx="{cx:.1f}" cy="{top + 15:.1f}" r="1.6" fill="{light}"/>'
        f'<circle cx="{cx:.1f}" cy="{top + 21:.1f}" r="1.6" fill="{light}"/></g>'
    )


@dataclass(frozen=True)
class FigureSize:
    torso: float  # torso height
    body_drop: float  # from the top of the head to where the body rests
    head: float  # head (avatar) size
    bow: tuple[int, int]  # bow width and height


AT_DESK = FigureSize(torso=32, body_drop=70, head=38, bow=(20, 11))
AT_TABLE = FigureSize(torso=30, body_drop=65, head=36, bow=(18, 10))


def figure_svg(agent: Agent, cx: float, head_top: float, size: FigureSize) -> str:
    """Body, head (avatar), bow and status mark."""
    bow_width, bow_height = size.bow
    return (
        f'<g class="figure" data-figure="{agent.key}">'
        + torso_svg(cx, head_top + size.body_drop, size.torso, shirt_color(agent.role))
        + f'<use href="#head-{agent.key}" x="{cx - size.head / 2:.1f}" y="{head_top:.1f}" '
        f'width="{size.head:g}" height="{size.head:g}"/>'
        + f'<use href="#decor-bow" x="{cx - size.head / 2 + 9:.1f}" y="{head_top - 6:.1f}" '
        f'width="{bow_width}" height="{bow_height}"/>' + _status_mark(agent, cx, head_top) + "</g>"
    )


def _status_mark(agent: Agent, cx: float, head_top: float) -> str:
    if agent.status == "blocked":
        return (
            f'<g class="blocked-mark"><circle cx="{cx + 17:.1f}" cy="{head_top + 4:.1f}" r="8"/>'
            f'<text x="{cx + 17:.1f}" y="{head_top + 8:.1f}" text-anchor="middle">!</text></g>'
        )
    if agent.status == "paused":
        return f'<text class="sleep-mark" x="{cx + 14:.1f}" y="{head_top + 6:.1f}">z<tspan dy="-5">z</tspan></text>'
    return ""


def bubble_svg(cx: float, base: float, tr: Translator) -> str:
    """«Working…» speech bubble over the head, with animated dots."""
    width, height = 92, 24
    x, y = cx - width / 2, base - height
    return (
        f'<g class="bubble" aria-hidden="true"><g class="bubble-float">'
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width}" height="{height}" rx="12"/>'
        f'<path class="bubble-tail" d="M{cx - 6:.1f} {base - 1:.1f} L{cx - 1:.1f} {base + 7:.1f} '
        f'L{cx + 5:.1f} {base - 1:.1f} Z"/>'
        f'<text x="{x + 12:.1f}" y="{y + 16:.1f}">{tr("room.bubble")}<tspan class="dot1">.</tspan>'
        '<tspan class="dot2">.</tspan><tspan class="dot3">.</tspan></text></g></g>'
    )


def agent_group(agent: Agent, view: str, inner: str, tr: Translator, extra_class: str = "") -> str:
    status = tr(f"status.{agent.status}")
    task = agent_task(agent, tr)
    label = f"{escape(agent.name)} {escape(agent.role)}"
    css = extra_class or f"status-{agent.status}"
    return (
        f'<g class="agent {css}" tabindex="0" role="button" data-modal="agent-{agent.key}-{view}" '
        f'aria-label="{label}: {status}"><title>{label} — {status}{": " + escape(task) if task else ""}</title>'
        f"{inner}</g>"
    )


# --------------------------------------------------------------- defs
def defs_svg(avatars: str | None, tr: Translator) -> str:
    """Heads and decorations defined once and reused with <use>."""
    parts = ['<clipPath id="head-clip"><rect width="40" height="40" rx="11"/></clipPath>']
    for agent in AGENTS:
        path = Path(avatars) / f"{agent.key}.png" if avatars else None
        if path and path.is_file():
            data = base64.b64encode(path.read_bytes()).decode()
            inner = (
                f'<image href="data:image/png;base64,{data}" width="40" height="44" y="-1" '
                'preserveAspectRatio="xMidYMin slice" clip-path="url(#head-clip)"/>'
            )
        else:
            drawing = character_svg(agent.role, tr("room.character", role=agent.role))
            inner = (
                '<g clip-path="url(#head-clip)">' + drawing.replace("<svg ", '<svg width="40" height="40" ', 1) + "</g>"
            )
        parts.append(
            f'<symbol id="head-{agent.key}" viewBox="0 0 40 40">{inner}'
            '<rect class="head-border" width="40" height="40" rx="11"/></symbol>'
        )
    for name in ("pot", "plant", "bow"):
        parts.append(
            f'<symbol id="decor-{name}" viewBox="0 0 10 10" preserveAspectRatio="none">'
            f'<image href="{DECOR[name]}" width="10" height="10" preserveAspectRatio="none" '
            'style="image-rendering:pixelated"/></symbol>'
        )
    return (
        '<svg class="defs" width="0" height="0" aria-hidden="true" focusable="false"><defs>'
        + "".join(parts)
        + "</defs></svg>"
    )


# -------------------------------------------------------------- walls
def _walls() -> list[str]:
    height = WALL_HEIGHT
    layers = [
        polygon(
            [project(x, y), project(x + 1, y), project(x + 1, y + 1), project(x, y + 1)],
            "floor-a" if (x + y) % 2 else "floor-b",
        )
        for x in range(ROOM_WIDTH)
        for y in range(ROOM_DEPTH)
    ]
    layers += [
        polygon(
            [project(0, 0), project(0, ROOM_DEPTH), project(0, ROOM_DEPTH, height), project(0, 0, height)], "wall-left"
        ),
        polygon(
            [project(0, 0), project(ROOM_WIDTH, 0), project(ROOM_WIDTH, 0, height), project(0, 0, height)], "wall-right"
        ),
        polygon(
            [project(0, 0), project(0, ROOM_DEPTH), project(0, ROOM_DEPTH, 10), project(0, 0, 10)], "baseboard-left"
        ),
        polygon(
            [project(0, 0), project(ROOM_WIDTH, 0), project(ROOM_WIDTH, 0, 10), project(0, 0, 10)], "baseboard-right"
        ),
        polygon(
            [
                project(0, 0, height),
                project(0, ROOM_DEPTH, height),
                project(-0.25, ROOM_DEPTH, height + 6),
                project(-0.25, -0.25, height + 6),
            ],
            "wall-top",
        ),
        polygon(
            [
                project(0, 0, height),
                project(ROOM_WIDTH, 0, height),
                project(ROOM_WIDTH, -0.25, height + 6),
                project(-0.25, -0.25, height + 6),
            ],
            "wall-top",
        ),
    ]
    return layers


def _phase_cards(phases: list[Phase], tr: Translator) -> str:
    current = current_phase(phases)
    cards = []
    for index, phase in enumerate(phases):
        if index < 4:  # left wall, from the front towards the corner
            x, plane = -(ROOM_DEPTH - 0.9 - index * CARD_STEP) * TILE, LEFT_WALL
        else:  # right wall, from the corner
            x, plane = (RIGHT_WALL_START + (index - 4) * CARD_STEP) * TILE, RIGHT_WALL
        is_current = phase.key == current
        name = phase_name(phase.key, tr)
        detail = (
            phase.live_task
            and tr("session.phase_in_progress", task=phase.live_task)
            or phase.status_text
            or tr("agent.no_record")
        )
        y, w, h = CARD_TOP, CARD_WIDTH, CARD_HEIGHT
        cards.append(
            f'<g class="phase-card phase-{phase.status}{" current" if is_current else ""}" transform="{plane}">'
            f"<title>{index + 1}. {escape(name)}: {escape(detail)}"
            f'{" · " + readable(phase.date) if phase.date else ""}</title>'
            f'<rect class="phase-card-shadow" x="{x + 3:.1f}" y="{y + 4}" width="{w}" height="{h}" rx="10"/>'
            f'<rect class="phase-card-bg" x="{x:.1f}" y="{y}" width="{w}" height="{h}" rx="10"/>'
            f'<rect class="phase-card-band" x="{x:.1f}" y="{y}" width="{w}" height="26" rx="10"/>'
            f'<rect class="phase-card-band" x="{x:.1f}" y="{y + 14}" width="{w}" height="12"/>'
            f'<circle class="phase-card-circle" cx="{x + 18:.1f}" cy="{y + 13}" r="9"/>'
            f'<text class="phase-card-num" x="{x + 18:.1f}" y="{y + 17.5}" text-anchor="middle">{index + 1}</text>'
            f'<text class="phase-card-status" x="{x + 32:.1f}" y="{y + 17}">{tr(f"phase_status.{phase.status}")}</text>'
            f'<text class="phase-card-name" x="{x + w / 2:.1f}" y="{y + 50}" text-anchor="middle">'
            f"{escape(clip(name, 14))}</text>"
            f'<text class="phase-card-date" x="{x + w / 2:.1f}" y="{y + 68}" text-anchor="middle">'
            f'{phase.date.strftime("%d/%m") if phase.date else "—"}</text>'
            + (
                f'<g class="phase-card-now">'
                f'<rect x="{x + w / 2 - 26:.1f}" y="{y + h - 9}" width="52" height="16" rx="8"/>'
                f'<text x="{x + w / 2:.1f}" y="{y + h + 2.5}" text-anchor="middle">{tr("room.now")}</text></g>'
                if is_current
                else ""
            )
            + "</g>"
        )
    return "".join(cards)


def plan_board_svg(stages: list[Stage], cx: float, base: float, tr: Translator) -> str:
    """The change plan on a framed board; one row per stage."""
    width = BOARD_WIDTH
    height = 28 + max(1, len(stages)) * 13
    x, y = cx - width / 2, base - height
    rows = [
        f'<rect class="board-frame" x="{x - BOARD_FRAME:.1f}" y="{y - BOARD_FRAME:.1f}" '
        f'width="{width + 2 * BOARD_FRAME}" height="{height + 2 * BOARD_FRAME}" rx="10"/>'
        f'<rect class="board-bg" x="{x:.1f}" y="{y:.1f}" width="{width}" height="{height}" rx="7"/>'
        f'<text class="board-title" x="{cx:.1f}" y="{y + 18:.1f}" text-anchor="middle">{tr("room.plan_title")}</text>'
    ]
    if not stages:
        rows.append(
            f'<text class="board-text" x="{cx:.1f}" y="{y + 38:.1f}" text-anchor="middle">{tr("room.no_plan")}</text>'
        )
    for index, stage in enumerate(stages):
        row_y = y + 32 + index * 13
        css = f"board-row phase-{stage.status}" + (" current" if stage.status == "working" else "")
        rows.append(
            f'<g class="{css}"><title>{tr("room.stage", n=stage.number)}: {escape(stage.description)} — '
            f"{escape(stage_text(stage, tr))}</title>"
            f'<rect class="board-highlight" x="{x + 5:.1f}" y="{row_y - 9.5:.1f}" '
            f'width="{width - 10}" height="12.5" rx="6"/>'
            f'<circle class="board-dot" cx="{x + 14:.1f}" cy="{row_y - 3:.1f}" r="4"/>'
            f'<text class="board-num" x="{x + 23:.1f}" y="{row_y:.1f}">{tr("room.stage_short", n=stage.number)}</text>'
            f'<text class="board-text" x="{x + 44:.1f}" y="{row_y:.1f}">'
            f"{escape(clip(stage.description, 22))}</text></g>"
        )
    return '<g class="board">' + "".join(rows) + "</g>"


def _wall_plan(stages: list[Stage], view: str, tr: Translator) -> str:
    return (
        f'<g class="wall-plan" data-modal="plan-{view}" tabindex="0" role="button" aria-label="{tr("plan.open")}">'
        f'<title>{tr("plan.open")}</title><g transform="{RIGHT_WALL}">'
        + plan_board_svg(stages, BOARD_CENTRE * TILE, -6, tr)
        + "</g></g>"
    )


# ---------------------------------------------------------------- floor
def _rugs() -> list[str]:
    return [
        polygon([project(x0, y0, 1), project(x1, y0, 1), project(x1, y1, 1), project(x0, y1, 1)], f"rug rug-{zone}")
        for zone, (x0, y0, x1, y1) in RUGS.items()
    ]


def _zone_labels(tr: Translator) -> list[tuple[float, str]]:
    """Each label sits on the front edge of its rug, centred on the longer side."""
    labels = []
    for zone, (x0, y0, x1, y1) in RUGS.items():
        cx, cy = project((x0 + x1) / 2, y1) if (x1 - x0) >= (y1 - y0) else project(x1, (y0 + y1) / 2)
        labels.append(
            (
                -1,
                f'<g class="zone-label"><rect x="{cx - 52:.1f}" y="{cy - 4:.1f}" width="104" height="19" rx="9.5"/>'
                f'<text x="{cx:.1f}" y="{cy + 10:.1f}" text-anchor="middle">{tr(f"zones.{zone}")}</text></g>',
            )
        )
    return labels


def _plants() -> list[tuple[float, str]]:
    plants = []
    for x, y in PLANTS:
        sx, sy = project(x, y)
        plants.append((x + y, f'<use href="#decor-plant" x="{sx - 22:.1f}" y="{sy - 62:.1f}" width="44" height="62"/>'))
    return plants


def _desk(agent: Agent, view: str, tr: Translator) -> tuple[float, str]:
    x0, y0 = DESKS[agent.key]
    at_desk = agent.status == "working"
    cx, cy = project(x0 + 1.0, y0 - 0.62, 16)
    parts = [
        box(x0 + 0.62, y0 - 1.05, x0 + 1.38, y0 - 0.92, 16, 30, "chair"),  # backrest
        box(x0 + 0.62, y0 - 1.0, x0 + 1.38, y0 - 0.3, 0, 16, "chair"),
    ]  # seat
    if at_desk:
        parts.append(figure_svg(agent, cx, cy - 66, AT_DESK))
    parts.append(box(x0, y0, x0 + 2, y0 + 1, 0, 26, "desk"))
    px, py = project(x0, y0 + 1)
    parts.append(
        f'<g class="nameplate" transform="matrix(1,0.5,0,1,{px:.1f},{py:.1f})">'
        f'<rect x="5" y="-23" width="54" height="19" rx="3"/>'
        f'<text class="nameplate-name" x="32" y="-13" text-anchor="middle">{escape(agent.name)}</text>'
        f'<text class="nameplate-role" x="32" y="-6.5" text-anchor="middle">{escape(agent.role.upper())}</text></g>'
    )
    parts.append(box(x0 + 0.18, y0 + 0.12, x0 + 0.36, y0 + 0.82, 26, 4, "monitor"))  # stand
    parts.append(box(x0 + 0.24, y0 + 0.05, x0 + 0.32, y0 + 0.9, 30, 24, "monitor"))  # monitor
    sx0, sy0 = project(x0 + 0.32, y0 + 0.12, 33)
    sx1, sy1 = project(x0 + 0.32, y0 + 0.83, 33)
    parts.append(
        f'<polygon class="screen" points="{sx0:.1f},{sy0:.1f} {sx1:.1f},{sy1:.1f} '
        f'{sx1:.1f},{sy1 - 18:.1f} {sx0:.1f},{sy0 - 18:.1f}"/>'
    )
    mx, my = project(x0 + 1.65, y0 + 0.5, 26)
    parts.append(f'<use href="#decor-pot" x="{mx - 9:.1f}" y="{my - 22:.1f}" width="18" height="24"/>')
    hx, hy = project(x0 + 1.0, y0 + 0.45, 26)
    parts.append(
        f'<polygon class="paper" points="{hx:.1f},{hy - 3:.1f} {hx + 11:.1f},{hy + 2:.1f} '
        f'{hx + 2:.1f},{hy + 7:.1f} {hx - 9:.1f},{hy + 2:.1f}"/>'
    )
    if at_desk:
        parts.append(bubble_svg(cx, cy - 74, tr))
    css = f"status-{agent.status}" if at_desk else "empty-desk status-idle"
    return x0 + y0 + 1, agent_group(agent, view, "".join(parts), tr, css)


# ------------------------------------------------------- meeting table
def _seats() -> list[tuple[float, float]]:
    """Front side first (it is the most visible), then the back side."""
    x0, y0, x1, y1 = MEETING_TABLE
    step = (x1 - x0) / SEATS_PER_SIDE
    front = [(x0 + step * (i + 0.5), y1 + 0.55) for i in range(SEATS_PER_SIDE)]
    back = [(x0 + step * (i + 0.5), y0 - 0.55) for i in range(SEATS_PER_SIDE)]
    return front + back


def _seat(x: float, y: float, agent: Agent | None, view: str, tr: Translator) -> str:
    sx, sy = project(x, y)
    stool = cylinder(sx, sy, 13, 6.5, 16, "stool")
    if not agent:
        return stool
    return agent_group(agent, view, stool + figure_svg(agent, sx, sy - 78, AT_TABLE), tr)


def _waiting_alert(count: int, view: str, tr: Translator) -> str:
    x0, y0, x1, y1 = MEETING_TABLE
    bx, by = project((x0 + x1) / 2, (y0 + y1) / 2, 24 + 70)
    text = tr("room.waiting") if count == 1 else tr("room.waiting_many", n=count)
    width = len(text) * 6.4 + 34
    left = bx - width / 2
    return (
        f'<g class="waiting-alert" data-modal="waiting-{view}" tabindex="0" role="button" '
        f'aria-label="{tr("room.waiting_aria", n=count)}">'
        f'<title>{tr("room.waiting_tooltip", coordinator=COORDINATOR_NAME)}'
        '</title><g class="bounce">'
        f'<rect x="{left:.1f}" y="{by - 26:.1f}" width="{width:.1f}" height="26" rx="13"/>'
        f'<path class="waiting-alert-tail" '
        f'd="M{bx - 6:.1f} {by - 1:.1f} L{bx:.1f} {by + 8:.1f} L{bx + 6:.1f} {by - 1:.1f} Z"/>'
        f'<circle class="waiting-alert-icon" cx="{left + 14:.1f}" cy="{by - 13:.1f}" r="8"/>'
        f'<text class="waiting-alert-mark" x="{left + 14:.1f}" y="{by - 9:.1f}" text-anchor="middle">!</text>'
        f'<text class="waiting-alert-text" x="{left + 27:.1f}" y="{by - 9:.1f}">{escape(text)}</text></g></g>'
    )


def _meeting_table(seated: list[Agent], waiting: int, view: str, tr: Translator) -> tuple[str, str]:
    """Fixed-size rectangular table with one seat per team member.
    Returns the table and, separately, the name tags (drawn above everything)."""
    x0, y0, x1, y1 = MEETING_TABLE
    middle = (y0 + y1) / 2
    height, thickness, leg = 24, 5, 0.22
    occupants = seated[: 2 * SEATS_PER_SIDE] + [None] * (2 * SEATS_PER_SIDE - len(seated))
    seats = list(zip(_seats(), occupants, strict=True))
    parts = [
        polygon(
            [
                project(x0 - 0.25, y0 - 0.25),
                project(x1 + 0.25, y0 - 0.25),
                project(x1 + 0.25, y1 + 0.25),
                project(x0 - 0.25, y1 + 0.25),
            ],
            "table-shadow",
        )
    ]
    parts += [_seat(x, y, agent, view, tr) for (x, y), agent in seats if y < middle]
    for lx, ly in (
        (x0 + 0.15, y0 + 0.15),
        (x1 - 0.15 - leg, y0 + 0.15),
        (x0 + 0.15, y1 - 0.15 - leg),
        (x1 - 0.15 - leg, y1 - 0.15 - leg),
    ):
        parts.append(box(lx, ly, lx + leg, ly + leg, 0, height - thickness, "desk leg"))
    parts.append(box(x0, y0, x1, y1, height - thickness, thickness, "desk"))
    for index in range(min(waiting, 6)):  # one sheet per pending answer
        hx, hy = project(x0 + 0.6 + index * 0.8, middle + (0.25 if index % 2 else -0.25), height)
        parts.append(
            f'<polygon class="paper" points="{hx:.1f},{hy - 4:.1f} {hx + 11:.1f},{hy + 1.5:.1f} '
            f'{hx + 1:.1f},{hy + 7:.1f} {hx - 10:.1f},{hy + 1.5:.1f}"/>'
        )
    parts += [_seat(x, y, agent, view, tr) for (x, y), agent in seats if y > middle]
    if waiting:
        parts.append(_waiting_alert(waiting, view, tr))
    names = []
    for (x, y), agent in seats:
        if agent:
            sx, sy = project(x, y)
            width = min(58, len(agent.name) * 6.2 + 12)
            names.append(
                f'<g class="seat-name" data-figure="{agent.key}" data-modal="agent-{agent.key}-{view}">'
                f'<rect x="{sx - width / 2:.1f}" y="{sy + 5:.1f}" width="{width:.1f}" height="14" rx="7"/>'
                f'<text x="{sx:.1f}" y="{sy + 15:.1f}" text-anchor="middle" textLength="{width - 10:.1f}" '
                f'lengthAdjust="spacingAndGlyphs">{escape(agent.name)}</text></g>'
            )
    label = tr("room.table_waiting", n=waiting) if waiting else tr("room.table")
    return f'<g class="meeting" aria-label="{label}">' + "".join(parts) + "</g>", "".join(names)


# ---------------------------------------------------------------- room
def room_svg(agents: list[Agent], state: OfficeState, view: str, waiting: int, tr: Translator) -> str:
    layers = _walls()
    layers.append(_wall_plan(state.stages, view, tr))
    layers.append(_phase_cards(state.phases, tr))
    layers += _rugs()
    objects = _zone_labels(tr) + _plants()
    table, names = _meeting_table([agent for agent in agents if agent.status != "working"], waiting, view, tr)
    objects.append((MEETING_TABLE[1] + MEETING_TABLE[3], table))
    objects += [_desk(agent, view, tr) for agent in agents]
    objects.sort(key=lambda item: item[0])  # back to front
    layers += [svg for _depth, svg in objects]
    layers.append(f'<g class="name-layer">{names}</g>')  # above everything: never hidden
    return (
        f'<svg class="room" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="{tr("room.aria")}">' + "".join(layers) + "</svg>"
    )
