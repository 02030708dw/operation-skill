---
name: youtube-video-downloader
description: Download YouTube videos and Shorts locally from individual links, playlists, or channel Shorts pages, with bounded batches, resumable downloads, deduplication and JSON reports. Use for YouTube 视频下载、频道首页批量下载、Shorts 抓取; not metadata-only analytics or publishing.
metadata:
  version: "1.1.1"
  platforms:
    - linux
    - macos
  hermes:
    tags:
      - youtube
      - video
      - download
    requires_tools:
      - terminal
---

# YouTube 视频下载

Use `scripts/download.py`. “首页批量下载” means downloading from the supplied channel page, not building a website. Process content the user has permission to download. Browser playback and incognito mode do not guarantee download success.

## Setup

Resolve `<skill-dir>` from this loaded `SKILL.md`, whether installed by Hermes or located at `operation-skill/skills/youtube-video-downloader`. Linux and macOS are supported (the download lock uses `fcntl`). Python 3.10+ and Node.js 22+ or supported Deno must be installed.

```bash
bash "<skill-dir>/scripts/setup.sh"
```

The setup script creates an isolated environment at `${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/youtube-video-downloader/venv` (override with `YOUTUBE_DOWNLOADER_VENV`). Keep this environment, downloads and credentials outside the managed skill directory so updates do not package or replace runtime state. Prefer system FFmpeg; the script falls back to the imageio-ffmpeg binary in the environment.

For server installation, credentials and concrete commands, read [server-usage.md](references/server-usage.md).

## Workflow

1. Download a requested single test video before a channel batch. Default output: `~/Downloads/YouTube`; maximum frame height: 1080 pixels (portrait 1080×1920 requires `--height 1920`). Bare channel URLs are normalized to the Shorts tab.
2. Preview a batch with `--list-only`. Default scope is the first 10 entries returned by the source, including previously completed videos. State this scope when quantity is unspecified. Use `--limit N` to adjust; use `--all` only for an explicitly requested full collection. A limit counts inspected entries, not successful new downloads.
3. Try without credentials first. If login is needed, use a user-authorized regular browser profile via `--browser chrome:Default`, or an explicitly provided local Netscape Cookie file via `--cookies FILE`. Never assume permission to access unrelated credentials. Incognito sessions cannot be read using `--browser chrome`. Read [authentication.md](references/authentication.md) when authentication is needed.
4. The script runs sequentially with bounded retries and stops on authentication/rate-limit errors. Reuse the output directory for deduplication. Successful video IDs are stored in `.download-archive.txt`; partial files remain resumable. To restore a deleted video, use `--redownload` with its single URL.
5. Inspect `.reports/` and actual files. Verify the first media file by playback or an FFmpeg decode. A list-only result is not a media download. Report downloaded, skipped, failed and unattempted counts separately. Do not promise success while login remains blocked.

## Usage

```bash
# Single video
"${YOUTUBE_DOWNLOADER_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/youtube-video-downloader/venv}/bin/python" \
  "<skill-dir>/scripts/download.py" \
  'https://www.youtube.com/shorts/0ixRDhyAsMY'

# Preview first 10 channel Shorts
"${YOUTUBE_DOWNLOADER_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/youtube-video-downloader/venv}/bin/python" \
  "<skill-dir>/scripts/download.py" \
  'https://www.youtube.com/@Wimbledon/shorts' --limit 10 --list-only

# Download first 10, skip completed IDs
"${YOUTUBE_DOWNLOADER_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/operation-skill/youtube-video-downloader/venv}/bin/python" \
  "<skill-dir>/scripts/download.py" \
  'https://www.youtube.com/@Wimbledon/shorts' --limit 10
```

Optional: `--output DIR`, `--height 720`, `--browser chrome:Default`, `--cookies FILE`, `--subtitles`, `--sub-langs 'en.*,zh.*'`, `--thumbnail`, `--ffmpeg /usr/bin/ffmpeg`.

MP4/H.264/AAC are preferred with other available codecs as fallback. Reports retain only selected metadata, never raw extraction JSON or signed media URLs. Do not schedule, deploy or publish unless asked.

## VN public-first server policy

When the trusted regional registry sets `capturePolicy: PUBLIC_FIRST` (VN only),
`facebook-video-ingest/scripts/hm_public_capture.py` runs public discovery/download
without cookies or saved browser profiles. A confirmed login requirement permits
one supplement with an already available regional account. Missing/expired/busy
accounts end that item rather than waiting. Rate limits or security challenges
stop the batch; they never trigger an account switch. Public success does not
prove account login. Durable item receipts precede callbacks, and execution logs
include anonymous/authenticated attempts and separate downloaded/skipped/failed/
unattempted counts (unknown when source enumeration failed). Other regions and
standalone CLI behavior remain unchanged.
