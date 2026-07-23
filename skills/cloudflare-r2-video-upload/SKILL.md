---
name: cloudflare-r2-video-upload
description: Safely preview and upload local video files or directories to Cloudflare R2 object storage using its S3-compatible API, with recursive discovery, per-run limits, multipart transfers, concurrent files, duplicate checks, post-upload verification, and reports. Use when the user asks Hermes to configure or check R2 access, preview video object keys, upload downloaded videos, upload a folder, resume a batch, or troubleshoot R2 uploads.
metadata:
  version: "1.0.0"
  platforms:
    - windows
    - macos
    - linux
  prerequisites:
    commands:
      - python
  hermes:
    tags:
      - cloudflare
      - r2
      - video
      - upload
      - object-storage
    requires_tools:
      - terminal
---

# Cloudflare R2 Video Upload

Upload permitted local videos to Cloudflare R2 through its S3-compatible API. Resolve `<skill-dir>` from the directory containing this loaded `SKILL.md`; never assume a drive letter, username, source folder, bucket, or endpoint.

## Safety Rules

- Preview by default. Add `--execute` only when the user clearly asks to upload or says to execute immediately.
- Never request that access keys or secret keys be pasted into chat.
- Read credentials only from the `CLOUDFLARE_R2_*` environment variables already configured on the local computer.
- Never print, log, serialize, or include credentials in a command line.
- Do not create API tokens, buckets, public domains, lifecycle policies, or access policies unless the user separately asks.
- Do not make a private bucket public.
- Upload only files selected by the user's source, filters, and requested count.
- Never delete local files after upload.
- Existing same-size objects are skipped. A different-size object is a conflict and must not be replaced unless the user explicitly authorizes overwrite.

Read [references/cloudflare-r2-setup.md](references/cloudflare-r2-setup.md) when configuration is missing. Read [references/upload-behavior.md](references/upload-behavior.md) before troubleshooting large transfers or duplicate results.

## Entry Point

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" <arguments>
```

Python accepts `/` in paths on Windows. Do not place secret values in command arguments.

## Required Local Configuration

- `CLOUDFLARE_R2_ACCOUNT_ID`
- `CLOUDFLARE_R2_ACCESS_KEY_ID`
- `CLOUDFLARE_R2_SECRET_ACCESS_KEY`
- `CLOUDFLARE_R2_BUCKET`, or pass non-secret `--bucket`

Optional:

- `CLOUDFLARE_R2_SESSION_TOKEN`
- `CLOUDFLARE_R2_ENDPOINT` for a custom R2 jurisdiction endpoint
- `CLOUDFLARE_R2_PREFIX`
- `CLOUDFLARE_R2_PUBLIC_BASE_URL` for displaying usable public URLs; setting this variable does not make a bucket public
- `CLOUDFLARE_R2_REPORTS`

The endpoint defaults to:

```text
https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```

The S3 region is always `auto`.

## Intent Mapping

- “检查连接、检查配置” -> `--check`
- “预演、看看会上传什么、不要上传” -> omit `--execute`
- “上传、立即执行” -> add `--execute`
- “上传这个文件/目录” -> `--source "<path>"`
- “放到 videos/2026 目录” -> `--prefix "videos/2026"`
- “最多 N 个” -> `--count N`
- “全部” -> `--count 0`
- “并发 N 个文件” -> `--workers N`
- “包含所有文件” -> `--all-files`; otherwise only common video extensions are selected
- “覆盖同名但不同的对象” -> `--overwrite`, only after explicit authorization
- “详细进度” -> `--verbose`

`--count` is a per-run file limit after deterministic path sorting. The default `0` means all matching videos.

## Common Operations

### Check R2 Access

This is read-only:

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" --check
```

Report only whether the bucket is accessible. Never echo credential values.

### Preview A Download Folder

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" --source "<video-folder>" --prefix "facebook" --count 10
```

Preview recursively discovers videos and checks each planned object key with `HeadObject`. It does not upload files or write reports.

### Upload Videos

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" --source "<video-folder>" --prefix "facebook" --count 10 --workers 3 --execute
```

### Upload One Video

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" --source "<video-file>" --prefix "facebook" --execute
```

### Upload Everything Under A Folder

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" --source "<video-folder>" --prefix "archive" --count 0 --execute
```

## Object-Key Rules

- Directory uploads preserve relative paths beneath the selected source directory.
- Single-file uploads use the filename.
- `--prefix` is prepended with `/` separators.
- Hidden files and hidden directories are ignored by default.
- Only common video extensions are included by default.
- `--flatten` discards subdirectories but stops on filename collisions.
- Unicode filenames are preserved.

Before execution, tell the user the bucket, source, prefix, file count, total size, and whether overwrite is enabled.

## Execution Rules

1. If required values are present and the user explicitly requested upload, execute without an extra confirmation.
2. If the source or bucket is missing, ask for only that non-secret value.
3. If credentials are missing, explain which environment-variable names are missing; do not ask for their values.
4. Use one process for the requested batch. File uploads run concurrently inside the process.
5. Do not impose a short fixed outer timeout. Large multipart video uploads may run for a long time while making valid progress.
6. If the terminal yields while the process is still active, monitor the same process. Never start a duplicate upload merely because the interface stopped waiting.
7. Treat missing progress, repeated network failures, a stopped process, or a final nonzero exit as failure—not normal long runtime.
8. After upload, rely on the script's `HeadObject` size verification. Do not claim success from the initial PUT alone.
9. Report uploaded, skipped-existing, conflict, and failed counts separately, plus report paths.

## Existing Objects

- Same key and same byte size -> `skipped-existing`
- Same key and different byte size -> `conflict`
- Missing key -> ready for upload
- `--overwrite` permits replacement only for different-size conflicts

The script does not use ETag as a local-file MD5 comparison because multipart ETags are not plain file MD5 values.

## Reports

Actual upload runs write timestamped and `latest` Markdown/JSON reports under:

```text
<hermes-home>/cloudflare-r2-video-upload/reports/
```

Reports contain local file paths, object keys, sizes, status, ETag, and optional public URLs. They never contain credentials.

## Dependencies

The script requires `boto3`. If missing, install it in the Python environment Hermes uses:

```text
python -m pip install -r "<skill-dir>/scripts/requirements.txt"
```

If that Python environment has no `pip`, use an approved local package manager or recreate the environment with `boto3`; do not download and execute an unverified installer.
