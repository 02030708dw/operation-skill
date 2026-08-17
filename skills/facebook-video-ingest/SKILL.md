---
name: facebook-video-ingest
description: Run customer-side, on-demand Hermes Workers for backend-managed Facebook capture executions, download permitted new videos locally, upload verified files to Cloudflare R2, and report results to HM. Use when Hermes must install its loopback API, pair with the HM admin browser, create or run local Cron jobs, execute a targeted HM ingest job, or troubleshoot the local video pipeline while HM runs on another server.
---

# Facebook Video Ingest

Run the phase-one HM pipeline as one idempotent Worker: backend execution claim -> local download -> Cloudflare R2 upload -> backend result recording. Resolve `<skill-dir>` from this `SKILL.md` and invoke only its Python entry point.

## Boundaries

- Treat HM as the task schedule and execution source. The production backend queues work but never calls a server-local Hermes CLI.
- Keep the Hermes Gateway and its authenticated loopback API on the customer computer. Do not expose port 8642 beyond `127.0.0.1`.
- Create the capture Worker only when a local Cron fires. Do not keep a continuous HM polling Worker running in the normal production mode.
- Download only public content or content the user is authorized to save. Never bypass privacy, login, DRM, payment, or rate-limit controls.
- Read Worker and R2 secrets only from environment variables. Never print them or place them in command arguments.
- Keep local files after upload. Do not delete, repost, publish, or change R2 access policies.
- Let the downloader and uploader enforce duplicate rules. Do not manually rewrite their archives or overwrite an R2 conflict.

## Required Configuration

Configure these values in the environment used by Hermes:

```text
HM_BACKEND_URL=https://backend.example.com
HM_WORKER_ID=hermes-worker-01
HM_WORKER_TOKEN=<backend worker token>
CLOUDFLARE_R2_ACCOUNT_ID=<account id>
CLOUDFLARE_R2_ACCESS_KEY_ID=<access key id>
CLOUDFLARE_R2_SECRET_ACCESS_KEY=<secret access key>
CLOUDFLARE_R2_BUCKET=<bucket>
```

The entry point and Cron runner also load missing values from
`<hermes-home>/.env`. Keep that file private; process environment values take
precedence and secret values are never printed by readiness checks.

Use a unique, stable `HM_WORKER_ID` for each customer computer. The installer
includes that non-secret identity in the browser pairing code so HM can bind
new tasks to the correct local Hermes instance.

Optional values:

```text
HM_WORKER_POLL_SECONDS=15
HM_WORKER_HEARTBEAT_SECONDS=30
HM_INGEST_STATE_DIR=<durable local state directory>
CLOUDFLARE_R2_PUBLIC_BASE_URL=https://media.example.com
```

The backend `CAPTURE_WORKER_TOKEN` and local `HM_WORKER_TOKEN` must contain the same secret. Do not include either value in chat or logs.

## Entry Point

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" <arguments>
```

The entry point composes the sibling `facebook-followed-video-download` and `cloudflare-r2-video-upload` Skills. Install all three complete directories, including scripts and dependencies.

The backend Worker passes `--initial-count 10` to the downloader. The first execution for a source requests at most 10 recent videos. Later executions use the downloader archive as the previous-run boundary and select every newer video without a count limit: two updates selects two and 30 updates selects all 30. When there are no updates, the downloader falls back to the 10 most recent discovered videos. If Facebook exposes no public video URLs at all, treat the execution as failed rather than reporting a false successful no-update result.

The Hermes Cron runner resolves only `<hermes-home>/skills/facebook-video-ingest/scripts/facebook_video_ingest.py`. It must fail clearly if that installed Skill is missing; never fall back to a categorized copy or a recursive match.

## Operations

### Check Readiness

Run this before starting a Worker or after changing its environment:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --check
```

This does not claim a job. Report every failed check by name without revealing secret values.

### Run One Execution

Use this for acceptance tests, manual operation, or one-shot Hermes invocations:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --execute --json
```

It claims at most one queued Facebook execution. A `no-work` result is successful and requires no retry.

When a prompt names a backend task number, always target that task and wait briefly for HM's scheduler to enqueue the matching execution:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --execute --task-no "<task-no>" --wait-for-work-seconds 30 --json
```

Never omit `--task-no` when the invoking Hermes Cron job or user supplied one; targeted jobs must not consume another backend task's execution.

For an immediate HM execution, target both identifiers so an older scheduled execution for the same task cannot be claimed first:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --execute --task-no "<task-no>" --execution-no "<execution-no>" --wait-for-work-seconds 30 --json
```

### Run The Legacy Continuous Worker

Use this only for temporary diagnostics or migration from the old polling architecture:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --watch
```

Do not install this mode as a service. Stop it with `Ctrl+C`; do not start a second copy with the same Worker ID while the first is active.

The Worker holds a local lock. A duplicate `--watch` process exits successfully
instead of claiming the same queue concurrently.
The legacy receiver writes operational output to
`<hermes-home>/facebook-video-ingest/receiver.log`; empty queue polls are silent.
Its backend client sends an explicit `HM-Hermes-Worker` user agent so Cloudflare
does not reject Python's default `urllib` fingerprint with error 1010.

### Install The Customer-Side Hermes Bridge

On the customer computer, run the installer once:

```text
python "<skill-dir>/scripts/install_hermes_worker.py"
```

The installer is idempotent. It installs dependencies and the deterministic
runner, configures the authenticated Hermes API at `127.0.0.1:8642`, restricts
CORS to the configured HM admin origins, starts or restarts Hermes Gateway, and
prints an `HMHERMES1.` pairing code containing the configured Worker ID. Paste
that code into the HM admin browser;
the API key stays in that browser's local storage and is never sent to HM.

The installer removes the old `HM 后台任务接收 Worker`, `HM 视频抓取 Worker`,
and stale `HM 立即抓取 C-*` jobs, and stops the old continuous `--watch`
process on POSIX machines. Preserve `HM 视频抓取 C-*` scheduled jobs during
migration so the browser can update or remove them safely.

When an operator creates a task, the HM browser calls `POST /api/jobs` on the
same computer and creates one visible five-field Hermes Cron for each configured
start time. Clicking “立即执行” first queues an exact backend execution, then
creates a future one-shot (`repeat=1`) local no-agent Cron whose trusted runner
filename contains both the task and execution numbers. The browser calls
`POST /api/jobs/{job_id}/run` immediately, so the job does not wait for a daily
time slot. Never use a recurring every-minute schedule for this one-shot: a
long first run can overlap the following minute before its repeat count is
committed. Use the deterministic no-agent Cron route instead of a generic
`/v1/runs` or LLM-backed Cron run: the local script must still execute when the
configured model provider is unavailable. The Cron creates an operator-visible
task and records its final status and error. The Worker exits after reporting
completion.

## Execution Rules

1. Run `--check` before first execution. Fix missing scripts, `yt-dlp`, Node `ws`, `boto3`, Chrome, backend settings, or R2 settings before claiming work.
2. Claim only through the HM internal Worker API. For a scheduled local Cron, include its backend task number. For immediate execution, include both the backend task and execution numbers. Never invent an execution ID, source URL, or result.
3. Use the globally unique account name supplied by HM as the local source name. Use the backend-provided `r2Prefix` unchanged; it has the form `PH/Sports/yyyyMM/dd` and is derived from the task region, category, and execution date.
4. Record the download result before starting R2 upload, then update the same backend video record with its R2 result.
5. Keep a periodic heartbeat active while child processes run. The backend may return an expired execution to the queue after its configured lease timeout.
6. Keep `<state-dir>/<executionId>/download.json` until operational retention removes the whole completed execution directory. A lease retry reuses this manifest only while every downloaded file still exists at its recorded byte size; the R2 step also verifies its recorded SHA-256 before upload.
7. Treat same-key/same-size R2 objects as `SKIPPED_EXISTING`, different-size objects as `R2_CONFLICT`, and verified new objects as `UPLOADED`.
8. Complete every claimed execution as `COMPLETED`, `PARTIAL`, or `FAILED`. If a backend callback fails, leave the execution running for lease retry and do not claim that HM recorded completion.
9. Do not impose a short overall timeout. Monitor the same active process rather than starting a duplicate.

Read [references/backend-api.md](references/backend-api.md) before changing the backend contract or diagnosing claim, heartbeat, callback, or lease behavior.

## Result Interpretation

- `COMPLETED`: every selected/reused video and upload succeeded, or the source exposed no videos.
- `PARTIAL`: at least one R2 upload succeeded or was already present, and at least one item failed or conflicted.
- `FAILED`: no item completed and the download, upload, or orchestration failed.
- `no-work`: no queued Facebook execution was available; the continuous Worker waits and polls again.

Use the HM task detail API for per-video fields and the execution-history API for progress, terminal status, error, and combined result JSON.
