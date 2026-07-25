---
name: myt-cloud-phone-video-upload
description: Backward-compatible alias for the generic MYT cloud-phone file uploader. Use when the user explicitly invokes the older myt-cloud-phone-video-upload command to upload a user-specified file or every regular file under a directory to selected MYT devices. Prefer myt-cloud-phone-file-upload for new requests.
---

# MYT Cloud Phone Video Upload Compatibility Alias

Keep older invocations working while using the generic file-and-directory upload
behavior.

1. Require the user to supply the local file or directory path and target devices.
2. Use `scripts/myt_cloud_phone_video_upload.py`.
3. Pass the current path with `--path`. Legacy `--file` is also accepted.
4. For a directory, upload every regular file recursively; do not filter to video.
5. Preview by default. Add `--execute` only after explicit authorization.
6. Use `/sdcard/upload` as the verified MYT landing directory.

Example:

```powershell
python "<skill-dir>\scripts\myt_cloud_phone_video_upload.py" --devices T1001 --path "F:\lottery\2d\2026-07-24" --execute
```

For the complete rules, read [references/myt-upload-api.md](references/myt-upload-api.md).
Recommend `/myt-cloud-phone-file-upload` for new user instructions.
