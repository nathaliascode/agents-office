#!/usr/bin/env python3
"""Agents office: an isometric pixel-art view of what your Claude Code agents are doing.

    python3 office.py --watch 10     live: http://localhost:8765, updates without reloading
    python3 office.py                one-off: writes office.html (and one file per language)

Reads (read-only) the project log, the metadata of Claude Code sessions and the
usage limits. Nothing is published or downloaded.
"""

import argparse
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from office.build import Options, build, output_files
from office.config import CONFIG, EXAMPLE, LOG_DIR, ROOT
from office.server import serve
from office.sessions import default_sessions_dir
from office.usage import refresh_usage


def open_window(url: str) -> None:
    """Open the office in a new window of the default browser."""
    try:
        desktop = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        desktop = ""
    name = desktop.removesuffix(".desktop")
    for candidate in (name, name.split(".")[-1], "brave-browser", "google-chrome", "chromium", "firefox"):
        executable = candidate and shutil.which(candidate)
        if executable:
            subprocess.Popen(
                [executable, "--new-window", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
    webbrowser.open_new(url)


def parse_tokens(values: list[str] | None) -> dict[str, int]:
    tokens = {}
    for value in values or []:
        key, _, number = value.partition("=")
        try:
            tokens[key.strip().lower()] = int(number.replace(".", "").replace("_", ""))
        except ValueError:
            sys.exit(f"--tokens: invalid value «{value}» (use name=number)")
    return tokens


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agents office (local HTML page).")
    parser.add_argument("--module", default=CONFIG["module"], help="module whose log is shown")
    parser.add_argument("--board", default=str(LOG_DIR / "tablero.md"), help="path to tablero.md")
    parser.add_argument("--plans", default=str(LOG_DIR / "planes"), help="folder with the plan files")
    parser.add_argument(
        "--sessions",
        default=str(default_sessions_dir()),
        help='Claude Code sessions folder of this project; "" to skip sessions',
    )
    parser.add_argument(
        "--avatars", default=str(ROOT / "avatars"), help='folder with <agent>.png; "" to use only the drawn characters'
    )
    parser.add_argument("--output", default=str(ROOT / "office.html"), help="page to write")
    parser.add_argument("--tokens", action="append", metavar="NAME=N", help="tokens of an agent, written by hand")
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        metavar="SECONDS",
        help="serve on http://localhost and update every SECONDS seconds",
    )
    parser.add_argument("--port", type=int, default=8765, help="local port for --watch (default 8765)")
    parser.add_argument("--no-usage", action="store_true", help="do not query the usage limits")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser window")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    if not Path(args.board).exists() and (EXAMPLE / "tablero.md").exists():
        args.board, args.plans = str(EXAMPLE / "tablero.md"), str(EXAMPLE / "planes")
        print("No log found next to the office: using the example in example/.")
    options = Options(
        args.module,
        args.board,
        args.plans,
        args.sessions or None,
        args.avatars or None,
        args.output,
        args.watch,
        parse_tokens(args.tokens),
    )

    def ready(url: str) -> None:
        print(f"Office live at {url} — checks every {args.watch} s (Ctrl+C to stop)")
        if not args.no_open:
            open_window(url)

    if args.watch:
        serve(options, args.port, ready)
        return
    if not args.no_usage:
        refresh_usage()
    result = build(options)
    working = sum(1 for agent in result.state.agents if agent.status != "idle")
    waiting = sum(1 for session in result.sessions if session.waiting)
    print(
        f"Office written: {args.output} ({working} agents with a task, {waiting} waiting for you, "
        f"{len(result.sessions)} active sessions)"
    )
    if not args.no_open:
        open_window(output_files(args.output)[CONFIG["language"]].resolve().as_uri())


if __name__ == "__main__":
    main()
