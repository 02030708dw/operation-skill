---
name: facebook-video-ingest
description: Claim backend-managed Facebook capture executions, download permitted new videos locally, upload verified files to Cloudflare R2, and report per-video plus execution results back to HM. Use when Hermes must check, run once, continuously watch, operate, or troubleshoot the HM phase-one video ingest worker.
---

# Facebook Video Ingest

Run the phase-one HM pipeline as one idempotent Worker: backend execution claim -> local download -> Cloudflare R2 upload -> backend result recording. Resolve `<skill-dir>` from this `SKILL.md` and invoke only its Python entry point.

## Boundaries

- Treat HM as the task configuration and execution source. HM manages one visible, task-targeted Hermes no-agent Cron script per configured start time; do not manually create duplicate schedules.
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

The Hermes Cron runner resolves only `<hermes-home>/skills/operation-skill/facebook-video-ingest/scripts/facebook_video_ingest.py`. It must fail clearly if that installed operation-skill is missing; never fall back to a legacy skill directory or a recursive match.

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

### Run The Continuous Worker

Use this for normal automatic operation:

```text
python "<skill-dir>/scripts/facebook_video_ingest.py" --watch
```

Keep this process under the machine's normal service supervisor. Stop it with the supervisor or `Ctrl+C`; do not start a second copy with the same Worker ID while the first is active.

### Prepare Hermes Cron Execution

For desktop operation, run the installer once to start the Hermes Gateway and verify the local Worker runtime:

```text
python "<skill-dir>/scripts/install_hermes_worker.py"
```

The installer is idempotent. It starts the Hermes Gateway as a login service and installs the no-agent runner under `~/.hermes/scripts/`. HM creates or updates visible task-specific Cron jobs with five-field Cron expressions such as `30 14 * * *`. Clicking “立即执行” creates and triggers a separate one-shot job bound to that backend execution number; it does not wait for or reuse the daily time slot.

## Execution Rules

1. Run `--check` before first execution. Fix missing scripts, `yt-dlp`, Node `ws`, `boto3`, Chrome, backend settings, or R2 settings before claiming work.
2. Claim only through the HM internal Worker API. Never invent an execution ID, source URL, or result.
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
