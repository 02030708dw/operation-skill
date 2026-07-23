# Upload Behavior

## Multipart Transfers

The script uses boto3's managed S3 transfer:

- Multipart threshold: 64 MiB by default
- Multipart chunk: 16 MiB by default
- Per-file multipart workers: 4 by default
- Concurrent files: 3 by default

Cloudflare R2 requires multipart parts to be at least 5 MiB except for the final part. Keep `--multipart-chunk-mib` at 5 or greater.
For exceptionally large files, the script automatically raises the effective chunk size as needed to stay within R2's 10,000-part limit.

For slow or unstable connections, reduce `--workers` before increasing retry attempts. Excessive file and part concurrency can consume substantial memory and bandwidth.

## Duplicate Detection

Before uploading, `HeadObject` checks the destination key:

- Identical byte size: skip
- Different byte size: conflict
- Not found: upload

After upload, another `HeadObject` verifies the remote byte size.

## Object Limits

Cloudflare documents single PUT for smaller files and multipart upload for large objects such as video. Managed S3 transfers automatically choose multipart based on the configured threshold.

## Recovery

If a run stops:

1. Confirm the original process is no longer active.
2. Rerun the same command.
3. Completed same-size objects will be skipped.
4. Investigate conflicts rather than automatically adding `--overwrite`.

Do not delete local videos as part of recovery.
