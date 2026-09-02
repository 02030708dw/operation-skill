#!/usr/bin/env python3
"""Build a deterministic Operation Skills archive and release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = 1
IGNORED_PARTS = {"__pycache__", "node_modules"}
IGNORED_NAMES = {".DS_Store"}
VERSION_PATTERN = re.compile(r'^\s+version:\s*["\']?([^"\'\s]+)')
NAME_PATTERN = re.compile(r'^name:\s*["\']?([^"\'\s]+)')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--public-base-url", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--published-at", default="")
    parser.add_argument("--release-sequence", type=positive_integer, default=1)
    return parser.parse_args()


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def include_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if path.name in IGNORED_NAMES or path.suffix == ".pyc":
        return False
    return not any(part in IGNORED_PARTS for part in relative.parts)


def relative_path_key(path: Path, root: Path) -> str:
    """Return an OS-independent, case-sensitive path ordering key."""
    return path.relative_to(root).as_posix()


def skill_files(skill_dir: Path) -> list[Path]:
    symlinks = sorted(
        (path for path in skill_dir.rglob("*") if path.is_symlink()),
        key=lambda path: relative_path_key(path, skill_dir),
    )
    if symlinks:
        relative = symlinks[0].relative_to(skill_dir)
        raise ValueError(f"skill contains unsupported symlink: {skill_dir.name}/{relative}")
    return sorted(
        (
            path
            for path in skill_dir.rglob("*")
            if path.is_file() and include_file(path, skill_dir)
        ),
        key=lambda path: relative_path_key(path, skill_dir),
    )


def directory_hash(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in skill_files(skill_dir):
        relative = path.relative_to(skill_dir).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frontmatter(skill_md: Path) -> tuple[str, str]:
    name = ""
    version = ""
    lines = skill_md.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"missing YAML frontmatter: {skill_md}")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        name_match = NAME_PATTERN.match(line)
        if name_match:
            name = name_match.group(1)
        version_match = VERSION_PATTERN.match(line)
        if version_match and not version:
            version = version_match.group(1)
    if not name:
        raise ValueError(f"missing skill name: {skill_md}")
    if name != skill_md.parent.name:
        raise ValueError(f"skill name {name!r} does not match directory {skill_md.parent.name!r}")
    return name, version


def git_commit(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def zip_info(name: str, executable: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def build_archive(skills_dir: Path, archive_path: Path) -> list[dict[str, object]]:
    skills: list[dict[str, object]] = []
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for skill_dir in sorted(
            (path for path in skills_dir.iterdir() if path.is_dir()),
            key=lambda path: relative_path_key(path, skills_dir),
        ):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            name, version = frontmatter(skill_md)
            files = skill_files(skill_dir)
            for source in files:
                relative = PurePosixPath("skills", name, source.relative_to(skill_dir).as_posix()).as_posix()
                executable = bool(source.stat().st_mode & 0o111) or source.suffix in {".sh", ".py"}
                archive.writestr(zip_info(relative, executable), source.read_bytes())
            skills.append(
                {
                    "name": name,
                    "version": version,
                    "path": f"skills/{name}",
                    "sha256": directory_hash(skill_dir),
                    "fileCount": len(files),
                }
            )
    if not skills:
        raise ValueError(f"no skills found below {skills_dir}")
    return skills


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    commit = args.commit.strip() or git_commit(repo_root)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("--commit must be a full 40-character lowercase Git SHA")
    published_at = args.published_at.strip() or datetime.now(timezone.utc).isoformat()
    public_base = args.public_base_url.rstrip("/")
    archive_name = f"operation-skills-{commit}.zip"
    archive_path = output_dir / archive_name
    skills = build_archive(repo_root / "skills", archive_path)

    updater_path = repo_root / "scripts" / "operation_skill_updater.py"
    if not updater_path.is_file():
        raise ValueError(f"updater is missing: {updater_path}")
    updater_sha256 = file_hash(updater_path)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "releaseSequence": args.release_sequence,
        "channel": "main",
        "repository": "02030708dw/operation-skill",
        "commit": commit,
        "publishedAt": published_at,
        "archive": {
            "url": f"{public_base}/releases/{archive_name}",
            "sha256": file_hash(archive_path),
            "size": archive_path.stat().st_size,
        },
        "updater": {
            "url": f"{public_base}/releases/operation_skill_updater-{updater_sha256}.py",
            "sha256": updater_sha256,
            "size": updater_path.stat().st_size,
        },
        "skills": skills,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"archive": str(archive_path), "manifest": str(manifest_path), "skills": len(skills)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
