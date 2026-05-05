from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import shlex
from typing import Any
from urllib.parse import parse_qsl, urlsplit


FXG_ORIGIN = "https://fxg.jinritemai.com"
EXPORT_PATH = "/shopuser/aftersale/export"
TASKS_PATH = "/shopuser/aftersale/export/tasks"
DOWNLOAD_PATH = "/shopuser/aftersale/export/download"
QUERY_WHITELIST = (
    "appid",
    "__token",
    "_bid",
    "aid",
    "aftersale_platform_source",
    "msToken",
    "a_bogus",
    "verifyFp",
    "fp",
)


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    params: dict[str, str]
    headers: dict[str, str]
    cookies: dict[str, str]
    json: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExportFilterConfig:
    body: dict[str, Any]

    @staticmethod
    def model_dump(config: "ExportFilterConfig") -> dict[str, Any]:
        return asdict(config)

    def build_export_body(self, start_ts: int, end_ts: int, request_lid: str) -> dict[str, Any]:
        body = dict(self.body)
        body["apply_time_start"] = start_ts
        body["apply_time_end"] = end_ts
        body["_lid"] = request_lid
        return body


@dataclass(frozen=True)
class SessionSeed:
    origin: str
    query: dict[str, str]
    headers: dict[str, str]
    cookies: dict[str, str]

    @staticmethod
    def model_dump(seed: "SessionSeed") -> dict[str, Any]:
        return asdict(seed)

    def build_export_request(
        self,
        *,
        filter_config: ExportFilterConfig,
        start_ts: int,
        end_ts: int,
        request_lid: str,
    ) -> HttpRequest:
        return HttpRequest(
            method="POST",
            url=f"{self.origin}{EXPORT_PATH}",
            params=self._build_params(request_lid),
            headers=dict(self.headers),
            cookies=dict(self.cookies),
            json=filter_config.build_export_body(start_ts, end_ts, request_lid),
        )

    def build_tasks_request(self, *, task_ids: list[str], request_lid: str) -> HttpRequest:
        params = self._build_params(request_lid)
        params["task_id_list"] = ",".join(task_ids)
        return HttpRequest(
            method="GET",
            url=f"{self.origin}{TASKS_PATH}",
            params=params,
            headers=dict(self.headers),
            cookies=dict(self.cookies),
            json=None,
        )

    def build_download_request(self, *, task_id: str, request_lid: str) -> HttpRequest:
        params = self._build_params(request_lid)
        params["task_id"] = task_id
        return HttpRequest(
            method="GET",
            url=f"{self.origin}{DOWNLOAD_PATH}",
            params=params,
            headers=dict(self.headers),
            cookies=dict(self.cookies),
            json=None,
        )

    def _build_params(self, request_lid: str) -> dict[str, str]:
        params = dict(self.query)
        params["_lid"] = request_lid
        return params


def parse_seed_curl(command: str) -> SessionSeed:
    tokens = shlex.split(command.replace("\\\n", " "))
    if not tokens or tokens[0] != "curl":
        raise ValueError("command must start with curl")

    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    target_url: str | None = None

    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token in {"-H", "--header"}:
            name, value = _split_header(tokens[idx + 1])
            if name.lower() == "cookie":
                cookies.update(_parse_cookie_header(value))
            else:
                headers[name.lower()] = value
            idx += 2
            continue
        if token in {"-b", "--cookie"}:
            cookies.update(_parse_cookie_header(tokens[idx + 1]))
            idx += 2
            continue
        if token.startswith("http://") or token.startswith("https://"):
            target_url = token
        idx += 1

    if target_url is None:
        raise ValueError("curl command does not contain a URL")

    parsed = urlsplit(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin != FXG_ORIGIN:
        raise ValueError(f"seed curl must target {FXG_ORIGIN}")

    raw_query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query = {name: raw_query[name] for name in QUERY_WHITELIST if name in raw_query}
    missing = [name for name in QUERY_WHITELIST if name not in query]
    if missing:
        raise ValueError(f"missing required query fields: {', '.join(missing)}")

    return SessionSeed(
        origin=origin,
        query=query,
        headers=headers,
        cookies=cookies,
    )


def load_filter_config(raw_json: str) -> ExportFilterConfig:
    data = json.loads(raw_json)
    if not isinstance(data, dict) or not data:
        raise ValueError("filter JSON must be a non-empty JSON object")
    return ExportFilterConfig(body=data)


def _split_header(raw_header: str) -> tuple[str, str]:
    name, value = raw_header.split(":", 1)
    return name.strip(), value.strip()


def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        piece = part.strip()
        if not piece:
            continue
        name, value = piece.split("=", 1)
        cookies[name] = value
    return cookies
