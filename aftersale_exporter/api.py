from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import unquote_to_bytes

import requests

from aftersale_exporter.curl_template import (
    DEFAULT_EXPORT_FILTER_CONFIG,
    ExportFilterConfig,
    HttpRequest,
    SessionSeed,
)
from aftersale_exporter.workflow import (
    AuthenticationError,
    OverLimitError,
    TaskPollResult,
    TaskResult,
)


class RequestFailedError(Exception):
    """Raised when the remote platform returns an unexpected error."""


class TaskTimeoutError(RequestFailedError):
    """Raised when an export task does not complete before timeout."""


@dataclass
class AftersaleApiService:
    session_seed: SessionSeed
    filter_config: ExportFilterConfig = DEFAULT_EXPORT_FILTER_CONFIG
    session: Any | None = None
    lid_factory: Callable[[], str] | None = None
    sleep_fn: Callable[[float], None] = time.sleep
    monotonic_fn: Callable[[], float] = time.monotonic
    wall_clock_fn: Callable[[], float] = time.time
    max_retries: int = 3

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        if self.lid_factory is None:
            self.lid_factory = lambda: str(int(time.time() * 1000))

    def create_export(self, start_ts: int, end_ts: int) -> str:
        request = self.session_seed.build_export_request(
            filter_config=self.filter_config,
            start_ts=start_ts,
            end_ts=end_ts,
            request_lid=self.lid_factory(),
        )
        payload = self._send_json(request)
        return _extract_task_id(payload)

    def wait_for_task(
        self,
        task_id: str,
        poll_interval: float,
        timeout: float,
        status_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> TaskResult:
        deadline = self.monotonic_fn() + timeout
        while True:
            poll_result = self.poll_task(task_id)
            if status_callback is not None:
                status_callback(
                    {
                        "requested_at_ts": poll_result.requested_at_ts,
                        "result_text": poll_result.result_text,
                    }
                )
            if poll_result.is_complete:
                return TaskResult(download_name=poll_result.download_name or f"{task_id}.bin")
            if self.monotonic_fn() + poll_interval > deadline:
                raise TaskTimeoutError(f"task {task_id} did not finish before timeout")
            self.sleep_fn(poll_interval)

    def poll_task(self, task_id: str) -> TaskPollResult:
        request = self.session_seed.build_tasks_request(
            task_ids=[task_id],
            request_lid=self.lid_factory(),
        )
        payload = self._send_json(request)
        task = _extract_task(payload, task_id)
        is_complete = _is_task_complete(task)
        return TaskPollResult(
            requested_at_ts=int(self.wall_clock_fn()),
            result_text=_build_task_poll_result_text(is_complete),
            is_complete=is_complete,
            download_name=_extract_download_name(task, task_id) if is_complete else None,
        )

    def download_export(self, task_id: str, destination) -> Any:
        request = self.session_seed.build_download_request(
            task_id=task_id,
            request_lid=self.lid_factory(),
        )
        response = self._send(request)
        final_path = _resolve_download_destination(Path(destination), response.headers)
        final_path.write_bytes(response.content)
        return final_path

    def _send_json(self, request: HttpRequest) -> dict[str, Any]:
        response = self._send(request)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RequestFailedError("response body is not a JSON object")
        _raise_for_business_error(payload)
        return payload

    def _send(self, request: HttpRequest):
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.request(
                    request.method,
                    request.url,
                    params=request.params,
                    headers=request.headers,
                    cookies=request.cookies,
                    json=request.json,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise RequestFailedError(f"request failed after retries: {exc}") from exc
                continue

            if response.status_code in {401, 403}:
                raise AuthenticationError(f"authentication failed with HTTP {response.status_code}")
            return response

        raise RequestFailedError(f"request failed after retries: {last_error}")


def _raise_for_business_error(payload: dict[str, Any]) -> None:
    code = payload.get("code", payload.get("st", 0))
    if code in (0, None):
        return
    message = str(payload.get("msg", payload.get("message", f"business error {code}")))
    if int(code) == 20309001 and ("5万条" in message or "超过限制" in message):
        raise OverLimitError(message)
    raise RequestFailedError(message)


def _extract_task_id(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("task_id", "taskId", "id"):
            value = data.get(key)
            if value:
                return str(value)
    if isinstance(data, str) and data:
        return data
    raise RequestFailedError("unable to locate export task id in response")


def _extract_task(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("task_list"), list):
        for item in data["task_list"]:
            if not isinstance(item, dict):
                continue
            current_id = item.get("task_id") or item.get("taskId") or item.get("id")
            if current_id is None or str(current_id) == task_id:
                return item
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            current_id = item.get("task_id") or item.get("taskId") or item.get("id")
            if current_id is None or str(current_id) == task_id:
                return item
    if isinstance(data, dict):
        return data
    raise RequestFailedError("unable to locate task details in response")


def _is_task_complete(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or task.get("task_status") or task.get("state") or "").lower()
    progress = task.get("progress")
    return (
        status in {"2", "success", "finished", "done", "complete", "completed"}
        or progress == 100
    )


def _extract_download_name(task: dict[str, Any], task_id: str) -> str:
    for key in ("file_name", "filename", "name"):
        value = task.get(key)
        if value:
            return str(value)
    return f"{task_id}.bin"


def _build_task_poll_result_text(is_complete: bool) -> str:
    if is_complete:
        return "文件已生成"
    return "文件未生成"


def _resolve_download_destination(destination: Path, headers: dict[str, Any]) -> Path:
    filename = _filename_from_content_disposition(headers.get("content-disposition"))
    if filename:
        return destination.with_name(filename)

    content_type = str(headers.get("content-type", "")).lower()
    if "spreadsheetml.sheet" in content_type:
        return destination.with_suffix(".xlsx")
    if "text/csv" in content_type or "application/csv" in content_type:
        return destination.with_suffix(".csv")
    return destination


def _filename_from_content_disposition(header_value: Any) -> str | None:
    if not header_value:
        return None
    filename: str | None = None
    filename_star: str | None = None
    for part in str(header_value).split(";"):
        piece = part.strip()
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        normalized_key = key.strip().lower()
        if normalized_key == "filename*":
            filename_star = _decode_rfc5987_filename(value)
        elif normalized_key == "filename":
            filename = _repair_mojibake_filename(_strip_header_value_quotes(value.strip()))
    return filename_star or filename


def _decode_rfc5987_filename(value: str) -> str | None:
    candidate = _strip_header_value_quotes(value.strip())
    if not candidate:
        return None

    charset = "utf-8"
    encoded_value = candidate
    if candidate.count("'") >= 2:
        charset, _, encoded_value = candidate.split("'", 2)
        if not charset:
            charset = "utf-8"

    try:
        return unquote_to_bytes(encoded_value).decode(charset)
    except (LookupError, UnicodeDecodeError, ValueError):
        return None


def _repair_mojibake_filename(value: str) -> str:
    if not value:
        return value
    try:
        repaired = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    return repaired


def _strip_header_value_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
