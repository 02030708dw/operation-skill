# Facebook post publishing notes

## Media source

The companion MYT file-upload Skill places files under `/sdcard/upload`.
This Skill recursively scans that directory and classifies media by extension.

Supported images: JPG, JPEG, PNG, GIF, WEBP, BMP, HEIC, HEIF.

Supported videos: MP4, MOV, M4V, MKV, AVI, WEBM, 3GP, MPEG, MPG, TS.

Other files remain visible in diagnostics but are not valid Facebook post media.

An image or video may be published without `android.intent.extra.TEXT`. Do not
create an empty placeholder caption and do not require text when the user asks to
publish media only.

## Selection failures

- `no-media-match`: no image/video satisfies the requested type, filename, and
  keyword.
- `ambiguous-media`: more than one candidate remains. Use exact `--media-file`,
  add `--match`, or use `--latest` only with explicit user permission.
- Different devices may contain different uploaded files. Preflight must succeed
  on every device before any device publishes.

## Composer flow

The UI flow is based on the supplied Chinese Facebook screenshots:

1. Confirm whether the current package and UI are Facebook.
2. Open Facebook home only when the current page is not the home page.
3. Tap the top `+` create control.
4. Tap `帖子`.
5. On `新帖`, tap `图库`.
6. Select the deterministic `/sdcard/upload` media candidate.
7. Return to `新帖`, add optional text, then tap enabled `发布`.

Do not block on MediaStore `content query`. On the tested MYT device, Facebook's
gallery displayed `/sdcard/upload` videos while the Android MediaStore query
returned no matching row.

Before opening the gallery, the script refreshes the selected file's modification
time and requests a media scan. It chooses a filename-labelled UI node when
available. If Facebook exposes thumbnails without filenames, a video is
identified by its visible duration badge (`00:10`, `1:23`, and similar), while
an image is identified by the absence of a duration badge. It then chooses the
newest tile of the requested type and reports `media_selection=video-duration`
or `media_selection=image-no-duration`.

The picker must never fall back across media types. If the user requested a
video and no duration-labelled tile is visible, stop without tapping a photo.
Likewise, an image request must not tap a duration-labelled video. If the picker
retains an earlier wrong selection, clear that selection before choosing the
verified target.

All controls are found from current UI XML using enabled clickable nodes and
localized exact labels such as Post, Publish, 发布, and 發佈. Generic Share/分享
labels are intentionally excluded because Facebook feed posts contain unrelated
share buttons. Fixed
screen coordinates are not used except for a screenshot-relative fallback for
the unlabeled top `+` control after the Facebook home page is verified.

## Verification and retry

After one publish tap, the script records the content fingerprint before waiting
for the composer to close. This prevents a timeout or terminal interruption from
causing an automatic duplicate post.

`unverified-submit` means the tap occurred but the script could not prove that the
composer closed. Inspect Facebook manually. Use `--allow-repeat` only when the
user confirms that no post was created or accepts duplicate risk.
