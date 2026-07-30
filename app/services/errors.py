from __future__ import annotations

from typing import Any

import httpx


class AppError(Exception):
    """Stable application error with public message and machine code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INTERNAL_ERROR",
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id
        self.details = details or {}

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "retry_after": self.retry_after,
                "request_id": self.request_id,
                "details": self.details,
            }
        }


def extract_upstream_request_id(resp: httpx.Response) -> str | None:
    for key in (
        "x-request-id",
        "x-openai-request-id",
        "openai-request-id",
        "x-kong-request-id",
    ):
        val = resp.headers.get(key)
        if val:
            return val
    return None


def _error_body_message(resp: httpx.Response) -> tuple[str, dict[str, Any]]:
    details: dict[str, Any] = {}
    try:
        data = resp.json()
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("code") or err)
            if err.get("type"):
                details["type"] = err["type"]
            if err.get("code"):
                details["upstream_code"] = err["code"]
            if err.get("param"):
                details["param"] = err["param"]
            return msg, details
        if isinstance(err, str):
            return err, details
        return (resp.text[:500] or f"HTTP {resp.status_code}"), details
    except Exception:
        return (resp.text[:500] or f"HTTP {resp.status_code}"), details


def classify_http_error(resp: httpx.Response) -> AppError:
    """Map upstream HTTP response to a stable AppError."""
    request_id = extract_upstream_request_id(resp)
    msg, details = _error_body_message(resp)
    status = resp.status_code
    lower = msg.lower()

    retry_after: float | None = None
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            retry_after = float(ra)
        except ValueError:
            # HTTP-date form — fall back to a short default backoff
            from datetime import UTC, datetime
            from email.utils import parsedate_to_datetime

            try:
                when = parsedate_to_datetime(ra)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                retry_after = max(0.0, (when - datetime.now(UTC)).total_seconds())
            except Exception:
                retry_after = None

    if status == 400:
        if "moderation" in lower or "safety" in lower or "blocked" in lower:
            return AppError(
                msg or "内容审核未通过",
                code="MODERATION_BLOCKED",
                status_code=status,
                retryable=False,
                request_id=request_id,
                details=details,
            )
        return AppError(
            msg or "请求无效",
            code="INPUT_INVALID",
            status_code=status,
            retryable=False,
            request_id=request_id,
            details=details,
        )
    if status == 401:
        return AppError(
            msg or "认证失败",
            code="AUTH_FAILED",
            status_code=status,
            retryable=False,
            request_id=request_id,
            details=details,
        )
    if status == 403:
        return AppError(
            msg or "无权限",
            code="AUTH_FAILED",
            status_code=status,
            retryable=False,
            request_id=request_id,
            details=details,
        )
    if status == 404:
        return AppError(
            msg or "模型或资源不存在",
            code="MODEL_NOT_FOUND",
            status_code=status,
            retryable=False,
            request_id=request_id,
            details=details,
        )
    if status == 429:
        return AppError(
            msg or "请求过于频繁，请稍后重试",
            code="RATE_LIMITED",
            status_code=status,
            retryable=True,
            retry_after=retry_after,
            request_id=request_id,
            details=details,
        )
    if status in (408, 504):
        return AppError(
            msg or "上游超时",
            code="UPSTREAM_TIMEOUT",
            status_code=status,
            retryable=True,
            retry_after=retry_after,
            request_id=request_id,
            details=details,
        )
    if status >= 500:
        return AppError(
            msg or "上游服务不可用",
            code="UPSTREAM_UNAVAILABLE",
            status_code=status,
            retryable=True,
            retry_after=retry_after,
            request_id=request_id,
            details=details,
        )
    return AppError(
        msg or f"上游协议错误 HTTP {status}",
        code="UPSTREAM_PROTOCOL_ERROR",
        status_code=status,
        retryable=False,
        request_id=request_id,
        details=details,
    )


def is_retryable(error: BaseException) -> bool:
    if isinstance(error, AppError):
        return error.retryable
    if isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)
    ):
        return True
    return False
