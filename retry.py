from asyncio import sleep
from typing import Callable, TypeVar

# CC 的默认常量 — 源码: client.rs:18-20
DEFAULT_INITIAL_BACKOFF_MS = 200
DEFAULT_MAX_BACKOFF_MS = 2000
DEFAULT_MAX_RETRIES = 2
T = TypeVar("T")

class ApiError(Exception):
    is_retryable: bool = False

class HttpApiError(ApiError):
    status_code: int

    # 可重试的 HTTP 状态码
    RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}

    def __init__(self, status_code: int, message: str = ""):
        self.status_code = status_code
        if status_code in self.RETRYABLE_STATUS_CODES:
            self.is_retryable = True
        super().__init__(f"HTTP {status_code}: {message}" if message else f"HTTP {status_code}")

class ConnectionError(ApiError):
    def __init__(self, message: str = "connection error"):
        self.is_retryable = True
        super().__init__(message)


class AuthError(ApiError):
    def __init__(self, message: str = "authentication failed"):
        super().__init__(message)

class RetriesExhausted(ApiError):
    def __init(self, attempts: int, last_error: ApiError):
        self.attempts = attempts
        self.last_error = last_error
        self.is_retryable = last_error.is_retryable
        super().__init__(f"failed after {attempts} attempts: {last_error}")

_MAX_SAFE_EXPONENT = 31  # 2^31 = 2147483648，超过任何合理 backoff

def backoff_for_attempt(attempt: int,
                        initial_ms: float = DEFAULT_INITIAL_BACKOFF_MS,
                        max_ms: float = DEFAULT_MAX_BACKOFF_MS) -> float:
    exponent = attempt - 1

    if(exponent > _MAX_SAFE_EXPONENT):
        return  max_ms
    multiplier = 1 << exponent  # 2^exponent
    delay_ms = initial_ms * multiplier

    return min(delay_ms, max_ms) / 1000.0

def send_with_retry(
    fn: Callable[[], T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_backoff_ms: int = DEFAULT_INITIAL_BACKOFF_MS,
    max_backoff_ms: int = DEFAULT_MAX_BACKOFF_MS,
) -> T:
    retry_count = 0
    while True:
        retry_count += 1

        try:
            return fn()
        except ApiError as e:
            if e.is_retryable and retry_count <= max_retries + 1:
                last_error = e
            else:
                raise
        if retry_count > max_retries:
            break

        backoff_time = backoff_for_attempt(retry_count, initial_backoff_ms, max_backoff_ms)
        sleep(backoff_time)


    raise RetriesExhausted(initial_backoff_ms, last_error)



