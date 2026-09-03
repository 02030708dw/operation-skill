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

Use Node.js 12.22.0 or newer. The entry point checks the actual Node version and
runs `node --check` against every runtime JavaScript file before reporting the
download runtime ready.

Install the local Node dependency once from the Skill's scripts folder:

```text
npm install
```

Install `yt-dlp` through the user's normal package manager and make it available on `PATH`, or pass its executable path with `--yt-dlp`.

The engine auto-detects common Google Chrome and Chromium locations on Windows, macOS, and Linux. Use `--chrome` only when auto-detection fails. By default Chrome chooses a free CDP port and the engine reads its own `DevToolsActivePort`; use `FACEBOOK_FOLLOWED_CDP_PORT` only for a controlled diagnostic override.

Windows readiness checks read the Chrome executable's version information without
starting it. If the Chrome check fails, verify that the configured executable exists
and has readable Windows version information. Browser startup is checked separately
when the engine opens its isolated headless CDP session. There is no need to select
a personal Google account or change Chrome's profile-picker startup setting.

## Reports

Actual executions write:

- `reports/runs/<mode>-<timestamp>.log`
- `reports/<timestamp>-<mode>.md`
- `reports/<timestamp>-<mode>.json`
- `reports/latest.md`
- `reports/latest.json`

Preview mode does not create reports.
