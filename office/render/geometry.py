"""Isometric projection and basic SVG shapes."""

from ..config import CONFIG

TILE = 32  # screen width of half a tile diagonal
WALL_HEIGHT = 132
PADDING = 24
ROOM_WIDTH = CONFIG["room"]["width_tiles"]
ROOM_DEPTH = CONFIG["room"]["depth_tiles"]
ORIGIN_X = ROOM_DEPTH * TILE + PADDING
ORIGIN_Y = WALL_HEIGHT + 46
SVG_WIDTH = (ROOM_WIDTH + ROOM_DEPTH) * TILE + 2 * PADDING
SVG_HEIGHT = ORIGIN_Y + (ROOM_WIDTH + ROOM_DEPTH) * TILE // 2 + 18
LEFT_WALL = f"matrix(1,-0.5,0,1,{ORIGIN_X},{ORIGIN_Y})"  # plane x = 0
RIGHT_WALL = f"matrix(1,0.5,0,1,{ORIGIN_X},{ORIGIN_Y})"  # plane y = 0


def project(x: float, y: float, z: float = 0) -> tuple[float, float]:
    """Room coordinates in tiles (and z in pixels) to screen pixels."""
    return ORIGIN_X + (x - y) * TILE, ORIGIN_Y + (x + y) * TILE / 2 - z


def polygon(points, css_class: str) -> str:
    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polygon class="{css_class}" points="{coords}"/>'


def box(x0: float, y0: float, x1: float, y1: float, z0: float, height: float, css_class: str) -> str:
    """A block with its three visible faces."""
    top = z0 + height
    return (
        polygon(
            [project(x0, y0, top), project(x1, y0, top), project(x1, y1, top), project(x0, y1, top)],
            f"{css_class} face-top",
        )
        + polygon(
            [project(x1, y0, z0), project(x1, y1, z0), project(x1, y1, top), project(x1, y0, top)],
            f"{css_class} face-x",
        )
        + polygon(
            [project(x0, y1, z0), project(x1, y1, z0), project(x1, y1, top), project(x0, y1, top)],
            f"{css_class} face-y",
        )
    )


def cylinder(cx: float, cy: float, rx: float, ry: float, height: float, css_class: str) -> str:
    """(cx, cy) is the centre of the base on screen."""
    top = cy - height
    side = (
        f"M{cx - rx:.1f} {top:.1f} L{cx - rx:.1f} {cy:.1f} A{rx} {ry} 0 0 0 {cx + rx:.1f} {cy:.1f} "
        f"L{cx + rx:.1f} {top:.1f} A{rx} {ry} 0 0 1 {cx - rx:.1f} {top:.1f} Z"
    )
    return (
        f'<path class="{css_class} face-x" d="{side}"/>'
        f'<ellipse class="{css_class} face-top" cx="{cx:.1f}" cy="{top:.1f}" rx="{rx}" ry="{ry}"/>'
    )
