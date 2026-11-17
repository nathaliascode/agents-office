"""Pixel art drawn here: fallback characters and small decorations (no external images)."""

import base64

# 16x16 character: H hair, S skin, E eye, W eye light, M mouth, T shirt.
CHARACTER = [
    "................",
    "...HHHHHHHHHH...",
    "...HHHHHHHHHH...",
    "...HSSSSSSSSH...",
    "...SSSSSSSSSS...",
    "...SWESSSSWES...",
    "...SSSSSSSSSS...",
    "...SSSSMMSSSS...",
    "...SSSSSSSSSS...",
    "......SSSS......",
    "..TTTTTTTTTTTT..",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
    ".TTTTTTTTTTTTTT.",
]

# role: (hair, skin, shirt, accessory pixels [(x, y, color), ...])
ROLE_PIXELS = {
    "Orchestrator": (
        "#3b2416",
        "#e8b48a",
        "#6c4fd1",  # headset
        [
            (2, 4, "#222"),
            (2, 5, "#222"),
            (2, 6, "#222"),
            (13, 4, "#222"),
            (13, 5, "#222"),
            (13, 6, "#222"),
            (12, 7, "#222"),
            (11, 8, "#222"),
            (3, 0, "#222"),
            (12, 0, "#222"),
        ]
        + [(x, 0, "#222") for x in range(4, 12)],
    ),
    "Analyst": (
        "#1c1410",
        "#c98d62",
        "#2a8c80",  # magnifying glass
        [
            (11, 11, "#cfe9f7"),
            (12, 11, "#cfe9f7"),
            (10, 11, "#1d3b37"),
            (13, 11, "#1d3b37"),
            (11, 10, "#1d3b37"),
            (12, 10, "#1d3b37"),
            (11, 12, "#1d3b37"),
            (12, 12, "#1d3b37"),
            (13, 13, "#1d3b37"),
            (14, 14, "#1d3b37"),
        ],
    ),
    "Designer": (
        "#8a4430",
        "#f0c49c",
        "#e0703f",  # beret and pencil
        [(x, 0, "#c0392b") for x in range(3, 13)]
        + [(x, 1, "#c0392b") for x in range(2, 12)]
        + [(12, 10, "#f1c40f"), (12, 11, "#f1c40f"), (12, 12, "#f1c40f"), (12, 13, "#e67e22")],
    ),
    "Developer": (
        "#2a2a2a",
        "#e3a77c",
        "#2f6fd0",  # glasses and </>
        [(x, 5, "#14213d") for x in (3, 6, 9, 12)]
        + [(7, 5, "#14213d"), (8, 5, "#14213d")]
        + [(5, 12, "#ffffff"), (6, 12, "#7ee0a8"), (8, 12, "#7ee0a8"), (9, 12, "#ffffff")],
    ),
    "Tester": (
        "#5a3418",
        "#f2c8a0",
        "#d9a52b",  # checklist
        [(x, y, "#fff6dc") for x in range(10, 14) for y in range(11, 15)]
        + [(10, 12, "#2e9e3a"), (11, 13, "#2e9e3a"), (12, 12, "#2e9e3a"), (13, 11, "#2e9e3a")],
    ),
    "Auditor": (
        "#121212",
        "#d9a074",
        "#4a5568",  # shield
        [(x, y, "#d4a017") for x in range(10, 14) for y in range(11, 14)]
        + [(11, 14, "#d4a017"), (12, 14, "#d4a017"), (11, 12, "#7a5c0a"), (12, 12, "#7a5c0a")],
    ),
    "Reviewer": (
        "#6b3f26",
        "#f0c49c",
        "#c24f7a",  # glasses and check mark
        [
            (3, 5, "#3a1d2a"),
            (6, 5, "#3a1d2a"),
            (9, 5, "#3a1d2a"),
            (12, 5, "#3a1d2a"),
            (10, 13, "#ffffff"),
            (11, 14, "#ffffff"),
            (12, 13, "#ffffff"),
            (13, 12, "#ffffff"),
        ],
    ),
    "Documenter": (
        "#2c1d14",
        "#c98d62",
        "#4f8a35",  # book
        [(x, y, "#f4eee0") for x in range(9, 14) for y in range(11, 15)]
        + [(11, y, "#6b4f2a") for y in range(11, 15)]
        + [(9, 12, "#9b8a6a"), (13, 12, "#9b8a6a")],
    ),
}


def shirt_color(role: str) -> str:
    return ROLE_PIXELS[role][2]


def _runs(grid: list[list[str | None]]) -> str:
    """One <rect> per run of equal pixels in a row, to keep the SVG small."""
    rects = []
    for y, row in enumerate(grid):
        x = 0
        while x < len(row):
            end = x
            while end + 1 < len(row) and row[end + 1] == row[x]:
                end += 1
            if row[x]:
                rects.append(f'<rect x="{x}" y="{y}" width="{end - x + 1}" height="1" fill="{row[x]}"/>')
            x = end + 1
    return "".join(rects)


def character_svg(role: str, label: str) -> str:
    hair, skin, shirt, extra = ROLE_PIXELS[role]
    colors = {"H": hair, "S": skin, "E": "#1b1b1b", "W": "#ffffff", "M": "#8a4a32", "T": shirt, ".": None}
    grid = [[colors[char] for char in row] for row in CHARACTER]
    for x, y, color in extra:
        grid[y][x] = color
    return (
        f'<svg viewBox="0 0 16 16" shape-rendering="crispEdges" role="img" aria-label="{label}">'
        '<rect width="16" height="16" fill="var(--avatar-bg)"/>' + _runs(grid) + "</svg>"
    )


def _data_uri(rows: list[str], colors: dict[str, str]) -> str:
    grid = [[colors.get(char) for char in row] for row in rows]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {len(rows[0])} {len(rows)}" '
        f'shape-rendering="crispEdges">{_runs(grid)}</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


PINK, DARK_PINK, YELLOW, GREEN = "#f48fb1", "#e0679a", "#ffe08a", "#7cc49a"
DECOR = {
    "bow": _data_uri(["XX.....XX", "XXX.K.XXX", "XXXXKXXXX", "XXX.K.XXX", "XX..K..XX"], {"X": PINK, "K": DARK_PINK}),
    "flower": _data_uri(["..P..", ".PPP.", "PPYPP", ".PPP.", "..P.."], {"P": "#f7a8c4", "Y": YELLOW}),
    "petal": _data_uri([".PP.", "PPPP", "PPL.", ".P.."], {"P": "#f9b8cf", "L": "#fde1ec"}),
    "pot": _data_uri(
        [
            "..PP.PP..",
            ".PPYPPYP.",
            "..PP.PP..",
            "...G.G...",
            "....G....",
            "..TTTTT..",
            ".TTTTTTT.",
            "..TWTWT..",
            "..TTTTT..",
            "...TTT...",
        ],
        {"P": "#f7a8c4", "Y": YELLOW, "G": GREEN, "T": "#c9b6e8", "W": "#e6dcf6"},
    ),
    "plant": _data_uri(
        [
            "...G.G..G...",
            "..GGG.GGG.G.",
            ".GGLGGGLGGG.",
            "GGLGG.GGLGGG",
            ".GGGGLGGGG..",
            "..GGGGGGLGG.",
            ".GLGG.GGGG..",
            "..GGGGGG....",
            "....GG......",
            "....G.......",
            "..TTTTTTTT..",
            "..TWTTWTTT..",
            "...TTTTTT...",
            "...TTTTTT...",
            "....TTTT....",
        ],
        {"G": "#86c9a0", "L": "#b5e6c6", "T": "#f2b8cc", "W": "#fbd9e5"},
    ),
}


def decor_css() -> str:
    """CSS variables for the decorations used as backgrounds."""
    return f':root{{--px-flower:url("{DECOR["flower"]}");--px-petal:url("{DECOR["petal"]}")}}'


def mix(color: str, other: str, amount: float) -> str:
    start = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    end = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    red, green, blue = (round(a + (b - a) * amount) for a, b in zip(start, end, strict=True))
    return f"#{red:02x}{green:02x}{blue:02x}"
