#!/usr/bin/env python3
"""Local, bounded YouTube downloads with resume, deduplication and reports."""
import argparse
import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import shutil
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def normalize_url(value):
    p = urlparse(value)
    if p.scheme != "https" or p.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"} or p.username or p.password or p.port:
        raise ValueError("需要有效的 YouTube HTTPS 链接")
    parts = p.path.strip("/").split("/")
    vid = None
    if p.hostname == "youtu.be":
        vid = parts[0]
    elif parts[0] in {"shorts", "live", "embed"} and len(parts) == 2:
        vid = parts[1]
    elif p.path == "/watch":
        vid = parse_qs(p.query).get("v", [""])[0]
    if vid is not None:
        if not VIDEO_ID.fullmatch(vid):
            raise ValueError("视频 ID 无效")
        return "https://www.youtube.com/watch?v=" + vid, vid
    if parts[0].startswith("@") or parts[0] in {"channel", "c", "user"}:
        base = 1 if parts[0].startswith("@") else 2
        if len(parts) < base or not parts[base - 1]:
            raise ValueError("频道链接无效")
        if len(parts) == base:
            parts.append("shorts")
        if len(parts) != base + 1 or parts[-1] not in {"shorts", "videos", "streams"}:
            raise ValueError("需要频道 Shorts、videos 或 streams 页")
        return "https://www.youtube.com/" + "/".join(parts), None
    if p.path == "/playlist" and parse_qs(p.query).get("list"):
        return "https://www.youtube.com/playlist?" + urlencode({"list": parse_qs(p.query)["list"][0]}), None
    raise ValueError("不支持的 YouTube 链接")


def classify(message):
    text = message.lower()
    if any(s in text for s in ("sign in", "not a bot", "login", "cookie", "authentication")):
        return "authentication_required"
    if any(s in text for s in ("429", "too many requests", "rate limit", "this content isn't available, try again later")):
        return "rate_limited"
    return "download_error"


def redact(message):
    return re.sub(r"https?://\S+", "[URL omitted]", str(message))[:800]


class Logger:
    def debug(self, message):
        pass

    def warning(self, message):
        # Warnings may contain signed URLs or response bodies; reports omit them.
        pass

    def error(self, message):
        pass


def save(path, report):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def resolve_ffmpeg(output, explicit=None):
    """Never write into the Python installation or the managed skill directory."""
    selected = explicit or shutil.which("ffmpeg")
    if selected:
        selected = Path(selected).expanduser().resolve()
        if not selected.is_file():
            raise ValueError("FFmpeg 文件不存在")
        return str(selected)
    import imageio_ffmpeg
    binary = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    tools = output / ".tools"
    tools.mkdir(exist_ok=True)
    alias = tools / "ffmpeg"
    if alias.is_symlink() and alias.resolve() != binary:
        alias.unlink()
    if not alias.exists():
        alias.symlink_to(binary)
    return str(alias)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, default=Path.home() / "Downloads/YouTube")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int, default=10)
    scope.add_argument("--all", action="store_true")
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--cookies", type=Path)
    auth.add_argument("--browser", help="授权的普通 Chrome 配置，例如 chrome:Default")
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--redownload", action="store_true")
    parser.add_argument("--subtitles", action="store_true")
    parser.add_argument("--sub-langs", default="en.*,zh.*")
    parser.add_argument("--thumbnail", action="store_true")
    parser.add_argument("--ffmpeg", help="服务器 FFmpeg 可执行文件路径；默认优先使用 PATH")
    args = parser.parse_args()
    if args.height < 1 or args.limit < 1:
        parser.error("height 和 limit 必须为正整数")
    try:
        url, single = normalize_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))
    if args.cookies and not args.cookies.expanduser().is_file():
        parser.error("Cookie 文件不存在")
    if args.browser and args.browser.split(":", 1)[0] != "chrome":
        parser.error("--browser 支持 chrome 或 chrome:配置名称")
    import yt_dlp
    from yt_dlp.postprocessor.common import PostProcessor
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = (output / ".download.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("同一输出目录已有下载任务运行")
    runtime = {}
    for name in ("deno", "node"):
        executable = shutil.which(name)
        fallback = Path.home() / ".local/bin" / name
        if not executable and fallback.is_file():
            executable = str(fallback)
        if executable:
            runtime[name] = {"path": executable}
            break
    if not runtime:
        parser.error("需要 Node.js 或 Deno 运行环境")
    common = {"logger": Logger(), "quiet": True, "no_warnings": True, "noprogress": True,
              "socket_timeout": 20, "retries": 1, "extractor_retries": 1, "fragment_retries": 1,
              "js_runtimes": runtime, "cachedir": False, "ignoreerrors": False}
    if args.cookies:
        common["cookiefile"] = str(args.cookies.expanduser().resolve())
    if args.browser:
        parts = args.browser.split(":", 1)
        common["cookiesfrombrowser"] = (parts[0], parts[1] if len(parts) > 1 else None, None, None)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    reports = output / ".reports"
    reports.mkdir(exist_ok=True)
    report_path = reports / (stamp + ".json")
    report = {"source": url, "mode": "list" if args.list_only else "download", "started_at": stamp,
              "scope": 1 if single else ("all" if args.all else args.limit), "output": str(output), "results": []}
    entries = [{"id": single, "title": None}] if single else []
    if not single or args.list_only:
        options = dict(common, extract_flat="in_playlist", skip_download=True)
        if not args.all:
            options["playlistend"] = args.limit
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                data = ydl.extract_info(url, download=False)
                entries = [{"id": item["id"], "title": item.get("title")} for item in data.get("entries", [data])
                           if item and VIDEO_ID.fullmatch(item.get("id", ""))]
        except (yt_dlp.utils.DownloadError, ValueError, TypeError, AttributeError) as exc:
            report["error"] = {"kind": classify(str(exc)), "message": redact(exc)}
            save(report_path, report)
            print(json.dumps({"report": str(report_path), **report["error"]}, ensure_ascii=False))
            return 1
    entries = list({entry["id"]: entry for entry in entries}.values())
    report["discovered"] = len(entries)
    if args.list_only:
        report["results"] = [dict(item, status="listed") for item in entries]
        save(report_path, report)
        print(json.dumps({"report": str(report_path), "entries": entries}, ensure_ascii=False, indent=2))
        return 0
    try:
        ffmpeg = resolve_ffmpeg(output, args.ffmpeg)
    except (ValueError, RuntimeError, OSError) as exc:
        parser.error(str(exc))
    archive = output / ".download-archive.txt"
    completed = set(archive.read_text().splitlines()) if archive.exists() else set()
    options = dict(common, noplaylist=True, continuedl=True, overwrites=False,
                   ffmpeg_location=ffmpeg, merge_output_format="mp4",
                   format=f"bv*[height<={args.height}][vcodec^=avc1]+ba[acodec^=mp4a]/b[height<={args.height}][ext=mp4]/bv*[height<={args.height}]+ba/b[height<={args.height}]",
                   outtmpl=str(output / "%(channel_id)s/%(upload_date)s_%(title).120B_[%(id)s].%(ext)s"),
                   windowsfilenames=True, writesubtitles=args.subtitles, writeautomaticsub=args.subtitles,
                   subtitleslangs=args.sub_langs.split(","), writethumbnail=args.thumbnail)
    if not args.redownload:
        options["download_archive"] = str(archive)
    captured = []

    class CaptureFile(PostProcessor):
        def run(self, info):
            path = Path(info["filepath"])
            if not path.is_file() or not path.stat().st_size:
                raise yt_dlp.utils.PostProcessingError("输出文件为空或不存在")
            captured.append({"id": info["id"], "title": info.get("title"), "path": str(path),
                             "bytes": path.stat().st_size, "duration": info.get("duration"),
                             "width": info.get("width"), "height": info.get("height")})
            return [], info

    attempted = 0
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.add_post_processor(CaptureFile(ydl), when="after_move")
        for item in entries:
            if not args.redownload and "youtube " + item["id"] in completed:
                report["results"].append(dict(item, status="skipped"))
                continue
            if attempted:
                time.sleep(5)
            attempted += 1
            captured.clear()
            try:
                ydl.extract_info("https://www.youtube.com/watch?v=" + item["id"], download=True)
                if not captured:
                    raise yt_dlp.utils.DownloadError("未生成可验证的视频文件")
                record = dict(captured[-1], status="downloaded")
            except yt_dlp.utils.DownloadError as exc:
                record = dict(item, status="failed", kind=classify(str(exc)), message=redact(exc))
            report["results"].append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            save(report_path, report)
            if record.get("kind") in {"authentication_required", "rate_limited"}:
                report["stopped_reason"] = record["kind"]
                break
    report["counts"] = {key: sum(r["status"] == key for r in report["results"]) for key in ("downloaded", "skipped", "failed")}
    report["counts"]["unattempted"] = len(entries) - len(report["results"])
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save(report_path, report)
    print(json.dumps({"report": str(report_path), **report["counts"]}, ensure_ascii=False))
    return 1 if report["counts"]["failed"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中断；保留临时文件供续传。", file=sys.stderr)
        raise SystemExit(130)
