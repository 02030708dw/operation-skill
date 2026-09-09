#!/usr/bin/env python3
"""Generate one accepted draw snapshot, with durable upload/callback recovery."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import threading
import urllib.error
from hm_server_worker import api, state_root


def module_at(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def template():
    skills = Path(__file__).resolve().parents[2]
    return module_at("hm_lottery_template", skills / "philippines-lottery-result-media/scripts/philippines_lottery_result_media.py")


def validated_numbers(job: dict) -> list[str]:
    values = job.get("numbers")
    if not isinstance(values, list) or not values or len(values) > 100:
        raise ValueError("开奖场次缺少有效的号码快照")
    numbers = [str(value).strip() for value in values]
    if any(not value or len(value) > 100 for value in numbers):
        raise ValueError("开奖场次号码格式无效")
    return numbers


def text_result(job: dict) -> str:
    numbers = validated_numbers(job)
    return "\n".join([str(job.get("lotteryName") or job.get("lotteryCode") or "Lottery"),
                      "Issue: " + str(job.get("issue") or ""), " · ".join(numbers),
                      "Source: " + str(job.get("winningSourceUrl") or "")])


def render_image(job: dict, output: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    media = template()
    numbers = validated_numbers(job)
    game = str(job.get("game") or "").lower()
    draw = {"14:00": "2:00 PM", "17:00": "5:00 PM", "21:00": "9:00 PM"}.get(str(job.get("drawTime") or "")[:5])
    if (str(job.get("lotteryCode") or "").startswith("ph_") and game in media.GAMES
            and draw in media.GAMES[game].draws and len(numbers) == media.GAMES[game].number_count):
        preview = str(job.get("issue") or "").startswith("TEST")
        result = media.SourceResult(game, str(job.get("drawDate") or ""), draw, tuple(numbers),
                                    "TEST DATA" if preview else "DRAW RESULT", str(job.get("winningSourceUrl") or ""))
        selection = media.Selection(result, (result,), (), "PREVIEW" if preview else "CONFIRMED", ())
        media.render_selection(selection, output, "single", media.BRAND_DOMAIN)
        return
    image = Image.new("RGB", (1080, 1920), "#080f25")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((50, 80, 1030, 1760), radius=44, fill="#101d38", outline="#263c61", width=3)
    draw.text((90, 130), "LOTTERY RESULTS", font=media.font(48, True), fill="#f5ce71")
    title = str(job.get("lotteryName") or job.get("lotteryCode") or "Lottery")
    cjk = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    face = ImageFont.truetype(str(cjk), 38) if cjk.exists() else media.font(38, True)
    shown = title[:38]
    while len(shown) > 1 and draw.textbbox((0, 0), shown, font=face)[2] > 900:
        shown = shown[:-1]
    draw.text((90, 228), shown, font=face, fill="white")
    draw.text((90, 300), "Issue: " + str(job.get("issue") or "")[:45], font=media.font(27), fill="#aec4e5")
    columns = 2 if len(numbers) <= 12 else 4 if len(numbers) <= 48 else 6
    rows = math.ceil(len(numbers) / columns)
    width = 900 / columns
    height = min(155, 1150 / rows)
    top = 440 + max(0, (1100-height*rows)/2)
    for index, number in enumerate(numbers):
        x = 90 + index % columns * width
        y = top + index // columns * height
        draw.rounded_rectangle((x+4, y+5, x+width-8, y+height-6), radius=15, fill="#203a5f", outline="#537da6")
        size = min(60, int(height*.48))
        face = media.number_font(size)
        while draw.textbbox((0, 0), number, font=face)[2] > width-30 and size>10:
            size -= 1
            face = media.number_font(size)
        draw.text((x+width/2-2, y+height/2), number, font=face, fill="#ffe198", anchor="mm")
    if media.BRAND_LOGO_PATH.exists():
        logo = Image.open(media.BRAND_LOGO_PATH).convert("RGBA")
        logo.thumbnail((280, 130))
        image.paste(logo, (90, 1620), logo)
    draw.text((540, 1830), media.BRAND_DOMAIN, font=media.font(36, True), fill="#f5ce71", anchor="mm")
    image.save(output)


def upload_artifact(job: dict, path: Path) -> str:
    skills = Path(__file__).resolve().parents[2]
    uploader = module_at("hm_lottery_r2", skills / "cloudflare-r2-video-upload/scripts/cloudflare_r2_video_upload.py")
    client = uploader.create_client(argparse.Namespace(endpoint=None))
    prefix = os.getenv("HM_R2_KEY_PREFIX", "").strip("/")
    if not prefix or not all(part.replace("-", "").replace("_", "").isalnum() for part in prefix.split("/")):
        raise ValueError("Lottery server requires a valid R2 environment prefix")
    key = f'{prefix}/lottery/{job["taskNo"]}/{job["jobNo"]}{path.suffix}'
    base = os.environ["CLOUDFLARE_R2_PUBLIC_BASE_URL"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    bucket = os.environ["CLOUDFLARE_R2_BUCKET"]
    existing = uploader.remote_metadata(client, bucket, key)
    if existing is not None:
        if existing.get("Metadata", {}).get("sha256") != digest:
            raise ValueError("R2 existing artifact does not match this generation result")
    else:
        import mimetypes
        client.upload_file(str(path), bucket, key, ExtraArgs={
            "ContentType": mimetypes.guess_type(str(path))[0], "Metadata": {"sha256": digest}})
        saved = client.head_object(Bucket=bucket, Key=key)
        if saved.get("ContentLength") != path.stat().st_size or saved.get("Metadata", {}).get("sha256") != digest:
            raise ValueError("R2 artifact verification failed")
    return uploader.public_url(base, key)


def generate(job: dict, root: Path) -> str:
    validated_numbers(job)
    directory = root / job["jobNo"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    if job["contentType"] == "TEXT":
        return text_result(job)
    image = directory / "result.png"
    if not image.exists():
        temporary = directory / "rendering.png"
        render_image(job, temporary)
        temporary.replace(image)
    if job["contentType"] == "IMAGE":
        return upload_artifact(job, image)
    if job["contentType"] != "VIDEO":
        raise ValueError("Unsupported generation content type")
    video = directory / "result.mp4"
    if not video.exists():
        media = template()
        temporary = directory / "rendering.mp4"
        media.make_video(image, temporary, 10, 30, "subtle", media.DEFAULT_MUSIC_PATH)
        temporary.replace(video)
    return upload_artifact(job, video)


def main() -> int:
    root = state_root() / "lottery"
    root.mkdir(parents=True, exist_ok=True)
    worker = os.environ["HM_WORKER_ID"]
    # Persist before callback. Reclaim an expired lease before replaying its result.
    for journal in root.glob("*/completion.json"):
        body = json.loads(journal.read_text())
        job_no = journal.parent.name
        try:
            api(f"/api/internal/lottery/generation-jobs/{job_no}/complete", body)
        except urllib.error.HTTPError as error:
            if error.code != 409:
                raise
            claimed = api("/api/internal/lottery/generation-jobs/claim", {"workerId": worker, "jobNo": job_no})
            if not claimed: continue
            api(f"/api/internal/lottery/generation-jobs/{job_no}/complete", body)
        journal.unlink()
    job = api("/api/internal/lottery/generation-jobs/claim", {"workerId": worker})
    if not job: return 0
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(25):
            try:
                api(f'/api/internal/lottery/generation-jobs/{job["jobNo"]}/heartbeat', {"workerId": worker})
            except OSError: pass  # Outer dispatch lease terminates work during prolonged disconnect.
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    body = {"workerId": worker, "status": "COMPLETED", "resultPayload": None, "errorMessage": None}
    try:
        body["resultPayload"] = generate(job, root)
    except Exception as error:
        body.update(status="FAILED", errorMessage=f"{type(error).__name__}: {error}"[:1000])
    finally:
        stop.set()
        thread.join(timeout=20)
    journal = root / job["jobNo"] / "completion.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    temporary = journal.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    temporary.replace(journal)
    api(f'/api/internal/lottery/generation-jobs/{job["jobNo"]}/complete', body)
    journal.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
