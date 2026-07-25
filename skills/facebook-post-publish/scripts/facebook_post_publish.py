#!/usr/bin/env python3
"""Publish text/image/video Facebook posts through MYT Android HTTP APIs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET


VERSION = "1.2.0"
FACEBOOK_PACKAGE = "com.facebook.katana"
DEFAULT_MEDIA_ROOT = "/sdcard/upload"
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif"
}
VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mpeg",
    ".mpg", ".ts"
}
POST_LABELS = {
    "post", "post now", "publish", "publish now",
    "发布", "立即发布", "發佈", "立即發佈", "发表", "發表"
}
CREATE_LABELS = {
    "create", "create new", "创建", "建立", "新建", "新增", "+"
}
POST_MENU_LABELS = {"post", "帖子", "貼文"}
GALLERY_LABELS = {
    "gallery", "photo/video", "photos/videos", "图库", "圖庫", "照片/视频",
    "相片/影片"
}
NEXT_LABELS = {"next", "done", "add", "下一步", "完成", "添加", "加入"}
COMPOSER_TITLES = {"create post", "new post", "新帖", "新帖子", "新增貼文"}
TEXT_HINTS = {
    "what's on your mind?", "share something", "分享新鲜事", "分享新鮮事",
    "说点什么", "說點什麼"
}
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
DURATION_RE = re.compile(r"(?<!\d)(?:\d{1,2}:)?\d{1,2}:\d{2}(?!\d)")
PRINT_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()


class ConfigurationError(ValueError):
    pass


class MytError(RuntimeError):
    pass


class MediaSelectionError(MytError):
    pass


@dataclass(frozen=True)
class Device:
    label: str
    port: int


@dataclass(frozen=True)
class RemoteMedia:
    path: str
    size: int
    mtime: int
    media_type: str
    mime_type: str

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class UiTarget:
    description: str
    x: int
    y: int


@dataclass(frozen=True)
class GalleryTile:
    target: UiTarget
    rect: tuple[int, int, int, int]
    has_duration: bool
    selected: bool


def log(message: str, *, error: bool = False) -> None:
    with PRINT_LOCK:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


def normalize_host(value: str) -> str:
    host = value.strip().rstrip("/")
    if "://" in host:
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("MYT host is invalid")
        if parsed.port:
            raise ConfigurationError("Do not include a port in --host")
        host = parsed.hostname
    if not host or "/" in host or ":" in host:
        raise ConfigurationError("MYT host must not include protocol, path, or port")
    return host


def parse_devices(value: str, base_port: int, stride: int) -> list[Device]:
    devices: list[Device] = []
    seen: set[int] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        match = re.fullmatch(r"(?i)T100(\d+)", item)
        if match:
            index = int(match.group(1))
            if index < 1:
                raise ConfigurationError(f"Invalid device: {item}")
            port = base_port + (index - 1) * stride
            label = f"T100{index}"
        elif item.isdigit():
            port = int(item)
            label = f"port-{port}"
        else:
            raise ConfigurationError(f"Invalid device: {item}")
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"Invalid port: {port}")
        if port not in seen:
            devices.append(Device(label, port))
            seen.add(port)
    if not devices:
        raise ConfigurationError("--devices is required")
    return devices


def classify_media(path: str) -> tuple[str, str] | None:
    extension = Path(path).suffix.casefold()
    if extension in IMAGE_EXTENSIONS:
        return "image", mimetypes.types_map.get(extension, "image/jpeg")
    if extension in VIDEO_EXTENSIONS:
        return "video", mimetypes.types_map.get(extension, "video/mp4")
    return None


def parse_media_listing(text: str) -> list[RemoteMedia]:
    media: list[RemoteMedia] = []
    for line in text.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        try:
            mtime = int(float(parts[0]))
            size = int(parts[1])
        except ValueError:
            continue
        path = parts[2]
        classified = classify_media(path)
        if classified:
            kind, mime = classified
            media.append(RemoteMedia(path, size, mtime, kind, mime))
    return sorted(media, key=lambda item: (-item.mtime, item.path.casefold()))


def select_media(
    candidates: list[RemoteMedia],
    *,
    media_file: str | None,
    media_type: str,
    match_text: str | None,
    latest: bool,
) -> RemoteMedia | None:
    wants_media = bool(media_file or match_text or latest or media_type != "none")
    if not wants_media:
        return None
    filtered = candidates
    if media_type in {"image", "video"}:
        filtered = [item for item in filtered if item.media_type == media_type]
    if media_file:
        needle = media_file.casefold()
        if media_file.startswith("/"):
            filtered = [item for item in filtered if item.path.casefold() == needle]
        else:
            filtered = [item for item in filtered if item.name.casefold() == needle]
    if match_text:
        needle = match_text.casefold()
        filtered = [
            item for item in filtered
            if needle in item.name.casefold() or needle in item.path.casefold()
        ]
    if not filtered:
        raise MediaSelectionError("no-media-match")
    if len(filtered) == 1:
        return filtered[0]
    if latest:
        newest = max(item.mtime for item in filtered)
        newest_items = [item for item in filtered if item.mtime == newest]
        if len(newest_items) == 1:
            return newest_items[0]
    names = ", ".join(item.path for item in filtered[:8])
    raise MediaSelectionError(f"ambiguous-media ({len(filtered)}): {names}")


def validate_post_content(text: str, wants_media: bool, list_media: bool) -> None:
    if not list_media and not text and not wants_media:
        raise ConfigurationError("Provide --text or a media selection")


class MytClient:
    def __init__(self, host: str, device: Device, timeout: float, verbose: bool):
        self.host = normalize_host(host)
        self.device = device
        self.timeout = timeout
        self.verbose = verbose

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.device.port}"

    @staticmethod
    def _raise_api_error(payload: bytes, operation: str) -> None:
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(parsed, dict) and parsed.get("code") not in {None, 0, 200}:
            reason = parsed.get("reason") or parsed.get("error") or parsed.get("msg")
            raise MytError(f"{operation} failed: {reason or parsed}")

    def get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        request = urllib.request.Request(
            f"{self.base_url}{path}{query}",
            headers={"User-Agent": f"Hermes-Facebook-Post/{VERSION}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read(4 * 1024 * 1024)
                if not 200 <= response.status < 300:
                    raise MytError(f"HTTP {response.status}")
                self._raise_api_error(payload, path)
                return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MytError(f"{self.device.label} request failed: {exc}") from exc

    def shell(self, command: str) -> bytes:
        if self.verbose:
            log(f"[{self.device.label}] shell: {command}")
        return self.get("/modifydev", {"cmd": "6", "cmdline": command})

    def capture(self, command: str) -> str:
        marker = f"/sdcard/.hermes-post-{uuid.uuid4().hex}.txt"
        marker_q = shlex.quote(marker)
        try:
            self.shell(f"( {command} ) > {marker_q} 2>&1")
            payload = self.get("/download", {"path": marker})
            return payload.decode("utf-8", errors="replace")
        finally:
            try:
                self.shell(f"rm -f {marker_q}")
            except Exception:
                pass

    def check(self) -> None:
        self.shell("true")

    def list_media(self, root: str) -> list[RemoteMedia]:
        root_q = shlex.quote(root)
        command = (
            f"find {root_q} -type f 2>/dev/null | "
            "while IFS= read -r f; do "
            "stat -c '%Y|%s|%n' \"$f\" 2>/dev/null; done"
        )
        return parse_media_listing(self.capture(command))

    def scan_media(self, path: str) -> None:
        self.shell(
            "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
            f"-d {shlex.quote('file://' + path)}"
        )

    def launch_facebook_home(self) -> None:
        self.shell("am start -a android.intent.action.VIEW -d fb:///")

    def prepare_media_for_gallery(self, media: RemoteMedia) -> None:
        # Facebook's own picker can see /sdcard/upload even on devices whose
        # MediaStore query API does not return the file. Refresh mtime to move
        # the selected asset near the front. The gallery picker still verifies
        # the thumbnail type before tapping it.
        self.shell(f"touch {shlex.quote(media.path)}")
        self.scan_media(media.path)

    def input_text(self, text: str) -> None:
        if not text:
            return
        # Prefer clipboard paste because it preserves UTF-8 on Android builds
        # that expose cmd clipboard. Fall back to input text for ASCII.
        try:
            self.shell(f"cmd clipboard set text {shlex.quote(text)}")
            self.shell("input keyevent 279")
            return
        except MytError:
            pass
        if not text.isascii() or any(ord(char) < 32 for char in text):
            raise MytError(
                "UTF-8 clipboard paste is unavailable on this Android build"
            )
        encoded = text.replace("%", r"\%").replace(" ", "%s")
        self.shell(f"input text {shlex.quote(encoded)}")

    def dump_ui(self) -> bytes:
        remote = f"/sdcard/hermes_fb_post_{self.device.port}.xml"
        self.shell(f"uiautomator dump {shlex.quote(remote)}")
        payload = self.get("/download", {"path": remote})
        ET.fromstring(payload)
        return payload

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")

    def keyevent(self, keycode: int) -> None:
        self.shell(f"input keyevent {keycode}")


def deep_description(node: ET.Element) -> str:
    values: list[str] = []
    for item in node.iter():
        for key in ("text", "content-desc"):
            value = item.attrib.get(key, "").strip()
            if value and value not in values:
                values.append(value)
    return " | ".join(values)


def node_center(node: ET.Element) -> tuple[int, int] | None:
    match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def node_rect(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
    if not match:
        return None
    return tuple(int(value) for value in match.groups())  # type: ignore[return-value]


def screen_size(root: ET.Element) -> tuple[int, int]:
    width = 0
    height = 0
    for node in root.iter():
        rect = node_rect(node)
        if rect:
            width = max(width, rect[2])
            height = max(height, rect[3])
    return width or 240, height or 400


def description_labels(description: str) -> set[str]:
    return {
        value.strip().casefold()
        for value in re.split(r"[|,\n]", description)
        if value.strip()
    }


def find_labeled_target(
    xml_data: bytes,
    labels: set[str],
    *,
    enabled: bool = True,
) -> UiTarget | None:
    root = ET.fromstring(xml_data)
    matches: list[UiTarget] = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if enabled and node.attrib.get("enabled", "true").casefold() == "false":
            continue
        description = deep_description(node)
        if not description_labels(description).intersection(labels):
            continue
        center = node_center(node)
        if center:
            matches.append(UiTarget(description, center[0], center[1]))
    return min(matches, key=lambda item: (item.y, item.x)) if matches else None


def find_home_create_button(xml_data: bytes) -> UiTarget | None:
    labeled = find_labeled_target(xml_data, CREATE_LABELS)
    if labeled:
        return labeled
    root = ET.fromstring(xml_data)
    width, height = screen_size(root)
    candidates: list[UiTarget] = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if node.attrib.get("enabled", "true").casefold() == "false":
            continue
        center = node_center(node)
        if not center:
            continue
        x, y = center
        # Screenshot-verified fallback: the create '+' is the first action
        # after the Facebook wordmark, before search and Messenger.
        if 0.58 * width <= x <= 0.76 * width and y <= 0.15 * height:
            candidates.append(UiTarget(deep_description(node) or "top-create", x, y))
    return min(candidates, key=lambda item: item.x) if candidates else None


def find_composer_input(xml_data: bytes) -> UiTarget | None:
    root = ET.fromstring(xml_data)
    candidates: list[tuple[int, UiTarget]] = []
    for node in root.iter():
        class_name = node.attrib.get("class", "").casefold()
        editable = node.attrib.get("editable", "").casefold() == "true"
        if not editable and not class_name.endswith("edittext"):
            continue
        center = node_center(node)
        if not center:
            continue
        description = deep_description(node)
        score = 0 if description_labels(description).intersection(TEXT_HINTS) else 10
        candidates.append((score, UiTarget(description, center[0], center[1])))
    return min(candidates, key=lambda item: (item[0], item[1].y))[1] if candidates else None


def find_gallery_tile(
    xml_data: bytes,
    media: RemoteMedia,
) -> tuple[UiTarget | None, str]:
    tiles = gallery_tiles(xml_data)
    needle = media.name.casefold()
    exact = [
        tile
        for tile in tiles
        if needle and needle in tile.target.description.casefold()
    ]
    if exact:
        return min(
            exact, key=lambda tile: (tile.target.y, tile.target.x)
        ).target, "filename"

    if media.media_type == "video":
        matching = [tile for tile in tiles if tile.has_duration]
        method = "video-duration"
    elif media.media_type == "image":
        matching = [tile for tile in tiles if not tile.has_duration]
        method = "image-no-duration"
    else:
        matching = []
        method = "unsupported-type"

    if matching:
        return min(
            matching, key=lambda tile: (tile.target.y, tile.target.x)
        ).target, method
    return None, f"no-{media.media_type}-tile"


def gallery_tiles(xml_data: bytes) -> list[GalleryTile]:
    root = ET.fromstring(xml_data)
    width, height = screen_size(root)
    duration_points: list[tuple[int, int]] = []
    for node in root.iter():
        own_description = " | ".join(
            value
            for value in (
                node.attrib.get("text", "").strip(),
                node.attrib.get("content-desc", "").strip(),
            )
            if value
        )
        if DURATION_RE.search(own_description):
            center = node_center(node)
            if center:
                duration_points.append(center)

    found: dict[tuple[int, int, int, int], GalleryTile] = {}
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if node.attrib.get("enabled", "true").casefold() == "false":
            continue
        rect = node_rect(node)
        center = node_center(node)
        if not rect or not center:
            continue
        description = deep_description(node)
        target = UiTarget(description, center[0], center[1])
        x1, y1, x2, y2 = rect
        cell_width = x2 - x1
        cell_height = y2 - y1
        if (
            y1 >= 0.12 * height
            and y1 <= 0.75 * height
            and cell_width >= 0.20 * width
            and cell_height >= 0.12 * height
        ):
            has_duration = bool(DURATION_RE.search(description)) or any(
                x1 <= point_x <= x2 and y1 <= point_y <= y2
                for point_x, point_y in duration_points
            )
            selected = any(
                item.attrib.get("selected", "").casefold() == "true"
                or item.attrib.get("checked", "").casefold() == "true"
                for item in node.iter()
            )
            labels = description_labels(description)
            selected = selected or bool(
                labels.intersection(
                    {"selected", "已选择", "已選擇", "已选中", "已選取"}
                )
            )
            found[rect] = GalleryTile(
                target=target,
                rect=rect,
                has_duration=has_duration,
                selected=selected,
            )
    return sorted(found.values(), key=lambda tile: (tile.target.y, tile.target.x))


def find_selected_gallery_tiles(xml_data: bytes) -> list[UiTarget]:
    return [tile.target for tile in gallery_tiles(xml_data) if tile.selected]


def same_gallery_tile(first: UiTarget, second: UiTarget) -> bool:
    return first.x == second.x and first.y == second.y


def classify_facebook_screen(xml_data: bytes) -> str:
    packages = screen_packages(xml_data)
    if packages and FACEBOOK_PACKAGE not in packages:
        return "other-app"
    if find_labeled_target(xml_data, GALLERY_LABELS):
        return "composer"
    if find_labeled_target(xml_data, POST_MENU_LABELS):
        return "create-menu"
    if find_home_create_button(xml_data):
        return "home"
    labels = description_labels(deep_description(ET.fromstring(xml_data)))
    if labels.intersection(COMPOSER_TITLES):
        return "composer"
    if FACEBOOK_PACKAGE in packages:
        return "facebook-other"
    return "unknown"


def find_publish_button(xml_data: bytes) -> UiTarget | None:
    root = ET.fromstring(xml_data)
    matches: list[tuple[int, UiTarget]] = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if node.attrib.get("enabled", "true").casefold() == "false":
            continue
        description = deep_description(node)
        labels = description_labels(description)
        if not labels.intersection(POST_LABELS):
            continue
        center = node_center(node)
        if not center:
            continue
        x, y = center
        score = y - x // 10
        matches.append((score, UiTarget(description, x, y)))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def screen_packages(xml_data: bytes) -> set[str]:
    root = ET.fromstring(xml_data)
    return {
        node.attrib.get("package", "")
        for node in root.iter()
        if node.attrib.get("package")
    }


def content_fingerprint(text: str, media: RemoteMedia | None) -> str:
    payload = {
        "text": text,
        "media": None if media is None else {
            "path": media.path, "size": media.size, "mtime": media.mtime
        },
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"submissions": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"submissions": []}


def duplicate_exists(path: Path, device: str, fingerprint: str) -> bool:
    with STATE_LOCK:
        state = load_state(path)
        cutoff = time.time() - 7 * 86400
        return any(
            item.get("device") == device
            and item.get("fingerprint") == fingerprint
            and float(item.get("timestamp", 0)) >= cutoff
            for item in state.get("submissions", [])
        )


def record_submission(path: Path, device: str, fingerprint: str) -> None:
    with STATE_LOCK:
        state = load_state(path)
        submissions = [
            item for item in state.get("submissions", [])
            if float(item.get("timestamp", 0)) >= time.time() - 30 * 86400
        ]
        submissions.append({
            "device": device,
            "fingerprint": fingerprint,
            "timestamp": time.time(),
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"submissions": submissions}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


def wait_for_ui(
    client: MytClient,
    predicate,
    *,
    timeout: float,
    description: str,
) -> tuple[bytes, object]:
    deadline = time.monotonic() + timeout
    last_state = "no UI"
    while time.monotonic() < deadline:
        try:
            xml_data = client.dump_ui()
            last_state = classify_facebook_screen(xml_data)
            value = predicate(xml_data)
            if value:
                return xml_data, value
        except Exception as exc:
            last_state = str(exc)
        time.sleep(1)
    raise MytError(f"timeout waiting for {description}; last_state={last_state}")


def input_contains_text(xml_data: bytes, text: str) -> bool:
    needle = text.casefold().strip()
    if not needle:
        return True
    root = ET.fromstring(xml_data)
    for node in root.iter():
        class_name = node.attrib.get("class", "").casefold()
        editable = node.attrib.get("editable", "").casefold() == "true"
        if not editable and not class_name.endswith("edittext"):
            continue
        if needle in deep_description(node).casefold():
            return True
    return False


def ensure_facebook_home(client: MytClient) -> tuple[bytes, UiTarget]:
    try:
        current = client.dump_ui()
        state = classify_facebook_screen(current)
    except Exception:
        current = b""
        state = "unknown"
    if state == "home":
        target = find_home_create_button(current)
        if target:
            return current, target
    client.launch_facebook_home()
    _, value = wait_for_ui(
        client,
        find_home_create_button,
        timeout=20,
        description="Facebook home create button",
    )
    return client.dump_ui(), value  # type: ignore[return-value]


def open_composer_through_ui(
    client: MytClient,
    text: str,
    media: RemoteMedia | None,
) -> tuple[UiTarget, str | None]:
    _, create_target = ensure_facebook_home(client)
    client.tap(create_target.x, create_target.y)
    _, post_target = wait_for_ui(
        client,
        lambda xml: find_labeled_target(xml, POST_MENU_LABELS),
        timeout=10,
        description="create menu Post/帖子 item",
    )
    client.tap(post_target.x, post_target.y)  # type: ignore[attr-defined]

    wait_for_ui(
        client,
        lambda xml: find_labeled_target(xml, GALLERY_LABELS),
        timeout=15,
        description="new-post composer",
    )

    selection_method: str | None = None
    if media:
        client.prepare_media_for_gallery(media)
        composer_xml = client.dump_ui()
        gallery_target = find_labeled_target(composer_xml, GALLERY_LABELS)
        if not gallery_target:
            raise MytError("Gallery/图库 button not found in Facebook composer")
        client.tap(gallery_target.x, gallery_target.y)

        def gallery_match(xml_data: bytes):
            tile, method = find_gallery_tile(xml_data, media)
            return (tile, method) if tile else None

        try:
            gallery_xml, tile_info = wait_for_ui(
                client,
                gallery_match,
                timeout=15,
                description=f"Facebook gallery {media.media_type} thumbnail",
            )
        except MytError as exc:
            raise MytError(
                f"no verified {media.media_type} thumbnail found in Facebook "
                "gallery; video thumbnails must expose a duration label and "
                "image thumbnails must not expose one"
            ) from exc
        tile, selection_method = tile_info  # type: ignore[misc]
        if not tile:
            raise MytError(f"selected media not visible in gallery: {media.path}")

        # Facebook can reopen the picker with a previous, wrong thumbnail still
        # selected. Clear every other selected cell before choosing the verified
        # image/video tile. A requested video is never allowed to fall back to
        # an image just because that image is the first item in the grid.
        selected_tiles = find_selected_gallery_tiles(gallery_xml)
        target_was_selected = any(
            same_gallery_tile(selected, tile) for selected in selected_tiles
        )
        wrong_selected = [
            selected
            for selected in selected_tiles
            if not same_gallery_tile(selected, tile)
        ]
        for selected in wrong_selected:
            client.tap(selected.x, selected.y)
            time.sleep(0.5)
        if wrong_selected:
            gallery_xml = client.dump_ui()
            refreshed_tile, refreshed_method = find_gallery_tile(
                gallery_xml, media
            )
            if not refreshed_tile:
                raise MytError(
                    f"no verified {media.media_type} thumbnail after "
                    "clearing the previous selection"
                )
            tile = refreshed_tile
            selection_method = refreshed_method
            target_was_selected = any(
                same_gallery_tile(selected, tile)
                for selected in find_selected_gallery_tiles(gallery_xml)
            )
        if not target_was_selected:
            client.tap(tile.x, tile.y)

        # Some Facebook versions return immediately; others require Next/Done.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(1)
            current = client.dump_ui()
            target = find_publish_button(current)
            if target and find_labeled_target(current, GALLERY_LABELS):
                break
            next_target = find_labeled_target(current, NEXT_LABELS)
            if next_target:
                client.tap(next_target.x, next_target.y)
                continue
        else:
            raise MytError("did not return to Facebook composer after media selection")

    if text:
        current = client.dump_ui()
        input_target = find_composer_input(current)
        if not input_target:
            raise MytError("post text input not found")
        client.tap(input_target.x, input_target.y)
        client.input_text(text)
        time.sleep(1)
        current = client.dump_ui()
        if not input_contains_text(current, text):
            raise MytError("post text could not be verified in composer")

    final_xml = client.dump_ui()
    final_publish = find_publish_button(final_xml)
    if not final_publish:
        raise MytError("enabled publish button not found after composing post")
    return final_publish, selection_method


def preflight(
    client: MytClient,
    args: argparse.Namespace,
) -> dict:
    result = {"device": client.device.label, "port": client.device.port}
    try:
        client.check()
        try:
            result["screen_state"] = classify_facebook_screen(client.dump_ui())
        except Exception as exc:
            result["screen_state"] = f"unreadable: {exc}"
        candidates = (
            client.list_media(args.media_root)
            if args.list_media or args.wants_media
            else []
        )
        if args.list_media:
            result.update(status="listed", candidates=candidates)
            return result
        selected = select_media(
            candidates,
            media_file=args.media_file,
            media_type=args.media_type,
            match_text=args.match,
            latest=args.latest,
        )
        result.update(status="ready", media=selected)
    except Exception as exc:
        result.update(status="failed", error=str(exc))
    return result


def publish(
    client: MytClient,
    text: str,
    media: RemoteMedia | None,
    *,
    verify_timeout: float,
    state_file: Path,
) -> dict:
    result = {
        "device": client.device.label,
        "port": client.device.port,
        "media": None if media is None else media.path,
    }
    fingerprint = content_fingerprint(text, media)
    try:
        target, selection_method = open_composer_through_ui(client, text, media)
        result["publish_button"] = target.description
        if selection_method:
            result["media_selection"] = selection_method
        client.tap(target.x, target.y)
        record_submission(state_file, client.device.label, fingerprint)
        deadline = time.monotonic() + verify_timeout
        while time.monotonic() < deadline:
            time.sleep(2)
            try:
                current = client.dump_ui()
            except Exception:
                continue
            if FACEBOOK_PACKAGE in screen_packages(current) and not find_publish_button(current):
                result.update(status="submitted")
                return result
        result.update(
            status="unverified-submit",
            error="publish was tapped but composer did not close; inspect manually",
        )
    except Exception as exc:
        result.update(status="failed", error=str(exc))
    return result


def media_to_dict(media: RemoteMedia) -> dict:
    return {
        "path": media.path,
        "type": media.media_type,
        "mime": media.mime_type,
        "size": media.size,
        "mtime": media.mtime,
    }


def print_results(results: list[dict], *, json_output: bool) -> None:
    if json_output:
        serializable = []
        for item in results:
            copy = dict(item)
            if isinstance(copy.get("media"), RemoteMedia):
                copy["media"] = media_to_dict(copy["media"])
            if copy.get("candidates"):
                copy["candidates"] = [
                    media_to_dict(value) for value in copy["candidates"]
                ]
            serializable.append(copy)
        print(json.dumps(serializable, ensure_ascii=False, indent=2))
        return
    for item in results:
        line = (
            f"{item['device']} port={item['port']} status={item['status']}"
        )
        media = item.get("media")
        if isinstance(media, RemoteMedia):
            line += f" media={media.path} type={media.media_type}"
        elif media:
            line += f" media={media}"
        if item.get("error"):
            line += f" error={item['error']}"
        if item.get("screen_state"):
            line += f" screen={item['screen_state']}"
        log(line, error=item["status"] in {"failed", "unverified-submit"})
        for candidate in item.get("candidates", []):
            log(
                f"  {candidate.media_type} {candidate.path} "
                f"size={candidate.size} mtime={candidate.mtime}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish Facebook posts on MYT Android cloud phones"
    )
    parser.add_argument("--devices")
    parser.add_argument("--text", default="")
    parser.add_argument("--media-file")
    parser.add_argument(
        "--media-type",
        choices=("none", "auto", "image", "video"),
        default="none",
    )
    parser.add_argument("--match")
    parser.add_argument("--latest", action="store_true")
    parser.add_argument("--media-root", default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--list-media", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-repeat", action="store_true")
    parser.add_argument("--host", default=os.getenv("MYT_HOST", ""))
    parser.add_argument("--base-port", type=int, default=10005)
    parser.add_argument("--port-stride", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--verify-timeout", type=float, default=45)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument(
        "--state-file",
        default=str(
            Path(os.getenv("HERMES_STATE_DIR", Path.home() / ".hermes" / "state"))
            / "facebook-post-publish.json"
        ),
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.host:
            raise ConfigurationError("MYT_HOST is missing")
        host = normalize_host(args.host)
        devices = parse_devices(
            args.devices or "", args.base_port, args.port_stride
        )
        if args.execute and args.list_media:
            raise ConfigurationError("--execute cannot be used with --list-media")
        if args.allow_repeat and not args.execute:
            raise ConfigurationError("--allow-repeat requires --execute")
        if args.latest and not (
            args.media_file or args.match or args.media_type != "none"
        ):
            args.media_type = "auto"
        args.wants_media = bool(
            args.media_file or args.match or args.latest
            or args.media_type != "none"
        )
        validate_post_content(args.text, args.wants_media, args.list_media)
        if args.media_file and args.media_type == "none":
            args.media_type = "auto"
        if args.match and args.media_type == "none":
            args.media_type = "auto"
        if not args.media_root.startswith("/") or ".." in args.media_root.split("/"):
            raise ConfigurationError("--media-root must be a safe absolute path")
        if len(args.text) > 5000:
            raise ConfigurationError("--text is limited to 5000 characters")
        if args.timeout <= 0 or args.verify_timeout <= 0:
            raise ConfigurationError("Timeout values must be positive")

        clients = {
            device.port: MytClient(
                host, device, args.timeout, args.verbose
            )
            for device in devices
        }
        workers = args.workers or min(8, len(devices))
        preflight_results: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(preflight, clients[device.port], args): device
                for device in devices
            }
            for future in as_completed(futures):
                result = future.result()
                preflight_results[int(result["port"])] = result
        ordered = [preflight_results[item.port] for item in devices]
        if args.list_media:
            print_results(ordered, json_output=args.json)
            return 0 if all(item["status"] == "listed" for item in ordered) else 1
        if any(item["status"] != "ready" for item in ordered):
            print_results(ordered, json_output=args.json)
            return 1

        log("Mode: EXECUTE" if args.execute else "Mode: PREVIEW")
        log(f"Text: {args.text!r}")
        print_results(ordered, json_output=args.json)
        if not args.execute:
            return 0

        state_file = Path(args.state_file).expanduser().resolve()
        for item in ordered:
            fingerprint = content_fingerprint(args.text, item.get("media"))
            if (
                duplicate_exists(state_file, item["device"], fingerprint)
                and not args.allow_repeat
            ):
                raise ConfigurationError(
                    f"duplicate protection blocked {item['device']}; "
                    "use --allow-repeat only with explicit user approval"
                )

        published: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for item in ordered:
                client = clients[int(item["port"])]
                future = executor.submit(
                    publish,
                    client,
                    args.text,
                    item.get("media"),
                    verify_timeout=args.verify_timeout,
                    state_file=state_file,
                )
                futures[future] = item
            for future in as_completed(futures):
                result = future.result()
                published[int(result["port"])] = result
        final = [published[item.port] for item in devices]
        print_results(final, json_output=args.json)
        return 0 if all(item["status"] == "submitted" for item in final) else 1
    except ConfigurationError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
