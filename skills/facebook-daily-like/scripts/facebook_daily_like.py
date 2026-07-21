#!/usr/bin/env python3
"""Find and optionally click unliked Facebook buttons through the MYT V1 API."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable


DEFAULT_INCLUDE_LABELS = ("赞", "讚", "like")
DEFAULT_EXCLUDE_LABELS = (
    "已按下",
    "已赞",
    "已讚",
    "unlike",
    "remove like",
    "取消赞",
    "取消讚",
)
DEFAULT_CONTEXT_EXCLUDE_LABELS = (
    "comment",
    "reply",
    "react to",
    "评论",
    "評論",
    "回复",
    "回覆",
    "留下心情",
)
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
PRINT_LOCK = threading.Lock()
FACEBOOK_PACKAGE = "com.facebook.katana"


class ConfigurationError(ValueError):
    """Raised when required local configuration is missing or invalid."""


class MytError(RuntimeError):
    """Raised when the MYT API cannot complete an operation."""


class RuntimeLimitError(MytError):
    """Raised before the outer Hermes terminal timeout can kill the script."""


class ScreenStateError(MytError):
    """Raised when the current Android screen cannot safely reach Facebook feed."""


def log(message: str, *, error: bool = False) -> None:
    """Write one complete line without interleaving concurrent device output."""
    with PRINT_LOCK:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


@dataclass(frozen=True)
class Device:
    label: str
    port: int


@dataclass(frozen=True)
class LikeButton:
    description: str
    x: int
    y: int


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
            safe_url = url.replace("%2F", "/")
            log(f"    [{self.port}] GET {safe_url}")
        request = urllib.request.Request(
            url, headers={"User-Agent": "Hermes-MYT-Like-Skill/2.2.1"}
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
        remote_path = "/sdcard/hermes_fb_like.xml"
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


def parse_csv_labels(value: str) -> tuple[str, ...]:
    labels = tuple(label.strip().casefold() for label in value.split(",") if label.strip())
    if not labels:
        raise ConfigurationError("Label lists cannot be empty")
    return labels


def extract_description(node: ET.Element) -> str:
    candidates = (
        node.attrib.get("content-desc", ""),
        node.attrib.get("text", ""),
    )
    return " | ".join(value.strip() for value in candidates if value.strip())


def parse_xml(xml_data: bytes) -> ET.Element:
    try:
        return ET.fromstring(xml_data)
    except ET.ParseError as exc:
        preview = xml_data[:160].decode("utf-8", errors="replace")
        raise MytError(f"Downloaded UI dump is not valid XML: {preview!r}") from exc


def is_feed_like_description(
    description: str,
    include_labels: Iterable[str],
) -> bool:
    """Accept a Like control while excluding comment/reply reaction controls."""
    folded = description.casefold()
    return bool(description) and any(
        label.casefold() in folded for label in include_labels
    ) and not any(label in folded for label in DEFAULT_CONTEXT_EXCLUDE_LABELS)


def node_rect(node: ET.Element) -> tuple[int, int, int, int] | None:
    bounds_match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
    if not bounds_match:
        return None
    x1, y1, x2, y2 = (int(value) for value in bounds_match.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def node_center(node: ET.Element) -> tuple[int, int] | None:
    rect = node_rect(node)
    if rect is None:
        return None
    x1, y1, x2, y2 = rect
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_like_buttons(
    xml_data: bytes,
    include_labels: Iterable[str] = DEFAULT_INCLUDE_LABELS,
    exclude_labels: Iterable[str] = DEFAULT_EXCLUDE_LABELS,
) -> list[LikeButton]:
    root = parse_xml(xml_data)

    include = tuple(label.casefold() for label in include_labels)
    exclude = tuple(label.casefold() for label in exclude_labels)
    results: list[LikeButton] = []
    seen: set[tuple[int, int]] = set()

    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        description = extract_description(node)
        folded = description.casefold()
        if not is_feed_like_description(description, include):
            continue
        if any(label in folded for label in exclude):
            continue
        center = node_center(node)
        if center is None:
            continue
        if center in seen:
            continue
        seen.add(center)
        results.append(
            LikeButton(description=description, x=center[0], y=center[1])
        )
    return results


def screen_packages(xml_data: bytes) -> set[str]:
    root = parse_xml(xml_data)
    return {
        package
        for node in root.iter()
        if (package := node.attrib.get("package", "").strip())
    }


def classify_screen(
    xml_data: bytes,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
) -> str:
    packages = screen_packages(xml_data)
    if packages and FACEBOOK_PACKAGE not in packages:
        return "other-app"
    if find_like_buttons(xml_data, include_labels, ()):
        return "feed"
    if FACEBOOK_PACKAGE in packages:
        return "facebook-other"
    return "unknown"


def button_distance(first: LikeButton, second: LikeButton) -> int:
    return abs(first.x - second.x) + abs(first.y - second.y)


def pressed_like_near(
    xml_data: bytes,
    target: LikeButton,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
    radius: int,
) -> str | None:
    root = parse_xml(xml_data)
    include = tuple(label.casefold() for label in include_labels)
    exclude = tuple(label.casefold() for label in exclude_labels)
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        description = extract_description(node)
        folded = description.casefold()
        if not is_feed_like_description(description, include):
            continue
        if not any(label in folded for label in exclude):
            continue
        center = node_center(node)
        if center is None:
            continue
        distance = abs(center[0] - target.x) + abs(center[1] - target.y)
        if distance <= radius:
            return description
    return None


def unliked_like_near(
    xml_data: bytes,
    target: LikeButton,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
    radius: int,
) -> LikeButton | None:
    nearby = [
        button
        for button in find_like_buttons(xml_data, include_labels, exclude_labels)
        if button_distance(button, target) <= radius
    ]
    if not nearby:
        return None
    return min(nearby, key=lambda button: button_distance(button, target))


def verify_like_after_tap(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
    button: LikeButton,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
) -> tuple[bool, str, bytes]:
    last_xml = b""
    last_reason = "like verification did not run"
    for attempt in range(1, args.like_verify_retries + 1):
        wait = args.tap_wait if attempt == 1 else args.like_verify_wait
        client.sleep(wait, "like verification")
        last_xml = client.dump_ui()
        state = classify_screen(last_xml, include_labels, exclude_labels)
        if state != "feed":
            last_reason = (
                f"post-tap screen state is {state} on attempt {attempt}; "
                "not counted as verified"
            )
        else:
            pressed = pressed_like_near(
                last_xml,
                button,
                include_labels,
                exclude_labels,
                args.like_verify_radius,
            )
            if pressed:
                return True, f"pressed Like state visible: {pressed!r}", last_xml
            still_unliked = unliked_like_near(
                last_xml,
                button,
                include_labels,
                exclude_labels,
                args.like_verify_radius,
            )
            if still_unliked is None:
                return True, f"unliked Like button disappeared on attempt {attempt}", last_xml
            last_reason = (
                f"unliked Like button still visible near tap target on attempt {attempt}: "
                f"{still_unliked.description!r}"
            )
        if args.verbose:
            log(
                f"  [{device_label}] like verification "
                f"{attempt}/{args.like_verify_retries}: {last_reason}"
            )
    return False, last_reason, last_xml


def print_clickable_nodes(xml_data: bytes, device_label: str) -> None:
    root = parse_xml(xml_data)
    descriptions = []
    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() == "true":
            description = extract_description(node)
            if description:
                descriptions.append(description)
    if descriptions:
        log(f"  [{device_label}] clickable node descriptions:")
        for description in descriptions[:30]:
            log(f"    [{device_label}] - {description}")


def wait_for_feed_state(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
) -> str:
    last_state = "unknown"
    for attempt in range(1, args.feed_ready_retries + 1):
        xml_data = client.dump_ui()
        last_state = classify_screen(xml_data, include_labels, exclude_labels)
        if args.verbose:
            log(
                f"  [{device_label}] feed readiness "
                f"{attempt}/{args.feed_ready_retries}: {last_state}"
            )
        if last_state == "feed":
            return last_state
        if last_state in {"facebook-other", "unknown"} and attempt < args.feed_ready_retries:
            client.swipe_up(args.swipe)
        if attempt < args.feed_ready_retries:
            client.sleep(args.feed_ready_wait, "Facebook feed readiness")
    raise ScreenStateError(
        f"Facebook feed did not become ready; last screen state={last_state}"
    )


def prepare_facebook_screen(
    client: MytClient,
    args: argparse.Namespace,
    device_label: str,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
) -> str:
    if not args.no_launch:
        client.launch_facebook(force_restart=args.force_restart)
        launch_mode = "force restart" if args.force_restart else "feed deep link"
        log(f"  [{device_label}] Facebook launch requested ({launch_mode})")
        client.sleep(args.launch_wait, "Facebook launch")

    xml_data = client.dump_ui()
    state = classify_screen(xml_data, include_labels, exclude_labels)
    if args.verbose:
        log(f"  [{device_label}] initial screen state: {state}")
    if state == "other-app":
        if args.no_launch:
            raise ScreenStateError(
                "current screen is not Facebook; rerun without --no-launch"
            )
        client.launch_facebook(force_restart=False)
        client.sleep(args.feed_ready_wait, "Facebook relaunch")
        state = classify_screen(client.dump_ui(), include_labels, exclude_labels)
        if state == "other-app":
            raise ScreenStateError(
                "Facebook feed deep link did not reach the Facebook app"
            )
    if state in {"feed", "facebook-other"}:
        return state
    if state != "feed":
        state = wait_for_feed_state(
            client, args, device_label, include_labels, exclude_labels
        )
    return state


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
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
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
        prepare_facebook_screen(
            client, args, device_label, include_labels, exclude_labels
        )
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


def run_device(
    args: argparse.Namespace,
    device: Device,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
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
        "liked": 0,
        "tap_sent": 0,
        "recoveries": 0,
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
            prepare_facebook_screen(
                client, args, device.label, include_labels, exclude_labels
            )
        except MytError as exc:
            if not recover_facebook(
                client,
                args,
                device.label,
                summary,
                f"initial Facebook preparation failed: {exc}",
                include_labels,
                exclude_labels,
            ):
                raise

        no_button_cycles = 0
        for cycle in range(1, args.max_cycles + 1):
            client.ensure_time("like search")
            summary["cycles"] = cycle
            client.swipe_up(args.swipe)
            client.sleep(args.scroll_wait, "search scroll")
            try:
                xml_data = client.dump_ui()
            except MytError as exc:
                if recover_facebook(
                    client,
                    args,
                    device.label,
                    summary,
                    f"UI dump failed: {exc}",
                    include_labels,
                    exclude_labels,
                ):
                    no_button_cycles = 0
                    continue
                raise
            buttons = find_like_buttons(xml_data, include_labels, exclude_labels)
            state = classify_screen(xml_data, include_labels, exclude_labels)

            if args.verbose:
                log(
                    f"  [{device.label}] cycle {cycle}: "
                    f"{len(buttons)} unliked candidate(s), state={state}"
                )
                if not buttons:
                    print_clickable_nodes(xml_data, device.label)
            if not buttons:
                no_button_cycles += 1
                if state in {"other-app", "unknown"}:
                    reason = f"unsafe screen state while searching likes: {state}"
                    if recover_facebook(
                        client,
                        args,
                        device.label,
                        summary,
                        reason,
                        include_labels,
                        exclude_labels,
                    ):
                        no_button_cycles = 0
                        continue
                    raise ScreenStateError(reason)
                if state == "facebook-other" and no_button_cycles >= args.not_feed_limit:
                    reason = "Facebook is open but Feed/Like controls did not load"
                    if recover_facebook(
                        client,
                        args,
                        device.label,
                        summary,
                        reason,
                        include_labels,
                        exclude_labels,
                    ):
                        no_button_cycles = 0
                        continue
                    raise ScreenStateError(reason)
                if no_button_cycles >= args.no_button_limit:
                    reason = (
                        f"no unliked Like buttons found for {no_button_cycles} "
                        "consecutive cycles"
                    )
                    if recover_facebook(
                        client,
                        args,
                        device.label,
                        summary,
                        reason,
                        include_labels,
                        exclude_labels,
                    ):
                        no_button_cycles = 0
                        continue
                    summary["status"] = "no-like-buttons"
                    summary["error"] = reason
                    return summary
                continue
            no_button_cycles = 0

            button = buttons[0]
            log(
                f"  [{device.label}] candidate: ({button.x}, {button.y}) "
                f"description={button.description!r}"
            )
            if not args.execute:
                log(
                    f"  [{device.label}] DRY RUN: no tap sent; "
                    "add --execute after user authorization"
                )
                summary["status"] = "dry-run"
                return summary

            summary["tap_sent"] = int(summary["tap_sent"]) + 1
            try:
                client.tap(button.x, button.y)
            except MytError as exc:
                summary["status"] = "unverified-like"
                summary["error"] = f"like tap command could not be confirmed: {exc}"
                log(
                    f"  [{device.label}] UNVERIFIED LIKE: tap command could not "
                    "be confirmed; stop before risking unlike/toggle",
                    error=True,
                )
                return summary
            log(
                f"  [{device.label}] tap sent "
                f"(tap={summary['tap_sent']}, verified={summary['liked']}/{args.count})"
            )
            try:
                verified, verify_reason, verify_xml = verify_like_after_tap(
                    client,
                    args,
                    device.label,
                    button,
                    include_labels,
                    exclude_labels,
                )
            except MytError as exc:
                summary["status"] = "unverified-like"
                summary["error"] = f"like tap could not be verified: {exc}"
                log(
                    f"  [{device.label}] UNVERIFIED LIKE: verification failed; "
                    "stop before risking unlike/toggle",
                    error=True,
                )
                return summary
            if not verified:
                summary["status"] = "unverified-like"
                summary["error"] = f"like tap could not be verified: {verify_reason}"
                log(
                    f"  [{device.label}] UNVERIFIED LIKE: {verify_reason}; "
                    "stop before risking unlike/toggle",
                    error=True,
                )
                if args.verbose and verify_xml:
                    print_clickable_nodes(verify_xml, device.label)
                return summary
            summary["liked"] = int(summary["liked"]) + 1
            log(
                f"  [{device.label}] like verified: {verify_reason} "
                f"({summary['liked']}/{args.count})"
            )
            if int(summary["liked"]) >= args.count:
                return summary

        summary["status"] = "target-not-reached"
        return summary
    except RuntimeLimitError as exc:
        summary["status"] = "time-limit-reached"
        summary["error"] = str(exc)
        log(
            f"  [{device.label}] TIME LIMIT: returning partial result "
            f"before Hermes terminal timeout ({summary['liked']}/{args.count})",
            error=True,
        )
        return summary
    except ScreenStateError as exc:
        summary["status"] = "screen-state-error"
        summary["error"] = str(exc)
        log(f"  [{device.label}] SCREEN STATE: {exc}", error=True)
        return summary
    except (MytError, ET.ParseError) as exc:
        summary["status"] = "error"
        summary["error"] = str(exc)
        log(f"  [{device.label}] ERROR: {exc}", error=True)
        return summary


def run_devices(
    args: argparse.Namespace,
    devices: list[Device],
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
) -> list[dict[str, object]]:
    """Run all target devices concurrently and return results in input order."""
    order = {device.port: index for index, device in enumerate(devices)}
    summaries: list[dict[str, object]] = []
    with ThreadPoolExecutor(
        max_workers=len(devices), thread_name_prefix="myt-facebook"
    ) as executor:
        futures = {
            executor.submit(
                run_device, args, device, include_labels, exclude_labels
            ): device
            for device in devices
        }
        for future in as_completed(futures):
            device = futures[future]
            try:
                summaries.append(future.result())
            except Exception as exc:  # keep other devices running on an unexpected error
                log(f"  [{device.label}] UNEXPECTED ERROR: {exc}", error=True)
                summaries.append(
                    {
                        "device": device.label,
                        "port": device.port,
                        "liked": 0,
                        "tap_sent": 0,
                        "recoveries": 0,
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
            "Find unliked Facebook feed buttons through the MYT V1 HTTP API. "
            "The default is a dry run; --execute is required to tap."
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
        help="required number of posts to like per device (except with --check)",
    )
    parser.add_argument(
        "--max-cycles", type=int, default=os.getenv("MYT_MAX_CYCLES", "40")
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
    parser.add_argument(
        "--include-labels",
        default=",".join(DEFAULT_INCLUDE_LABELS),
        help="comma-separated case-insensitive fragments for an unliked button",
    )
    parser.add_argument(
        "--exclude-labels",
        default=",".join(DEFAULT_EXCLUDE_LABELS),
        help="comma-separated case-insensitive fragments for a pressed/unlike button",
    )
    parser.add_argument("--launch-wait", type=float, default=6.0)
    parser.add_argument(
        "--feed-ready-retries",
        type=int,
        default=5,
        help="wait attempts for Facebook Feed/Like controls after launch; default 5",
    )
    parser.add_argument(
        "--feed-ready-wait",
        type=float,
        default=1.5,
        help="seconds between Feed readiness checks; default 1.5",
    )
    parser.add_argument("--scroll-wait", type=float, default=2.0)
    parser.add_argument("--tap-wait", type=float, default=1.5)
    parser.add_argument(
        "--like-verify-retries",
        type=int,
        default=3,
        help="attempts to verify Like state after tapping; default 3",
    )
    parser.add_argument(
        "--like-verify-wait",
        type=float,
        default=1.0,
        help="seconds between Like verification attempts after the first; default 1",
    )
    parser.add_argument(
        "--like-verify-radius",
        type=int,
        default=90,
        help="pixels around the tapped Like button used for verification; default 90",
    )
    parser.add_argument(
        "--not-feed-limit",
        type=int,
        default=4,
        help="consecutive facebook-other states before recovery/abort; default 4",
    )
    parser.add_argument(
        "--no-button-limit",
        type=int,
        default=8,
        help="consecutive no-Like-button cycles before recovery/summary; default 8",
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
        help="send tap commands; without this flag the script is a dry run",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="print final summary as JSON")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if not args.host:
        raise ConfigurationError("MYT_HOST is missing; set it or pass --host")
    args.host = normalize_host(args.host)
    if not args.check and args.count is None:
        raise ConfigurationError("--count is required for dry-run and execution")
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
    if args.ui_dump_retries < 1:
        raise ConfigurationError("--ui-dump-retries must be at least 1")
    if args.feed_ready_retries < 1:
        raise ConfigurationError("--feed-ready-retries must be at least 1")
    if args.like_verify_retries < 1:
        raise ConfigurationError("--like-verify-retries must be at least 1")
    if args.like_verify_radius < 1:
        raise ConfigurationError("--like-verify-radius must be at least 1")
    if args.not_feed_limit < 1 or args.no_button_limit < 1:
        raise ConfigurationError("--not-feed-limit and --no-button-limit must be at least 1")
    if args.max_recoveries < 0:
        raise ConfigurationError("--max-recoveries cannot be negative")
    if args.no_launch and args.force_restart:
        raise ConfigurationError("--no-launch and --force-restart cannot be combined")
    if args.execute and args.verbose:
        raise ConfigurationError(
            "--execute and --verbose cannot be combined; remove --execute for diagnosis"
        )
    if args.recovery_scrolls < 0:
        raise ConfigurationError("--recovery-scrolls cannot be negative")
    for name in (
        "launch_wait",
        "feed_ready_wait",
        "scroll_wait",
        "tap_wait",
        "ui_dump_retry_wait",
        "like_verify_wait",
        "recents_wait",
        "app_close_wait",
        "recovery_min_runtime",
    ):
        if getattr(args, name) < 0:
            raise ConfigurationError(f"--{name.replace('_', '-')} cannot be negative")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        validate_args(args)
        devices = parse_devices(args.devices, args.base_port, args.port_stride)
        include_labels = parse_csv_labels(args.include_labels)
        exclude_labels = parse_csv_labels(args.exclude_labels)
    except (ConfigurationError, ValueError) as exc:
        parser.error(str(exc))

    mode = "CHECK" if args.check else ("EXECUTE" if args.execute else "DRY RUN")
    print(
        f"=== Facebook Daily Like | {mode} | host={args.host} | "
        f"devices={','.join(device.label for device in devices)} ==="
    )
    log(f"Starting {len(devices)} device task(s) concurrently")
    summaries = run_devices(args, devices, include_labels, exclude_labels)
    target_count = args.count or 0
    for item in summaries:
        item["target"] = target_count
        item["remaining"] = max(0, target_count - int(item["liked"]))
        item["tap_sent"] = int(item.get("tap_sent", 0))
        item["recoveries"] = int(item.get("recoveries", 0))
    log("=== Summary ===")
    for item in summaries:
        log(
            f"  {item['device']} port={item['port']} status={item['status']} "
            f"liked={item['liked']}/{item['target']} "
            f"tap_sent={item['tap_sent']} recoveries={item['recoveries']} "
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
            "For status=unverified-like, manually inspect the device first. "
            "Do not add --no-launch unless the device was manually verified "
            "to be on Feed, and never reuse the original total count"
        )
    if args.json:
        print(json.dumps(summaries, ensure_ascii=False))

    if args.check:
        success = all(item["status"] == "ok" for item in summaries)
    elif args.execute:
        success = all(
            item["status"] == "ok" and int(item["liked"]) >= args.count
            for item in summaries
        )
    else:
        success = all(item["status"] == "dry-run" for item in summaries)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
