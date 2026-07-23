# Cloudflare R2 Setup

Use Cloudflare's R2 dashboard to create an S3 API credential with Object Read & Write permission. Scope it to only the required bucket whenever possible.

Store configuration outside the Skill and repository:

```text
CLOUDFLARE_R2_ACCOUNT_ID
CLOUDFLARE_R2_ACCESS_KEY_ID
CLOUDFLARE_R2_SECRET_ACCESS_KEY
CLOUDFLARE_R2_BUCKET
```

Optional temporary credentials also require:

```text
CLOUDFLARE_R2_SESSION_TOKEN
```

Do not put credentials in `SKILL.md`, source files, shell history, screenshots, reports, or Git. Do not pass secrets as command-line arguments.

The normal endpoint is:

```text
https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```

Use `CLOUDFLARE_R2_ENDPOINT` only when Cloudflare supplies a different endpoint, such as a jurisdiction-specific endpoint.

After configuring the local environment, validate without uploading:

```text
python "<skill-dir>/scripts/cloudflare_r2_video_upload.py" --check
```

The access key must allow `HeadBucket`, `HeadObject`, multipart upload, and object upload operations for full functionality.

Official references:

- https://developers.cloudflare.com/r2/get-started/s3/
- https://developers.cloudflare.com/r2/examples/aws/boto3/
- https://developers.cloudflare.com/r2/objects/upload-objects/
