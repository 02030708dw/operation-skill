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
BOUNDS_RE = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
PRINT_LOCK = threading.Lock()


class ConfigurationError(ValueError):
    """Raised when required local configuration is missing or invalid."""


class MytError(RuntimeError):
    """Raised when the MYT API cannot complete an operation."""


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
    def __init__(self, host: str, port: int, timeout: float, verbose: bool = False):
        self.host = normalize_host(host)
        self.port = port
        self.timeout = timeout
        self.verbose = verbose

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _get(self, path: str, params: dict[str, str] | None = None) -> bytes:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base_url}{path}{query}"
        if self.verbose:
            safe_url = url.replace("%2F", "/")
            log(f"    [{self.port}] GET {safe_url}")
        request = urllib.request.Request(url, headers={"User-Agent": "Hermes-MYT-Skill/2.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MytError(f"{self.host}:{self.port}: {exc}") from exc

    def shell(self, command: str) -> bytes:
        return self._get("/modifydev", {"cmd": "6", "cmdline": command})

    def check(self) -> None:
        self.shell("echo hermes_myt_ok")

    def launch_facebook(self) -> None:
        self.shell(
            "monkey -p com.facebook.katana "
            "-c android.intent.category.LAUNCHER 1"
        )

    def swipe_up(self, swipe: tuple[int, int, int, int, int]) -> None:
        x1, y1, x2, y2, duration_ms = swipe
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

    def dump_ui(self) -> bytes:
        remote_path = "/sdcard/hermes_fb_like.xml"
        self.shell(f"uiautomator dump {remote_path}")
        return self._get("/download", {"path": remote_path})

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")


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


def find_like_buttons(
    xml_data: bytes,
    include_labels: Iterable[str] = DEFAULT_INCLUDE_LABELS,
    exclude_labels: Iterable[str] = DEFAULT_EXCLUDE_LABELS,
) -> list[LikeButton]:
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError as exc:
        preview = xml_data[:160].decode("utf-8", errors="replace")
        raise MytError(f"Downloaded UI dump is not valid XML: {preview!r}") from exc

    include = tuple(label.casefold() for label in include_labels)
    exclude = tuple(label.casefold() for label in exclude_labels)
    results: list[LikeButton] = []
    seen: set[tuple[int, int]] = set()

    for node in root.iter():
        if node.attrib.get("clickable", "").casefold() != "true":
            continue
        description = extract_description(node)
        folded = description.casefold()
        if not description or not any(label in folded for label in include):
            continue
        if any(label in folded for label in exclude):
            continue
        bounds_match = BOUNDS_RE.fullmatch(node.attrib.get("bounds", ""))
        if not bounds_match:
            continue
        x1, y1, x2, y2 = (int(value) for value in bounds_match.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        center = ((x1 + x2) // 2, (y1 + y2) // 2)
        if center in seen:
            continue
        seen.add(center)
        results.append(
            LikeButton(description=description, x=center[0], y=center[1])
        )
    return results


def print_clickable_nodes(xml_data: bytes, device_label: str) -> None:
    try:
        root = ET.fromstring(xml_data)
    except ET.ParseError:
        return
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


def run_device(
    args: argparse.Namespace,
    device: Device,
    include_labels: tuple[str, ...],
    exclude_labels: tuple[str, ...],
) -> dict[str, object]:
    client = MytClient(args.host, device.port, args.timeout, args.verbose)
    summary: dict[str, object] = {
        "device": device.label,
        "port": device.port,
        "liked": 0,
        "cycles": 0,
        "status": "ok",
    }

    log(f"[{device.label}] port={device.port}")
    try:
        client.check()
        log(f"  [{device.label}] connection: OK")
        if args.check:
            return summary
        if not args.no_launch:
            client.launch_facebook()
            log(f"  [{device.label}] Facebook launch requested")
            time.sleep(args.launch_wait)

        for cycle in range(1, args.max_cycles + 1):
            summary["cycles"] = cycle
            client.swipe_up(args.swipe)
            time.sleep(args.scroll_wait)
            xml_data = client.dump_ui()
            buttons = find_like_buttons(xml_data, include_labels, exclude_labels)

            if args.verbose:
                log(
                    f"  [{device.label}] cycle {cycle}: "
                    f"{len(buttons)} unliked candidate(s)"
                )
                if not buttons:
                    print_clickable_nodes(xml_data, device.label)
            if not buttons:
                continue

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

            client.tap(button.x, button.y)
            summary["liked"] = int(summary["liked"]) + 1
            log(f"  [{device.label}] tap sent ({summary['liked']}/{args.count})")
            if int(summary["liked"]) >= args.count:
                return summary
            time.sleep(args.tap_wait)

        summary["status"] = "target-not-reached"
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
    parser.add_argument("--scroll-wait", type=float, default=2.0)
    parser.add_argument("--tap-wait", type=float, default=1.5)
    parser.add_argument("--check", action="store_true", help="only test API access")
    parser.add_argument("--no-launch", action="store_true")
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
    for name in ("launch_wait", "scroll_wait", "tap_wait"):
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

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(
        f"=== Facebook Daily Like | {mode} | host={args.host} | "
        f"devices={','.join(device.label for device in devices)} ==="
    )
    log(f"Starting {len(devices)} device task(s) concurrently")
    summaries = run_devices(args, devices, include_labels, exclude_labels)
    log("=== Summary ===")
    for item in summaries:
        log(
            f"  {item['device']} port={item['port']} status={item['status']} "
            f"liked={item['liked']} cycles={item['cycles']}"
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
