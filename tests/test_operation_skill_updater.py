from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


UPDATER = load_module("operation_skill_updater", ROOT / "scripts" / "operation_skill_updater.py")


def write_skill(directory: Path, marker: str, name: str = "demo-skill") -> None:
    (directory / "scripts").mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: demo\nmetadata:\n  version: "1.0.0"\n---\n',
        encoding="utf-8",
    )
    (directory / "scripts" / "run.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")


def make_release(
    root: Path,
    marker: str,
    commit: str,
    release_sequence: int = 1,
    *,
    skill_name: str = "demo-skill",
    node_package: bool = False,
    bridge_installer: bool = False,
) -> Path:
    source = root / f"source-{commit[0]}" / "skills" / skill_name
    write_skill(source, marker, skill_name)
    if node_package:
        (source / "scripts" / "package.json").write_text(
            '{"name":"test-package","private":true,"dependencies":{"ws":"1.0.0"}}\n',
            encoding="utf-8",
        )
        (source / "scripts" / "package-lock.json").write_text(
            '{"name":"test-package","lockfileVersion":3,"packages":{}}\n',
            encoding="utf-8",
        )
    if bridge_installer:
        (source / "scripts" / "install_hermes_worker.py").write_text(
            "# test bridge installer\n", encoding="utf-8"
        )
    archive = root / f"release-{commit[0]}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.write(path, f"skills/{skill_name}/{path.relative_to(source).as_posix()}")
    updater_path = ROOT / "scripts" / "operation_skill_updater.py"
    manifest = {
        "schemaVersion": 1,
        "channel": "main",
        "repository": "02030708dw/operation-skill",
        "commit": commit,
        "releaseSequence": release_sequence,
        "publishedAt": "2026-09-02T00:00:00+00:00",
        "archive": {
            "url": archive.as_uri(),
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "size": archive.stat().st_size,
        },
        "updater": {
            "url": updater_path.as_uri(),
            "sha256": hashlib.sha256(updater_path.read_bytes()).hexdigest(),
            "size": updater_path.stat().st_size,
        },
        "skills": [
            {
                "name": skill_name,
                "version": "1.0.0",
                "path": f"skills/{skill_name}",
                "sha256": UPDATER.directory_hash(source),
                "fileCount": sum(1 for path in source.rglob("*") if path.is_file()),
            }
        ],
    }
    manifest_path = root / f"manifest-{commit[0]}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def seed_managed_state(home: Path, installed: Path) -> None:
    UPDATER.atomic_json_write(
        UPDATER.state_path(home),
        {
            "schemaVersion": 1,
            "skills": {
                "demo-skill": {
                    "sha256": UPDATER.directory_hash(installed),
                }
            },
        },
    )


class OperationSkillUpdaterTest(unittest.TestCase):
    def setUp(self):
        self.file_url = mock.patch.dict(os.environ, {"OPERATION_SKILL_UPDATER_ALLOW_FILE_URL": "1"})
        self.file_url.start()

    def tearDown(self):
        self.file_url.stop()

    def test_updates_existing_skill_then_preserves_local_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "old")
            seed_managed_state(home, installed)
            first_manifest = make_release(root, "new", "a" * 40)

            result = UPDATER.run_update(home, first_manifest.as_uri(), 0, True)

            self.assertEqual("updated", result["status"])
            self.assertEqual(["demo-skill"], result["changedSkills"])
            self.assertIn("new", (installed / "scripts" / "run.py").read_text(encoding="utf-8"))
            backups = list((home / "operation-skill-updater" / "backups").glob("*/skills/demo-skill"))
            self.assertEqual(1, len(backups))
            self.assertIn("old", (backups[0] / "scripts" / "run.py").read_text(encoding="utf-8"))
            self.assertFalse((home / "ESTOP").exists())

            (installed / "scripts" / "run.py").write_text("MARKER = 'operator-edit'\n", encoding="utf-8")
            second_manifest = make_release(root, "newer", "b" * 40, 2)
            second = UPDATER.run_update(home, second_manifest.as_uri(), 0, True)
            self.assertEqual("local_conflict", second["status"])
            self.assertEqual(["demo-skill"], second["localConflicts"])
            self.assertIn("operator-edit", (installed / "scripts" / "run.py").read_text(encoding="utf-8"))

    def test_first_takeover_preserves_unrecorded_existing_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "operator-edit-before-updater")
            manifest = make_release(root, "published", "f" * 40)

            result = UPDATER.run_update(home, manifest.as_uri(), 0, True)

            self.assertEqual("adoption_required", result["status"])
            self.assertEqual([], result["localConflicts"])
            self.assertEqual(["demo-skill"], result["unmanagedExisting"])
            self.assertEqual([], result["changedSkills"])
            self.assertIn(
                "operator-edit-before-updater",
                (installed / "scripts" / "run.py").read_text(encoding="utf-8"),
            )

    def test_explicit_adoption_replaces_unmanaged_skill_with_permanent_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "old-official-copy")
            manifest = make_release(root, "published", "a" * 40, 7)

            result = UPDATER.run_update(
                home,
                manifest.as_uri(),
                0,
                True,
                adoption_names={"demo-skill"},
                adoption_release_sequence=7,
            )

            self.assertEqual("updated", result["status"])
            self.assertEqual(["demo-skill"], result["adoptedSkills"])
            self.assertIn(
                "published",
                (installed / "scripts" / "run.py").read_text(encoding="utf-8"),
            )
            adoption_backup = Path(result["adoptionBackup"])
            self.assertIn(
                "old-official-copy",
                (
                    adoption_backup
                    / "skills"
                    / "demo-skill"
                    / "scripts"
                    / "run.py"
                ).read_text(encoding="utf-8"),
            )
            self.assertIn("demo-skill", UPDATER.load_managed_skills(home))
            state = UPDATER.load_state(home)
            self.assertEqual(["demo-skill"], state["adoptions"][0]["skills"])

            (installed / "scripts" / "run.py").write_text(
                "MARKER = 'managed-local-edit'\n", encoding="utf-8"
            )
            blocked = UPDATER.run_update(
                home,
                manifest.as_uri(),
                0,
                True,
                adoption_names={"demo-skill"},
                adoption_release_sequence=7,
            )
            self.assertEqual(["demo-skill"], blocked["adoptionBlocked"])
            self.assertIn(
                "managed-local-edit",
                (installed / "scripts" / "run.py").read_text(encoding="utf-8"),
            )

    def test_adoption_requires_exact_current_release_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "old")
            manifest = make_release(root, "published", "a" * 40, 9)

            with self.assertRaisesRegex(UPDATER.UpdaterError, "必须指定"):
                UPDATER.run_update(
                    home, manifest.as_uri(), 0, True, adoption_names={"demo-skill"}
                )
            with self.assertRaisesRegex(UPDATER.UpdaterError, "发布序列与当前发布不一致"):
                UPDATER.run_update(
                    home,
                    manifest.as_uri(),
                    0,
                    True,
                    adoption_names={"demo-skill"},
                    adoption_release_sequence=8,
                )
            self.assertIn(
                "old", (installed / "scripts" / "run.py").read_text(encoding="utf-8")
            )

    def test_managed_install_intent_survives_deferred_first_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            manifest = make_release(root, "published", "a" * 40)

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=["busy worker"]
            ):
                first = UPDATER.run_update(
                    home, manifest.as_uri(), 0, True, {"demo-skill"}
                )

            self.assertEqual("deferred", first["status"])
            self.assertEqual({"demo-skill"}, UPDATER.load_managed_skills(home))
            self.assertFalse((home / "skills" / "demo-skill").exists())

            second = UPDATER.run_update(home, manifest.as_uri(), 0, True)
            self.assertEqual(["demo-skill"], second["changedSkills"])
            self.assertTrue((home / "skills" / "demo-skill" / "SKILL.md").is_file())

    def test_accepts_release_durably_before_self_update_and_deferred_return(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            stale = make_release(root, "stale", "a" * 40, 1)
            latest = make_release(root, "latest", "b" * 40, 2)

            with mock.patch.object(
                UPDATER, "update_self", return_value=True
            ) as self_update, mock.patch.object(
                UPDATER, "active_skill_processes", return_value=["busy worker"]
            ):
                result = UPDATER.run_update(
                    home, latest.as_uri(), 0, True, {"demo-skill"}
                )

            self.assertEqual("deferred", result["status"])
            self_update.assert_called_once()
            accepted = UPDATER.load_state(home)["acceptedRelease"]
            self.assertEqual(2, accepted["releaseSequence"])
            self.assertEqual("b" * 40, accepted["commit"])

            with mock.patch.object(UPDATER, "update_self") as stale_self_update:
                with self.assertRaisesRegex(UPDATER.UpdaterError, "旧发布序列"):
                    UPDATER.run_update(home, stale.as_uri(), 0, True)
            stale_self_update.assert_not_called()

    def test_bootstrap_crash_after_state_write_keeps_new_anti_rollback_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            stale = make_release(root, "stale", "a" * 40, 1)
            latest = make_release(root, "latest", "b" * 40, 2)

            with mock.patch.object(
                UPDATER,
                "durable_file_write",
                side_effect=OSError("simulated power loss before executable replace"),
            ):
                with self.assertRaisesRegex(OSError, "simulated power loss"):
                    UPDATER.bootstrap_install(
                        home, latest, latest.as_uri(), manage_core=False
                    )

            accepted = UPDATER.load_state(home)["acceptedRelease"]
            self.assertEqual(2, accepted["releaseSequence"])
            self.assertEqual("b" * 40, accepted["commit"])
            self.assertFalse(
                (home / "operation-skill-updater" / "operation_skill_updater.py").exists()
            )

            with mock.patch.object(UPDATER, "durable_file_write") as replace:
                with self.assertRaisesRegex(UPDATER.UpdaterError, "旧发布序列"):
                    UPDATER.bootstrap_install(
                        home, stale, stale.as_uri(), manage_core=False
                    )
            replace.assert_not_called()

    def test_bootstrap_installs_under_lock_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            manifest = make_release(root, "latest", "c" * 40, 7)
            UPDATER.durable_json_write(
                UPDATER.config_path(home),
                {"managedSkills": ["custom-skill"], "customSetting": {"keep": True}},
            )

            result = UPDATER.bootstrap_install(
                home, manifest, manifest.as_uri(), manage_core=True
            )

            self.assertEqual("installed", result["status"])
            installed_updater = (
                home / "operation-skill-updater" / "operation_skill_updater.py"
            )
            self.assertEqual(
                (ROOT / "scripts" / "operation_skill_updater.py").read_bytes(),
                installed_updater.read_bytes(),
            )
            state = UPDATER.load_state(home)
            self.assertEqual(7, state["acceptedRelease"]["releaseSequence"])
            config = UPDATER.load_json(UPDATER.config_path(home), {})
            self.assertTrue(config["customSetting"]["keep"])
            self.assertEqual(
                {"custom-skill", *UPDATER.CORE_SKILLS},
                set(config["managedSkills"]),
            )
            self.assertEqual(manifest.as_uri(), config["manifestUrl"])

    def test_durable_replace_uses_windows_write_through_rename(self):
        source = inspect.getsource(UPDATER.durable_replace)

        self.assertIn("MoveFileExW", source)
        self.assertIn("MOVEFILE_WRITE_THROUGH", source)
        self.assertIn("MOVEFILE_REPLACE_EXISTING", source)

    def test_managed_intent_merge_is_serialized_with_bootstrap_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            existing = {
                "manifestUrl": "https://skills.example.test/new/manifest.json",
                "managedSkills": ["custom-skill"],
                "customSetting": "preserve-me",
            }
            UPDATER.durable_json_write(UPDATER.config_path(home), existing)
            lock_context = mock.MagicMock()
            lock_context.__enter__.return_value = None
            lock_context.__exit__.return_value = False

            with mock.patch.object(
                UPDATER, "updater_lock", return_value=lock_context
            ) as intent_lock:
                managed = UPDATER.persist_managed_skills_locked(
                    home, {"facebook-daily-like"}
                )

            intent_lock.assert_called_once_with(home)
            self.assertEqual(
                {"custom-skill", "facebook-daily-like"}, managed
            )
            config = UPDATER.load_json(UPDATER.config_path(home), {})
            self.assertEqual(existing["manifestUrl"], config["manifestUrl"])
            self.assertEqual("preserve-me", config["customSetting"])

    def test_busy_main_never_writes_state_outside_updater_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            original = {
                "schemaVersion": 1,
                "skills": {},
                "acceptedRelease": {
                    "releaseSequence": 9,
                    "commit": "d" * 40,
                    "acceptedAt": "2026-09-02T00:00:00+00:00",
                },
            }
            UPDATER.durable_json_write(UPDATER.state_path(home), original)

            with mock.patch.object(
                UPDATER,
                "run_update",
                side_effect=UPDATER.UpdaterBusyError("another updater owns the lock"),
            ):
                exit_code = UPDATER.main(
                    [
                        "--hermes-home",
                        str(home),
                        "--manifest-url",
                        "https://skills.example.test/manifest.json",
                        "--no-notify",
                        "run",
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(original, UPDATER.load_state(home))

    def test_nonbusy_main_merges_failure_state_under_updater_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            UPDATER.durable_json_write(
                UPDATER.state_path(home),
                {
                    "schemaVersion": 1,
                    "skills": {},
                    "acceptedRelease": {
                        "releaseSequence": 12,
                        "commit": "e" * 40,
                        "acceptedAt": "2026-09-02T00:00:00+00:00",
                    },
                },
            )
            lock_context = mock.MagicMock()
            lock_context.__enter__.return_value = None
            lock_context.__exit__.return_value = False

            with mock.patch.object(
                UPDATER, "run_update", side_effect=UPDATER.UpdaterError("failed run")
            ), mock.patch.object(
                UPDATER, "updater_lock", return_value=lock_context
            ) as failure_lock:
                exit_code = UPDATER.main(
                    [
                        "--hermes-home",
                        str(home),
                        "--manifest-url",
                        "https://skills.example.test/manifest.json",
                        "--no-notify",
                        "run",
                    ]
                )

            self.assertEqual(1, exit_code)
            failure_lock.assert_called_once_with(home.resolve())
            state = UPDATER.load_state(home)
            self.assertEqual(12, state["acceptedRelease"]["releaseSequence"])
            self.assertEqual("e" * 40, state["acceptedRelease"]["commit"])
            self.assertEqual("failed", state["lastResult"]["status"])

    def test_platform_installers_delegate_atomic_bootstrap_to_verified_updater(self):
        shell = (ROOT / "scripts" / "install-operation-skill-updater.sh").read_text(
            encoding="utf-8"
        )
        powershell = (
            ROOT / "scripts" / "install-operation-skill-updater.ps1"
        ).read_text(encoding="utf-8")

        for installer in (shell, powershell):
            self.assertIn("bootstrap-install", installer)
            self.assertIn("--manifest-file", installer)
            self.assertIn("--manifest-url", installer)
        self.assertNotIn('"${updater_home}/state.json"', shell)
        self.assertNotIn('$StatePath = Join-Path $UpdaterHome "state.json"', powershell)

    def test_preexisting_estop_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "old")
            seed_managed_state(home, installed)
            sentinel = home / "ESTOP"
            sentinel.write_text('{"reason":"administrator"}\n', encoding="utf-8")
            manifest = make_release(root, "new", "c" * 40)

            UPDATER.run_update(home, manifest.as_uri(), 0, True)

            self.assertEqual('{"reason":"administrator"}\n', sentinel.read_text(encoding="utf-8"))

    def test_explicit_install_adds_missing_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            manifest = make_release(root, "new", "e" * 40)

            result = UPDATER.run_update(
                home, manifest.as_uri(), 0, True, {"demo-skill"}
            )

            self.assertEqual(["demo-skill"], result["changedSkills"])
            installed = home / "skills" / "demo-skill"
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertIn("new", (installed / "scripts" / "run.py").read_text(encoding="utf-8"))

    def test_busy_machine_defers_without_downloading_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "old")
            seed_managed_state(home, installed)
            manifest = make_release(root, "new", "d" * 40)
            with mock.patch.object(UPDATER, "active_skill_processes", return_value=["python demo"]):
                result = UPDATER.run_update(home, manifest.as_uri(), 0, True)
            self.assertEqual("deferred", result["status"])
            self.assertIn("old", (installed / "scripts" / "run.py").read_text(encoding="utf-8"))
            self.assertFalse((home / "ESTOP").exists())

    def test_rejects_zip_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("skills/../outside.txt", "bad")
            target = root / "target"
            target.mkdir()
            with self.assertRaises(UPDATER.UpdaterError):
                UPDATER.safe_extract(archive, target)
            self.assertFalse((root / "outside.txt").exists())

    def test_rejects_oversized_extracted_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "oversized.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr("skills/demo-skill/payload.txt", "too large")
            target = root / "target"
            target.mkdir()
            with mock.patch.object(UPDATER, "MAX_EXTRACTED_BYTES", 4):
                with self.assertRaises(UPDATER.UpdaterError):
                    UPDATER.safe_extract(archive, target)

    def test_rejects_too_many_or_overlong_zip_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "too-many.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("skills/demo-skill/one.txt", "1")
                output.writestr("skills/demo-skill/two.txt", "2")
            target = root / "target"
            target.mkdir()
            with mock.patch.object(UPDATER, "MAX_ARCHIVE_ENTRIES", 1):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "条目数量"):
                    UPDATER.safe_extract(archive, target)

            overlong = root / "overlong.zip"
            with zipfile.ZipFile(overlong, "w") as output:
                output.writestr("skills/demo-skill/" + "x" * 20, "bad")
            with mock.patch.object(UPDATER, "MAX_ARCHIVE_PATH_CHARS", 10):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "路径过长"):
                    UPDATER.safe_extract(overlong, target)

    def test_process_inspection_failure_defers_update(self):
        failed = subprocess.CompletedProcess(args=["process-list"], returncode=1, stdout="", stderr="error")
        with mock.patch.object(UPDATER.subprocess, "run", return_value=failed):
            self.assertEqual(
                ["process-inspection-unavailable"],
                UPDATER.active_skill_processes([Path.cwd()]),
            )

    def test_rejects_release_sequence_rollback_for_check_and_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            accepted = make_release(root, "accepted", "a" * 40, 2)
            stale = make_release(root, "stale", "b" * 40, 1)
            UPDATER.run_update(home, accepted.as_uri(), 0, True, {"demo-skill"})

            with self.assertRaisesRegex(UPDATER.UpdaterError, "旧发布序列"):
                UPDATER.run_check(home, stale.as_uri())
            with self.assertRaisesRegex(UPDATER.UpdaterError, "旧发布序列"):
                UPDATER.run_update(home, stale.as_uri(), 0, True)

            state = UPDATER.load_state(home)
            self.assertEqual(
                {"releaseSequence": 2, "commit": "a" * 40},
                {
                    "releaseSequence": state["acceptedRelease"]["releaseSequence"],
                    "commit": state["acceptedRelease"]["commit"],
                },
            )
            self.assertIn(
                "accepted",
                (home / "skills" / "demo-skill" / "scripts" / "run.py").read_text(
                    encoding="utf-8"
                ),
            )

    def test_rejects_different_commit_at_accepted_release_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            accepted = make_release(root, "accepted", "a" * 40, 3)
            conflicting = make_release(root, "conflicting", "b" * 40, 3)
            UPDATER.run_update(home, accepted.as_uri(), 0, True, {"demo-skill"})

            with self.assertRaisesRegex(UPDATER.UpdaterError, "同一发布序列"):
                UPDATER.run_check(home, conflicting.as_uri())
            with self.assertRaisesRegex(UPDATER.UpdaterError, "同一发布序列"):
                UPDATER.run_update(home, conflicting.as_uri(), 0, True)

    def test_manifest_requires_positive_integer_release_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = make_release(root, "published", "a" * 40)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for invalid in (None, 0, -1, True, "1"):
                candidate = dict(manifest)
                candidate["releaseSequence"] = invalid
                with self.subTest(invalid=invalid):
                    with self.assertRaises(UPDATER.UpdaterError):
                        UPDATER.validate_manifest(candidate)

    def test_rejects_https_redirect_to_http_before_reading_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "http://downloads.example.test/release.zip"
        response.headers = {}

        with mock.patch.object(UPDATER.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(UPDATER.UpdaterError, "只允许 HTTPS"):
                UPDATER.request_bytes("https://downloads.example.test/release.zip", limit=10)

        response.read.assert_not_called()

    def test_update_holds_downloader_lock_even_before_first_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            lock_path = home / "facebook-followed-video-download" / ".capture-run.lock"

            handles = UPDATER.acquire_worker_locks(home)
            self.assertIsNotNone(handles)
            try:
                self.assertTrue(lock_path.is_file())
                self.assertIsNone(UPDATER.try_lock(lock_path))
            finally:
                for handle in handles or []:
                    handle.close()

    def test_pause_rechecks_processes_after_acquiring_worker_locks(self):
        with tempfile.TemporaryDirectory() as temporary:
            handles = [mock.Mock()]
            with mock.patch.object(
                UPDATER, "active_skill_processes", side_effect=[[], ["late process"]]
            ) as inspect, mock.patch.object(
                UPDATER, "acquire_worker_locks", return_value=handles
            ):
                with UPDATER.paused_for_update(
                    Path(temporary) / "home", [Path("/skill")], 0
                ) as idle:
                    self.assertFalse(idle)

        self.assertEqual(2, inspect.call_count)
        handles[0].close.assert_called_once_with()

    def test_fresh_downloader_install_prepares_locked_node_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-followed-video-download"
            manifest = make_release(
                root,
                "published",
                "a" * 40,
                skill_name=skill_name,
                node_package=True,
            )

            def fake_npm(command, **kwargs):
                dependency = Path(kwargs["cwd"]) / "node_modules" / "ws" / "index.js"
                dependency.parent.mkdir(parents=True)
                dependency.write_text("module.exports = {}\n", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "find_npm", return_value="/fake/npm"
            ), mock.patch.object(UPDATER.subprocess, "run", side_effect=fake_npm) as npm_run:
                result = UPDATER.run_update(home, manifest.as_uri(), 0, True, {skill_name})

            self.assertEqual([skill_name], result["changedSkills"])
            self.assertEqual(
                ["/fake/npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                npm_run.call_args.args[0],
            )
            runtime_path = npm_run.call_args.kwargs["env"]["PATH"].split(os.pathsep)
            self.assertEqual(str(Path("/fake/npm").parent), runtime_path[0])
            self.assertIn(str(home / "node" / "bin"), runtime_path)
            self.assertTrue(
                (
                    home
                    / "skills"
                    / skill_name
                    / "scripts"
                    / "node_modules"
                    / "ws"
                    / "index.js"
                ).is_file()
            )

    def test_npm_failure_does_not_replace_fresh_downloader_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-followed-video-download"
            manifest = make_release(
                root,
                "published",
                "a" * 40,
                skill_name=skill_name,
                node_package=True,
            )
            failed = subprocess.CompletedProcess(
                ["/fake/npm", "ci"], 1, stdout="", stderr="registry unavailable"
            )

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "find_npm", return_value="/fake/npm"
            ), mock.patch.object(UPDATER.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "依赖安装失败"):
                    UPDATER.run_update(home, manifest.as_uri(), 0, True, {skill_name})

            self.assertFalse((home / "skills" / skill_name).exists())

    def test_find_npm_uses_hermes_node_when_path_is_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "custom-hermes"
            npm_name = "npm.cmd" if os.name == "nt" else "npm"
            npm = home / "node" / "bin" / npm_name
            npm.parent.mkdir(parents=True)
            npm.write_text("placeholder\n", encoding="utf-8")
            npm.chmod(0o700)

            with mock.patch.dict(os.environ, {"PATH": ""}), mock.patch.object(
                UPDATER.shutil, "which", return_value=None
            ):
                self.assertEqual(str(npm), UPDATER.find_npm(home))

    def test_staging_directory_is_created_below_updater_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            manifest = make_release(root, "published", "a" * 40)
            real_temporary_directory = tempfile.TemporaryDirectory
            observed_parents = []

            def tracked_temporary_directory(*args, **kwargs):
                observed_parents.append(Path(kwargs["dir"]))
                return real_temporary_directory(*args, **kwargs)

            with mock.patch.object(
                UPDATER.tempfile,
                "TemporaryDirectory",
                side_effect=tracked_temporary_directory,
            ):
                UPDATER.run_update(home, manifest.as_uri(), 0, True, {"demo-skill"})

            self.assertEqual([UPDATER.updater_home(home) / "tmp"], observed_parents)

    def test_refresh_bridge_passes_custom_hermes_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "custom-hermes"
            ingest = root / "facebook-video-ingest"
            installer = ingest / "scripts" / "install_hermes_worker.py"
            installer.parent.mkdir(parents=True)
            installer.write_text("# test installer\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with mock.patch.object(
                UPDATER.subprocess, "run", return_value=completed
            ) as run:
                UPDATER.refresh_bridge(
                    home,
                    [{"name": "facebook-video-ingest", "path": str(ingest)}],
                )

            self.assertEqual(str(home), run.call_args.kwargs["env"]["HERMES_HOME"])

    def test_bridge_failure_keeps_new_skill_and_persists_pending_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-video-ingest"
            manifest_path = make_release(
                root,
                "new-ingest",
                "a" * 40,
                4,
                skill_name=skill_name,
                bridge_installer=True,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("bridge boom")
            ), mock.patch.object(
                UPDATER, "update_self", return_value=True
            ):
                result = UPDATER.run_update(
                    home, manifest_path.as_uri(), 0, True, {skill_name}
                )

            installed = home / "skills" / skill_name
            state = UPDATER.load_state(home)
            sentinel = json.loads((home / "ESTOP").read_text(encoding="utf-8"))
            self.assertEqual("bridge_repair_pending", result["status"])
            self.assertEqual([skill_name], result["changedSkills"])
            self.assertFalse(result["reloadRequired"])
            self.assertTrue(result["updaterUpdated"])
            self.assertIn(
                "new-ingest",
                (installed / "scripts" / "run.py").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                manifest["skills"][0]["sha256"], state["skills"][skill_name]["sha256"]
            )
            self.assertEqual(4, state["acceptedRelease"]["releaseSequence"])
            self.assertEqual("pending", state["bridgeRepair"]["status"])
            self.assertIn("bridge boom", state["bridgeRepair"]["error"])
            self.assertEqual("operation-skill-update", sentinel["reason"])
            self.assertEqual(state["bridgeRepair"]["sentinelOwner"], sentinel["owner"])

    def test_next_run_retries_pending_bridge_without_archive_and_clears_owned_estop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-video-ingest"
            manifest = make_release(
                root,
                "new-ingest",
                "a" * 40,
                skill_name=skill_name,
                bridge_installer=True,
            )
            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("bridge boom")
            ):
                first = UPDATER.run_update(home, manifest.as_uri(), 0, True, {skill_name})
            self.assertEqual("bridge_repair_pending", first["status"])
            self.assertTrue((home / "ESTOP").exists())

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(UPDATER, "refresh_bridge") as refresh, mock.patch.object(
                UPDATER, "request_bytes", wraps=UPDATER.request_bytes
            ) as download, mock.patch.object(UPDATER, "notify") as notify:
                second = UPDATER.run_update(home, manifest.as_uri(), 0, False)

            self.assertTrue(second["bridgeRepaired"])
            self.assertEqual("updated", second["status"])
            self.assertEqual([skill_name], second["changedSkills"])
            self.assertTrue(second["reloadRequired"])
            self.assertNotIn("bridgeRepair", UPDATER.load_state(home))
            self.assertFalse((home / "ESTOP").exists())
            refresh.assert_called_once()
            self.assertIn("bridge 已修复", notify.call_args.args[0])
            self.assertEqual([manifest.as_uri()], [call.args[0] for call in download.call_args_list])

    def test_failed_retry_recreates_missing_owned_bridge_repair_estop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-video-ingest"
            manifest = make_release(
                root,
                "new-ingest",
                "a" * 40,
                skill_name=skill_name,
                bridge_installer=True,
            )
            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("first failure")
            ):
                UPDATER.run_update(home, manifest.as_uri(), 0, True, {skill_name})
            first_owner = UPDATER.load_state(home)["bridgeRepair"]["sentinelOwner"]
            (home / "ESTOP").unlink()

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("retry failure")
            ):
                result = UPDATER.run_update(home, manifest.as_uri(), 0, True)

            state = UPDATER.load_state(home)
            sentinel = json.loads((home / "ESTOP").read_text(encoding="utf-8"))
            self.assertEqual("bridge_repair_pending", result["status"])
            self.assertNotEqual(first_owner, state["bridgeRepair"]["sentinelOwner"])
            self.assertEqual(state["bridgeRepair"]["sentinelOwner"], sentinel["owner"])
            self.assertEqual("operation-skill-update", sentinel["reason"])

    def test_owner_only_orphan_sentinel_never_authorizes_unknown_bridge_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-video-ingest"
            intermediate = make_release(
                root,
                "intermediate",
                "a" * 40,
                1,
                skill_name=skill_name,
                bridge_installer=True,
            )
            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("bridge boom")
            ):
                UPDATER.run_update(home, intermediate.as_uri(), 0, True, {skill_name})
            intermediate_manifest = json.loads(intermediate.read_text(encoding="utf-8"))
            intermediate_sha = intermediate_manifest["skills"][0]["sha256"]

            state = UPDATER.load_state(home)
            state.pop("bridgeRepair", None)
            state.pop("acceptedRelease", None)
            state["skills"] = {}
            UPDATER.atomic_json_write(UPDATER.state_path(home), state)
            latest = make_release(
                root,
                "latest",
                "b" * 40,
                2,
                skill_name=skill_name,
                bridge_installer=True,
            )

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(UPDATER, "refresh_bridge") as refresh:
                repaired = UPDATER.run_update(home, latest.as_uri(), 0, True)

            repaired_state = UPDATER.load_state(home)
            self.assertFalse(repaired["bridgeRepaired"])
            self.assertEqual("adoption_required", repaired["status"])
            self.assertEqual([skill_name], repaired["unmanagedExisting"])
            self.assertEqual({}, repaired_state["skills"])
            self.assertFalse((home / "ESTOP").exists())
            refresh.assert_not_called()
            check = UPDATER.run_check(home, latest.as_uri())
            self.assertEqual("unmanaged_existing", check["skills"][0]["status"])

    def test_bridge_repair_never_removes_preexisting_administrator_estop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(parents=True)
            administrator_stop = '{"reason":"administrator"}\n'
            (home / "ESTOP").write_text(administrator_stop, encoding="utf-8")
            skill_name = "facebook-video-ingest"
            manifest = make_release(
                root,
                "new-ingest",
                "a" * 40,
                skill_name=skill_name,
                bridge_installer=True,
            )
            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("bridge boom")
            ):
                first = UPDATER.run_update(home, manifest.as_uri(), 0, True, {skill_name})
            self.assertEqual("bridge_repair_pending", first["status"])
            self.assertEqual(
                administrator_stop, (home / "ESTOP").read_text(encoding="utf-8")
            )

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(UPDATER, "refresh_bridge"):
                second = UPDATER.run_update(home, manifest.as_uri(), 0, True)

            self.assertTrue(second["bridgeRepaired"])
            self.assertNotIn("bridgeRepair", UPDATER.load_state(home))
            self.assertEqual(
                administrator_stop, (home / "ESTOP").read_text(encoding="utf-8")
            )

    def test_pending_bridge_does_not_run_locally_modified_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-video-ingest"
            manifest = make_release(
                root,
                "new-ingest",
                "a" * 40,
                skill_name=skill_name,
                bridge_installer=True,
            )
            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("bridge boom")
            ):
                UPDATER.run_update(home, manifest.as_uri(), 0, True, {skill_name})
            installed = home / "skills" / skill_name
            (installed / "scripts" / "install_hermes_worker.py").write_text(
                "# untrusted local edit\n", encoding="utf-8"
            )

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(UPDATER, "refresh_bridge") as refresh:
                result = UPDATER.run_update(home, manifest.as_uri(), 0, True)

            self.assertEqual("bridge_repair_pending", result["status"])
            self.assertIn("本地修改", result["reason"])
            refresh.assert_not_called()
            self.assertTrue((home / "ESTOP").exists())

    def test_main_returns_nonzero_for_bridge_repair_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            skill_name = "facebook-video-ingest"
            manifest = make_release(
                root,
                "new-ingest",
                "a" * 40,
                skill_name=skill_name,
                bridge_installer=True,
            )
            argv = [
                "--hermes-home",
                str(home),
                "--manifest-url",
                manifest.as_uri(),
                "--no-notify",
                "run",
                "--install",
                skill_name,
            ]
            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(
                UPDATER, "refresh_bridge", side_effect=UPDATER.UpdaterError("bridge boom")
            ), mock.patch.object(UPDATER, "render"):
                self.assertEqual(1, UPDATER.main(argv))

    def test_exclusive_estop_claim_never_overwrites_existing_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "home" / "ESTOP"
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text('{"reason":"administrator"}\n', encoding="utf-8")

            created = UPDATER.create_json_exclusive(
                sentinel, {"reason": "operation-skill-update", "owner": "updater"}
            )

            self.assertFalse(created)
            self.assertEqual(
                '{"reason":"administrator"}\n', sentinel.read_text(encoding="utf-8")
            )

    def test_exclusive_estop_publish_failure_never_exposes_partial_sentinel(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "home" / "ESTOP"

            with mock.patch.object(
                UPDATER.os, "link", side_effect=OSError("hard link unavailable")
            ):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "无法原子创建"):
                    UPDATER.create_json_exclusive(
                        sentinel,
                        {"reason": "operation-skill-update", "owner": "a" * 32},
                    )

            self.assertFalse(sentinel.exists())
            self.assertEqual([], list(sentinel.parent.glob(".ESTOP.*.tmp")))

    def test_bridge_retention_does_not_rewrite_estop_in_place(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"

            with UPDATER.paused_for_update(home, [], 0) as pause:
                before = (home / "ESTOP").read_bytes()
                owner = pause.retain_for_bridge_repair(
                    {"commit": "a" * 40, "sha256": "b" * 64}
                )
                after = (home / "ESTOP").read_bytes()

            self.assertTrue(owner)
            self.assertEqual(before, after)
            self.assertEqual(
                "operation-skill-update",
                json.loads(after.decode("utf-8"))["reason"],
            )
            self.assertTrue((home / "ESTOP").exists())
            self.assertTrue(UPDATER.remove_owned_repair_sentinel(home / "ESTOP", owner))

    def test_macos_notification_passes_external_text_only_as_arguments(self):
        malicious = '\" & do shell script "touch /tmp/should-not-run" & \"'
        with mock.patch.object(UPDATER.sys, "platform", "darwin"), mock.patch.object(
            UPDATER.subprocess, "run"
        ) as run:
            UPDATER.notify("title", malicious, False)

        command = run.call_args.args[0]
        self.assertNotIn(malicious, command[2])
        self.assertEqual(["--", "title", malicious], command[3:])

    def test_rechecks_target_hash_immediately_before_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            write_skill(installed, "managed")
            seed_managed_state(home, installed)
            manifest = make_release(root, "published", "a" * 40)
            real_backup = UPDATER.backup_skills

            def backup_then_edit(actual_home, targets):
                backup = real_backup(actual_home, targets)
                (installed / "scripts" / "run.py").write_text(
                    "MARKER = 'late-operator-edit'\n", encoding="utf-8"
                )
                return backup

            with mock.patch.object(UPDATER, "backup_skills", side_effect=backup_then_edit):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "更新期间发生本地修改"):
                    UPDATER.run_update(home, manifest.as_uri(), 0, True)

            self.assertIn(
                "late-operator-edit",
                (installed / "scripts" / "run.py").read_text(encoding="utf-8"),
            )

    def test_transaction_rollback_is_idempotent_after_apply_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            staging = root / "staging"
            targets = []
            for name in ("one", "two"):
                installed = home / "skills" / name
                incoming = staging / "skills" / name
                installed.mkdir(parents=True)
                incoming.mkdir(parents=True)
                (installed / "marker.txt").write_text(f"old-{name}", encoding="utf-8")
                (incoming / "marker.txt").write_text(f"new-{name}", encoding="utf-8")
                targets.append({"name": name, "path": str(installed)})

            transaction = UPDATER.SkillTransaction(home, targets, staging)
            real_replace = os.replace

            def fail_second_install(source, destination):
                if Path(source) == staging / "skills" / "two":
                    raise OSError("injected install failure")
                return real_replace(source, destination)

            with mock.patch.object(UPDATER.os, "replace", side_effect=fail_second_install):
                with self.assertRaisesRegex(OSError, "injected install failure"):
                    transaction.apply()
                transaction.rollback()

            for name in ("one", "two"):
                installed = home / "skills" / name
                self.assertEqual(
                    f"old-{name}",
                    (installed / "marker.txt").read_text(encoding="utf-8"),
                )

    def test_transaction_rolls_back_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            staging = root / "staging"
            installed = home / "skills" / "demo-skill"
            incoming = staging / "skills" / "demo-skill"
            write_skill(installed, "old")
            write_skill(incoming, "new")
            transaction = UPDATER.SkillTransaction(
                home, [{"name": "demo-skill", "path": str(installed)}], staging
            )
            real_replace = os.replace

            def interrupt_new_install(source, destination):
                if Path(source) == incoming:
                    raise KeyboardInterrupt()
                return real_replace(source, destination)

            with mock.patch.object(
                UPDATER.os, "replace", side_effect=interrupt_new_install
            ):
                with self.assertRaises(KeyboardInterrupt):
                    transaction.apply()

            self.assertIn(
                "old", (installed / "scripts" / "run.py").read_text(encoding="utf-8")
            )
            self.assertFalse(transaction.rollback_root.exists())

    def test_failed_rollback_keeps_estop_for_next_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            transaction = mock.Mock()
            transaction.rollback.side_effect = UPDATER.UpdaterError("rollback failed")

            with UPDATER.paused_for_update(home, [], 0) as pause:
                with self.assertRaisesRegex(UPDATER.UpdaterError, "rollback failed"):
                    UPDATER.rollback_or_retain_pause(transaction, pause)

            self.assertTrue((home / "ESTOP").is_file())

    def test_fresh_target_rollback_keeps_metadata_when_removal_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            staging = root / "staging"
            installed = home / "skills" / "fresh"
            incoming = staging / "skills" / "fresh"
            incoming.mkdir(parents=True)
            (incoming / "marker.txt").write_text("new", encoding="utf-8")
            transaction = UPDATER.SkillTransaction(
                home, [{"name": "fresh", "path": str(installed)}], staging
            )
            transaction.apply()

            with mock.patch.object(UPDATER.shutil, "rmtree", return_value=None):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "无法回滚新安装"):
                    transaction.rollback()

            self.assertTrue(installed.exists())
            self.assertEqual(1, len(transaction.moved))

    def test_recovers_existing_skill_crash_after_old_directory_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(installed, "old")
            write_skill(incoming, "new")
            old_hash = UPDATER.directory_hash(installed)
            old_entry = {"sha256": old_hash, "version": "old-version"}
            UPDATER.atomic_json_write(
                UPDATER.state_path(home),
                {"schemaVersion": 1, "skills": {"demo-skill": old_entry}},
            )
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "currentSha256": old_hash,
                        "desiredSha256": UPDATER.directory_hash(incoming),
                    }
                ],
                staging,
            )
            journal_targets = transaction.begin()
            os.replace(journal_targets[0]["path"], journal_targets[0]["rollback"])

            recovered = UPDATER.recover_incomplete_transactions(home)

            self.assertEqual([transaction.rollback_root.name], recovered)
            self.assertIn(
                "old", (installed.resolve() / "scripts" / "run.py").read_text(encoding="utf-8")
            )
            self.assertEqual(
                old_entry, UPDATER.load_state(home)["skills"]["demo-skill"]
            )
            self.assertFalse(transaction.rollback_root.exists())

    def test_recovers_existing_skill_crash_after_new_directory_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(installed, "old")
            write_skill(incoming, "new")
            old_hash = UPDATER.directory_hash(installed)
            desired_hash = UPDATER.directory_hash(incoming)
            old_entry = {"sha256": old_hash, "version": "old-version"}
            state = {"schemaVersion": 1, "skills": {"demo-skill": old_entry}}
            UPDATER.atomic_json_write(UPDATER.state_path(home), state)
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "currentSha256": old_hash,
                        "desiredSha256": desired_hash,
                    }
                ],
                staging,
                state,
            )
            journal_targets = transaction.begin()
            target = journal_targets[0]
            os.replace(target["path"], target["rollback"])
            os.replace(incoming, target["path"])

            UPDATER.recover_incomplete_transactions(home)

            self.assertIn(
                "old", (installed.resolve() / "scripts" / "run.py").read_text(encoding="utf-8")
            )
            self.assertEqual(
                old_entry, UPDATER.load_state(home)["skills"]["demo-skill"]
            )
            self.assertFalse(transaction.rollback_root.exists())

    def test_recovers_fresh_skill_crash_after_new_directory_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(incoming, "new")
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "desiredSha256": UPDATER.directory_hash(incoming),
                    }
                ],
                staging,
            )
            journal_targets = transaction.begin()
            target = journal_targets[0]
            target["path"].parent.mkdir(parents=True, exist_ok=True)
            os.replace(incoming, target["path"])

            UPDATER.recover_incomplete_transactions(home)

            self.assertFalse(installed.resolve().exists())
            self.assertNotIn("demo-skill", UPDATER.load_state(home)["skills"])
            self.assertFalse(transaction.rollback_root.exists())

    def test_recovery_keeps_new_skill_when_atomic_state_marks_transaction_committed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(installed, "old")
            write_skill(incoming, "new")
            old_hash = UPDATER.directory_hash(installed)
            desired_hash = UPDATER.directory_hash(incoming)
            old_state = {
                "schemaVersion": 1,
                "skills": {"demo-skill": {"sha256": old_hash, "version": "old"}},
            }
            UPDATER.atomic_json_write(UPDATER.state_path(home), old_state)
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "currentSha256": old_hash,
                        "desiredSha256": desired_hash,
                    }
                ],
                staging,
                old_state,
            )
            transaction.apply()
            committed_state = {
                "schemaVersion": 1,
                "skills": {
                    "demo-skill": {"sha256": desired_hash, "version": "new"}
                },
            }
            UPDATER.atomic_json_write(UPDATER.state_path(home), committed_state)

            UPDATER.recover_incomplete_transactions(home)

            self.assertIn(
                "new", (installed.resolve() / "scripts" / "run.py").read_text(encoding="utf-8")
            )
            self.assertEqual(
                committed_state["skills"]["demo-skill"],
                UPDATER.load_state(home)["skills"]["demo-skill"],
            )
            self.assertFalse(transaction.rollback_root.exists())

    def test_recovery_rejects_malicious_journal_path_without_touching_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            outside = root / "outside" / "demo-skill"
            write_skill(outside, "must-survive")
            transaction_id = "a" * 32
            rollback_root = UPDATER.transaction_root(home) / transaction_id
            rollback_root.mkdir(parents=True)
            journal = {
                "schemaVersion": UPDATER.TRANSACTION_SCHEMA_VERSION,
                "kind": "operation-skill-update",
                "transactionId": transaction_id,
                "createdAt": UPDATER.utc_now(),
                "targets": [
                    {
                        "name": "demo-skill",
                        "path": str(outside.resolve()),
                        "existed": False,
                        "hadState": False,
                        "oldState": None,
                        "oldSha256": "",
                        "desiredSha256": UPDATER.directory_hash(outside),
                    }
                ],
            }
            UPDATER.durable_json_write(
                rollback_root / UPDATER.TRANSACTION_JOURNAL_NAME, journal
            )

            with self.assertRaisesRegex(UPDATER.UpdaterError, "路径越界"):
                UPDATER.recover_incomplete_transactions(home)

            self.assertIn(
                "must-survive",
                (outside / "scripts" / "run.py").read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (rollback_root / UPDATER.TRANSACTION_JOURNAL_NAME).is_file()
            )

    def test_recovery_cleans_prepublication_journal_temp_after_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            transaction_id = "a" * 32
            rollback_root = UPDATER.transaction_root(home) / transaction_id
            rollback_root.mkdir(parents=True)
            unfinished = rollback_root / (
                f".{UPDATER.TRANSACTION_JOURNAL_NAME}.{'b' * 32}.tmp"
            )
            unfinished.write_text('{"schemaVersion":', encoding="utf-8")

            recovered = UPDATER.recover_incomplete_transactions(home)

            self.assertEqual([transaction_id], recovered)
            self.assertFalse(rollback_root.exists())

    def test_recovery_delete_failure_keeps_journal_and_new_fresh_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(incoming, "new")
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "desiredSha256": UPDATER.directory_hash(incoming),
                    }
                ],
                staging,
            )
            transaction.apply()

            with mock.patch.object(UPDATER.shutil, "rmtree", return_value=None):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "无法回滚新安装"):
                    UPDATER.recover_incomplete_transactions(home)

            self.assertTrue(installed.resolve().is_dir())
            self.assertTrue(transaction.journal_path.is_file())

    def test_run_update_recovers_transactions_before_fetching_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            order = []

            def scan(_home):
                order.append("scan")
                return [
                    {
                        "rollbackRoot": UPDATER.transaction_root(home) / ("a" * 32),
                        "targets": [{"path": (home / "skills" / "demo-skill").resolve()}],
                    }
                ]

            def recover(_home):
                order.append("recover")
                return []

            def fetch(_url):
                order.append("fetch")
                raise UPDATER.UpdaterError("stop after ordering check")

            with mock.patch.object(
                UPDATER, "scan_incomplete_transactions", side_effect=scan
            ), mock.patch.object(
                UPDATER, "recover_incomplete_transactions", side_effect=recover
            ), mock.patch.object(
                UPDATER, "active_skill_processes", return_value=[]
            ), mock.patch.object(UPDATER, "fetch_manifest", side_effect=fetch):
                with self.assertRaisesRegex(UPDATER.UpdaterError, "ordering check"):
                    UPDATER.run_update(home, "https://example.test/manifest.json", 0, True)

            self.assertEqual(["scan", "recover", "fetch"], order)

    def test_busy_recovery_defers_before_fetch_and_scans_transaction_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(incoming, "new")
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "desiredSha256": UPDATER.directory_hash(incoming),
                    }
                ],
                staging,
            )
            transaction.apply()

            with mock.patch.object(
                UPDATER, "active_skill_processes", return_value=["busy worker"]
            ) as inspect, mock.patch.object(UPDATER, "fetch_manifest") as fetch:
                result = UPDATER.run_update(
                    home, "https://example.test/manifest.json", 0, True
                )

            self.assertEqual("deferred", result["status"])
            self.assertIn("transaction-recovery", result["reason"])
            fetch.assert_not_called()
            self.assertIn(installed.resolve(), inspect.call_args.args[0])
            self.assertTrue(installed.resolve().is_dir())
            self.assertTrue(transaction.journal_path.is_file())

    def test_successful_recovery_removes_only_matching_orphan_update_estop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            installed = home / "skills" / "demo-skill"
            staging = root / "staging"
            incoming = staging / "skills" / "demo-skill"
            write_skill(incoming, "crashed-new")
            transaction = UPDATER.SkillTransaction(
                home,
                [
                    {
                        "name": "demo-skill",
                        "path": str(installed),
                        "desiredSha256": UPDATER.directory_hash(incoming),
                    }
                ],
                staging,
            )
            transaction.apply()
            owner = "b" * 32
            (home / "ESTOP").write_text(
                json.dumps(
                    {
                        "reason": "operation-skill-update",
                        "owner": owner,
                        "engaged_at": UPDATER.utc_now(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = make_release(root, "published", "a" * 40)

            with mock.patch.object(UPDATER, "active_skill_processes", return_value=[]):
                UPDATER.run_update(home, manifest.as_uri(), 0, True)

            self.assertFalse((home / "ESTOP").exists())
            self.assertFalse(installed.resolve().exists())
            self.assertFalse(transaction.rollback_root.exists())

    def test_windows_schedule_has_valid_trigger_array_and_two_hour_limit(self):
        script = UPDATER.windows_schedule_script(Path("C:/Hermes"), 7)

        self.assertIn("(New-ScheduledTaskTrigger -AtLogOn -User $userId)", script)
        self.assertIn("$userId = $currentUser.User.Value", script)
        self.assertIn("-UserId $userId -LogonType Interactive -RunLevel Limited", script)
        self.assertNotIn("-RunLevel Highest", script)
        self.assertIn("-Force -ErrorAction Stop", script)
        self.assertIn(
            "(New-ScheduledTaskTrigger -Daily -At '04:07')", script
        )
        self.assertNotIn("-AtLogOn,", script)
        self.assertIn("-ExecutionTimeLimit (New-TimeSpan -Hours 2)", script)
        self.assertNotIn("New-TimeSpan -Minutes 15", script)

    def test_windows_task_name_is_stable_and_isolated_by_installation(self):
        first = UPDATER.windows_task_name(Path("C:/Users/wei/.hermes"))
        self.assertEqual(first, UPDATER.windows_task_name(Path("c:/users/WEI/.hermes")))
        self.assertNotEqual(first, UPDATER.windows_task_name(Path("C:/Users/other/.hermes")))
        self.assertRegex(first, r"^HM Operation Skill Updater-[0-9a-f]{12}$")

    def test_windows_legacy_cleanup_checks_owner_and_exact_action(self):
        script = UPDATER.windows_legacy_schedule_cleanup()
        self.assertIn("$legacyUser -eq $userId", script)
        self.assertIn("$legacy.Actions[0].Execute -eq $action.Execute", script)
        self.assertIn("$legacy.Actions[0].Arguments -eq $action.Arguments", script)

    def test_windows_schedule_permission_error_is_readable_not_encoded_command(self):
        result = subprocess.CompletedProcess([], 1, "", "HRESULT 0x80070005,Register-ScheduledTask")
        with mock.patch.object(UPDATER.subprocess, "run", return_value=result) as run:
            with self.assertRaises(UPDATER.UpdaterError) as caught:
                UPDATER.run_windows_schedule_script("throw 'test'")
        message = str(caught.exception)
        self.assertIn("0x80070005", message)
        self.assertIn("网管", message)
        self.assertNotIn("EncodedCommand", message)
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual("utf-8", run.call_args.kwargs["encoding"])

    def test_windows_schedule_other_errors_and_timeout_are_concise(self):
        result = subprocess.CompletedProcess([], 1, "", "Task service unavailable")
        with mock.patch.object(UPDATER.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(UPDATER.UpdaterError, "Task service unavailable"):
                UPDATER.run_windows_schedule_script("test")
        with mock.patch.object(UPDATER.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 60)):
            with self.assertRaisesRegex(UPDATER.UpdaterError, "超时"):
                UPDATER.run_windows_schedule_script("test")

    def test_hidden_backup_skill_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_skill(root / ".backup" / "demo-skill", "old")
            self.assertEqual({}, UPDATER.find_installed_skills(root, {"demo-skill"}))


if __name__ == "__main__":
    unittest.main()
