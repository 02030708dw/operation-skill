"""CI-only real installer/scheduler check, run as a disposable standard user."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from test_operation_skill_updater import UPDATER, make_release, seed_managed_state, write_skill


def main() -> None:
    if os.name != "nt" or os.getenv("GITHUB_ACTIONS") != "true":
        raise RuntimeError("This smoke test is restricted to ephemeral Windows CI runners")
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("The installer smoke test must run WITHOUT administrator privileges")
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="hm-updater-smoke-") as temporary:
        root = Path(temporary)
        home = root / "\u8fd0\u8425 smoke O'Neil"
        installed = home / "skills" / "demo-skill"
        write_skill(installed, "old")
        seed_managed_state(home, installed)
        manifest = make_release(root, "new", "a" * 40)
        shutil.copyfile(manifest, root / "manifest.json")
        UPDATER.atomic_json_write(UPDATER.config_path(home), {"managedSkills": ["demo-skill"]})
        environment = dict(os.environ)
        environment.update({
            "HERMES_HOME": str(home),
            "OPERATION_SKILL_UPDATER_ALLOW_FILE_URL": "1",
            "PYTHONUTF8": "1",
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        })
        installer = repo / "scripts" / "install-operation-skill-updater.ps1"
        task_name = UPDATER.windows_task_name(home)
        try:
            for attempt in range(2):
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(installer),
                     "-BaseUrl", root.as_uri()],
                    env=environment, capture_output=True, timeout=120,
                )
                print(result.stdout.decode("utf-8", errors="replace"))
                print(result.stderr.decode("utf-8", errors="replace"))
                assert result.returncode == 0, f"installer attempt {attempt + 1} failed"
                expected = json.loads(manifest.read_text(encoding="utf-8"))["skills"][0]["sha256"]
                assert UPDATER.directory_hash(installed) == expected
            check = f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{task_name}' -TaskPath '\\'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($task.Principal.UserId -ne $sid) {{ throw 'Wrong principal' }}
if ($task.Principal.RunLevel -ne 'Limited') {{ throw 'Task must not be elevated' }}
$logon = @($task.Triggers | Where-Object {{ $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' }})
if ($logon.Count -ne 1 -or $logon[0].UserId -ne $sid) {{ throw 'Wrong logon trigger' }}
if (@($task.Triggers).Count -ne 2) {{ throw 'Missing daily trigger' }}
"""
            UPDATER.run_windows_schedule_script(check)
            print("PASS: PowerShell 5.1 install, update, repeat install and per-user task registration (non-admin)")
        finally:
            UPDATER.uninstall_schedule(home, False)
        UPDATER.run_windows_schedule_script(
            f"if (Get-ScheduledTask -TaskName '{task_name}' -TaskPath '\\' -ErrorAction SilentlyContinue) "
            "{ throw 'Task was not removed' }"
        )
        print("PASS: schedule uninstall")


if __name__ == "__main__":
    main()
