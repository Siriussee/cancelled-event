#!/usr/bin/env python3
"""Check Ticketmaster API access without printing keys, URLs, or raw responses."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

API_ROOT = "https://app.ticketmaster.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class VerificationError(Exception):
    """An error containing only safe, locally defined text."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # API keys are query parameters; never forward them to a redirect target.
        return None


def read_key(path: Path) -> str:
    """Read a private regular file, without evaluating shell or dotenv syntax."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise VerificationError("无法打开 key 文件；请确认文件存在且不是符号链接。") from None

    with os.fdopen(descriptor, encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise VerificationError("key 文件必须是当前用户拥有的普通文件。")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise VerificationError("key 文件权限过宽；请将文件权限设为 600。")
        content = handle.read(4097)
        if len(content) > 4096:
            raise VerificationError("key 文件过大；请只保留配置行和注释。")

    values = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() == "TICKETMASTER_API_KEY":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
        else:
            raise VerificationError("文件只接受 TICKETMASTER_API_KEY 配置和注释。")
    if len(values) != 1 or not values[0]:
        raise VerificationError("请在本地文件的 TICKETMASTER_API_KEY= 后填入 key。")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", values[0]):
        raise VerificationError("key 格式不正确；请仅粘贴开发者后台提供的 API key。")
    return values[0]


def get_json(path: str, api_key: str, **parameters: str) -> dict[str, Any]:
    """Make one HTTPS request to a fixed official host; expose no server text."""
    if path not in {"/discovery/v2/events.json", "/discovery-feed/v2/events"}:
        raise VerificationError("拒绝访问未批准的 API 路径。")
    query = urlencode({**parameters, "apikey": api_key})
    request = Request(
        f"{API_ROOT}{path}?{query}",
        headers={"Accept": "application/json", "User-Agent": "TM-API-Access-Check/1.0"},
    )
    # Use normal certificate verification and avoid inherited proxy settings.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise VerificationError("API 未返回 HTTP 200。")
            raw_body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status_code = exc.code
        exc.close()
        hints = {
            401: "key 无效或尚未生效",
            403: "接口权限不足或访问被拒绝",
            429: "请求配额或频率受限；本次不重试",
        }
        hint = hints.get(status_code, "请求被拒绝或接口暂不可用")
        raise VerificationError(f"HTTP {status_code}：{hint}。") from None
    except (URLError, TimeoutError, OSError):
        raise VerificationError("网络、TLS 或超时错误；未输出含 key 的请求地址。") from None
    if len(raw_body) > MAX_RESPONSE_BYTES:
        raise VerificationError("API 响应超出验证脚本的大小限制。")
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeError):
        raise VerificationError("API 响应不是有效 JSON。") from None
    if not isinstance(payload, dict) or "fault" in payload:
        raise VerificationError("API 返回的 JSON 不符合成功响应结构。")
    return payload


def nonnegative_integer(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def discovery_summary(payload: dict[str, Any]) -> dict[str, Any]:
    page = payload.get("page")
    if not isinstance(page, dict) or nonnegative_integer(page.get("totalElements")) is None:
        raise VerificationError("Discovery API 缺少活动分页信息。")
    return {"ok": True, "http_status": 200, "total_events": page["totalElements"]}


def feed_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy only typed summary fields; never persist feed URIs or raw JSON."""
    countries = payload.get("countries")
    if not isinstance(countries, dict):
        raise VerificationError("Discovery Feed 缺少国家文件信息。")
    files: dict[str, Any] = {}
    for country in ("US", "CA"):
        formats = countries.get(country)
        country_files = {}
        if isinstance(formats, dict):
            for format_name in ("CSV", "JSON"):
                metadata = formats.get(format_name)
                if not isinstance(metadata, dict):
                    continue
                uri = metadata.get("uri")
                if not isinstance(uri, str) or not uri.startswith("https://"):
                    continue
                country_files[format_name] = {
                    "num_events": nonnegative_integer(metadata.get("num_events")),
                    "compressed_size_bytes": nonnegative_integer(
                        metadata.get("compressed_size_bytes")
                    ),
                }
        files[country] = {"available": bool(country_files), "formats": country_files}
    return {
        "ok": all(country["available"] for country in files.values()),
        "http_status": 200,
        "countries": files,
    }


def verify(api_key: str) -> dict[str, Any]:
    checks = {}
    probes = (
        ("discovery_api", "/discovery/v2/events.json", discovery_summary),
        ("discovery_feed", "/discovery-feed/v2/events", feed_summary),
    )
    for index, (name, path, summarize) in enumerate(probes):
        if index:
            time.sleep(1)
        try:
            parameters = {"countryCode": "US", "size": "1"} if index == 0 else {}
            checks[name] = summarize(get_json(path, api_key, **parameters))
        except VerificationError as exc:
            checks[name] = {"ok": False, "error": str(exc)}
    return {
        "checks": checks,
        "cancelled_event_coverage_verified": False,
        "note": "本次只验证接口权限和文件信息；取消活动数量及覆盖率需另行下载核对。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cancelled-event verify-ticketmaster", description=__doc__
    )
    parser.add_argument("--key-file", type=Path, default=Path.cwd() / ".env.ticketmaster.local")
    args = parser.parse_args(argv)
    try:
        api_key = read_key(args.key_file)
        result = verify(api_key)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        print(output.replace(api_key, "[REDACTED]"))
        return 0 if all(check["ok"] for check in result["checks"].values()) else 1
    except VerificationError as exc:
        print(f"验证未完成：{exc}")
        return 2
    except KeyboardInterrupt:
        print("验证已停止。")
        return 130
    except Exception:
        # Unexpected library exceptions may contain the full authenticated URL.
        print("验证未完成：发生未预期错误；原始异常已隐藏，避免泄露 key。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
