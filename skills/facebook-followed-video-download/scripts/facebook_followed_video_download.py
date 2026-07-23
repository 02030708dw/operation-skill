#!/usr/bin/env python3
"""Hermes entry point for the Facebook followed-video downloader."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SKILL_NAME = "facebook-followed-video-download"
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent


def inferred_hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    # Installed layout: <hermes-home>/skills/<skill-name>/scripts/...
    if SKILL_DIR.parent.name.lower() == "skills":
        return SKILL_DIR.parent.parent
    return Path.home() / ".hermes"


HERMES_HOME = inferred_hermes_home()
STATE_DIR = Path(
    os.environ.get(
        "FACEBOOK_FOLLOWED_STATE_DIR",
        os.environ.get("FB_FOLLOWED_STATE_DIR", str(HERMES_HOME / SKILL_NAME)),
    )
).expanduser()
DEFAULT_ACCOUNTS = Path(
    os.environ.get(
        "FACEBOOK_FOLLOWED_ACCOUNTS",
        os.environ.get("FB_FOLLOWED_ACCOUNTS", str(STATE_DIR / "accounts.txt")),
    )
).expanduser()
DEFAULT_OUTPUT = Path(
    os.environ.get(
        "FACEBOOK_FOLLOWED_OUTPUT",
        os.environ.get("FB_FOLLOWED_DESKTOP", str(Path.home() / "Desktop" / "Facebook")),
    )
).expanduser()
DEFAULT_REPORTS = Path(
    os.environ.get(
        "FACEBOOK_FOLLOWED_REPORTS",
        os.environ.get("FB_FOLLOWED_REPORTS", str(STATE_DIR / "reports")),
    )
).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find new videos from configured Facebook sources. The default is a "
            "dry run; add --execute only after the user explicitly requests downloads."
        )
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="check configuration and dependencies")
    actions.add_argument("--list-sources", action="store_true", help="show configured sources")
    actions.add_argument("--init", action="store_true", help="create an empty example accounts file")
    actions.add_argument(
        "--add-source",
        nargs=2,
        metavar=("FOLDER", "FACEBOOK_URL"),
        help="add or replace one source in the accounts file",
    )
    parser.add_argument("--mode", choices=("daily", "full"), default="daily")
    parser.add_argument(
        "--count",
        type=non_negative_int,
        default=None,
        help="maximum downloads per source; 0 means unlimited",
    )
    parser.add_argument("--scroll-rounds", type=non_negative_int, default=None)
    parser.add_argument("--wait-ms", type=positive_int, default=1400)
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument("--cookies", type=Path, default=None, help="optional authorized Netscape cookies file")
    parser.add_argument("--chrome", default=None, help="Chrome/Chromium executable path")
    parser.add_argument("--yt-dlp", dest="ytdlp", default=None, help="yt-dlp executable path")
    parser.add_argument("--execute", action="store_true", help="actually download files")
    parser.add_argument("--no-report", action="store_true", help="skip report generation after execution")
    parser.add_argument("--verbose", action="store_true")
    return parser


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def command_path(command: str | None) -> str | None:
    if not command:
        return None
    expanded = str(Path(command).expanduser())
    if Path(expanded).is_file():
        return expanded
    found = shutil.which(command)
    if found:
        return found
    if command in {"yt-dlp", "yt-dlp.exe"}:
        local_tool = Path.home() / ".local" / "bin" / (
            "yt-dlp.exe" if sys.platform == "win32" else "yt-dlp"
        )
        if local_tool.is_file():
            return str(local_tool)
    return None


def detect_chrome(configured: str | None) -> str | None:
    if configured:
        return command_path(configured)
    candidates: list[Path] = []
    if sys.platform == "win32":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.extend(
                    [
                        Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                        Path(base) / "Chromium" / "Application" / "chrome.exe",
                    ]
                )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        )
    else:
        candidates.extend(
            Path(value)
            for value in (
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
            )
        )
    found = next((str(candidate) for candidate in candidates if candidate.is_file()), None)
    return found or shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")


def configured_sources(accounts: Path) -> list[tuple[str, str]]:
    if not accounts.is_file():
        return []
    sources: list[tuple[str, str]] = []
    for raw in accounts.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\t+", line, maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Invalid source line (TAB required): {raw}")
        sources.append((parts[0].strip(), parts[1].strip()))
    return sources


def is_facebook_url(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").lower()
    except ValueError:
        return False
    return host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch"


def dependency_status(args: argparse.Namespace) -> dict[str, object]:
    node = command_path("node")
    ytdlp = command_path(args.ytdlp or os.environ.get("FACEBOOK_FOLLOWED_YTDLP") or "yt-dlp")
    chrome = detect_chrome(args.chrome or os.environ.get("FACEBOOK_FOLLOWED_CHROME"))
    ws_ok = False
    if node:
        probe = subprocess.run(
            [node, "-e", "require.resolve('ws');"],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        ws_ok = probe.returncode == 0
    try:
        sources = configured_sources(args.accounts.expanduser())
        source_error = None
    except ValueError as exc:
        sources = []
        source_error = str(exc)
    return {
        "skill": SKILL_NAME,
        "hermes_home": str(HERMES_HOME),
        "accounts": str(args.accounts.expanduser()),
        "accounts_exists": args.accounts.expanduser().is_file(),
        "sources": len(sources),
        "source_error": source_error,
        "output": str(args.output.expanduser()),
        "node": node,
        "ws_module": ws_ok,
        "yt_dlp": ytdlp,
        "chrome": chrome,
        "ready_for_preview": bool(node and ws_ok and chrome and sources and not source_error),
        "ready_for_execute": bool(node and ws_ok and chrome and ytdlp and sources and not source_error),
    }


def initialize_accounts(accounts: Path) -> None:
    accounts = accounts.expanduser()
    if accounts.exists():
        print(f"Accounts file already exists: {accounts}")
        return
    accounts.parent.mkdir(parents=True, exist_ok=True)
    example = SKILL_DIR / "examples" / "accounts.example.txt"
    accounts.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Created accounts file: {accounts}")


def add_source(accounts: Path, folder: str, url: str) -> None:
    if not is_facebook_url(url):
        raise ValueError("Source URL must use facebook.com or fb.watch")
    clean_folder = re.sub(r'[/:\\?%*"<>|]', "_", folder).strip()[:90]
    if not clean_folder:
        raise ValueError("Folder name cannot be empty")
    accounts = accounts.expanduser()
    existing = configured_sources(accounts)
    kept = [(name, source_url) for name, source_url in existing if name.casefold() != clean_folder.casefold()]
    kept.append((clean_folder, url))
    accounts.parent.mkdir(parents=True, exist_ok=True)
    header = "# folder-name<TAB>facebook-url\n"
    body = "".join(f"{name}\t{source_url}\n" for name, source_url in kept)
    accounts.write_text(header + body, encoding="utf-8")
    print(f"Saved source: {clean_folder} -> {url}")
    print(f"Accounts file: {accounts}")


def run_download(args: argparse.Namespace) -> int:
    status = dependency_status(args)
    if not status["ready_for_preview"]:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        print("Not ready. Run --check and fix the reported configuration.", file=sys.stderr)
        return 2
    if args.execute and not status["ready_for_execute"]:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        print("yt-dlp is required for --execute.", file=sys.stderr)
        return 2

    node = str(status["node"])
    engine = SCRIPT_DIR / "facebook_followed_video_engine.js"
    accounts = args.accounts.expanduser().resolve()
    output = args.output.expanduser().resolve()
    reports = args.reports.expanduser().resolve()
    count = args.count if args.count is not None else (0 if args.mode == "full" else 30)
    rounds = args.scroll_rounds if args.scroll_rounds is not None else (80 if args.mode == "full" else 8)
    command = [
        node,
        str(engine),
        "--mode",
        args.mode,
        "--accounts",
        str(accounts),
        "--desktop",
        str(output),
        "--scroll-rounds",
        str(rounds),
        "--wait-ms",
        str(args.wait_ms),
        "--max-downloads",
        str(count),
    ]
    if not args.execute:
        command.append("--dry-run")
    if args.cookies:
        command.extend(["--cookies", str(args.cookies.expanduser().resolve())])
    if args.chrome:
        command.extend(["--chrome", args.chrome])
    if args.execute:
        command.extend(["--yt-dlp", str(status["yt_dlp"])])

    run_dir = reports / "runs"
    if args.execute and not args.no_report:
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_log = run_dir / f"{args.mode}-{stamp}.log"
    else:
        run_log = None

    print("Mode:", "EXECUTE" if args.execute else "DRY RUN")
    print("Sources:", len(configured_sources(accounts)))
    print("Per-source limit:", "unlimited" if count == 0 else count)
    if args.verbose:
        print("Command:", subprocess.list2cmdline(command))

    process = subprocess.Popen(
        command,
        cwd=SCRIPT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        captured.append(line)
    exit_code = process.wait()

    if run_log:
        run_log.write_text("".join(captured), encoding="utf-8")
        report_command = [
            node,
            str(SCRIPT_DIR / "facebook_followed_video_report.js"),
            "--mode",
            args.mode,
            "--accounts",
            str(accounts),
            "--desktop",
            str(output),
            "--reports-dir",
            str(reports),
            "--run-log",
            str(run_log),
            "--status",
            str(exit_code),
            "--print",
        ]
        report = subprocess.run(report_command, cwd=SCRIPT_DIR, check=False)
        if report.returncode != 0 and exit_code == 0:
            exit_code = report.returncode
    return exit_code


def main() -> int:
    args = build_parser().parse_args()
    args.accounts = args.accounts.expanduser()
    args.output = args.output.expanduser()
    args.reports = args.reports.expanduser()

    if args.check:
        status = dependency_status(args)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status["ready_for_preview"] else 1
    if args.list_sources:
        sources = configured_sources(args.accounts)
        if not sources:
            print(f"No sources configured: {args.accounts}")
            return 1
        for folder, url in sources:
            print(f"{folder} -> {url}")
        return 0
    if args.init:
        initialize_accounts(args.accounts)
        return 0
    if args.add_source:
        add_source(args.accounts, args.add_source[0], args.add_source[1])
        return 0
    return run_download(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
