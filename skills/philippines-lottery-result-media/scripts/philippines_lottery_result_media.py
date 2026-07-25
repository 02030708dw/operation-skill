#!/usr/bin/env python3
"""Create Philippine lottery result images and videos from multiple sources."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import imageio_ffmpeg
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFilter, ImageFont


SKILL_NAME = "philippines-lottery-result-media"
SKILL_DIR = Path(__file__).resolve().parents[1]
MANILA_TZ = ZoneInfo("Asia/Manila")
PENDING_TEXT = "Pending"
DRAW_ORDER = ("2:00 PM", "5:00 PM", "9:00 PM")
DRAW_SHORT = {"2:00 PM": "2PM", "5:00 PM": "5PM", "9:00 PM": "9PM"}

ASSET_DIR = Path(
    os.environ.get("PHILIPPINES_LOTTERY_ASSET_DIR", SKILL_DIR / "assets")
).expanduser()
LOGO_DIR = ASSET_DIR / "logos"
AUDIO_DIR = ASSET_DIR / "audio"
BRAND_LOGO_PATH = LOGO_DIR / "88reels-logo-outline.png"
DEFAULT_MUSIC_PATH = AUDIO_DIR / "background.mp3"
BRAND_DOMAIN = os.environ.get("PHILIPPINES_LOTTERY_BRAND_DOMAIN", "88reels.net")
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get(
        "PHILIPPINES_LOTTERY_OUTPUT_DIR",
        Path.home() / "Desktop" / "Philippines-Lottery-Result-Media",
    )
).expanduser()
DEFAULT_ARCHIVE = Path(
    os.environ.get(
        "PHILIPPINES_LOTTERY_ARCHIVE",
        Path.home() / f".{SKILL_NAME}" / "archive.json",
    )
).expanduser()

PCSORESULTS_URL = "https://www.pcsoresults.org/lotto-results/"


@dataclass(frozen=True)
class GameSpec:
    key: str
    title: str
    source_key: str
    number_count: int
    draws: tuple[str, ...]
    lottopcso_url: str
    logo_name: str | None = None


GAMES: dict[str, GameSpec] = {
    "2d": GameSpec(
        key="2d",
        title="PCSO 2D / EZ2",
        source_key="ez2",
        number_count=2,
        draws=DRAW_ORDER,
        lottopcso_url="https://www.lottopcso.com/ez2-result-today/",
        logo_name="2d-lotto.webp",
    ),
    "3d": GameSpec(
        key="3d",
        title="PCSO 3D / SWERTRES",
        source_key="swertres",
        number_count=3,
        draws=DRAW_ORDER,
        lottopcso_url="https://www.lottopcso.com/swertres-result-today/",
        logo_name="3d-lotto.webp",
    ),
    "4d": GameSpec(
        key="4d",
        title="PCSO 4D LOTTO",
        source_key="4d",
        number_count=4,
        draws=("9:00 PM",),
        lottopcso_url="https://www.lottopcso.com/4d-lotto-result/",
        logo_name="4d-lotto.webp",
    ),
    "6d": GameSpec(
        key="6d",
        title="PCSO 6D LOTTO",
        source_key="6d",
        number_count=6,
        draws=("9:00 PM",),
        lottopcso_url="https://www.lottopcso.com/6d-lotto-result/",
        logo_name="6d-lotto.webp",
    ),
}


@dataclass(frozen=True)
class SourceResult:
    game: str
    date: str
    draw_time: str
    numbers: tuple[str, ...]
    source: str
    url: str
    latency_seconds: float = 0.0

    @property
    def ready(self) -> bool:
        return len(self.numbers) == GAMES[self.game].number_count

    @property
    def result_text(self) -> str:
        return "-".join(self.numbers) if self.ready else PENDING_TEXT

    @property
    def key(self) -> str:
        return f"{self.game}|{self.date}|{self.draw_time}|{self.result_text}"


@dataclass(frozen=True)
class SourceObservation:
    source: str
    url: str
    results: tuple[SourceResult, ...]
    latency_seconds: float
    error: str | None = None


@dataclass(frozen=True)
class Selection:
    selected: SourceResult
    board: tuple[SourceResult, ...]
    observations: tuple[SourceObservation, ...]
    agreement: str
    conflicts: tuple[str, ...]

    @property
    def key(self) -> str:
        rows = "|".join(f"{item.draw_time}:{item.result_text}" for item in self.board)
        return f"{self.selected.game}|{self.selected.date}|{rows}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Philippine lottery result images and videos."
    )
    parser.add_argument("--game", default="2d", choices=sorted(GAMES))
    parser.add_argument(
        "--draw",
        default="latest",
        help="latest, auto, 2pm, 5pm, 9pm, or an exact draw label",
    )
    parser.add_argument(
        "--layout",
        default="auto",
        choices=["auto", "all", "single"],
        help="auto uses all draws for 2D/3D and single for 4D/6D.",
    )
    parser.add_argument(
        "--sources",
        default="auto",
        help="auto, or comma-separated pcsoresults,lottopcso",
    )
    parser.add_argument(
        "--html-file",
        action="append",
        default=[],
        metavar="SOURCE=PATH",
        help="Use saved HTML for a source; repeat for multiple sources.",
    )
    parser.add_argument(
        "--conflict-policy",
        default="stop",
        choices=["stop", "first", "pcsoresults", "lottopcso"],
        help="What to do when sources publish different numbers for the same draw.",
    )
    parser.add_argument("--request-timeout", type=int, default=25)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--music", help="Override bundled background music.")
    parser.add_argument("--brand-domain", default=BRAND_DOMAIN)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--animation",
        choices=["cinematic", "subtle", "none"],
        default="cinematic",
        help="cinematic adds light sweeps, particles, glow, and camera motion.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--keep-previous",
        action="store_true",
        help="Keep older generated PNG/MP4 files when --draw latest is used.",
    )
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--retry-delay", type=int, default=30)
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def normalize_draw(value: str) -> str:
    compact = value.strip().lower().replace(" ", "")
    aliases = {
        "2": "2:00 PM",
        "2pm": "2:00 PM",
        "2:00pm": "2:00 PM",
        "5": "5:00 PM",
        "5pm": "5:00 PM",
        "5:00pm": "5:00 PM",
        "9": "9:00 PM",
        "9pm": "9:00 PM",
        "9:00pm": "9:00 PM",
    }
    return aliases.get(compact, value.strip())


def scheduled_draw(spec: GameSpec, now: datetime | None = None) -> str:
    if len(spec.draws) == 1:
        return spec.draws[0]
    now = (now or datetime.now(MANILA_TZ)).astimezone(MANILA_TZ)
    hhmm = now.hour * 100 + now.minute
    if hhmm < 1700:
        return "2:00 PM"
    if hhmm < 2100:
        return "5:00 PM"
    return "9:00 PM"


def normalize_date(value: str) -> str:
    clean = re.sub(r"\s+", " ", value).strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(clean, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    match = re.search(
        r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        clean,
    )
    if match:
        return normalize_date(match.group(1))
    raise ValueError(f"Could not parse result date: {value!r}")


def number_parts(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+", value))


def valid_numbers(parts: Iterable[str], spec: GameSpec) -> tuple[str, ...]:
    values = tuple(str(item).strip() for item in parts if str(item).strip())
    if len(values) != spec.number_count:
        return ()
    return values


def parse_pcsoresults(html: str, spec: GameSpec, url: str) -> list[SourceResult]:
    soup = BeautifulSoup(html, "lxml")
    results: list[SourceResult] = []
    for card in soup.select(".lr-card[data-game]"):
        if card.get("data-game", "").strip().lower() != spec.source_key:
            continue
        date_node = card.select_one(".lr-date")
        draw_node = card.select_one(".lr-draw-time")
        if not date_node or not draw_node:
            continue
        try:
            date = normalize_date(date_node.get_text(" ", strip=True))
        except ValueError:
            continue
        draw = normalize_draw(draw_node.get_text(" ", strip=True))
        numbers = valid_numbers(
            (node.get_text(" ", strip=True) for node in card.select(".lr-ball")),
            spec,
        )
        results.append(
            SourceResult(spec.key, date, draw, numbers, "pcsoresults", url)
        )
    if not results:
        raise RuntimeError(f"pcsoresults: no {spec.key.upper()} cards found")
    return results


def table_rows(table) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    return rows


def parse_lottopcso(html: str, spec: GameSpec, url: str) -> list[SourceResult]:
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        rows = table_rows(table)
        if not rows:
            continue
        heading = " ".join(rows[0]).lower()
        if spec.key not in heading and spec.title.split()[1].lower() not in heading:
            continue
        try:
            date = normalize_date(rows[0][0])
        except ValueError:
            try:
                date = normalize_date(" ".join(rows[0]))
            except ValueError:
                continue
        results: list[SourceResult] = []
        for row in rows[1:]:
            if len(row) < 2:
                continue
            draw = normalize_draw(row[0])
            if draw not in spec.draws:
                continue
            numbers = valid_numbers(number_parts(row[1]), spec)
            results.append(
                SourceResult(spec.key, date, draw, numbers, "lottopcso", url)
            )
        if results:
            return results
    raise RuntimeError(f"lottopcso: no {spec.key.upper()} result table found")


def parse_html(source: str, html: str, spec: GameSpec, url: str) -> list[SourceResult]:
    if source == "pcsoresults":
        return parse_pcsoresults(html, spec, url)
    if source == "lottopcso":
        return parse_lottopcso(html, spec, url)
    raise ValueError(f"Unsupported source: {source}")


def source_urls(spec: GameSpec) -> dict[str, str]:
    return {"pcsoresults": PCSORESULTS_URL, "lottopcso": spec.lottopcso_url}


def requested_sources(value: str) -> list[str]:
    names = ["pcsoresults", "lottopcso"] if value.strip().lower() == "auto" else [
        item.strip().lower() for item in value.split(",") if item.strip()
    ]
    invalid = [name for name in names if name not in {"pcsoresults", "lottopcso"}]
    if invalid:
        raise ValueError(f"Unsupported source(s): {', '.join(invalid)}")
    return list(dict.fromkeys(names))


def html_overrides(values: list[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--html-file must use SOURCE=PATH")
        source, raw_path = value.split("=", 1)
        source = source.strip().lower()
        if source not in {"pcsoresults", "lottopcso"}:
            raise ValueError(f"Unsupported HTML source: {source}")
        path = Path(raw_path.strip()).expanduser()
        if not path.is_file():
            raise ValueError(f"HTML file not found: {path}")
        overrides[source] = path
    return overrides


def fetch_source(
    source: str,
    spec: GameSpec,
    url: str,
    timeout: int,
    override: Path | None,
) -> SourceObservation:
    started = time.monotonic()
    try:
        if override:
            html = override.read_text(encoding="utf-8")
        else:
            response = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            response.raise_for_status()
            html = response.text
        latency = time.monotonic() - started
        parsed = parse_html(source, html, spec, url)
        results = tuple(
            SourceResult(
                item.game,
                item.date,
                item.draw_time,
                item.numbers,
                item.source,
                item.url,
                latency,
            )
            for item in parsed
        )
        return SourceObservation(source, url, results, latency)
    except Exception as exc:
        return SourceObservation(
            source,
            url,
            (),
            time.monotonic() - started,
            f"{type(exc).__name__}: {exc}",
        )


def collect_sources(args: argparse.Namespace, spec: GameSpec) -> tuple[SourceObservation, ...]:
    names = requested_sources(args.sources)
    urls = source_urls(spec)
    overrides = html_overrides(args.html_file)
    observations: list[SourceObservation] = []
    with ThreadPoolExecutor(max_workers=len(names)) as executor:
        futures = {
            executor.submit(
                fetch_source,
                name,
                spec,
                urls[name],
                args.request_timeout,
                overrides.get(name),
            ): name
            for name in names
        }
        for future in as_completed(futures):
            observations.append(future.result())
    return tuple(observations)


def draw_rank(draw: str) -> int:
    try:
        return DRAW_ORDER.index(draw)
    except ValueError:
        return -1


def candidate_group(
    observations: tuple[SourceObservation, ...],
    spec: GameSpec,
    requested_draw: str,
) -> list[SourceResult]:
    ready = [item for obs in observations for item in obs.results if item.ready]
    if not ready:
        errors = "; ".join(
            f"{obs.source}: {obs.error or 'result pending'}" for obs in observations
        )
        raise RuntimeError(f"No ready {spec.key.upper()} result found. {errors}")

    draw_value = requested_draw.strip().lower()
    if draw_value == "auto":
        target_draw = scheduled_draw(spec)
        ready = [item for item in ready if item.draw_time == target_draw]
    elif draw_value != "latest":
        target_draw = normalize_draw(requested_draw)
        ready = [item for item in ready if item.draw_time == target_draw]
    if not ready:
        raise RuntimeError(f"No ready result found for requested draw {requested_draw!r}")

    latest_date = max(item.date for item in ready)
    ready = [item for item in ready if item.date == latest_date]
    if draw_value == "latest":
        latest_draw = max(ready, key=lambda item: draw_rank(item.draw_time)).draw_time
        ready = [item for item in ready if item.draw_time == latest_draw]
    return ready


def choose_from_group(
    group: list[SourceResult],
    conflict_policy: str,
) -> tuple[SourceResult, str, tuple[str, ...]]:
    variants: dict[tuple[str, ...], list[SourceResult]] = {}
    for item in group:
        variants.setdefault(item.numbers, []).append(item)
    conflicts: tuple[str, ...] = ()
    if len(variants) > 1:
        conflicts = tuple(
            f"{item.source}={item.result_text}" for item in sorted(group, key=lambda row: row.source)
        )
        if conflict_policy == "stop":
            raise RuntimeError("Source conflict: " + ", ".join(conflicts))
        if conflict_policy in {"pcsoresults", "lottopcso"}:
            preferred = [item for item in group if item.source == conflict_policy]
            if preferred:
                return preferred[0], "conflict-overridden", conflicts
    selected = min(group, key=lambda item: item.latency_seconds)
    return selected, "confirmed" if len(group) > 1 else "single-source", conflicts


def build_board(
    selected: SourceResult,
    observations: tuple[SourceObservation, ...],
    spec: GameSpec,
    conflict_policy: str,
) -> tuple[tuple[SourceResult, ...], tuple[str, ...]]:
    rows: list[SourceResult] = []
    conflicts: list[str] = []
    for draw in spec.draws:
        candidates = [
            item
            for obs in observations
            for item in obs.results
            if item.date == selected.date and item.draw_time == draw and item.ready
        ]
        if not candidates:
            rows.append(
                SourceResult(spec.key, selected.date, draw, (), "none", "")
            )
            continue
        try:
            row, _, row_conflicts = choose_from_group(candidates, conflict_policy)
        except RuntimeError as exc:
            raise RuntimeError(f"{draw}: {exc}") from exc
        rows.append(row)
        conflicts.extend(f"{draw} {value}" for value in row_conflicts)
    return tuple(rows), tuple(conflicts)


def select_result(args: argparse.Namespace) -> Selection:
    spec = GAMES[args.game]
    observations = collect_sources(args, spec)
    group = candidate_group(observations, spec, args.draw)
    selected, agreement, selected_conflicts = choose_from_group(
        group, args.conflict_policy
    )
    board, board_conflicts = build_board(
        selected, observations, spec, args.conflict_policy
    )
    return Selection(
        selected,
        board,
        observations,
        agreement,
        tuple(dict.fromkeys((*selected_conflicts, *board_conflicts))),
    )


def load_archive(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_archive(path: Path, keys: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sorted(keys), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def font(size: int, bold: bool = False):
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [
        windows / ("arialbd.ttf" if bold else "arial.ttf"),
        windows / ("msyhbd.ttc" if bold else "msyh.ttc"),
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def number_font(size: int):
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    candidates = [
        windows / "ariblk.ttf",
        windows / "impact.ttf",
        windows / "arialbd.ttf",
        Path("/System/Library/Fonts/Supplemental/Arial Black.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return font(size, True)


def centered(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, fnt, fill) -> None:
    box = draw.textbbox((0, 0), text, font=fnt)
    draw.text(
        (xy[0] - (box[2] - box[0]) / 2, xy[1]),
        text,
        font=fnt,
        fill=fill,
    )


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def draw_gradient_text(
    image: Image.Image,
    xy: tuple[int, int],
    text: str,
    fnt,
    top_color: str,
    bottom_color: str,
    *,
    anchor: str = "mm",
    stroke_width: int = 2,
    stroke_fill: str = "#07111f",
    glow_color: str = "#5b7cff",
    glow_radius: int = 14,
) -> None:
    mask = Image.new("L", image.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text(xy, text, font=fnt, fill=255, anchor=anchor)
    bbox = mask.getbbox()
    if not bbox:
        return

    glow_mask = mask.filter(ImageFilter.GaussianBlur(glow_radius))
    glow = Image.new("RGBA", image.size, (*hex_rgb(glow_color), 0))
    glow.putalpha(glow_mask.point(lambda value: int(value * 0.55)))
    image.alpha_composite(glow)

    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (xy[0] + 5, xy[1] + 8),
        text,
        font=fnt,
        fill=(0, 0, 0, 150),
        anchor=anchor,
        stroke_width=stroke_width + 2,
        stroke_fill=(0, 0, 0, 110),
    )
    image.alpha_composite(shadow)

    outline = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(outline).text(
        xy,
        text,
        font=fnt,
        fill=(255, 255, 255, 0),
        anchor=anchor,
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    image.alpha_composite(outline)

    top = hex_rgb(top_color)
    bottom = hex_rgb(bottom_color)
    gradient = Image.new("RGBA", image.size, (0, 0, 0, 0))
    gradient_draw = ImageDraw.Draw(gradient)
    height = max(bbox[3] - bbox[1], 1)
    for row in range(bbox[1], bbox[3] + 1):
        ratio = (row - bbox[1]) / height
        color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
        gradient_draw.line((bbox[0], row, bbox[2], row), fill=(*color, 255))
    gradient.putalpha(mask)
    image.alpha_composite(gradient)


def draw_glass_panel(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: tuple[int, int, int, int],
    outline: str = "#4b6cae",
    glow: str = "#3157c8",
    glow_radius: int = 22,
) -> None:
    glow_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    glow_draw.rounded_rectangle(
        box,
        radius=radius,
        outline=(*hex_rgb(glow), 155),
        width=8,
    )
    image.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(glow_radius)))
    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    panel_draw = ImageDraw.Draw(panel)
    panel_draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=3)
    highlight = (box[0] + 7, box[1] + 7, box[2] - 7, box[3] - 7)
    panel_draw.rounded_rectangle(
        highlight,
        radius=max(radius - 6, 1),
        outline=(255, 255, 255, 26),
        width=2,
    )
    image.alpha_composite(panel)


def paste_asset(
    base: Image.Image,
    path: Path,
    center: tuple[int, int],
    max_size: tuple[int, int],
) -> bool:
    if not path.is_file():
        return False
    with Image.open(path) as opened:
        asset = opened.convert("RGBA")
    asset.thumbnail(max_size, Image.Resampling.LANCZOS)
    base.paste(
        asset,
        (int(center[0] - asset.width / 2), int(center[1] - asset.height / 2)),
        asset,
    )
    return True


def game_logo(spec: GameSpec) -> Path | None:
    return LOGO_DIR / spec.logo_name if spec.logo_name else None


def make_background() -> Image.Image:
    width, height = 1080, 1920
    image = Image.new("RGBA", (width, height), "#030712")
    pixels = image.load()
    for y in range(height):
        vertical = y / height
        for x in range(width):
            horizontal = x / width
            blue_glow = max(0.0, 1.0 - (((horizontal - 0.78) / 0.52) ** 2 + ((vertical - 0.18) / 0.35) ** 2))
            purple_glow = max(0.0, 1.0 - (((horizontal - 0.12) / 0.62) ** 2 + ((vertical - 0.72) / 0.45) ** 2))
            pixels[x, y] = (
                int(3 + purple_glow * 18),
                int(7 + blue_glow * 19 + purple_glow * 4),
                int(18 + vertical * 12 + blue_glow * 48 + purple_glow * 28),
                255,
            )

    effects = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(effects)
    for index in range(38):
        x = (index * 197 + 83) % width
        y = (index * 311 + 127) % height
        radius = 2 + (index % 5)
        alpha = 28 + (index % 4) * 15
        color = (255, 220, 92, alpha) if index % 3 == 0 else (110, 157, 255, alpha)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    for x in range(-1700, width, 310):
        draw.polygon(
            ((x, 0), (x + 110, 0), (x + 1280, height), (x + 1170, height)),
            fill=(255, 255, 255, 8),
        )
    effects = effects.filter(ImageFilter.GaussianBlur(0.5))
    image.alpha_composite(effects)
    return image


def draw_header(image: Image.Image, spec: GameSpec, date: str) -> None:
    draw = ImageDraw.Draw(image)
    logo = game_logo(spec)
    if logo and paste_asset(image, logo, (180, 206), (176, 176)):
        pass
    else:
        logo_glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(logo_glow)
        glow_draw.ellipse((98, 124, 262, 288), fill=(255, 211, 63, 135))
        image.alpha_composite(logo_glow.filter(ImageFilter.GaussianBlur(26)))
        draw = ImageDraw.Draw(image)
        draw.ellipse((100, 126, 260, 286), fill="#ffda43", outline="#fff3b0", width=5)
        centered(draw, (180, 165), spec.key.upper(), font(55, True), "#111827")
    draw_gradient_text(
        image,
        (620, 164),
        spec.title,
        font(55, True),
        "#fff5ad",
        "#ffc928",
        anchor="mm",
        stroke_width=2,
        glow_color="#ffb000",
        glow_radius=13,
    )
    draw_gradient_text(
        image,
        (620, 238),
        "LOTTERY RESULT",
        font(39, True),
        "#ffffff",
        "#9fc4ff",
        anchor="mm",
        stroke_width=1,
        glow_color="#4b76ff",
        glow_radius=10,
    )
    draw_glass_panel(
        image,
        (348, 316, 732, 386),
        radius=30,
        fill=(9, 20, 44, 205),
        outline="#35518b",
        glow="#294aa7",
        glow_radius=12,
    )
    draw = ImageDraw.Draw(image)
    centered(draw, (540, 331), date, font(35, True), "#d6e3ff")


def result_display(item: SourceResult) -> str:
    return "  ".join(item.numbers) if item.ready else "—  —"


def draw_crisp_text(
    image: Image.Image,
    xy: tuple[int, int],
    text: str,
    fnt,
    *,
    fill: str = "#ffffff",
    stroke_fill: str = "#071426",
    stroke_width: int = 5,
    shadow_offset: tuple[int, int] = (3, 5),
) -> None:
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.text(
        (xy[0] + shadow_offset[0], xy[1] + shadow_offset[1]),
        text,
        font=fnt,
        fill=(0, 0, 0, 105),
        anchor="mm",
        stroke_width=stroke_width,
        stroke_fill=(0, 0, 0, 85),
    )
    draw.text(
        xy,
        text,
        font=fnt,
        fill=fill,
        anchor="mm",
        stroke_width=stroke_width,
        stroke_fill=stroke_fill,
    )
    image.alpha_composite(layer)


def draw_number_orb(
    image: Image.Image,
    center: tuple[int, int],
    diameter: int,
    number: str,
    index: int,
) -> None:
    x, y = center
    radius = diameter // 2
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_color = (255, 195, 34, 180) if index % 2 == 0 else (79, 121, 255, 150)
    glow_draw.ellipse(
        (x - radius - 10, y - radius - 10, x + radius + 10, y + radius + 10),
        fill=glow_color,
    )
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))

    orb = Image.new("RGBA", image.size, (0, 0, 0, 0))
    orb_draw = ImageDraw.Draw(orb)
    for ring in range(radius, 0, -1):
        ratio = ring / radius
        color = (
            int(255 - (1 - ratio) * 28),
            int(201 + (1 - ratio) * 35),
            int(48 + (1 - ratio) * 38),
            255,
        )
        orb_draw.ellipse((x - ring, y - ring, x + ring, y + ring), fill=color)
    orb_draw.ellipse(
        (x - radius + 4, y - radius + 4, x + radius - 4, y + radius - 4),
        outline=(255, 250, 207, 230),
        width=max(3, diameter // 34),
    )
    orb_draw.arc(
        (x - radius + 15, y - radius + 15, x + radius - 15, y + radius - 15),
        205,
        330,
        fill=(255, 255, 255, 150),
        width=max(3, diameter // 38),
    )
    image.alpha_composite(orb)
    number_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    number_draw = ImageDraw.Draw(number_layer)
    number_face = number_font(int(diameter * 0.47))
    number_draw.text(
        (x + 4, y + 7),
        number,
        font=number_face,
        fill=(0, 0, 0, 115),
        anchor="mm",
        stroke_width=max(3, diameter // 45),
        stroke_fill=(0, 0, 0, 100),
    )
    number_draw.text(
        (x, y),
        number,
        font=number_face,
        fill="#ffffff",
        anchor="mm",
        stroke_width=max(4, diameter // 38),
        stroke_fill="#071426",
    )
    image.alpha_composite(number_layer)


def render_selection(selection: Selection, output_path: Path, layout: str, brand_domain: str) -> None:
    spec = GAMES[selection.selected.game]
    image = make_background()
    draw_glass_panel(
        image,
        (58, 70, 1022, 1810),
        radius=38,
        fill=(4, 10, 27, 225),
        outline="#36558f",
        glow="#244ec4",
        glow_radius=28,
    )
    draw_header(image, spec, selection.selected.date)

    if layout == "all":
        draw_gradient_text(
            image,
            (540, 474),
            "TODAY'S DRAW BOARD",
            font(34, True),
            "#ffffff",
            "#9bbcff",
            glow_color="#315bd8",
            glow_radius=8,
        )
        y = 548
        for index, item in enumerate(selection.board):
            draw_glass_panel(
                image,
                (112, y, 968, y + 154),
                radius=42,
                fill=(10, 24, 53, 205),
                outline="#324e87",
                glow="#1e43a0",
                glow_radius=12,
            )
            draw_glass_panel(
                image,
                (136, y + 23, 390, y + 131),
                radius=36,
                fill=(255, 210, 51, 245),
                outline="#fff0a3",
                glow="#ffb700",
                glow_radius=16,
            )
            draw_crisp_text(
                image,
                (263, y + 77),
                DRAW_SHORT[item.draw_time],
                number_font(50),
                fill="#ffffff",
                stroke_fill="#071426",
                stroke_width=5,
                shadow_offset=(3, 4),
            )
            value = result_display(item)
            draw_gradient_text(
                image,
                (676, y + 77),
                value,
                font(61 if item.ready else 48, True),
                "#ffffff",
                "#b4c9ff",
                glow_color="#5b7cff",
                glow_radius=10,
            )
            y += 204
    else:
        item = selection.selected
        draw_glass_panel(
            image,
            (370, 445, 710, 552),
            radius=42,
            fill=(10, 25, 56, 220),
            outline="#496bb1",
            glow="#315bd8",
            glow_radius=18,
        )
        draw_crisp_text(
            image,
            (540, 498),
            DRAW_SHORT[item.draw_time],
            number_font(58),
            fill="#ffffff",
            stroke_fill="#071426",
            stroke_width=5,
            shadow_offset=(3, 5),
        )
        draw_gradient_text(
            image,
            (540, 642),
            "WINNING NUMBERS",
            font(34, True),
            "#fff6b5",
            "#ffc928",
            glow_color="#ffb000",
            glow_radius=9,
        )
        count = len(item.numbers)
        diameter = min(194, int((890 - max(count - 1, 0) * 20) / max(count, 1)))
        gap = 20
        total = count * diameter + max(count - 1, 0) * gap
        x = (1080 - total) // 2 + diameter // 2
        y = 835
        for index, number in enumerate(item.numbers):
            draw_number_orb(image, (x, y), diameter, number, index)
            x += diameter + gap

    draw_glass_panel(
        image,
        (145, 1220, 935, 1378),
        radius=34,
        fill=(7, 18, 40, 195),
        outline="#2e477c",
        glow="#1f3d8e",
        glow_radius=12,
    )
    draw = ImageDraw.Draw(image)
    source_label = (
        f"{selection.selected.source.upper()}  ·  {selection.agreement.upper()}"
    )
    centered(draw, (540, 1252), source_label, font(28, True), "#b9ccf2")
    centered(draw, (540, 1307), "VERIFY WITH OFFICIAL PCSO CHANNELS BEFORE PUBLISHING", font(22, True), "#8194bc")
    paste_asset(image, BRAND_LOGO_PATH, (540, 1518), (455, 142))
    draw = ImageDraw.Draw(image)
    draw_gradient_text(
        image,
        (540, 1636),
        brand_domain,
        font(43, True),
        "#ffffff",
        "#b8caff",
        glow_color="#714dff",
        glow_radius=10,
    )
    draw = ImageDraw.Draw(image)
    centered(
        draw,
        (540, 1710),
        datetime.now(MANILA_TZ).strftime("Generated %Y-%m-%d %H:%M PHT"),
        font(25, False),
        "#71809a",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output_path, quality=95)


def find_music(path_arg: str | None) -> Path | None:
    path = Path(path_arg).expanduser() if path_arg else DEFAULT_MUSIC_PATH
    return path if path.is_file() else None


def create_motion_overlays(folder: Path) -> tuple[Path, Path]:
    width, height = 1080, 1920
    shine_path = folder / "light-sweep.png"
    particles_path = folder / "particles.png"

    shine = Image.new("RGBA", (520, height), (0, 0, 0, 0))
    shine_draw = ImageDraw.Draw(shine)
    shine_draw.polygon(
        ((20, 0), (170, 0), (500, height), (330, height)),
        fill=(255, 255, 255, 42),
    )
    shine_draw.polygon(
        ((130, 0), (205, 0), (520, height), (445, height)),
        fill=(255, 221, 108, 34),
    )
    shine.filter(ImageFilter.GaussianBlur(28)).save(shine_path)

    particles = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    particle_draw = ImageDraw.Draw(particles)
    for index in range(62):
        x = (index * 197 + 47) % width
        y = (index * 307 + 91) % height
        radius = 2 + index % 5
        alpha = 35 + (index % 5) * 16
        color = (255, 218, 94, alpha) if index % 3 == 0 else (92, 139, 255, alpha)
        particle_draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=color,
        )
    particles.filter(ImageFilter.GaussianBlur(0.8)).save(particles_path)
    return shine_path, particles_path


def make_video(
    image_path: Path,
    video_path: Path,
    duration: int,
    fps: int,
    animation: str,
    music: Path | None,
) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    fade_out = max(duration - 0.55, 0)
    video_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lottery-motion-") as temp_folder:
        folder = Path(temp_folder)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-t",
            str(duration),
            "-i",
            str(image_path),
        ]

        if animation == "cinematic":
            shine_path, particles_path = create_motion_overlays(folder)
            command += [
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-t",
                str(duration),
                "-i",
                str(shine_path),
                "-loop",
                "1",
                "-framerate",
                str(fps),
                "-t",
                str(duration),
                "-i",
                str(particles_path),
            ]

        audio_index: int | None = None
        if music:
            audio_index = 3 if animation == "cinematic" else 1
            command += ["-stream_loop", "-1", "-i", str(music)]

        if animation == "cinematic":
            filter_complex = (
                f"[0:v]scale=1080:1920,"
                f"zoompan=z='min(zoom+0.00012,1.032)':d=1:s=1080x1920:fps={fps},"
                f"format=rgba[base];"
                f"[1:v]format=rgba,colorchannelmixer=aa=0.68[shine];"
                f"[2:v]format=rgba[particles];"
                f"[base][shine]overlay="
                f"x='-overlay_w+(main_w+overlay_w)*mod(t,4.8)/4.8':y=0:eval=frame[swept];"
                f"[swept][particles]overlay="
                f"x='14*sin(t*0.75)':y='20*cos(t*0.62)':eval=frame,"
                f"eq=brightness='0.012*sin(2*PI*t/3.8)':saturation=1.08,"
                f"vignette=PI/5,"
                f"fade=t=in:st=0:d=0.35,"
                f"fade=t=out:st={fade_out}:d=0.55,"
                f"format=yuv420p[v]"
            )
            command += ["-filter_complex", filter_complex, "-map", "[v]"]
        else:
            video_filter = "scale=1080:1920,format=yuv420p"
            if animation == "subtle":
                video_filter = (
                    f"scale=1080:1920,"
                    f"zoompan=z='min(zoom+0.00008,1.022)':d=1:s=1080x1920:fps={fps},"
                    f"fade=t=in:st=0:d=0.3,fade=t=out:st={fade_out}:d=0.55,"
                    f"format=yuv420p"
                )
            command += ["-vf", video_filter, "-map", "0:v:0"]

        command += ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
        if music and audio_index is not None:
            command += [
                "-map",
                f"{audio_index}:a:0",
                "-af",
                f"afade=t=in:st=0:d=0.4,afade=t=out:st={max(duration - 1, 0)}:d=1",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-shortest",
            ]
        else:
            command += ["-an"]
        command += [
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-t",
            str(duration),
            str(video_path),
        ]
        subprocess.run(command, check=True)


def effective_layout(args: argparse.Namespace, spec: GameSpec) -> str:
    if args.layout != "auto":
        return args.layout
    return "all" if len(spec.draws) > 1 else "single"


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


def output_paths(selection: Selection, args: argparse.Namespace) -> tuple[Path, Path | None]:
    item = selection.selected
    out_dir = Path(args.output_dir).expanduser() / item.game / item.date
    base = safe_name(f"{item.date}-{item.game}-{item.draw_time}-{item.result_text}")
    image_path = out_dir / f"{base}.png"
    return image_path, None if args.no_video else out_dir / f"{base}.mp4"


def cleanup_previous_latest_media(
    out_dir: Path,
    game: str,
    date: str,
) -> tuple[Path, ...]:
    if not out_dir.exists():
        return ()
    prefix = safe_name(f"{date}-{game}-")
    removed: list[Path] = []
    for extension in (".png", ".mp4"):
        for path in out_dir.glob(f"{prefix}*{extension}"):
            if not path.is_file():
                continue
            path.unlink()
            removed.append(path)
    return tuple(sorted(removed, key=lambda path: str(path).lower()))


def payload(
    selection: Selection,
    image_path: Path,
    video_path: Path | None,
    mode: str,
    removed_previous: tuple[Path, ...] = (),
) -> dict[str, object]:
    return {
        "skill": SKILL_NAME,
        "mode": mode,
        "game": selection.selected.game,
        "date": selection.selected.date,
        "draw": selection.selected.draw_time,
        "result": selection.selected.result_text,
        "selected_source": selection.selected.source,
        "agreement": selection.agreement,
        "conflicts": list(selection.conflicts),
        "sources": [
            {
                "name": obs.source,
                "url": obs.url,
                "latency_seconds": round(obs.latency_seconds, 3),
                "error": obs.error,
                "results": [
                    {
                        "date": item.date,
                        "draw": item.draw_time,
                        "result": item.result_text,
                        "ready": item.ready,
                    }
                    for item in obs.results
                ],
            }
            for obs in selection.observations
        ],
        "image": str(image_path),
        "video": str(video_path) if video_path else None,
        "removed_previous": [str(path) for path in removed_previous],
    }


def execute(
    selection: Selection,
    args: argparse.Namespace,
) -> tuple[Path, Path | None, tuple[Path, ...]]:
    image_path, video_path = output_paths(selection, args)
    removed_previous: tuple[Path, ...] = ()
    if args.draw.strip().lower() == "latest" and not args.keep_previous:
        removed_previous = cleanup_previous_latest_media(
            image_path.parent,
            selection.selected.game,
            selection.selected.date,
        )
    archive_path = Path(args.archive).expanduser()
    archive = load_archive(archive_path)
    if selection.key in archive and not args.force:
        if image_path.exists() and (video_path is None or video_path.exists()):
            return image_path, video_path, removed_previous
    render_selection(
        selection,
        image_path,
        effective_layout(args, GAMES[args.game]),
        args.brand_domain,
    )
    if video_path:
        make_video(
            image_path,
            video_path,
            args.duration,
            args.fps,
            args.animation,
            find_music(args.music),
        )
    archive.add(selection.key)
    save_archive(archive_path, archive)
    return image_path, video_path, removed_previous


def check_installation(args: argparse.Namespace) -> int:
    checks: dict[str, object] = {
        "skill": SKILL_NAME,
        "python": sys.version.split()[0],
        "games": sorted(GAMES),
        "asset_dir": str(ASSET_DIR),
        "logo_dir": str(LOGO_DIR),
        "audio_dir": str(AUDIO_DIR),
        "2d_logo": (LOGO_DIR / "2d-lotto.webp").is_file(),
        "3d_logo": (LOGO_DIR / "3d-lotto.webp").is_file(),
        "4d_logo": (LOGO_DIR / "4d-lotto.webp").is_file(),
        "6d_logo": (LOGO_DIR / "6d-lotto.webp").is_file(),
        "brand_logo": BRAND_LOGO_PATH.is_file(),
        "background_music": find_music(args.music) is not None,
    }
    try:
        checks["ffmpeg"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        checks["ffmpeg"] = None
        checks["ffmpeg_error"] = str(exc)
    checks["ready"] = bool(
        checks["2d_logo"]
        and checks["3d_logo"]
        and checks["4d_logo"]
        and checks["6d_logo"]
        and checks["brand_logo"]
        and checks["background_music"]
        and checks["ffmpeg"]
    )
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["ready"] else 1


def validate_args(args: argparse.Namespace) -> None:
    if args.duration < 1:
        raise ValueError("--duration must be at least 1")
    if args.fps < 1:
        raise ValueError("--fps must be at least 1")
    if args.request_timeout < 1:
        raise ValueError("--request-timeout must be at least 1")
    if args.retries < 0 or args.retry_delay < 0:
        raise ValueError("retry values cannot be negative")
    if args.music and not Path(args.music).expanduser().is_file():
        raise ValueError(f"Music file not found: {args.music}")
    html_overrides(args.html_file)
    requested_sources(args.sources)


def print_summary(
    selection: Selection,
    image_path: Path,
    video_path: Path | None,
    mode: str,
    removed_previous: tuple[Path, ...] = (),
) -> None:
    item = selection.selected
    print(
        f"{mode.upper()}: {item.game.upper()} {item.date} "
        f"{item.draw_time} = {item.result_text}"
    )
    print(
        f"Selected source: {item.source} "
        f"({item.latency_seconds:.3f}s, {selection.agreement})"
    )
    for observation in selection.observations:
        state = observation.error or ", ".join(
            f"{row.date} {DRAW_SHORT.get(row.draw_time, row.draw_time)}={row.result_text}"
            for row in observation.results
        )
        print(f"- {observation.source} ({observation.latency_seconds:.3f}s): {state}")
    if removed_previous:
        print(f"Removed previous generated media: {len(removed_previous)} file(s)")
        for path in removed_previous:
            print(f"- removed: {path}")
    print(f"Image: {image_path}")
    if video_path:
        print(f"Video: {video_path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        if args.check:
            return check_installation(args)
        last_error: Exception | None = None
        for attempt in range(args.retries + 1):
            try:
                selection = select_result(args)
                image_path, video_path = output_paths(selection, args)
                removed_previous: tuple[Path, ...] = ()
                if args.execute:
                    image_path, video_path, removed_previous = execute(selection, args)
                    mode = "execute"
                else:
                    mode = "dry-run"
                    print("DRY RUN: no image, video, or archive file was written.")
                print_summary(
                    selection,
                    image_path,
                    video_path,
                    mode,
                    removed_previous,
                )
                if args.json:
                    print(
                        json.dumps(
                            payload(
                                selection,
                                image_path,
                                video_path,
                                mode,
                                removed_previous,
                            ),
                            ensure_ascii=False,
                        )
                    )
                return 0
            except Exception as exc:
                last_error = exc
                if attempt < args.retries:
                    print(f"{exc}; retrying in {args.retry_delay}s", file=sys.stderr)
                    time.sleep(args.retry_delay)
                    continue
                raise
        raise RuntimeError(str(last_error))
    except (ValueError, RuntimeError, requests.RequestException, subprocess.CalledProcessError) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
