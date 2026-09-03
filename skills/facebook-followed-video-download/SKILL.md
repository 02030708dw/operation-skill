---
name: facebook-followed-video-download
description: Find, download, or locally reuse recent permitted Facebook Page, creator, Reels, watch, or direct video URLs in per-source folders with archive-backed duplicate prevention and reports. Use when the user asks Hermes to configure followed Facebook video sources, preview recent videos, download the latest or all videos, list sources, check dependencies, or troubleshoot this downloader. Defaults to preview and downloads only with explicit execution approval.
metadata:
  version: "1.7.2"
  platforms:
    - windows
    - macos
    - linux
  prerequisites:
    commands:
      - python
      - node
      - npm
      - yt-dlp
  hermes:
    tags:
      - facebook
      - video
      - download
      - media
    requires_tools:
      - terminal
---

# Facebook Followed Video Download

Manage a portable list of permitted Facebook sources, discover new videos, avoid duplicate downloads, and produce local reports. Resolve `<skill-dir>` from the directory containing this loaded `SKILL.md`; never assume a drive letter, username, or fixed Hermes installation path.

## Safety

- Download only public content or content the user is explicitly allowed to save.
- Do not bypass private pages, paid access, DRM, login restrictions, rate limits, or other access controls.
- Never ask the user to paste cookie contents, session tokens, passwords, or private browser data into chat.
- An already-created, locally authorized Netscape-format cookies file may be referenced by path with `--cookies`; never display its contents.
- Treat downloading as an external action. Preview by default and add `--execute` only when the user clearly asks to download or says to execute immediately.
- Do not upload, repost, publish, or share downloaded files unless the user separately asks.

Read [references/facebook-download-notes.md](references/facebook-download-notes.md) before diagnosing discovery or access failures.

## Entry Point

Use the same entry point for Windows, macOS, and Linux:

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" <arguments>
```

Paths with `/` are intentional and work with Python on Windows. Do not call the JavaScript engine directly unless troubleshooting.

## Defaults

- State: `<hermes-home>/facebook-followed-video-download/`
- Sources: `<state>/accounts.txt`
- Reports: `<state>/reports/`
- Downloads: the current user's `Desktop/Facebook/<source-folder>/`
- Daily mode: the first execution selects at most 10 recent videos; later executions select every update before the previous archive boundary, or fall back to the 10 most recent videos when there are no updates
- Daily scanning: up to 80 adaptive scroll rounds, stopping early after the first archived video or three rounds without new links
- Full mode: unlimited new videos per source and 80 scroll rounds
- Execution: dry run unless `--execute` is present
- Login profile: disabled until the user explicitly runs `--login`; stored only in the isolated Skill state directory
- Concurrency: runs sharing the isolated Chrome profile are serialized with an OS-released lock; overlapping schedules wait instead of launching a competing Chrome
- Archived fallback: a recent-window item already present in yt-dlp's archive with no retained local file is reported as `archived-existing`; it is not downloaded again and is not a failure

The entry point infers `<hermes-home>` from its installed location. All defaults can be overridden with arguments, so the Skill is portable across computers.

## Source File

Each active line must contain a folder name, a literal TAB, and a Facebook URL:

```text
folder-name<TAB>facebook-url
```

Blank lines and lines beginning with `#` are ignored. Folder names are sanitized before filesystem use.

## Intent Mapping

Map the user's words to arguments:

- “预演、看看、查找、不要下载” -> omit `--execute`
- “立即执行、开始下载、下载” -> add `--execute`
- “最新、每日、新增” -> `--mode daily`
- “全部、首次导入、全量” -> `--mode full`
- “首次每个来源 N 个” -> `--initial-count N`
- “首次不限制数量” -> `--initial-count 0`
- “详细” -> `--verbose`
- A custom destination -> `--output "<path>"`

`--initial-count` applies independently to every configured source and defines the recent-video fallback window (`--count` remains a compatibility alias). Never silently replace the user's number with a fixed value. The first execution selects that recent window. Later executions select every newest-first URL before the first archived video: two updates means two selected and 30 updates means all 30 selected. When there are no updates, the Skill selects the 10 most recent discovered videos instead.

Use `--max-duration-seconds 1200` for HM ingestion. The downloader asks yt-dlp for duration metadata before downloading and records longer videos as `filtered-duration`; it must not download those files.

## Common Operations

### Check Readiness

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --check
```

Report the source count and whether preview and execution are ready. `yt-dlp` is required only for actual downloads. If the `ws` module is missing, run `npm install` inside `<skill-dir>/scripts`, then check again.

For a source-independent execution preflight, including Node.js 12.22+, both
runtime JavaScript syntax checks, `ws`, Chrome, and `yt-dlp`, run:

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --runtime-check
```

`runtimeReady: true` is required before a backend Worker claims work.

On Windows, the Chrome preflight reads the executable's version resource through
the system API without launching Chrome. A missing executable or unreadable
version resource fails readiness. The engine then verifies actual browser startup
through its isolated headless CDP session; it reports `CHROME_CDP_START_FAILED`
if that session cannot start. Normal checks and captures do not open the user's
Chrome profile picker. Explicit `--login` still opens the dedicated login window.

### Authorize An Isolated Facebook Login

Only after the user explicitly authorizes browser login, run:

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --login
```

This opens Facebook in a dedicated Chrome profile under the Skill state directory. The user enters credentials directly on `facebook.com`, verifies the configured source is visible, and closes the dedicated window. The Skill never prints the password or cookie values. Backend runs reuse only this isolated profile; they never reuse the user's normal Chrome profile.

### Initialize Sources

Only run this when the user asks to initialize or configure the Skill:

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --init
```

This creates an example `accounts.txt` only when one does not already exist.

### Add A Source

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --add-source "source-folder" "https://www.facebook.com/example/reels/"
```

This explicitly writes the source file. If the folder name already exists, its URL is replaced.

### List Sources

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --list-sources
```

### Preview Latest Videos

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --mode daily --count 3 --verbose
```

Preview scans Facebook but does not create download folders, media files, archives, or reports.

### Download Latest Videos

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --mode daily --count 3 --execute
```

### Run One Backend-Supplied Source

Use `--source` when an orchestrator supplies one source for this execution. This does not edit the persistent source file:

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --source "C-0123456789AB" "https://www.facebook.com/example/reels/" --mode daily --initial-count 10 --execute --execution-id "E-0123456789ABCDEF" --result-json "<result.json>"
```

`--result-json` writes the machine-readable download manifest even when the engine fails. Each successfully downloaded video includes its canonical URL, local path, byte size, SHA-256, and Facebook publish time when yt-dlp exposes it. `publishedAtPrecision` is `SECOND` when `timestamp` or `release_timestamp` provides an exact time, and `DATE` when only `upload_date` is available; a `DATE` value uses `T00:00:00` as a placeholder and must not be presented as an exact midnight publish time. Use this manifest as the input to the R2 Skill; do not rediscover the output directory.

`archived-existing` means yt-dlp previously completed that video and the local file is no longer retained, normally because the approved upload flow already removed it after an accepted R2 callback. It advances the scan but does not count as a new download or a failure. Never clear the archive to retry this state.

When a trusted orchestrator sets `HM_VIDEO_RESULT_EVENTS=1`, the entry point
also flushes one structured stdout line immediately after each selected video
finishes downloading, fails, or is filtered. Each line starts with
`__HM_VIDEO_RESULT__:` and contains a `video-result` JSON object with the
source, completed/total counts, and the same video fields written to the final
manifest. This stream is an early-notification channel only; the final
`--result-json` manifest remains authoritative and must still be reconciled.
The protocol lines are deliberately excluded from human run logs and HM
`rawOutput`.

### Initial Full Import

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --mode full --count 0 --execute
```

Before a large first import, prefer a preview or a low count unless the user explicitly requested the full set.

## Execution Rules

1. If the user supplied all required details and explicitly requested execution, run the download directly; do not add an unnecessary confirmation.
2. If no source is configured, stop and ask for permitted Facebook source URLs. Do not invent sources.
3. Use one command for all configured sources. The downloader handles them in the same run and keeps a separate archive per source.
4. Do not impose a short fixed terminal timeout. A valid large download should continue until it finishes. If the terminal interface yields while the process is still running, monitor the same process rather than starting a duplicate run.
5. A genuine failure is a stopped process, missing dependency, repeated page discovery failure, inaccessible/irrelevant page, or a nonzero final exit—not merely a long normal download.
6. Never rerun an uncertain active process. Check whether it is still running and inspect the run log first.
7. On success, summarize each source's found, pending, and downloaded counts and provide the output and `latest.md` report paths.

## Duplicate Prevention

Each source output folder keeps:

- `.fb-video-urls.txt` for URLs successfully handled by this workflow
- `.yt-dlp-archive.txt` for media IDs recorded by `yt-dlp`

Do not delete or rewrite these files during routine use. A dry run never appends to either archive. The first daily execution is detected when both archives are empty; afterward the newest archived video is the boundary separating current updates from older backlog.

## Troubleshooting

- `ready_for_preview: false`: inspect `node`, `ws_module`, the accounts file, and its source count.
- `ready_for_execute: false`: install or configure `yt-dlp`.
- Chrome not found: pass `--chrome "<executable-path>"` or set `FACEBOOK_FOLLOWED_CHROME`.
- `CHROME_CDP_START_FAILED`: Chrome exited early or its dynamic CDP endpoint did not become ready after one cleanup/retry. Check stale Chrome processes, profile locks, available memory, and the bounded startup detail.
- `CDP_RUNTIME_TIMEOUT`: Facebook did not answer the bounded page extraction after the page session was rebuilt and retried once. Check page responsiveness, network, and machine load.
- `FACEBOOK_ACCESS_REQUIRED`: Facebook returned a login, verification, checkpoint, or access page. Handle it only through the explicitly authorized isolated profile; never bypass the control.
- `FACEBOOK_LAYOUT_UNSUPPORTED`: video elements are visible but the current public layout exposes none of the supported links.
- `FACEBOOK_DISCOVERY_EMPTY`: the reachable public page exposed no supported video link. This differs from a successful no-update run.
- Direct Reel URLs work but Page scanning finds nothing: treat this as a discovery limitation; do not claim the Page contains no videos.
- Zero discovered URLs is a discovery failure, not a successful no-update result. The recent-video fallback requires at least one publicly discoverable URL.
- Access/login errors: stop unless the user has an authorized, non-bypassing access method.
- Large jobs: reduce `--count` only when the user agrees; do not mislabel normal runtime as a timeout.

Read [references/configuration.md](references/configuration.md) for portable paths and environment overrides.
