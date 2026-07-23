# Facebook Download Notes

Facebook Page discovery changes frequently and is best effort. Direct Reel, watch, or video URLs are more reliable than scanning a Page.

Use this workflow only for content the user has permission to download. Do not bypass private Pages or groups, paid access, DRM, login walls, rate limits, or technical restrictions. Do not request cookies or session tokens. If the user already has an authorized local cookies file, use only its path and never print its contents.

`yt-dlp` performs the media download. Keep both duplicate-prevention files enabled:

- `.fb-video-urls.txt`: URLs successfully handled by this workflow
- `.yt-dlp-archive.txt`: `yt-dlp` media archive

For a new source, preview with a low per-source limit:

```text
python "<skill-dir>/scripts/facebook_followed_video_download.py" --mode daily --count 3 --verbose
```

If no videos are found, test a permitted direct video URL from the same source. If direct videos work but Page scanning does not, report a discovery limitation rather than a downloader failure.
