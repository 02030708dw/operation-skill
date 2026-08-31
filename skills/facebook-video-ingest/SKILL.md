---
name: facebook-video-ingest
description: Run customer-side Hermes Workers for backend-managed Facebook capture executions, filter videos over 20 minutes before download, hold videos for operator review or honor task-level backend auto-review, upload approved files to Cloudflare R2, consume rejected-file deletion jobs on the source computer, and report results to HM. Use when Hermes must install its local API, pair with the HM admin browser, create or run local Cron jobs, execute a targeted HM ingest job, or troubleshoot the local video pipeline while HM runs on another server.
---

# Facebook Video Ingest

Run the HM review-gated pipeline as one idempotent Worker: backend execution claim -> duration check -> local download -> backend review decision. By default HM records the file as pending review; when that task's backend auto-review switch is enabled, HM may approve a newly downloaded file and enqueue it automatically. Separately claim backend-approved upload jobs and rejected-file deletion jobs on the source Hermes, then report each result to HM. Resolve `<skill-dir>` from this `SKILL.md` and invoke only its Python entry point.

## Boundaries

- Treat HM as the task schedule and execution source. The production backend queues work but never calls a server-local Hermes CLI.
- Keep browser pairing on `127.0.0.1`. The installer may bind Hermes Gateway to
  `0.0.0.0:8642` only to let the HM backend reach the exact GET
  `/api/hm-capture/video` preview route through a dedicated derived media token.
  Manual local deletion does not use an inbound backend-to-Hermes DELETE call;
  the source Hermes claims it from HM. Use the Gateway listener only on a
  trusted LAN/VPN and restrict the host firewall to the HM backend. All other
  Hermes routes still require `API_SERVER_KEY`.
- Create capture Workers only when their task Cron fires. Keep the installer's
  no-agent review-queue poller enabled so uploads and local deletions requested
  from any computer are processed by the Hermes machine that owns the source
  file.
- Download only public content or content the user is authorized to save. Never bypass privacy, login, DRM, payment, or rate-limit controls.
- Read Worker and R2 secrets only from environment variables. Never print them or place them in command arguments.
- Retain local files while review or upload can still be retried. Delete a
  rejected file only after the source Hermes claims HM's local-delete queue job;
  validate the path against the configured ingest roots and report `DELETED`,
  `NOT_FOUND`, or `DELETE_FAILED`. Delete an approved file only after R2 reports `UPLOADED` or
  `SKIPPED_EXISTING` and HM accepts the upload callback. Keep the file on
  conflict, upload failure, or callback failure.
- Let the downloader and uploader enforce duplicate rules. Do not manually rewrite their archives or overwrite an R2 conflict.
- Never infer automatic approval locally. Hermes reports the download result and
  uploads only jobs returned by HM; the task-level `autoReviewEnabled` flag is
  informational and HM remains the only authority that changes review state.

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

Optional values:

```text
HM_WORKER_POLL_SECONDS=15
HM_WORKER_HEARTBEAT_SECONDS=30
HM_INGEST_STATE_DIR=<durable local state directory>
HM_CAPTURE_MEDIA_BASE_URL=http://<customer-private-ip>:8642
CLOUDFLARE_R2_PUBLIC_BASE_URL=https://media.example.com
```

The backend `CAPTURE_WORKER_TOKEN` and local `HM_WORKER_TOKEN` must contain the same secret. Do not include either value in chat or logs.

## Entry Point

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" <arguments>
```

The entry point composes the sibling `facebook-followed-video-download` and `cloudflare-r2-video-upload` Skills. Install all three complete directories, including scripts and dependencies.

The backend Worker passes `--initial-count 10` and `--max-duration-seconds 1200` to the downloader. Videos longer than 20 minutes must be filtered from metadata before download and must not be recorded as captured videos. The first execution for a source requests at most 10 recent videos. Later executions use the downloader archive as the previous-run boundary and select every newer video without a count limit: two updates selects two and 30 updates selects all 30. When there are no updates, the downloader falls back to the 10 most recent discovered videos. If Facebook exposes no public video URLs at all, treat the execution as failed rather than reporting a false successful no-update result.

During an active batch, opt in to the downloader's structured per-video stdout
events and send each terminal download result to HM as soon as that file is
ready. Process these callbacks on a separate delivery thread so a slow backend
does not stop later downloads. The final download manifest remains
authoritative: reconcile it before completing the execution, skip callbacks
already accepted by HM, and retry any streamed callback that failed. Because HM
upserts by the canonical video URL, uncertain or repeated callbacks remain
idempotent. If final reconciliation still cannot reach HM, leave the execution
running for its lease retry rather than reporting completion.

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

To diagnose the approved-upload and rejected-file deletion queues without
claiming capture work, use:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --watch --upload-only
```

The installer normally provides equivalent reliable polling through a recurring
no-agent Cron, so do not also leave this diagnostic process running.

### Install The Customer-Side Hermes Bridge

On the customer computer, run the installer once:

```text
python "<skill-dir>/scripts/install_hermes_worker.py"
```

The installer is idempotent. It installs dependencies and the deterministic
capture/upload runners, keeps browser pairing at `127.0.0.1:8642`, binds the authenticated
Gateway to the local network, derives a Worker-specific media token from
`HM_WORKER_TOKEN`, and registers `HM_CAPTURE_MEDIA_BASE_URL` with HM. It
auto-detects the private IPv4; when a computer has multiple network adapters,
pass `--media-base-url http://<private-ip>:8642`. It restricts CORS to the
configured HM admin origins, starts or restarts Hermes Gateway, and prints an
`HMHERMES1.` pairing code. The full API key stays only in that browser's local
storage and is never sent to HM; the backend can use only the restricted media
token it derives independently.

The installer also maintains one `HM 审核上传队列 Worker` no-agent Cron that
polls every minute. Despite its legacy name, `--execute --upload-only` drains
local-delete jobs before approved uploads to free disk space promptly, never
claims capture executions,
and is the correctness path for cross-computer review. A browser on the source
computer may still start a one-shot upload as a latency optimization.

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
   The check also refreshes the Worker media-address registration without
   printing either Worker or media credentials.
2. Claim only through the HM internal Worker API. For a scheduled local Cron, include its backend task number. For immediate execution, include both the backend task and execution numbers. Never invent an execution ID, source URL, or result.
3. Use the globally unique account name supplied by HM as the local source name. Use the backend-provided `r2Prefix` unchanged; it has the form `PH/Sports/yyyyMM/dd` and is derived from the task region, category, and execution date.
4. Record every download result through HM and upload only jobs claimed from
   `/api/internal/capture/uploads/claim`. With automatic review disabled, the
   backend keeps a new download in `PENDING_REVIEW` until an operator approves
   it. With task-level automatic review enabled, HM may approve that new record
   and enqueue it without an operator action. Hermes must never manufacture an
   approval or upload an unclaimed local file.
5. The recurring source-Hermes upload Cron drains approved jobs at least once a
   minute. The HM browser may additionally start a one-shot `--upload-only
   --task-no <task-no>` runner only when its paired Worker ID matches the video's
   owning Worker. Never rely on the reviewing computer's `127.0.0.1` Hermes for
   cross-computer delivery.
6. Keep a periodic heartbeat active while child processes run. The backend may return an expired execution to the queue after its configured lease timeout.
   Advance its in-memory progress as per-video terminal events arrive; the
   heartbeat thread must remain active through final callback reconciliation.
7. Keep `<state-dir>/<executionId>/download.json` until operational retention removes the whole completed execution directory. A lease retry reuses this manifest only while every downloaded file still exists at its recorded byte size; the R2 step also verifies its recorded SHA-256 before upload.
8. Treat same-key/same-size R2 objects as `SKIPPED_EXISTING`, different-size objects as `R2_CONFLICT`, and verified new objects as `UPLOADED`.
9. After HM accepts a successful approved-upload callback, delete that job's
   exact local video path. Do not delete it before the callback, and retain it
   for `R2_CONFLICT`, `UPLOAD_FAILED`, or callback failure. Persist the terminal
   R2 result in the job cleanup journal before the callback; each upload poll
   must replay unfinished callbacks and local deletes. Repeating an identical
   terminal callback is safe, but a different Worker or result must remain a
   conflict.
10. Claim rejected-file deletion only from
    `/api/internal/capture/local-deletes/claim`. Accept only video paths beneath
    `HM_INGEST_STATE_DIR` or the configured `Desktop/Facebook` roots, and reject
    a symbolic link in the final file or any descendant path component. Report
    a missing allowed file as `NOT_FOUND`; report an unsafe path or unlink
    failure as `DELETE_FAILED`. Stop the current drain after a failed deletion
    so a backend retry policy cannot make one Cron invocation loop forever. A
    missing or incompatible local-delete API must be reported in
    `localDeleteError` without preventing approved uploads in the same poll.
11. Complete every claimed execution as `COMPLETED`, `PARTIAL`, or `FAILED`. If a backend callback fails, leave the execution running for lease retry and do not claim that HM recorded completion.
12. Do not impose a short overall timeout. Monitor the same active process rather than starting a duplicate.

Read [references/backend-api.md](references/backend-api.md) before changing the backend contract or diagnosing claim, heartbeat, callback, or lease behavior.

## Result Interpretation

- `COMPLETED`: every selected/reused video downloaded successfully and is waiting for review or was automatically queued by HM, or the source exposed no videos.
- `PARTIAL`: at least one selected video downloaded and entered review, while at least one other selected video failed to download.
- `FAILED`: no item completed and the download, upload, or orchestration failed.
- `no-work`: no queued Facebook execution, approved upload, or local-delete job was available; the continuous Worker waits and polls again.

Use the HM task detail API for per-video fields and the execution-history API for progress, terminal status, error, and combined result JSON.
