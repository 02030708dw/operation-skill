---
name: myt-cloud-phone-file-upload
description: Upload a user-specified local file or all regular files under a local directory to one or more MYT (魔云腾) Android cloud phones through the MYT HTTP API. Use when the user asks to copy, transfer, import, or upload files, videos, images, audio, generated assets, or a completed output folder to selected MYT devices. The local path and target devices must be supplied for every upload request.
---

# MYT Cloud Phone File Upload

Upload one file or recursively upload every regular file under one directory to
one or more MYT cloud phones. Preserve relative subdirectories. Preview by
default; use `--execute` only after explicit authorization.

## Required input

For every upload, require:

- A user-supplied local file or directory path. Never invent, remember, scan for,
  or reuse a default path.
- Target MYT device IDs or ports, such as `T1001,T1002`.
- Explicit execution intent before adding `--execute`.

If the path or devices are absent, ask for the missing value. A connectivity-only
`--check` does not require a local path.

## Workflow

1. Use `scripts/myt_cloud_phone_file_upload.py`.
2. Pass the exact current-request path with `--path`.
3. If it is a directory, let the script recursively collect all regular files.
   Do not manually select only MP4 files; PNG, audio, JSON, and other files must
   also be included.
4. Run a preview without `--execute`. Preview checks connectivity and prints the
   complete local-to-remote plan without creating remote files.
5. If the user already authorized an immediate upload, run the same command with
   `--execute`; do not ask again.
6. Keep the same process running until all files and devices finish. Active large
   transfers are normal work, not a task timeout.
7. Report per-device and per-file statuses. Retry a directory safely: files
   already present with the same size are skipped.

## Commands

Connection check:

```powershell
python "<skill-dir>\scripts\myt_cloud_phone_file_upload.py" --devices T1001,T1002 --check
```

Preview a directory:

```powershell
python "<skill-dir>\scripts\myt_cloud_phone_file_upload.py" --devices T1001 --path "F:\lottery\2d\2026-07-24"
```

Upload every file in that directory:

```powershell
python "<skill-dir>\scripts\myt_cloud_phone_file_upload.py" --devices T1001 --path "F:\lottery\2d\2026-07-24" --execute
```

Upload one file:

```powershell
python "<skill-dir>\scripts\myt_cloud_phone_file_upload.py" --devices T1001,T1002 --path "D:\assets\result.png" --execute
```

Explicitly replace different same-name remote files:

```powershell
python "<skill-dir>\scripts\myt_cloud_phone_file_upload.py" --devices T1001 --path "D:\assets" --execute --overwrite
```

## Rules

- Read the controller host from `MYT_HOST` or `--host`; never store a real IP.
- Map `T1001` to port `10005`, then increase by `3` for each device.
- Run selected devices concurrently. Process files in stable order on each device
  so one MYT upload endpoint is not flooded with simultaneous requests.
- Treat `/sdcard/upload` as both the actual MYT upload landing directory and the
  default final destination. Real-device feedback showed that `/upload` did not
  place files under `/sdcard/Download`.
- For a directory, upload every regular file recursively and preserve its path
  relative to the selected directory. Skip symbolic links to prevent leaving the
  selected tree.
- Accept all file extensions. Do not filter the directory to videos only.
- Use a unique temporary upload name, verify its exact byte count, move it to the
  final path, and trigger Android media scanning.
- Return `already-present` for the same path and size.
- Return `conflict` for the same path with a different size. Use `--overwrite`
  only when explicitly authorized.
- Keep legacy `--file` support for older calls, but generate new calls with
  `--path`.

## Statuses

- `uploaded`: one file was uploaded and verified.
- `already-present`: same path and size already exists.
- `conflict`: different content occupies the path; nothing was overwritten.
- `preview`: no remote file was created.
- `partial`: some files succeeded while others conflicted or failed.
- `failed`: the device or every attempted file failed.

Read [references/myt-upload-api.md](references/myt-upload-api.md) for protocol and
troubleshooting details.
