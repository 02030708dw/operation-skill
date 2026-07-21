#!/usr/bin/env python3
"""Find Facebook posts and optionally comment through the MYT V1 HTTP API."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
SEND_LABELS = ("发送", "傳送", "送出", "发布", "發佈", "post", "send")
CLOSE_LABELS = ("关闭", "關閉", "close")
FACEBOOK_PACKAGE = "com.facebook.katana"


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
            url, headers={"User-Agent": "Hermes-MYT-Comment-Skill/2.1"}
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

    def shell(self, command: str) -> bytes:
        return self._get("/modifydev", {"cmd": "6", "cmdline": command})

    def check(self) -> None:
        self.shell("echo hermes_myt_ok")

    def launch_facebook(self, force_restart: bool = False) -> None:
        if force_restart:
            self.shell("am force-stop com.facebook.katana")
            time.sleep(1)
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
                    time.sleep(self.ui_dump_retry_wait)
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


def bounds_target(node: ET.Element, description: str) -> UiTarget | None:
    match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
    if not match:
        return None
    x1, y1, x2, y2 = (int(value) for value in match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return UiTarget(description, (x1 + x2) // 2, (y1 + y2) // 2)


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
        match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
        if match:
            height = max(height, int(match.group(4)))
    return height or 1280


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
        if not (editable or class_is_input or (label_is_input and (focusable or clickable))):
            continue
        if node.attrib.get("enabled", "true").casefold() == "false":
            continue
        target = bounds_target(node, description or class_name)
        if target:
            score = (
                1
                + (20 if label_is_input else 0)
                + (10 if editable else 0)
                + (5 if class_is_input else 0)
                + (2 if focusable else 0)
            )
            candidates.append((score, target))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], -item[1].y))
    return candidates[0][1]


def find_send_button(xml_data: bytes, input_field: UiTarget) -> UiTarget | None:
    root = parse_xml(xml_data)
    candidates: list[tuple[int, UiTarget]] = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        description = node_description(node)
        if not description or not contains_any(description, SEND_LABELS):
            continue
        target = bounds_target(node, description)
        if target:
            distance = abs(target.x - input_field.x) + abs(target.y - input_field.y)
            candidates.append((distance, target))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


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
            time.sleep(args.retry_wait)
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
        send_button = find_send_button(last_xml, current_input)
        if send_button:
            if args.verbose and attempt > 1:
                log(f"  [{device_label}] send button found on attempt {attempt}")
            return send_button, last_xml
        if attempt < args.send_retries:
            if args.verbose:
                log(
                    f"  [{device_label}] send button not ready "
                    f"({attempt}/{args.send_retries}); retrying"
                )
            time.sleep(args.retry_wait)
    return None, last_xml


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
    time.sleep(args.close_wait)
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
        time.sleep(args.launch_wait)

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
        time.sleep(args.retry_wait)
        state = classify_screen(client.dump_ui())
        if state == "other-app":
            raise ScreenStateError(
                "Facebook feed deep link did not reach the Facebook app"
            )

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
        time.sleep(args.scroll_wait)
    if args.verbose:
        log(f"  [{device_label}] returned to feed")


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
        "cycles": 0,
        "status": "ok",
    }
    log(f"[{device.label}] port={device.port}")
    try:
        client.check()
        log(f"  [{device.label}] connection: OK")
        if args.check:
            return summary
        screen_state = prepare_facebook_screen(client, args, device.label)
        if args.verbose:
            log(f"  [{device.label}] prepared screen state: {screen_state}")
        for _ in range(args.initial_scrolls):
            client.swipe_up(args.swipe)
            time.sleep(args.scroll_wait)

        for cycle in range(1, args.max_cycles + 1):
            summary["cycles"] = cycle
            xml_data = client.dump_ui()
            buttons = find_comment_buttons(xml_data)
            if args.verbose:
                log(
                    f"  [{device.label}] cycle {cycle}: "
                    f"{len(buttons)} comment candidate(s)"
                )
                if not buttons:
                    print_clickable_nodes(xml_data, device.label)
            if not buttons:
                client.swipe_up(args.swipe)
                time.sleep(args.scroll_wait)
                continue

            button = random.choice(buttons)
            log(
                f"  [{device.label}] candidate: ({button.x}, {button.y}) "
                f"description={button.description!r}"
            )
            if not args.execute:
                log(
                    f"  [{device.label}] DRY RUN: no text entered or sent; "
                    "add --execute after user authorization"
                )
                summary["status"] = "dry-run"
                return summary

            client.tap(button.x, button.y)
            time.sleep(args.panel_wait)
            input_field, panel_xml = wait_for_input_field(
                client, args, device.label
            )
            if not input_field:
                log(
                    f"  [{device.label}] input field not found after "
                    f"{args.panel_retries} attempts; skipping"
                )
                if args.verbose and panel_xml:
                    print_clickable_nodes(panel_xml, device.label)
                return_to_feed(client, args, device.label)
                continue

            client.tap(input_field.x, input_field.y)
            time.sleep(args.input_wait)
            client.input_text(args.comment)
            time.sleep(args.input_wait)

            send_button, typed_xml = wait_for_send_button(
                client, args, device.label, input_field
            )
            if not send_button:
                log(
                    f"  [{device.label}] send button not found after "
                    f"{args.send_retries} attempts; comment not sent"
                )
                if args.verbose and typed_xml:
                    print_clickable_nodes(typed_xml, device.label)
                client.keyevent(4)
                time.sleep(0.5)
                return_to_feed(client, args, device.label)
                continue

            client.tap(send_button.x, send_button.y)
            summary["commented"] = int(summary["commented"]) + 1
            log(
                f"  [{device.label}] send tap issued "
                f"({summary['commented']}/{args.count})"
            )
            time.sleep(args.send_wait)
            if int(summary["commented"]) >= args.count:
                log(
                    f"  [{device.label}] target reached; "
                    "skipping final feed cleanup"
                )
                return summary
            return_to_feed(client, args, device.label)

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
        default=os.getenv("MYT_MAX_RUNTIME", "105"),
        help="per-device runtime limit in seconds; default 105",
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
    parser.add_argument("--scroll-wait", type=float, default=2.0)
    parser.add_argument("--panel-wait", type=float, default=2.0)
    parser.add_argument("--panel-retries", type=int, default=4)
    parser.add_argument("--send-retries", type=int, default=3)
    parser.add_argument("--retry-wait", type=float, default=1.0)
    parser.add_argument("--input-wait", type=float, default=1.5)
    parser.add_argument("--send-wait", type=float, default=3.0)
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
    if args.ui_dump_retries < 1:
        raise ConfigurationError("--ui-dump-retries must be at least 1")
    if args.no_launch and args.force_restart:
        raise ConfigurationError("--no-launch and --force-restart cannot be combined")
    if args.initial_scrolls < 0 or args.skip_scrolls < 0:
        raise ConfigurationError("scroll counts cannot be negative")
    for name in (
        "launch_wait",
        "scroll_wait",
        "panel_wait",
        "retry_wait",
        "ui_dump_retry_wait",
        "input_wait",
        "send_wait",
        "close_wait",
    ):
        if getattr(args, name) < 0:
            raise ConfigurationError(f"--{name.replace('_', '-')} cannot be negative")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate_args(args)
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
    log("=== Summary ===")
    for item in summaries:
        log(
            f"  {item['device']} port={item['port']} status={item['status']} "
            f"commented={item['commented']}/{item['target']} "
            f"remaining={item['remaining']} cycles={item['cycles']}"
        )
    log(f"RESULT_JSON: {json.dumps(summaries, ensure_ascii=False)}")
    incomplete = [item for item in summaries if int(item["remaining"]) > 0]
    if incomplete and args.execute:
        remaining_text = ", ".join(
            f"{item['device']}={item['remaining']}" for item in incomplete
        )
        log(f"REMAINING: {remaining_text}")
        log(
            "RETRY RULE: retry only incomplete devices with --count set to "
            "that device's remaining value; use the default feed deep link. "
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
