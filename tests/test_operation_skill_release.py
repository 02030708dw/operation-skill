from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


BUILD = load_module("operation_skill_release_builder", ROOT / "scripts" / "build-operation-skill-release.py")
PUBLISH = load_module("operation_skill_release_publisher", ROOT / "scripts" / "publish-operation-skill-release.py")


class MissingKeyError(Exception):
    def __init__(self):
        super().__init__("missing")
        self.response = {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }


class FakeR2Client:
    def __init__(self, remote_manifest=None):
        self.remote_manifest = remote_manifest
        self.calls = []

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        if self.remote_manifest is None:
            raise MissingKeyError()
        return {"Body": io.BytesIO(json.dumps(self.remote_manifest).encode("utf-8"))}

    def upload_file(self, source, bucket, key, ExtraArgs):
        self.calls.append(("upload", key))


class OperationSkillReleaseTest(unittest.TestCase):
    def make_publish_fixture(self, root, sequence, commit):
        dist = root / "dist"
        scripts = root / "scripts"
        dist.mkdir()
        scripts.mkdir()
        archive_name = f"operation-skills-{commit}.zip"
        archive_path = dist / archive_name
        archive_path.write_bytes(b"archive")
        for name in (
            "operation_skill_updater.py",
            "install-operation-skill-updater.ps1",
            "install-operation-skill-updater.sh",
        ):
            (scripts / name).write_text(name, encoding="utf-8")
        updater_path = scripts / "operation_skill_updater.py"
        updater_sha = hashlib.sha256(updater_path.read_bytes()).hexdigest()
        manifest = {
            "releaseSequence": sequence,
            "commit": commit,
            "archive": {
                "url": f"https://skills.example.test/operation-skills/releases/{archive_name}",
                "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "size": archive_path.stat().st_size,
            },
            "updater": {
                "url": f"https://skills.example.test/operation-skills/releases/operation_skill_updater-{updater_sha}.py",
                "sha256": updater_sha,
                "size": updater_path.stat().st_size,
            },
        }
        (dist / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return dist, updater_sha

    def run_publisher(self, root, dist, client):
        fake_boto3 = SimpleNamespace(client=lambda *args, **kwargs: client)
        environment = {
            "CLOUDFLARE_R2_ACCOUNT_ID": "account",
            "CLOUDFLARE_R2_ACCESS_KEY_ID": "key",
            "CLOUDFLARE_R2_SECRET_ACCESS_KEY": "secret",
            "CLOUDFLARE_R2_BUCKET": "bucket",
        }
        with mock.patch.dict("sys.modules", {"boto3": fake_boto3}), mock.patch.dict(
            os.environ, environment
        ), mock.patch.object(
            PUBLISH,
            "parse_args",
            return_value=Namespace(
                dist=dist,
                repo_root=root,
                prefix="operation-skills",
                verify_only=False,
            ),
        ):
            return PUBLISH.main()

    def test_archive_is_deterministic_and_contains_skill_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills" / "demo-skill"
            (skill / "scripts").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                '---\nname: demo-skill\ndescription: demo\nmetadata:\n  version: "1.2.3"\n---\n',
                encoding="utf-8",
            )
            (skill / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
            first = root / "first.zip"
            second = root / "second.zip"

            first_skills = BUILD.build_archive(root / "skills", first)
            second_skills = BUILD.build_archive(root / "skills", second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_skills, second_skills)
            self.assertEqual("demo-skill", first_skills[0]["name"])
            self.assertEqual("1.2.3", first_skills[0]["version"])
            self.assertEqual(BUILD.directory_hash(skill), first_skills[0]["sha256"])
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(
                    ["skills/demo-skill/SKILL.md", "skills/demo-skill/scripts/run.py"],
                    archive.namelist(),
                )

    def test_archive_ignores_runtime_cache_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "demo-skill"
            (skill / "__pycache__").mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
            before = BUILD.directory_hash(skill)
            (skill / "__pycache__" / "run.pyc").write_bytes(b"cache")
            self.assertEqual(before, BUILD.directory_hash(skill))

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires optional privileges")
    def test_archive_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            skill = Path(temporary) / "demo-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("---\nname: demo-skill\n---\n", encoding="utf-8")
            (skill / "linked.md").symlink_to(skill / "SKILL.md")
            with self.assertRaises(ValueError):
                BUILD.directory_hash(skill)

    def test_repository_release_has_all_current_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "release.zip"
            skills = BUILD.build_archive(ROOT / "skills", archive)
            directories = {path.name for path in (ROOT / "skills").iterdir() if (path / "SKILL.md").is_file()}
            self.assertEqual(directories, {entry["name"] for entry in skills})
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), BUILD.file_hash(archive))

    def test_manifest_uses_immutable_updater_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with mock.patch(
                "sys.argv",
                [
                    "build-operation-skill-release.py",
                    "--repo-root",
                    str(ROOT),
                    "--output-dir",
                    str(output),
                    "--public-base-url",
                    "https://skills.example.test/operation-skills",
                    "--commit",
                    "1" * 40,
                    "--published-at",
                    "2026-09-02T00:00:00+00:00",
                    "--release-sequence",
                    "42",
                ],
            ):
                self.assertEqual(0, BUILD.main())
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(42, manifest["releaseSequence"])
            updater = manifest["updater"]
            self.assertEqual(BUILD.file_hash(ROOT / "scripts" / "operation_skill_updater.py"), updater["sha256"])
            self.assertEqual(
                (ROOT / "scripts" / "operation_skill_updater.py").stat().st_size,
                updater["size"],
            )
            self.assertTrue(updater["url"].endswith(f"/releases/operation_skill_updater-{updater['sha256']}.py"))

    def test_verify_only_checks_artifacts_without_loading_r2_dependencies_or_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, _ = self.make_publish_fixture(root, 7, "1" * 40)
            with mock.patch.object(
                PUBLISH,
                "parse_args",
                return_value=Namespace(
                    dist=dist,
                    repo_root=root,
                    prefix="operation-skills",
                    verify_only=True,
                ),
            ), mock.patch.object(
                PUBLISH,
                "required_env",
                side_effect=AssertionError("verify-only must not read credentials"),
            ), mock.patch.dict(
                "sys.modules", {"boto3": None}
            ):
                self.assertEqual(0, PUBLISH.main())

    def test_publisher_rejects_tampered_archive_before_r2_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, _ = self.make_publish_fixture(root, 7, "2" * 40)
            archive = next(dist.glob("operation-skills-*.zip"))
            archive.write_bytes(archive.read_bytes() + b"tampered")
            client = FakeR2Client()

            with self.assertRaisesRegex(ValueError, "archive size mismatch"):
                self.run_publisher(root, dist, client)

            self.assertEqual([], client.calls)

    def test_publisher_rejects_tampered_updater_before_r2_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, _ = self.make_publish_fixture(root, 7, "3" * 40)
            updater = root / "scripts" / "operation_skill_updater.py"
            updater.write_bytes(b"x" * updater.stat().st_size)
            client = FakeR2Client()

            with self.assertRaisesRegex(ValueError, "updater SHA-256 mismatch"):
                self.run_publisher(root, dist, client)

            self.assertEqual([], client.calls)

    def test_publisher_uploads_stable_manifest_last(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, updater_sha = self.make_publish_fixture(root, 7, "2" * 40)
            client = FakeR2Client()

            self.assertEqual(0, self.run_publisher(root, dist, client))

            self.assertEqual(("get", "operation-skills/stable/manifest.json"), client.calls[0])
            uploads = [key for action, key in client.calls if action == "upload"]
            self.assertEqual("operation-skills/stable/manifest.json", uploads[-1])
            self.assertIn(
                f"operation-skills/releases/operation_skill_updater-{updater_sha}.py",
                uploads,
            )

    def test_publisher_rejects_older_sequence_before_any_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, _ = self.make_publish_fixture(root, 7, "4" * 40)
            client = FakeR2Client(
                {"releaseSequence": 8, "commit": "5" * 40}
            )

            with self.assertRaisesRegex(ValueError, "older release sequence"):
                self.run_publisher(root, dist, client)

            self.assertEqual(
                [("get", "operation-skills/stable/manifest.json")], client.calls
            )

    def test_publisher_rejects_same_sequence_for_different_commit_before_any_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist, _ = self.make_publish_fixture(root, 7, "6" * 40)
            client = FakeR2Client(
                {"releaseSequence": 7, "commit": "7" * 40}
            )

            with self.assertRaisesRegex(ValueError, "reuse release sequence"):
                self.run_publisher(root, dist, client)

            self.assertEqual(
                [("get", "operation-skills/stable/manifest.json")], client.calls
            )

    def test_publisher_allows_idempotent_same_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit = "8" * 40
            dist, _ = self.make_publish_fixture(root, 7, commit)
            client = FakeR2Client({"releaseSequence": 7, "commit": commit})

            self.assertEqual(0, self.run_publisher(root, dist, client))

            uploads = [key for action, key in client.calls if action == "upload"]
            self.assertTrue(uploads)
            self.assertEqual("operation-skills/stable/manifest.json", uploads[-1])


if __name__ == "__main__":
    unittest.main()
