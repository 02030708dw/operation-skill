# Portable Configuration

The Python entry point accepts arguments first and also supports environment variables. Prefer command arguments for one-off Hermes tasks.

## Standard Variables

- `HERMES_HOME`: Hermes data root
- `FACEBOOK_FOLLOWED_STATE_DIR`: source list, reports, and run logs
- `FACEBOOK_FOLLOWED_ACCOUNTS`: source-list file
- `FACEBOOK_FOLLOWED_OUTPUT`: download root
- `FACEBOOK_FOLLOWED_REPORTS`: report folder
- `FACEBOOK_FOLLOWED_COOKIES`: optional authorized cookies file
- `FACEBOOK_FOLLOWED_CHROME`: Chrome or Chromium executable
- `FACEBOOK_FOLLOWED_YTDLP`: `yt-dlp` executable
- `FACEBOOK_FOLLOWED_CDP_PORT`: optional local Chrome debugging port

Legacy `FB_FOLLOWED_*` variables remain accepted where practical so an older installation can migrate without exposing or copying credentials.

## Dependencies

Install the local Node dependency once from the Skill's scripts folder:

```text
npm install
```

Install `yt-dlp` through the user's normal package manager and make it available on `PATH`, or pass its executable path with `--yt-dlp`.

The engine auto-detects common Google Chrome and Chromium locations on Windows, macOS, and Linux. Use `--chrome` only when auto-detection fails.

## Reports

Actual executions write:

- `reports/runs/<mode>-<timestamp>.log`
- `reports/<timestamp>-<mode>.md`
- `reports/<timestamp>-<mode>.json`
- `reports/latest.md`
- `reports/latest.json`

Preview mode does not create reports.
