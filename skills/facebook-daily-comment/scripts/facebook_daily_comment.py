#!/usr/bin/env python3
"""Find Facebook posts and optionally comment through the MYT V1 HTTP API."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import random
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable


BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
PRINT_LOCK = threading.Lock()
DEDUPE_LOCK = threading.Lock()
COMMENT_BUTTON_LABELS = {
    "评论",
    "評論",
    "评论按钮",
    "評論按鈕",
    "comment",
    "commentbutton",
}
INPUT_LABELS = (
    "写评论",
    "寫評論",
    "写下评论",
    "寫下評論",
    "添加评论",
    "新增評論",
    "发表评论",
    "發表評論",
    "发表公开评论",
    "發表公開評論",
    "write a comment",
    "write comment",
    "add a comment",
    "comment publicly",
    "comment as",
)
SEND_BUTTON_LABELS = {
    "发送",
    "傳送",
    "送出",
    "发布",
    "發佈",
    "发送按钮",
    "傳送按鈕",
    "送出按鈕",
    "发布按钮",
    "發佈按鈕",
    "发送评论",
    "傳送評論",
    "送出評論",
    "发布评论",
    "發佈評論",
    "send",
    "post",
    "sendbutton",
    "postbutton",
    "sendcomment",
    "postcomment",
    "submitcomment",
}
NON_SEND_COMPOSER_LABELS = (
    "add",
    "camera",
    "emoji",
    "gif",
    "more",
    "photo",
    "sticker",
    "添加",
    "相机",
    "相機",
    "照片",
    "图片",
    "圖片",
    "表情",
    "贴图",
    "貼圖",
    "更多",
)
CLOSE_LABELS = ("关闭", "關閉", "close")
FACEBOOK_PACKAGE = "com.facebook.katana"
GENERIC_SIGNATURE_TERMS = (
    "comment",
    "like",
    "reply",
    "share",
    "send",
    "post",
    "relevant",
    "write a comment",
    "评论",
    "評論",
    "回复",
    "回覆",
    "分享",
    "发送",
    "傳送",
    "最相关",
    "最相關",
)


class ConfigurationError(ValueError):
    """Raised when required local configuration is missing or invalid."""


class MytError(RuntimeError):
    """Raised when the MYT API cannot complete an operation."""


class RuntimeLimitError(MytError):
    """Raised before the outer Hermes terminal timeout can kill the script."""


class ScreenStateError(MytError):
    """Raised when the current Android screen cannot safely reach Facebook feed."""


@dataclass(frozen=True)
class Device:
    label: str
    port: int


@dataclass(frozen=True)
class UiTarget:
    description: str
    x: int
    y: int
    rect: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class PostSignature:
    digest: str
    preview: str
    reliable: bool


def log(message: str, *, error: bool = False) -> None:
    """Write a complete line without interleaving concurrent output."""
    with PRINT_LOCK:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


class MytClient:
    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        verbose: bool = False,
        deadline: float | None = None,
        ui_dump_retries: int = 4,
        ui_dump_retry_wait: float = 1.0,
    ):
        self.host = normalize_host(host)
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.deadline = deadline
        self.ui_dump_retries = ui_dump_retries
        self.ui_dump_retry_wait = ui_dump_retry_wait

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        timeout = self.timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeLimitError(
                    f"{self.host}:{self.port}: device runtime limit reached"
                )
            timeout = min(timeout, max(0.1, remaining))
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        if self.verbose:
            log(f"    [{self.port}] GET {url}")
        request = urllib.request.Request(
            url, headers={"User-Agent": "Hermes-MYT-Comment-Skill/2.7.2"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise RuntimeLimitError(
                    f"{self.host}:{self.port}: device runtime limit reached"
                ) from exc
            raise MytError(f"{self.host}:{self.port}: {exc}") from exc

    def remaining_runtime(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - time.monotonic()

    def ensure_time(self, context: str = "operation") -> None:
        remaining = self.remaining_runtime()
        if remaining is not None and remaining <= 0:
            raise RuntimeLimitError(
                f"{self.host}:{self.port}: device runtime limit reached during {context}"
            )

    def sleep(self, seconds: float, context: str = "wait") -> None:
        if seconds <= 0:
            self.ensure_time(context)
            return
        remaining = self.remaining_runtime()
        if remaining is not None:
            if remaining <= 0:
                raise RuntimeLimitError(
                    f"{self.host}:{self.port}: device runtime limit reached during {context}"
                )
            seconds = min(seconds, max(0.0, remaining))
        time.sleep(seconds)
        self.ensure_time(context)

    def shell(self, command: str) -> bytes:
        return self._get("/modifydev", {"cmd": "6", "cmdline": command})

    def check(self) -> None:
        self.shell("echo hermes_myt_ok")

    def force_stop_facebook(self) -> None:
        self.shell(f"am force-stop {FACEBOOK_PACKAGE}")

    def launch_facebook(self, force_restart: bool = False) -> None:
        if force_restart:
            self.force_stop_facebook()
            self.sleep(1, "force restart")
        self.shell("am start -a android.intent.action.VIEW -d fb:///")

    def swipe_up(self, swipe: tuple[int, int, int, int, int]) -> None:
        x1, y1, x2, y2, duration_ms = swipe
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def dump_ui(self) -> bytes:
        remote_path = "/sdcard/hermes_fb_comment.xml"
        last_error = "empty response"
        for attempt in range(1, self.ui_dump_retries + 1):
            try:
                self.shell(f"uiautomator dump {remote_path}")
                xml_data = self._get("/download", {"path": remote_path})
                if not xml_data.strip():
                    raise MytError("downloaded UI dump is empty")
                root = ET.fromstring(xml_data)
                if root.tag != "hierarchy":
                    raise MytError(f"unexpected UI root element: {root.tag}")
                if self.verbose and attempt > 1:
                    log(f"    [{self.port}] UI dump recovered on attempt {attempt}")
                return xml_data
            except RuntimeLimitError:
                raise
            except (MytError, ET.ParseError) as exc:
                last_error = str(exc)
                if attempt < self.ui_dump_retries:
                    if self.verbose:
                        log(
                            f"    [{self.port}] UI dump failed "
                            f"({attempt}/{self.ui_dump_retries}): {last_error}; retrying"
                        )
                    self.sleep(self.ui_dump_retry_wait, "UI dump retry")
        raise MytError(
            f"{self.host}:{self.port}: UI dump failed after "
            f"{self.ui_dump_retries} attempts: {last_error}"
        )

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")

    def keyevent(self, keycode: int) -> None:
        self.shell(f"input keyevent {keycode}")

    def input_text(self, value: str) -> None:
        encoded = encode_android_text(value)
        self.shell(f"input text {shlex.quote(encoded)}")


def normalize_host(value: str) -> str:
    host = value.strip().rstrip("/")
    if "://" in host:
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("MYT host is not a valid hostname or IP address")
        if parsed.port:
            raise ConfigurationError("Do not include a port in --host; use --devices")
        host = parsed.hostname
    if not host or "/" in host or ":" in host:
        raise ConfigurationError(
            "MYT host must be a hostname or IPv4 address without protocol or port"
        )
    return host


def parse_devices(value: str, base_port: int, stride: int) -> list[Device]:
    devices: list[Device] = []
    seen_ports: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip()
        if not item:
            continue
        match = re.fullmatch(r"(?i)T100(\d+)", item)
        if match:
            index = int(match.group(1))
            if index < 1:
                raise ConfigurationError(f"Invalid MYT device ID: {item}")
            port = base_port + (index - 1) * stride
            label = f"T100{index}"
        elif item.isdigit():
            port = int(item)
            label = f"port-{port}"
        else:
            raise ConfigurationError(
                f"Invalid device '{item}'; use IDs such as T1001 or numeric ports"
            )
        if not 1 <= port <= 65535:
            raise ConfigurationError(f"Port outside valid range: {port}")
        if port not in seen_ports:
            devices.append(Device(label=label, port=port))
            seen_ports.add(port)
    if not devices:
        raise ConfigurationError("At least one device ID or port is required")
    return devices


def parse_swipe(value: str) -> tuple[int, int, int, int, int]:
    try:
        parts = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ConfigurationError("--swipe values must be integers") from exc
    if len(parts) != 5 or any(part < 0 for part in parts):
        raise ConfigurationError("--swipe must be x1,y1,x2,y2,duration_ms")
    return parts  # type: ignore[return-value]


def encode_android_text(value: str) -> str:
    if not 1 <= len(value) <= 200:
        raise ConfigurationError("--comment must contain 1 to 200 characters")
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ConfigurationError(
            "--comment currently supports printable ASCII only; "
            "configure a Unicode-capable ADB input method for other text"
        )
    if "%" in value:
        raise ConfigurationError("--comment cannot contain '%' with Android input text")
    return value.replace(" ", "%s")


def parse_xml(xml_data: bytes) -> ET.Element:
    try:
        return ET.fromstring(xml_data)
    except ET.ParseError as exc:
        preview = xml_data[:160].decode("utf-8", errors="replace")
        raise MytError(f"Downloaded UI dump is not valid XML: {preview!r}") from exc


def node_rect(node: ET.Element) -> tuple[int, int, int, int] | None:
    match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def bounds_target(node: ET.Element, description: str) -> UiTarget | None:
    rect = node_rect(node)
    if rect is None:
        return None
    x1, y1, x2, y2 = rect
    return UiTarget(description, (x1 + x2) // 2, (y1 + y2) // 2, rect)


def node_description(node: ET.Element) -> str:
    return " | ".join(node_labels(node))


def node_labels(node: ET.Element) -> tuple[str, ...]:
    values = (
        node.attrib.get("content-desc", ""),
        node.attrib.get("text", ""),
    )
    return tuple(value.strip() for value in values if value.strip())


def contains_any(value: str, labels: Iterable[str]) -> bool:
    folded = value.casefold()
    return any(label.casefold() in folded for label in labels)


def normalize_comment_value(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def normalize_signature_label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def label_is_generic_for_signature(value: str) -> bool:
    folded = value.casefold()
    if not folded:
        return True
    if re.fullmatch(r"[\d\s,.:/]+", folded):
        return True
    normalized = normalize_accessibility_label(value)
    if normalized in COMMENT_BUTTON_LABELS:
        return True
    return any(term in folded for term in GENERIC_SIGNATURE_TERMS)


def post_signature_for_button(
    xml_data: bytes,
    button: UiTarget,
    window: int,
) -> PostSignature:
    root = parse_xml(xml_data)
    min_y = max(0, button.y - window)
    max_y = button.y + max(80, window // 5)
    labels: list[str] = []
    seen: set[str] = set()
    for node in root.iter():
        rect = node_rect(node)
        if rect is None:
            continue
        _, y1, _, y2 = rect
        center_y = (y1 + y2) // 2
        if center_y < min_y or center_y > max_y:
            continue
        for label in node_labels(node):
            normalized = normalize_signature_label(label)
            if label_is_generic_for_signature(normalized):
                continue
            folded = normalized.casefold()
            if folded not in seen:
                labels.append(normalized[:80])
                seen.add(folded)
    if labels:
        source = " | ".join(labels[:14])
        reliable = True
    else:
        source = f"button:{button.x // 20}:{button.y // 20}"
        reliable = False
    digest = hashlib.sha256(source.casefold().encode("utf-8")).hexdigest()[:20]
    return PostSignature(digest=digest, preview=source[:220], reliable=reliable)


def dedupe_comment_hash(value: str) -> str:
    normalized = normalize_comment_value(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def dedupe_key(device_label: str, comment: str, post_signature: PostSignature) -> str:
    if not post_signature.reliable:
        return ""
    return f"{device_label}:{dedupe_comment_hash(comment)}:{post_signature.digest}"


def visible_duplicate_comment_exists(xml_data: bytes, comment: str) -> bool:
    target = normalize_comment_value(comment)
    if not target:
        return False
    root = parse_xml(xml_data)
    for node in root.iter():
        if input_node_score(node) is not None:
            continue
        for label in node_labels(node):
            if normalize_comment_value(label) == target:
                return True
    return False


def load_dedupe_store(path: str, ttl_days: float) -> list[dict[str, object]]:
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log(f"WARNING: failed to read dedupe store {path}: {exc}", error=True)
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return []
    if ttl_days <= 0:
        return [entry for entry in entries if isinstance(entry, dict) and entry.get("key")]
    cutoff = time.time() - ttl_days * 86400
    valid_entries: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        try:
            created_at = float(entry.get("created_at", 0))
        except (TypeError, ValueError):
            continue
        if created_at >= cutoff:
            valid_entries.append(entry)
    return valid_entries


def save_dedupe_store(path: str, entries: list[dict[str, object]]) -> None:
    if not path:
        return
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    payload = {"version": 1, "entries": entries[-5000:]}
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def configure_dedupe(args: argparse.Namespace) -> None:
    args.dedupe_store_path = os.path.expanduser(args.dedupe_store)
    args.dedupe_entries = []
    args.dedupe_keys = set()
    if not args.dedupe or not args.dedupe_store_enabled:
        return
    entries = load_dedupe_store(args.dedupe_store_path, args.dedupe_ttl_days)
    args.dedupe_entries = entries
    args.dedupe_keys = {str(entry["key"]) for entry in entries if entry.get("key")}


def dedupe_key_seen(args: argparse.Namespace, key: str) -> bool:
    if not args.dedupe or not key:
        return False
    with DEDUPE_LOCK:
        return key in args.dedupe_keys


def record_dedupe_key(
    args: argparse.Namespace,
    key: str,
    device_label: str,
    post_signature: PostSignature,
) -> None:
    if not args.dedupe or not args.dedupe_store_enabled or not key:
        return
    with DEDUPE_LOCK:
        if key in args.dedupe_keys:
            return
        args.dedupe_keys.add(key)
        args.dedupe_entries.append(
            {
                "key": key,
                "device": device_label,
                "comment_hash": dedupe_comment_hash(args.comment),
                "post_hash": post_signature.digest,
                "post_preview": post_signature.preview,
                "created_at": time.time(),
            }
        )
        try:
            save_dedupe_store(args.dedupe_store_path, args.dedupe_entries)
        except OSError as exc:
            log(
                f"WARNING: failed to save dedupe store "
                f"{args.dedupe_store_path}: {exc}",
                error=True,
            )


def input_node_score(node: ET.Element) -> int | None:
    class_name = node.attrib.get("class", "").casefold()
    editable = node.attrib.get("editable", "").casefold() == "true"
    class_is_input = (
        class_name.endswith("edittext")
        or class_name.endswith("autocompletetextview")
    )
    description = node_description(node)
    label_is_input = contains_any(description, INPUT_LABELS)
    focusable = node.attrib.get("focusable", "").casefold() == "true"
    clickable = node.attrib.get("clickable", "").casefold() == "true"
    if not (
        editable
        or class_is_input
        or (label_is_input and (focusable or clickable))
    ):
        return None
    if node.attrib.get("enabled", "true").casefold() == "false":
        return None
    return (
        1
        + (20 if label_is_input else 0)
        + (10 if editable else 0)
        + (5 if class_is_input else 0)
        + (2 if focusable else 0)
    )


def normalize_accessibility_label(value: str) -> str:
    """Normalize spacing and punctuation while preserving letters and CJK text."""
    return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)


def is_comment_button_node(node: ET.Element) -> bool:
    """Accept only an explicit Comment control, never descriptive help text."""
    return any(
        normalize_accessibility_label(label) in COMMENT_BUTTON_LABELS
        for label in node_labels(node)
    )


def screen_height(root: ET.Element) -> int:
    height = 0
    for node in root.iter():
        rect = node_rect(node)
        if rect:
            height = max(height, rect[3])
    return height or 1280


def screen_width(root: ET.Element) -> int:
    width = 0
    for node in root.iter():
        rect = node_rect(node)
        if rect:
            width = max(width, rect[2])
    return width or 720


def find_comment_buttons(xml_data: bytes) -> list[UiTarget]:
    root = parse_xml(xml_data)
    height = screen_height(root)
    results: list[UiTarget] = []
    seen: set[tuple[int, int]] = set()
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if not is_comment_button_node(node):
            continue
        description = node_description(node)
        target = bounds_target(node, description)
        if not target:
            continue
        if target.y < int(height * 0.2) or target.y > int(height * 0.97):
            continue
        center = (target.x, target.y)
        if center not in seen:
            results.append(target)
            seen.add(center)
    return results


def find_input_field(xml_data: bytes) -> UiTarget | None:
    root = parse_xml(xml_data)
    candidates: list[tuple[int, UiTarget]] = []
    for node in root.iter():
        description = node_description(node)
        score = input_node_score(node)
        if score is None:
            continue
        target = bounds_target(
            node, description or node.attrib.get("class", "input field")
        )
        if target:
            candidates.append((score, target))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], -item[1].y))
    return candidates[0][1]


def input_node_contains_text(xml_data: bytes, value: str) -> bool:
    folded_value = value.casefold().strip()
    if not folded_value:
        return False
    root = parse_xml(xml_data)
    for node in root.iter():
        if input_node_score(node) is None:
            continue
        if any(folded_value in label.casefold() for label in node_labels(node)):
            return True
    return False


def find_send_icon_fallback(root: ET.Element, input_field: UiTarget) -> UiTarget | None:
    """Find icon-only paper-plane style Send controls near the composer."""
    if input_field.rect is None:
        return None
    input_x1, input_y1, input_x2, input_y2 = input_field.rect
    del input_x1
    width = screen_width(root)
    composer_top = max(0, input_y1 - 80)
    composer_bottom = input_y2 + 120
    min_x = max(input_field.x + 60, int(width * 0.72), input_x2 - 20)
    candidates: list[tuple[int, UiTarget]] = []

    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if node.attrib.get("enabled", "true").casefold() == "false":
            continue
        if input_node_score(node) is not None:
            continue
        rect = node_rect(node)
        if rect is None:
            continue
        x1, y1, x2, y2 = rect
        target_width = x2 - x1
        target_height = y2 - y1
        if target_width > 120 or target_height > 120:
            continue
        target = bounds_target(node, node_description(node) or "icon-only send")
        if target is None:
            continue
        if target.x < min_x:
            continue
        if target.y < composer_top or target.y > composer_bottom:
            continue
        if target.description and contains_any(target.description, NON_SEND_COMPOSER_LABELS):
            continue
        distance = abs(target.x - input_x2) + abs(target.y - input_field.y)
        candidates.append((distance, target))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def find_send_button(
    xml_data: bytes,
    input_field: UiTarget,
    *,
    allow_icon_fallback: bool = True,
) -> UiTarget | None:
    root = parse_xml(xml_data)
    candidates: list[tuple[int, UiTarget]] = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        if node.attrib.get("enabled", "true").casefold() == "false":
            continue
        if input_node_score(node) is not None:
            continue
        labels = node_labels(node)
        if not any(
            normalize_accessibility_label(label) in SEND_BUTTON_LABELS
            for label in labels
        ):
            continue
        description = " | ".join(labels)
        target = bounds_target(node, description)
        if target:
            distance = abs(target.x - input_field.x) + abs(target.y - input_field.y)
            candidates.append((distance, target))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
    if allow_icon_fallback:
        return find_send_icon_fallback(root, input_field)
    return None


def find_close_button(xml_data: bytes) -> UiTarget | None:
    root = parse_xml(xml_data)
    candidates: list[UiTarget] = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        description = node_description(node)
        if not description or not contains_any(description, CLOSE_LABELS):
            continue
        target = bounds_target(node, description)
        if target:
            candidates.append(target)
    return min(candidates, key=lambda target: target.y) if candidates else None


def screen_packages(xml_data: bytes) -> set[str]:
    root = parse_xml(xml_data)
    return {
        package
        for node in root.iter()
        if (package := node.attrib.get("package", "").strip())
    }


def classify_screen(xml_data: bytes) -> str:
    """Classify enough UI state to avoid searching forever on the wrong screen."""
    packages = screen_packages(xml_data)
    if packages and FACEBOOK_PACKAGE not in packages:
        return "other-app"
    if find_input_field(xml_data) and not find_comment_buttons(xml_data):
        return "comment-detail"
    if find_comment_buttons(xml_data):
        return "feed"
    if FACEBOOK_PACKAGE in packages:
        return "facebook-other"
    return "unknown"


def print_clickable_nodes(xml_data: bytes, device_label: str) -> None:
    root = parse_xml(xml_data)
    descriptions: list[str] = []
    for node in root.iter():
        flags = [
            name
            for name in ("clickable", "editable", "focusable")
            if node.attrib.get(name, "").casefold() == "true"
        ]
        if flags:
            description = node_description(node)
            class_name = node.attrib.get("class", "")
            bounds = node.attrib.get("bounds", "")
            descriptions.append(
                f"flags={','.join(flags)} class={class_name!r} "
                f"description={description!r} bounds={bounds!r}"
            )
    if descriptions:
        log(f"  [{device_label}] interactive node diagnostics:")
        for description in descriptions[:30]:
            log(f"    [{device_label}] - {description}")


def wait_for_feed_or_comment_state(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
    state: str,
) -> str:
    """Wait briefly for Facebook to finish rendering before entering the main loop."""
    if state in {"feed", "comment-detail"}:
        return state
    last_state = state
    for attempt in range(1, args.feed_ready_retries + 1):
        client.sleep(args.feed_ready_wait, "Facebook feed readiness")
        xml_data = client.dump_ui()
        last_state = classify_screen(xml_data)
        if args.verbose:
            log(
                f"  [{device_label}] feed readiness "
                f"{attempt}/{args.feed_ready_retries}: {last_state}"
            )
        if last_state in {"feed", "comment-detail"}:
            return last_state
        if last_state in {"facebook-other", "unknown"} and attempt < args.feed_ready_retries:
            client.swipe_up(args.swipe)
    raise ScreenStateError(
        f"Facebook feed did not become ready; last screen state={last_state}"
    )


def wait_for_input_field(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
) -> tuple[UiTarget | None, bytes]:
    """Poll while the Facebook comment panel finishes rendering."""
    last_xml = b""
    for attempt in range(1, args.panel_retries + 1):
        last_xml = client.dump_ui()
        input_field = find_input_field(last_xml)
        if input_field:
            if args.verbose and attempt > 1:
                log(f"  [{device_label}] input field found on attempt {attempt}")
            return input_field, last_xml
        if attempt < args.panel_retries:
            if args.verbose:
                log(
                    f"  [{device_label}] input field not ready "
                    f"({attempt}/{args.panel_retries}); retrying"
                )
            client.sleep(args.retry_wait, "input field wait")
    return None, last_xml


def wait_for_send_button(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
    input_field: UiTarget,
) -> tuple[UiTarget | None, bytes]:
    """Poll after text input because keyboard animation can delay Send."""
    last_xml = b""
    for attempt in range(1, args.send_retries + 1):
        last_xml = client.dump_ui()
        current_input = find_input_field(last_xml) or input_field
        send_button = find_send_button(
            last_xml,
            current_input,
            allow_icon_fallback=(
                args.send_icon_fallback
                and input_node_contains_text(last_xml, args.comment)
            ),
        )
        if send_button:
            if args.verbose:
                log(
                    f"  [{device_label}] send button found on attempt "
                    f"{attempt}: description={send_button.description!r} "
                    f"coordinates=({send_button.x}, {send_button.y})"
                )
            return send_button, last_xml
        if attempt < args.send_retries:
            if args.verbose:
                log(
                    f"  [{device_label}] send button not ready "
                    f"({attempt}/{args.send_retries}); retrying"
                )
            client.sleep(args.retry_wait, "send button wait")
    return None, last_xml


def verify_post_send_state(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
) -> tuple[bool, str, bytes]:
    """Verify that a Send tap changed the composer state before counting success."""
    last_xml = b""
    last_reason = "post-send verification did not run"
    for attempt in range(1, args.post_send_verify_retries + 1):
        wait = args.send_wait if attempt == 1 else args.post_send_verify_wait
        client.sleep(wait, "post-send verification")
        last_xml = client.dump_ui()
        state = classify_screen(last_xml)

        if state == "feed":
            return True, f"screen returned to feed on attempt {attempt}", last_xml
        if state != "comment-detail":
            last_reason = (
                f"post-send screen state is {state} on attempt {attempt}; "
                "not counted as verified"
            )
        elif input_node_contains_text(last_xml, args.comment):
            last_reason = (
                f"comment text is still present in the input field on attempt {attempt}"
            )
        else:
            input_field = find_input_field(last_xml)
            send_button = (
                find_send_button(last_xml, input_field, allow_icon_fallback=False)
                if input_field
                else None
            )
            if input_field is None:
                return True, f"comment composer closed on attempt {attempt}", last_xml
            if send_button is None:
                return True, f"send button disappeared on attempt {attempt}", last_xml
            last_reason = (
                f"comment composer and send button are still active on attempt {attempt}"
            )

        if args.verbose:
            log(
                f"  [{device_label}] post-send verification "
                f"{attempt}/{args.post_send_verify_retries}: {last_reason}"
            )
    return False, last_reason, last_xml


def close_comment_panel(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
) -> str:
    """Close at most one confirmed comment layer, then report the new state."""
    xml_data = client.dump_ui()
    state = classify_screen(xml_data)
    if state != "comment-detail":
        return state
    close_button = find_close_button(xml_data)
    if close_button:
        client.tap(close_button.x, close_button.y)
    else:
        client.keyevent(4)
    client.sleep(args.close_wait, "close comment panel")
    new_state = classify_screen(client.dump_ui())
    if args.verbose:
        log(
            f"  [{device_label}] closed one comment layer: "
            f"{state} -> {new_state}"
        )
    return new_state


def prepare_facebook_screen(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
) -> str:
    """Reach a safe Facebook screen without blindly pressing Back."""
    if not args.no_launch:
        client.launch_facebook(force_restart=args.force_restart)
        launch_mode = "force restart" if args.force_restart else "feed deep link"
        log(f"  [{device_label}] Facebook launch requested ({launch_mode})")
        client.sleep(args.launch_wait, "Facebook launch")

    xml_data = client.dump_ui()
    state = classify_screen(xml_data)
    if args.verbose:
        log(f"  [{device_label}] initial screen state: {state}")

    if state == "other-app":
        if args.no_launch:
            raise ScreenStateError(
                "current screen is not Facebook; rerun without --no-launch"
            )
        client.launch_facebook(force_restart=False)
        client.sleep(args.retry_wait, "Facebook relaunch")
        state = classify_screen(client.dump_ui())
        if state == "other-app":
            raise ScreenStateError(
                "Facebook feed deep link did not reach the Facebook app"
            )

    state = wait_for_feed_or_comment_state(client, args, device_label, state)

    for _ in range(2):
        if state != "comment-detail":
            break
        state = close_comment_panel(client, args, device_label)
    if state == "comment-detail":
        raise ScreenStateError(
            "comment detail remained open after two safe close attempts; "
            "retry with --force-restart"
        )
    if state == "other-app":
        raise ScreenStateError(
            "navigation left Facebook; rerun without --no-launch"
        )
    return state


def return_to_feed(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
) -> None:
    state = close_comment_panel(client, args, device_label)
    if state == "comment-detail":
        raise ScreenStateError("unable to leave the current comment detail")
    if state == "other-app":
        raise ScreenStateError("closing the comment panel left Facebook")
    for _ in range(args.skip_scrolls):
        client.swipe_up(args.swipe)
        client.sleep(args.scroll_wait, "skip commented post")
    if args.verbose:
        log(f"  [{device_label}] returned to feed")


def close_facebook_for_recovery(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
) -> None:
    method = args.recovery_close_method
    if method in {"recents-swipe", "both"}:
        log(f"  [{device_label}] recovery: opening recent apps and swiping app away")
        client.keyevent(187)
        client.sleep(args.recents_wait, "open recent apps")
        client.swipe_up(args.recents_swipe)
        client.sleep(args.app_close_wait, "swipe app away")
    if method in {"force-stop", "both"}:
        log(f"  [{device_label}] recovery: force-stopping Facebook package")
        client.force_stop_facebook()
        client.sleep(args.app_close_wait, "force-stop Facebook")


def recover_facebook(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
    summary: dict[str, object],
    reason: str,
) -> bool:
    if not args.auto_recover:
        if args.verbose:
            log(f"  [{device_label}] auto recovery disabled: {reason}")
        return False
    if args.no_launch:
        if args.verbose:
            log(f"  [{device_label}] auto recovery disabled by --no-launch: {reason}")
        return False
    if int(summary.get("recoveries", 0)) >= args.max_recoveries:
        log(
            f"  [{device_label}] recovery limit reached "
            f"({summary.get('recoveries', 0)}/{args.max_recoveries}): {reason}",
            error=True,
        )
        return False
    remaining = client.remaining_runtime()
    if remaining is not None and remaining < args.recovery_min_runtime:
        log(
            f"  [{device_label}] not enough runtime left for recovery "
            f"({remaining:.1f}s < {args.recovery_min_runtime:.1f}s): {reason}",
            error=True,
        )
        return False

    summary["recoveries"] = int(summary.get("recoveries", 0)) + 1
    summary["last_recovery"] = reason
    log(
        f"  [{device_label}] AUTO RECOVERY "
        f"{summary['recoveries']}/{args.max_recoveries}: {reason}"
    )
    try:
        close_facebook_for_recovery(client, args, device_label)
        state = prepare_facebook_screen(client, args, device_label)
        if args.verbose:
            log(f"  [{device_label}] recovery prepared screen state: {state}")
        for _ in range(args.recovery_scrolls):
            client.ensure_time("recovery scroll")
            client.swipe_up(args.swipe)
            client.sleep(args.scroll_wait, "recovery scroll")
        return True
    except RuntimeLimitError:
        raise
    except MytError as exc:
        summary["recovery_error"] = str(exc)
        log(f"  [{device_label}] RECOVERY FAILED: {exc}", error=True)
        return False


def record_verified_comment(
    args: argparse.Namespace,
    summary: dict[str, object],
    processed_keys: set[str],
    key: str,
    device_label: str,
    post_signature: PostSignature,
    verify_reason: str,
) -> None:
    summary["commented"] = int(summary["commented"]) + 1
    if key:
        processed_keys.add(key)
        record_dedupe_key(args, key, device_label, post_signature)
    log(
        f"  [{device_label}] send verified: {verify_reason} "
        f"({summary['commented']}/{args.count})"
    )


def run_device(
    args: argparse.Namespace,
    device: Device,
) -> dict[str, object]:
    started_at = time.monotonic()
    client = MytClient(
        args.host,
        device.port,
        args.timeout,
        args.verbose,
        deadline=started_at + args.max_runtime,
        ui_dump_retries=args.ui_dump_retries,
        ui_dump_retry_wait=args.ui_dump_retry_wait,
    )
    summary: dict[str, object] = {
        "device": device.label,
        "port": device.port,
        "commented": 0,
        "sent_taps": 0,
        "recoveries": 0,
        "duplicates_skipped": 0,
        "cycles": 0,
        "status": "ok",
    }
    log(f"[{device.label}] port={device.port}")
    try:
        client.check()
        log(f"  [{device.label}] connection: OK")
        if args.check:
            return summary
        try:
            screen_state = prepare_facebook_screen(client, args, device.label)
        except ScreenStateError as exc:
            if not recover_facebook(
                client, args, device.label, summary, f"initial screen error: {exc}"
            ):
                raise
            screen_state = "feed"
        except MytError as exc:
            if not recover_facebook(
                client,
                args,
                device.label,
                summary,
                f"initial Facebook preparation failed: {exc}",
            ):
                raise
            screen_state = "feed"
        if args.verbose:
            log(f"  [{device.label}] prepared screen state: {screen_state}")
        for _ in range(args.initial_scrolls):
            client.ensure_time("initial scroll")
            client.swipe_up(args.swipe)
            client.sleep(args.scroll_wait, "initial scroll")

        no_button_cycles = 0
        skipped_duplicate_keys: set[str] = set()
        processed_keys: set[str] = set()
        for cycle in range(1, args.max_cycles + 1):
            client.ensure_time("comment search")
            summary["cycles"] = cycle
            try:
                xml_data = client.dump_ui()
            except MytError as exc:
                if recover_facebook(
                    client, args, device.label, summary, f"UI dump failed: {exc}"
                ):
                    no_button_cycles = 0
                    continue
                raise
            buttons = find_comment_buttons(xml_data)
            state = classify_screen(xml_data)
            if args.verbose:
                log(
                    f"  [{device.label}] cycle {cycle}: "
                    f"{len(buttons)} comment candidate(s), state={state}"
                )
                if not buttons:
                    print_clickable_nodes(xml_data, device.label)
            if not buttons:
                no_button_cycles += 1
                if state in {"other-app", "unknown"}:
                    reason = f"unsafe screen state while searching comments: {state}"
                    if recover_facebook(client, args, device.label, summary, reason):
                        no_button_cycles = 0
                        continue
                    raise ScreenStateError(reason)
                if state == "facebook-other" and no_button_cycles >= args.not_feed_limit:
                    reason = "Facebook is open but Feed/comment controls did not load"
                    if recover_facebook(client, args, device.label, summary, reason):
                        no_button_cycles = 0
                        continue
                    raise ScreenStateError(reason)
                if no_button_cycles >= args.no_button_limit:
                    reason = (
                        f"no comment buttons found for {no_button_cycles} "
                        "consecutive cycles"
                    )
                    if recover_facebook(client, args, device.label, summary, reason):
                        no_button_cycles = 0
                        continue
                    summary["status"] = "no-comment-buttons"
                    summary["error"] = reason
                    return summary
                client.swipe_up(args.swipe)
                client.sleep(args.scroll_wait, "search scroll")
                continue
            no_button_cycles = 0

            candidate_infos: list[tuple[UiTarget, PostSignature, str]] = []
            for candidate in buttons:
                post_signature = post_signature_for_button(
                    xml_data, candidate, args.post_signature_window
                )
                key = (
                    dedupe_key(device.label, args.comment, post_signature)
                    if args.dedupe
                    else ""
                )
                if args.dedupe and (
                    key in processed_keys or dedupe_key_seen(args, key)
                ):
                    if key not in skipped_duplicate_keys:
                        summary["duplicates_skipped"] = (
                            int(summary["duplicates_skipped"]) + 1
                        )
                        skipped_duplicate_keys.add(key)
                    log(
                        f"  [{device.label}] duplicate post skipped "
                        f"signature={post_signature.digest} "
                        f"preview={post_signature.preview!r}"
                    )
                    continue
                candidate_infos.append((candidate, post_signature, key))

            if not candidate_infos:
                log(
                    f"  [{device.label}] all visible comment candidates were "
                    "already processed; scrolling"
                )
                client.swipe_up(args.swipe)
                client.sleep(args.scroll_wait, "duplicate skip scroll")
                continue

            button, post_signature, key = random.choice(candidate_infos)
            log(
                f"  [{device.label}] candidate: ({button.x}, {button.y}) "
                f"description={button.description!r} "
                f"post_signature={post_signature.digest}"
            )
            if not args.execute:
                log(
                    f"  [{device.label}] DRY RUN: no text entered or sent; "
                    "add --execute after user authorization"
                )
                summary["status"] = "dry-run"
                return summary

            try:
                client.tap(button.x, button.y)
                client.sleep(args.panel_wait, "comment panel open")
            except MytError as exc:
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    f"comment panel open failed: {exc}",
                ):
                    no_button_cycles = 0
                    continue
                raise
            try:
                input_field, panel_xml = wait_for_input_field(
                    client, args, device.label
                )
            except MytError as exc:
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    f"input field wait failed: {exc}",
                ):
                    no_button_cycles = 0
                    continue
                raise
            if not input_field:
                log(
                    f"  [{device.label}] input field not found after "
                    f"{args.panel_retries} attempts; skipping"
                )
                if args.verbose and panel_xml:
                    print_clickable_nodes(panel_xml, device.label)
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    "input field not found after opening comment panel",
                ):
                    no_button_cycles = 0
                    continue
                return_to_feed(client, args, device.label)
                continue

            if args.visible_dedupe and visible_duplicate_comment_exists(
                panel_xml, args.comment
            ):
                if key and key not in skipped_duplicate_keys:
                    summary["duplicates_skipped"] = (
                        int(summary["duplicates_skipped"]) + 1
                    )
                    skipped_duplicate_keys.add(key)
                if key:
                    processed_keys.add(key)
                log(
                    f"  [{device.label}] duplicate comment text already visible; "
                    f"skipping post signature={post_signature.digest}"
                )
                try:
                    return_to_feed(client, args, device.label)
                except MytError as exc:
                    if recover_facebook(
                        client,
                        args,
                        device.label,
                        summary,
                        f"return to feed failed after duplicate skip: {exc}",
                    ):
                        no_button_cycles = 0
                        continue
                    raise
                continue

            try:
                client.tap(input_field.x, input_field.y)
                client.sleep(args.input_wait, "focus input field")
                client.input_text(args.comment)
                client.sleep(args.input_wait, "text input")

                send_button, typed_xml = wait_for_send_button(
                    client, args, device.label, input_field
                )
            except MytError as exc:
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    f"text input or send button wait failed: {exc}",
                ):
                    no_button_cycles = 0
                    continue
                raise
            if not send_button:
                input_text_visible = bool(
                    typed_xml and input_node_contains_text(typed_xml, args.comment)
                )
                summary["last_input_text_visible"] = input_text_visible
                log(
                    f"  [{device.label}] send button not found after "
                    f"{args.send_retries} attempts; "
                    f"input_text_visible={input_text_visible}; comment not sent"
                )
                if args.verbose and typed_xml:
                    print_clickable_nodes(typed_xml, device.label)
                no_send_reason = (
                    "send button not found after entering text"
                    if input_text_visible
                    else "send button not found and comment text was not visible"
                )
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    no_send_reason,
                ):
                    no_button_cycles = 0
                    continue
                client.keyevent(4)
                client.sleep(0.5, "dismiss keyboard")
                return_to_feed(client, args, device.label)
                continue

            summary["sent_taps"] = int(summary["sent_taps"]) + 1
            try:
                client.tap(send_button.x, send_button.y)
            except MytError as exc:
                summary["status"] = "unverified-send"
                summary["error"] = f"send tap command could not be confirmed: {exc}"
                log(
                    f"  [{device.label}] UNVERIFIED SEND: send tap command "
                    "could not be confirmed; stop before risking duplicates",
                    error=True,
                )
                return summary
            log(
                f"  [{device.label}] send tap issued "
                f"(tap={summary['sent_taps']}, verified={summary['commented']}/{args.count})"
            )
            try:
                verified, verify_reason, verify_xml = verify_post_send_state(
                    client, args, device.label
                )
            except MytError as exc:
                summary["status"] = "unverified-send"
                summary["error"] = f"send tap could not be verified: {exc}"
                log(
                    f"  [{device.label}] UNVERIFIED SEND: verification failed; "
                    "stop before risking duplicate comments",
                    error=True,
                )
                return summary
            if not verified:
                summary["status"] = "unverified-send"
                summary["error"] = f"send tap could not be verified: {verify_reason}"
                log(
                    f"  [{device.label}] UNVERIFIED SEND: {verify_reason}; "
                    "stop before risking duplicate comments",
                    error=True,
                )
                if args.verbose and verify_xml:
                    print_clickable_nodes(verify_xml, device.label)
                return summary

            record_verified_comment(
                args,
                summary,
                processed_keys,
                key,
                device.label,
                post_signature,
                verify_reason,
            )
            if int(summary["commented"]) >= args.count:
                log(
                    f"  [{device.label}] target reached; "
                    "skipping final feed cleanup"
                )
                return summary
            try:
                return_to_feed(client, args, device.label)
            except MytError as exc:
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    f"return to feed failed after verified comment: {exc}",
                ):
                    no_button_cycles = 0
                    continue
                raise

        summary["status"] = "target-not-reached"
        return summary
    except RuntimeLimitError as exc:
        summary["status"] = "time-limit-reached"
        summary["error"] = str(exc)
        log(
            f"  [{device.label}] TIME LIMIT: returning partial result "
            f"before Hermes terminal timeout ({summary['commented']}/{args.count})",
            error=True,
        )
        return summary
    except ScreenStateError as exc:
        summary["status"] = "screen-state-error"
        summary["error"] = str(exc)
        log(f"  [{device.label}] SCREEN STATE: {exc}", error=True)
        return summary
    except MytError as exc:
        summary["status"] = "error"
        summary["error"] = str(exc)
        log(f"  [{device.label}] ERROR: {exc}", error=True)
        return summary


def run_devices(
    args: argparse.Namespace,
    devices: list[Device],
) -> list[dict[str, object]]:
    order = {device.port: index for index, device in enumerate(devices)}
    summaries: list[dict[str, object]] = []
    with ThreadPoolExecutor(
        max_workers=len(devices), thread_name_prefix="myt-facebook-comment"
    ) as executor:
        futures = {
            executor.submit(run_device, args, device): device for device in devices
        }
        for future in as_completed(futures):
            device = futures[future]
            try:
                summaries.append(future.result())
            except Exception as exc:
                log(f"  [{device.label}] UNEXPECTED ERROR: {exc}", error=True)
                summaries.append(
                    {
                        "device": device.label,
                        "port": device.port,
                        "commented": 0,
                        "sent_taps": 0,
                        "recoveries": 0,
                        "duplicates_skipped": 0,
                        "cycles": 0,
                        "status": "error",
                        "error": str(exc),
                    }
                )
    summaries.sort(key=lambda item: order[int(item["port"])])
    return summaries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find Facebook comment buttons through the MYT V1 API. "
            "The default is a dry run; --execute is required to send."
        )
    )
    parser.add_argument("--host", default=os.getenv("MYT_HOST", ""))
    parser.add_argument(
        "--devices", default=os.getenv("MYT_DEVICE_IDS", "T1001,T1002")
    )
    parser.add_argument(
        "--count",
        type=int,
        default=None,
        help="required number of comments per device (except with --check)",
    )
    parser.add_argument(
        "--comment",
        default=None,
        help="required printable ASCII comment text (except with --check)",
    )
    parser.add_argument(
        "--max-cycles", type=int, default=os.getenv("MYT_MAX_CYCLES", "30")
    )
    parser.add_argument(
        "--base-port", type=int, default=os.getenv("MYT_BASE_PORT", "10005")
    )
    parser.add_argument(
        "--port-stride", type=int, default=os.getenv("MYT_PORT_STRIDE", "3")
    )
    parser.add_argument(
        "--timeout", type=float, default=os.getenv("MYT_TIMEOUT", "15")
    )
    parser.add_argument(
        "--max-runtime",
        type=float,
        default=os.getenv("MYT_MAX_RUNTIME", "80"),
        help="per-device runtime limit in seconds; default 80",
    )
    parser.add_argument(
        "--ui-dump-retries", type=int, default=4,
        help="attempts for each empty or invalid UI dump; default 4",
    )
    parser.add_argument(
        "--ui-dump-retry-wait", type=float, default=1.0,
        help="seconds between UI dump attempts; default 1",
    )
    parser.add_argument(
        "--swipe",
        type=parse_swipe,
        default=os.getenv("MYT_SWIPE", "360,1000,360,200,500"),
        help="x1,y1,x2,y2,duration_ms",
    )
    parser.add_argument("--initial-scrolls", type=int, default=3)
    parser.add_argument("--skip-scrolls", type=int, default=4)
    parser.add_argument("--launch-wait", type=float, default=8.0)
    parser.add_argument(
        "--feed-ready-retries",
        type=int,
        default=5,
        help="wait attempts for Facebook Feed/comment controls after launch; default 5",
    )
    parser.add_argument(
        "--feed-ready-wait",
        type=float,
        default=1.5,
        help="seconds between Feed readiness checks; default 1.5",
    )
    parser.add_argument("--scroll-wait", type=float, default=2.0)
    parser.add_argument("--panel-wait", type=float, default=2.0)
    parser.add_argument("--panel-retries", type=int, default=4)
    parser.add_argument("--send-retries", type=int, default=3)
    parser.add_argument(
        "--no-send-icon-fallback",
        dest="send_icon_fallback",
        action="store_false",
        default=True,
        help="disable icon-only Send detection near the comment composer",
    )
    parser.add_argument(
        "--not-feed-limit",
        type=int,
        default=4,
        help="consecutive facebook-other states before aborting; default 4",
    )
    parser.add_argument(
        "--no-button-limit",
        type=int,
        default=8,
        help="consecutive no-comment-button cycles before returning partial summary; default 8",
    )
    parser.add_argument("--retry-wait", type=float, default=1.0)
    parser.add_argument("--input-wait", type=float, default=1.5)
    parser.add_argument("--send-wait", type=float, default=3.0)
    parser.add_argument(
        "--post-send-verify-retries",
        type=int,
        default=3,
        help="attempts to verify UI state after tapping Send; default 3",
    )
    parser.add_argument(
        "--post-send-verify-wait",
        type=float,
        default=1.5,
        help="seconds between post-send verification attempts after the first; default 1.5",
    )
    parser.add_argument(
        "--no-dedupe",
        dest="dedupe",
        action="store_false",
        default=True,
        help="disable post-signature and persistent duplicate protection",
    )
    parser.add_argument(
        "--no-visible-dedupe",
        dest="visible_dedupe",
        action="store_false",
        default=True,
        help="disable skipping when the same comment text is already visible",
    )
    parser.add_argument(
        "--no-dedupe-store",
        dest="dedupe_store_enabled",
        action="store_false",
        default=True,
        help="disable the persistent local dedupe store",
    )
    parser.add_argument(
        "--dedupe-store",
        default=os.getenv(
            "MYT_DEDUPE_STORE",
            "~/.hermes/state/facebook-daily-comment-dedupe.json",
        ),
        help="local JSON store for processed post signatures",
    )
    parser.add_argument(
        "--dedupe-ttl-days",
        type=float,
        default=os.getenv("MYT_DEDUPE_TTL_DAYS", "14"),
        help="days to remember processed post signatures; default 14",
    )
    parser.add_argument(
        "--post-signature-window",
        type=int,
        default=520,
        help="vertical pixels above each Comment button used for post signature; default 520",
    )
    parser.add_argument(
        "--no-auto-recover",
        dest="auto_recover",
        action="store_false",
        default=True,
        help="disable automatic Facebook close/relaunch recovery",
    )
    parser.add_argument(
        "--max-recoveries",
        type=int,
        default=os.getenv("MYT_MAX_RECOVERIES", "2"),
        help="maximum automatic recovery attempts per device; default 2",
    )
    parser.add_argument(
        "--recovery-close-method",
        choices=("recents-swipe", "force-stop", "both"),
        default=os.getenv("MYT_RECOVERY_CLOSE_METHOD", "both"),
        help="how to close Facebook during recovery; default both",
    )
    parser.add_argument(
        "--recents-swipe",
        type=parse_swipe,
        default=os.getenv("MYT_RECENTS_SWIPE", "360,900,360,120,500"),
        help="recent-app-card swipe used by recovery: x1,y1,x2,y2,duration_ms",
    )
    parser.add_argument(
        "--recents-wait",
        type=float,
        default=1.0,
        help="seconds to wait after opening Android recent apps; default 1",
    )
    parser.add_argument(
        "--app-close-wait",
        type=float,
        default=2.0,
        help="seconds to wait after closing Facebook during recovery; default 2",
    )
    parser.add_argument(
        "--recovery-scrolls",
        type=int,
        default=1,
        help="feed scrolls after recovery relaunch; default 1",
    )
    parser.add_argument(
        "--recovery-min-runtime",
        type=float,
        default=20.0,
        help="minimum seconds left before attempting recovery; default 20",
    )
    parser.add_argument("--close-wait", type=float, default=2.0)
    parser.add_argument("--check", action="store_true", help="only test API access")
    parser.add_argument("--no-launch", action="store_true")
    parser.add_argument(
        "--force-restart",
        action="store_true",
        help="force-stop Facebook before launch; normally the app is only resumed",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="enter and send comments; otherwise perform a dry run",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="print final summary as JSON")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not args.host:
        raise ConfigurationError("MYT_HOST is missing; set it or pass --host")
    args.host = normalize_host(args.host)
    if not args.check:
        if args.count is None:
            raise ConfigurationError("--count is required for dry-run and execution")
        if args.comment is None:
            raise ConfigurationError("--comment is required for dry-run and execution")
        encode_android_text(args.comment)
    if args.count is not None and args.count < 1:
        raise ConfigurationError("--count must be at least 1")
    if args.max_cycles < 1:
        raise ConfigurationError("--max-cycles must be at least 1")
    if args.port_stride < 1:
        raise ConfigurationError("--port-stride must be at least 1")
    if args.timeout <= 0:
        raise ConfigurationError("--timeout must be greater than 0")
    if args.max_runtime <= 0:
        raise ConfigurationError("--max-runtime must be greater than 0")
    if args.panel_retries < 1 or args.send_retries < 1:
        raise ConfigurationError("retry counts must be at least 1")
    if args.post_send_verify_retries < 1:
        raise ConfigurationError("--post-send-verify-retries must be at least 1")
    if args.ui_dump_retries < 1:
        raise ConfigurationError("--ui-dump-retries must be at least 1")
    if args.feed_ready_retries < 1:
        raise ConfigurationError("--feed-ready-retries must be at least 1")
    if args.not_feed_limit < 1 or args.no_button_limit < 1:
        raise ConfigurationError(
            "--not-feed-limit and --no-button-limit must be at least 1"
        )
    if args.max_recoveries < 0:
        raise ConfigurationError("--max-recoveries cannot be negative")
    if args.dedupe_ttl_days < 0:
        raise ConfigurationError("--dedupe-ttl-days cannot be negative")
    if args.post_signature_window < 80:
        raise ConfigurationError("--post-signature-window must be at least 80")
    if args.no_launch and args.force_restart:
        raise ConfigurationError("--no-launch and --force-restart cannot be combined")
    if args.execute and args.verbose:
        raise ConfigurationError(
            "--execute and --verbose cannot be combined; remove --execute for diagnosis"
        )
    if args.initial_scrolls < 0 or args.skip_scrolls < 0 or args.recovery_scrolls < 0:
        raise ConfigurationError("scroll counts cannot be negative")
    for name in (
        "launch_wait",
        "feed_ready_wait",
        "scroll_wait",
        "panel_wait",
        "retry_wait",
        "ui_dump_retry_wait",
        "input_wait",
        "send_wait",
        "post_send_verify_wait",
        "recents_wait",
        "app_close_wait",
        "recovery_min_runtime",
        "close_wait",
    ):
        if getattr(args, name) < 0:
            raise ConfigurationError(f"--{name.replace('_', '-')} cannot be negative")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate_args(args)
        configure_dedupe(args)
        devices = parse_devices(args.devices, args.base_port, args.port_stride)
    except (ConfigurationError, ValueError) as exc:
        parser.error(str(exc))

    mode = "CHECK" if args.check else ("EXECUTE" if args.execute else "DRY RUN")
    log(
        f"=== Facebook Daily Comment | {mode} | host={args.host} | "
        f"devices={','.join(device.label for device in devices)} ==="
    )
    log(f"Starting {len(devices)} device task(s) concurrently")
    summaries = run_devices(args, devices)
    target_count = args.count or 0
    for item in summaries:
        item["target"] = target_count
        item["remaining"] = max(0, target_count - int(item["commented"]))
        item["recoveries"] = int(item.get("recoveries", 0))
        item["duplicates_skipped"] = int(item.get("duplicates_skipped", 0))
        item["unverified"] = max(
            0, int(item.get("sent_taps", 0)) - int(item["commented"])
        )
    log("=== Summary ===")
    for item in summaries:
        log(
            f"  {item['device']} port={item['port']} status={item['status']} "
            f"commented={item['commented']}/{item['target']} "
            f"sent_taps={item.get('sent_taps', 0)} "
            f"unverified={item['unverified']} "
            f"recoveries={item['recoveries']} "
            f"duplicates_skipped={item['duplicates_skipped']} "
            f"remaining={item['remaining']} cycles={item['cycles']}"
        )
    log(f"RESULT_JSON: {json.dumps(summaries, ensure_ascii=False)}")
    incomplete = [item for item in summaries if int(item["remaining"]) > 0]
    if incomplete and args.execute:
        unverified = [
            item for item in incomplete if int(item.get("unverified", 0)) > 0
        ]
        remaining_text = ", ".join(
            f"{item['device']}={item['remaining']}" for item in incomplete
        )
        log(f"REMAINING: {remaining_text}")
        if unverified:
            unverified_text = ", ".join(
                f"{item['device']} sent_taps={item.get('sent_taps', 0)} "
                f"verified={item['commented']}"
                for item in unverified
            )
            log(f"UNVERIFIED SENDS: {unverified_text}")
        log(
            "RETRY RULE: retry only incomplete devices with --count set to "
            "that device's remaining value; use the default feed deep link. "
            "For status=unverified-send, manually inspect the device first and "
            "subtract any visible posted comment before retrying. "
            "Do not add --no-launch unless the device was manually verified "
            "to be on Feed, and never reuse the original total count"
        )
    if args.json:
        log(json.dumps(summaries, ensure_ascii=False))

    if args.check:
        success = all(item["status"] == "ok" for item in summaries)
    elif args.execute:
        success = all(
            item["status"] == "ok" and int(item["commented"]) >= args.count
            for item in summaries
        )
    else:
        success = all(item["status"] == "dry-run" for item in summaries)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
