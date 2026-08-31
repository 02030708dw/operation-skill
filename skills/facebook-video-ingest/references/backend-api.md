# HM Facebook Video Ingest Contract

Use this reference when changing the Worker/backend protocol or diagnosing a rejected callback. The backend wraps successful data as `{"code": 200, "message": "success", "data": ...}`.

## Authentication

Send `X-HM-Worker-Token` on every internal request. Send `X-HM-Worker-Id` on the per-video endpoint. The token is environment-only and must never be logged.

## Worker Media Registration

Before claiming work, register the customer computer's backend-reachable media
address:

```http
POST /api/internal/capture/workers/register
Content-Type: application/json
X-HM-Worker-Token: <secret>

{"workerId":"hermes-worker-01","mediaBaseUrl":"http://192.168.1.20:8642"}
```

The backend accepts only explicit-port HTTP URLs whose host is a private IPv4
or `127.0.0.1`. It stores no Hermes API key. Both sides independently derive a
Worker-specific HMAC media token from `HM_WORKER_TOKEN` and `workerId`. Hermes
accepts that token only on GET/DELETE `/api/hm-capture/video`; every other
Gateway route continues to require `API_SERVER_KEY`.

Authenticated HM operators request
`GET /api/capture/videos/{videoNo}/preview`; HM checks ownership, resolves the
task's `hermes_worker_id`, and streams the file from that registered Worker.
After an operator has saved a rejection, HM calls
`DELETE /api/capture/videos/{videoNo}/local-file` and forwards the deletion to
the same Worker. A Worker 404 is treated as idempotent deletion success.

## Claim

```http
POST /api/internal/capture/executions/claim
Content-Type: application/json
X-HM-Worker-Token: <secret>

{"workerId":"hermes-worker-01","taskNo":"C-0123456789AB","executionNo":"E-0123456789ABCDEF"}
```

The normal customer-side scheduled Cron includes `taskNo`; an immediate one-shot
Cron includes both `taskNo` and `executionNo`. An unfiltered claim is reserved
for diagnostics and legacy continuous-receiver mode. When filters are present,
HM only returns the matching execution.
`data` is `null` when no matching job is queued. A claimed Facebook job contains:

```json
{
  "executionId": "E-0123456789ABCDEF",
  "taskId": "C-0123456789AB",
  "platform": "Facebook",
  "accountName": "PH Sports Official",
  "region": "PH",
  "sourceName": "PH Sports Official",
  "sourceUrl": "https://www.facebook.com/example/reels/",
  "category": "Sports",
  "r2Prefix": "PH/Sports/202608/10",
  "saveOriginal": true
}
```

The claim operation also returns expired `RUNNING` executions to `QUEUED` before selecting work. A live Worker must send heartbeats more frequently than the backend lease timeout. The Worker keeps a durable download manifest by execution ID, so a re-claimed lease can reuse verified local files instead of losing them to the downloader archive.

## Heartbeat

```http
POST /api/internal/capture/executions/{executionId}/heartbeat
X-HM-Worker-Token: <secret>
Content-Type: application/json

{"workerId":"hermes-worker-01","progress":55}
```

Progress is an integer from 0 through 100. The execution must still be `RUNNING` and owned by the same Worker ID.

## Per-video Result

```http
POST /api/internal/capture/executions/{executionId}/videos
X-HM-Worker-Token: <secret>
X-HM-Worker-Id: hermes-worker-01
Content-Type: application/json
```

The JSON body supports:

```json
{
  "platformVideoId": "123456",
  "sourceName": "PH Sports Official",
  "title": "video.mp4",
  "publishedAt": "2026-08-10T14:30:00",
  "originalUrl": "https://www.facebook.com/reel/123456",
  "canonicalUrl": "https://www.facebook.com/reel/123456",
  "localPath": "/data/facebook/video.mp4",
  "fileName": "video.mp4",
  "fileSize": 12345678,
  "fileSha256": "64-lowercase-hex-characters",
  "durationSeconds": 95,
  "downloadStatus": "DOWNLOADED",
  "uploadStatus": "UPLOADED",
  "r2Bucket": "media",
  "r2ObjectKey": "PH/Sports/202608/10/video.mp4",
  "r2Url": "https://media.example.com/PH/Sports/202608/10/video.mp4",
  "errorCode": null,
  "errorMessage": null,
  "metadataJson": "{...}"
}
```

Accepted download statuses are `DISCOVERED`, `DOWNLOADING`, `DOWNLOADED`, and `DOWNLOAD_FAILED`. Accepted upload statuses are `PENDING`, `UPLOADING`, `UPLOADED`, `SKIPPED_EXISTING`, `R2_CONFLICT`, and `UPLOAD_FAILED`. Initial capture callbacks always use `PENDING`; Cloudflare upload is forbidden until the operator approves the video.

The backend deduplicates videos by `(task_id, SHA-256(canonicalUrl))`. A second callback updates the same record, which is how the upload result enriches the earlier download record.

For a multi-video capture, the Worker sends this callback immediately after
each downloader `video-result` event instead of waiting for the whole child
process. Callback delivery uses transient retries in parallel with continuing
downloads. Before calling execution `complete`, the Worker reconciles the final
download manifest: already accepted canonical URLs are skipped and any failed
early callback is retried. If that final callback still fails, the Worker must
not call `complete`; the execution lease and durable final manifest provide the
safe retry path.

## Approved Upload Jobs

Claim an operator-approved upload with `POST /api/internal/capture/uploads/claim` and `{ "workerId": "...", "taskNo": "C-..." }`. The response includes the verified local path, SHA-256, category, and `r2Prefix`. Upload only that one file, then call `POST /api/internal/capture/uploads/{jobNo}/complete` with `status` set to `UPLOADED`, `SKIPPED_EXISTING`, `R2_CONFLICT`, or `UPLOAD_FAILED` plus the R2 fields. This is the only path that may publish a captured file to Cloudflare.

Every installed source Hermes runs a recurring no-agent `--upload-only` poller.
This outbound poller is the delivery path when an operator reviews from another
computer; the browser's loopback Hermes is only an optional same-machine
acceleration. Claim continues to require the task's owning Worker ID.

Keep the local file until the complete callback returns successfully. After an
accepted `UPLOADED` or `SKIPPED_EXISTING` callback, delete that job's exact
local file. Retain it for callback failure, `R2_CONFLICT`, or `UPLOAD_FAILED`
so the job can be inspected or retried.

Persist the R2 result and local path in the Worker's cleanup journal before
calling complete. The backend treats an exact repeat from the same Worker as an
idempotent success only when job status, upload status, R2 bucket/key/URL, and
error fields all match the stored terminal result. Any changed field or Worker
still returns conflict. After an accepted successful callback, delete the file
and remove the journal; replay an unfinished journal on later upload polls.

## Complete

```http
POST /api/internal/capture/executions/{executionId}/complete
X-HM-Worker-Token: <secret>
Content-Type: application/json
```

```json
{
  "workerId": "hermes-worker-01",
  "status": "COMPLETED",
  "progress": 100,
  "resultJson": "{...combined manifest...}",
  "rawOutput": "bounded child-process output",
  "errorCode": null,
  "errorMessage": null
}
```

Terminal statuses are `COMPLETED`, `PARTIAL`, `FAILED`, and `CANCELLED`. Only the owning Worker can complete a still-running execution.

## Operator Read APIs

Authenticated HM users can inspect:

- `GET /api/capture/tasks/{taskNo}` for task configuration and per-video download/R2 fields.
- `GET /api/capture/tasks/{taskNo}/executions` for execution status, progress, timestamps, error, and result JSON.
