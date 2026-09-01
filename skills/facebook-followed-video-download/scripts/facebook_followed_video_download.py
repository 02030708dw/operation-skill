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
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


SKILL_NAME = "facebook-followed-video-download"
SKILL_VERSION = "1.6.2"
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
DEFAULT_DAILY_COUNT = 10
DEFAULT_BROWSER_PROFILE = STATE_DIR / "chrome-profile"
LOGIN_MARKER = ".hermes-login-enabled"
RUN_LOCK = STATE_DIR / ".capture-run.lock"


class ConcurrentRunError(RuntimeError):
    """Raised when a second downloader run cannot acquire the shared lock."""


@contextmanager
def single_run_lock(path: Path, *, wait: bool = True):
    """Serialize runs that share the isolated Chrome profile.

    The operating system releases this advisory lock automatically if a worker
    exits or crashes, so a stale PID written in the file never blocks a later
    run. Scheduled runs wait in order instead of starting a competing Chrome.
    """
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    acquired = False
    announced_wait = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("0")
                handle.flush()
            while True:
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as exc:
                    if not wait:
                        raise ConcurrentRunError(
                            "another Facebook capture is already active"
                        ) from exc
                    if not announced_wait:
                        print(
                            "Another Facebook capture is active; waiting for the shared Chrome session...",
                            flush=True,
                        )
                        announced_wait = True
                    time.sleep(1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError) as exc:
                if not wait:
                    raise ConcurrentRunError(
                        "another Facebook capture is already active"
                    ) from exc
                print(
                    "Another Facebook capture is active; waiting for the shared Chrome session...",
                    flush=True,
                )
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


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
        "--login",
        action="store_true",
        help="open an isolated Chrome profile for a user-authorized Facebook login",
    )
    actions.add_argument(
        "--add-source",
        nargs=2,
        metavar=("FOLDER", "FACEBOOK_URL"),
        help="add or replace one source in the accounts file",
    )
    parser.add_argument("--mode", choices=("daily", "full"), default="daily")
    parser.add_argument(
        "--initial-count",
        "--count",
        dest="count",
        type=non_negative_int,
        default=None,
        help="first daily execution limit per source; 0 means unlimited",
    )
    parser.add_argument("--scroll-rounds", type=non_negative_int, default=None)
    parser.add_argument("--wait-ms", type=positive_int, default=1400)
    parser.add_argument(
        "--max-duration-seconds",
        type=non_negative_int,
        default=0,
        help="skip videos longer than this before download; 0 disables the filter",
    )
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument(
        "--source",
        nargs=2,
        metavar=("NAME", "FACEBOOK_URL"),
        help="run one source without modifying the persistent accounts file",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument("--cookies", type=Path, default=None, help="optional authorized Netscape cookies file")
    parser.add_argument("--chrome", default=None, help="Chrome/Chromium executable path")
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=DEFAULT_BROWSER_PROFILE,
        help="isolated Chrome profile used only after --login completes",
    )
    parser.add_argument("--yt-dlp", dest="ytdlp", default=None, help="yt-dlp executable path")
    parser.add_argument("--execute", action="store_true", help="actually download files")
    parser.add_argument("--no-report", action="store_true", help="skip report generation after execution")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="print the final manifest as JSON")
    parser.add_argument("--result-json", type=Path, help="write the final manifest to this path")
    parser.add_argument("--execution-id", help="backend execution identifier copied into the manifest")
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
        if args.source:
            if not is_facebook_url(args.source[1]):
                raise ValueError("Source URL must use facebook.com or fb.watch")
            sources = [(args.source[0], args.source[1])]
        else:
            sources = configured_sources(args.accounts.expanduser())
        source_error = None
    except ValueError as exc:
        sources = []
        source_error = str(exc)
    return {
        "skill": SKILL_NAME,
        "skill_version": SKILL_VERSION,
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
        "browser_profile": str(args.browser_profile.expanduser()),
        "browser_login_ready": enabled_browser_profile(args) is not None,
        "ready_for_preview": bool(node and ws_ok and chrome and sources and not source_error),
        "ready_for_execute": bool(node and ws_ok and chrome and ytdlp and sources and not source_error),
    }


def enabled_browser_profile(args: argparse.Namespace) -> Path | None:
    profile = args.browser_profile.expanduser().resolve()
    return profile if (profile / LOGIN_MARKER).is_file() else None


def prepare_browser_login(args: argparse.Namespace) -> int:
    chrome = detect_chrome(args.chrome or os.environ.get("FACEBOOK_FOLLOWED_CHROME"))
    if not chrome:
        print("Chrome/Chromium is required for Facebook login.", file=sys.stderr)
        return 2
    profile = args.browser_profile.expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    try:
        profile.chmod(0o700)
    except OSError:
        pass
    print("Opening the isolated Hermes Facebook browser profile.")
    print("Log in only on facebook.com, verify the source page opens, then close that Chrome window.")
    completed = subprocess.run(
        [
            chrome,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            "https://www.facebook.com/login",
        ],
        check=False,
    )
    if completed.returncode != 0:
        return completed.returncode
    (profile / LOGIN_MARKER).write_text(
        "User authorized this isolated profile for Hermes Facebook discovery.\n",
        encoding="utf-8",
    )
    print(f"Facebook login profile enabled: {profile}")
    return 0


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


def _write_single_source(accounts: Path, source: list[str]) -> None:
    name, url = source
    if not is_facebook_url(url):
        raise ValueError("Source URL must use facebook.com or fb.watch")
    clean_name = re.sub(r'[/:\\?%*"<>|]', "_", name).strip()[:90]
    if not clean_name:
        raise ValueError("Source name cannot be empty")
    accounts.write_text(f"{clean_name}\t{url}\n", encoding="utf-8")


def _load_result(path: Path, exit_code: int, execution_id: str | None) -> dict[str, object]:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {
            "schemaVersion": "1.0",
            "skill": SKILL_NAME,
            "mode": "unknown",
            "status": "failed",
            "sources": [],
            "error": "download engine did not produce a result manifest",
        }
    payload["executionId"] = execution_id
    payload["exitCode"] = exit_code
    return payload


def _run_download_with_accounts(args: argparse.Namespace, accounts: Path) -> int:
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
    accounts = accounts.expanduser().resolve()
    output = args.output.expanduser().resolve()
    reports = args.reports.expanduser().resolve()
    count = (
        args.count
        if args.count is not None
        else (0 if args.mode == "full" else DEFAULT_DAILY_COUNT)
    )
    rounds = args.scroll_rounds if args.scroll_rounds is not None else 80
    temporary_result = tempfile.TemporaryDirectory(prefix="hermes-fb-result-")
    requested_result = args.result_json.expanduser().resolve() if args.result_json else None
    engine_result = Path(temporary_result.name) / "result.json"
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
        "--first-run-limit",
        str(count),
        "--max-duration-seconds",
        str(args.max_duration_seconds),
        "--result-json",
        str(engine_result),
    ]
    if not args.execute:
        command.append("--dry-run")
    if args.cookies:
        command.extend(["--cookies", str(args.cookies.expanduser().resolve())])
    if args.chrome:
        command.extend(["--chrome", args.chrome])
    browser_profile = enabled_browser_profile(args)
    if browser_profile:
        command.extend(["--browser-profile", str(browser_profile)])
    if args.execute:
        command.extend(["--yt-dlp", str(status["yt_dlp"])])

    run_dir = reports / "runs"
    if args.execute and not args.no_report:
        run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_log = run_dir / f"{args.mode}-{stamp}.log"
    else:
        run_log = None

    print("Skill:", f"{SKILL_NAME}@{SKILL_VERSION}")
    print("Mode:", "EXECUTE" if args.execute else "DRY RUN")
    print("Sources:", len(configured_sources(accounts)))
    if args.mode == "daily":
        print("First-execution per-source limit:", "unlimited" if count == 0 else count)
        print("Later-execution per-source limit: unlimited until archive boundary")
        print("Daily strategy: fetch every update; if there are none, fall back to the latest window")
    else:
        print("Full-import per-source limit:", "unlimited" if count == 0 else count)
    if args.verbose:
        print("Command:", subprocess.list2cmdline(command))

    try:
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
            # Preserve the engine's opt-in per-video JSONL events in real time
            # when this entry point is itself piped into an orchestrator.
            print(line, end="", flush=True)
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

        payload = _load_result(engine_result, exit_code, args.execution_id)
        payload_text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if requested_result:
            requested_result.parent.mkdir(parents=True, exist_ok=True)
            requested_result.write_text(payload_text, encoding="utf-8")
        if args.execute and not args.no_report:
            reports.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (reports / f"{stamp}-{args.mode}-manifest.json").write_text(
                payload_text, encoding="utf-8"
            )
            (reports / "latest-manifest.json").write_text(payload_text, encoding="utf-8")
        if args.json:
            print(payload_text, end="")
        return exit_code
    finally:
        temporary_result.cleanup()


def run_download(args: argparse.Namespace) -> int:
    if not args.source:
        return _run_download_with_accounts(args, args.accounts)
    with tempfile.TemporaryDirectory(prefix="hermes-fb-source-") as temporary:
        accounts = Path(temporary) / "accounts.txt"
        _write_single_source(accounts, args.source)
        original_accounts = args.accounts
        args.accounts = accounts
        try:
            return _run_download_with_accounts(args, accounts)
        finally:
            args.accounts = original_accounts


def main() -> int:
    args = build_parser().parse_args()
    args.accounts = args.accounts.expanduser()
    args.output = args.output.expanduser()
    args.reports = args.reports.expanduser()

    if args.login:
        return prepare_browser_login(args)
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
    with single_run_lock(RUN_LOCK):
        return run_download(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(2)
