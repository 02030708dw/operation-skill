---
name: facebook-post-publish
description: Publish Facebook posts on one or more MYT (魔云腾) Android cloud phones, supporting text-only posts, image posts, video posts, and text combined with one selected image or video already uploaded under /sdcard/upload. Use when the user asks to create, compose, post, or publish Facebook content from uploaded media. Select media deterministically by exact filename, media type, filename keyword, or explicit newest-file preference; never guess when multiple candidates remain.
---

# Facebook Post Publish

Publish one Facebook post per selected MYT device. Preview by default. Click the
Facebook publish button only after explicit authorization with `--execute`.

## Required input

Require target devices and at least one of:

- Post text.
- An exact uploaded media filename.
- A requested media type (`image` or `video`) plus enough selection information
  to leave exactly one candidate.

Text is optional for image and video posts. If the user says “只发图片” or
“只发视频”, omit `--text`; do not ask for a caption.

Uploaded media is read from `/sdcard/upload` by default.

## Media selection

Use the narrowest rule available:

1. Exact filename or absolute remote path: `--media-file`.
2. Media type and filename keyword: `--media-type` plus `--match`.
3. Media type alone only when exactly one candidate exists.
4. `--latest` only when the user explicitly asks for the newest/latest file.

If several candidates remain, run `--list-media` and ask the user to choose. Do
not select the first file, the largest file, or a random thumbnail.

Examples for a folder containing matching PNG and MP4 results:

- “发布视频” → `--media-type video`; select the MP4 when it is the only video.
- “发布图片” → `--media-type image`; select the PNG when it is the only image.
- “发布这个文件 xxx.mp4” → `--media-file "xxx.mp4"`.

## Commands

List uploaded media without publishing:

```powershell
python "<skill-dir>\scripts\facebook_post_publish.py" --devices T1001 --list-media
```

Preview a text-only post:

```powershell
python "<skill-dir>\scripts\facebook_post_publish.py" --devices T1001 --text "Good morning"
```

Publish only the selected image, without text:

```powershell
python "<skill-dir>\scripts\facebook_post_publish.py" --devices T1001 --media-type image --execute
```

Publish only the selected video, without text:

```powershell
python "<skill-dir>\scripts\facebook_post_publish.py" --devices T1001 --media-type video --execute
```

Publish text with the only uploaded video:

```powershell
python "<skill-dir>\scripts\facebook_post_publish.py" --devices T1001 --text "Latest result" --media-type video --execute
```

Publish an exact image to two devices:

```powershell
python "<skill-dir>\scripts\facebook_post_publish.py" --devices T1001,T1002 --text "Latest result" --media-file "result.png" --execute
```

## Workflow

1. Run connection and media-selection preflight on every device concurrently.
   Read current UI XML and report `home`, `composer`, `facebook-other`, or
   `other-app` before changing the screen.
2. Stop all devices if any device has no match, an ambiguous match, or a
   connectivity failure.
3. In preview mode, report the exact text and remote media selected; do not open
   the composer or click anything.
4. In execute mode, use the current Facebook page when it is the home page;
   otherwise start Facebook home. Follow the screenshot-confirmed UI path:
   top `+` → `帖子` → `图库`.
5. Find media only under `/sdcard/upload`. Refresh the selected file's timestamp
   to move it near the front of Facebook's newest-first grid. Prefer an exact
   filename UI node. When filenames are hidden, select videos only from
   thumbnails with a duration label such as `00:10`, and select images only from
   thumbnails without a duration label.
6. Return to `新帖`, add optional text, and require an enabled `发布` button.
7. Click once, immediately record a local duplicate-protection fingerprint, and
   wait for the composer to close.
8. Report `submitted` only when the composer leaves the publish screen. Report
   `unverified-submit` otherwise and require manual inspection before retrying.

## Safety rules

- Never infer `--execute` from a preview request.
- Never retry `unverified-submit` automatically.
- Never use `--allow-repeat` unless the user explicitly accepts duplicate-post
  risk.
- Do not add a short total timeout while a video is uploading. The state-change
  verification timeout applies only after the publish tap.
- Do not require MediaStore `content query` success. This MYT device exposes
  uploaded files in Facebook's gallery even when Android MediaStore lookup fails.
- Never cross media types in the gallery. A video request must not select a
  thumbnail without a duration label; an image request must not select one with
  a duration label. Stop safely if the requested type cannot be verified.
- If the gallery opens with a previous wrong thumbnail selected, deselect it
  before selecting the verified target type.
- Run devices concurrently, but publish at most one post per device per command.
- First-time login, CAPTCHA, two-factor authentication, account checks, and
  audience/privacy selection require human handling.

Read [references/facebook-post-ui.md](references/facebook-post-ui.md) when
diagnosing composer labels, media selection, or unverified submission.
