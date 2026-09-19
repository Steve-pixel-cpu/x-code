import random
import time
from typing import Callable, Optional, TypeVar

# CC 的默认常量 — 源码: client.rs:18-20
DEFAULT_INITIAL_BACKOFF_MS = 200
DEFAULT_MAX_BACKOFF_MS = 2000
DEFAULT_MAX_RETRIES = 2

# 限流(429)专用曲线: 账户级速率/并发限制的窗口是秒~分钟级, 连接抖动曲线
# (200ms→400ms, 全程不到 1s)对它形同虚设——几次重试全撞在同一窗口里。
# 指数翻倍 + 抖动, 名义累计 2+4+8+16=30s。上限不能再放宽: 退避睡在 turn
# 工作线程里, 期间全局轮槽(MAX_CONCURRENT_TURNS)被持有。
RATE_LIMIT_INITIAL_BACKOFF_S = 2.0
RATE_LIMIT_MAX_BACKOFF_S = 30.0
RATE_LIMIT_MAX_RETRIES = 4

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
    def __init__(self, attempts: int, last_error: ApiError):
        self.attempts = attempts
        self.last_error = last_error
        self.is_retryable = last_error.is_retryable
        super().__init__(f"failed after {attempts} attempts: {last_error}")

_MAX_SAFE_EXPONENT = 31  # 2^31 = 2147483648，超过任何合理 backoff

def is_rate_limit_error(e: Exception) -> bool:
    """429 = 账户级限流, 走专用长退避; 其余可重试错误维持连接抖动曲线。"""
    return isinstance(e, HttpApiError) and e.status_code == 429

def backoff_for_attempt(attempt: int,
                        initial_ms: float = DEFAULT_INITIAL_BACKOFF_MS,
                        max_ms: float = DEFAULT_MAX_BACKOFF_MS) -> float:
    exponent = attempt - 1

    if(exponent > _MAX_SAFE_EXPONENT):
        return  max_ms
    multiplier = 1 << exponent  # 2^exponent
    delay_ms = initial_ms * multiplier

    return min(delay_ms, max_ms) / 1000.0

def _rate_limit_backoff(attempt: int) -> float:
    """限流退避: 2s 起步指数翻倍、单次上限 30s, 乘 0.8~1.2 抖动——
    并行会话/子代理同时被限流时把重试时刻错开, 避免下次又同时撞墙。"""
    exponent = attempt - 1
    if exponent > _MAX_SAFE_EXPONENT:
        delay = RATE_LIMIT_MAX_BACKOFF_S
    else:
        delay = min(RATE_LIMIT_INITIAL_BACKOFF_S * (1 << exponent),
                    RATE_LIMIT_MAX_BACKOFF_S)
    return delay * random.uniform(0.8, 1.2)

def _effective_max_retries(err: Optional[ApiError], base: int) -> int:
    """曲线按最近一次错误选择: 429 用限流重试上限, 其余用调用方给的基数。"""
    if err is not None and is_rate_limit_error(err):
        return RATE_LIMIT_MAX_RETRIES
    return base

def send_with_retry(
    fn: Callable[[], T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_backoff_ms: int = DEFAULT_INITIAL_BACKOFF_MS,
    max_backoff_ms: int = DEFAULT_MAX_BACKOFF_MS,
    on_retry: Optional[Callable[[int, int, float, ApiError], None]] = None,
) -> T:
    """同步退避重试。退避曲线按最近一次错误分流: 429 用限流长曲线
    （2s 起步、翻倍、上限 30s、最多重试 4 次）, 其余可重试错误维持
    200ms/2s 短曲线（重试 max_retries 次）。

    on_retry 在每次退避睡眠前调用, 参数 = (即将进行的重试序号, 本曲线
    max_retries, 退避秒数, 触发的错误); None = 不回调（CLI/静默重试）。"""
    last_error: Optional[ApiError] = None
    attempts = 0
    while True:
        attempts += 1

        try:
            return fn()
        except ApiError as e:
            last_error = e
            if not (e.is_retryable
                    and attempts <= _effective_max_retries(e, max_retries) + 1):
                raise
        effective_max = _effective_max_retries(last_error, max_retries)
        if attempts > effective_max:
            break

        if is_rate_limit_error(last_error):
            delay = _rate_limit_backoff(attempts)
        else:
            delay = backoff_for_attempt(attempts, initial_backoff_ms, max_backoff_ms)
        if on_retry is not None:
            on_retry(attempts, effective_max, delay, last_error)
        time.sleep(delay)   # 同步退避; asyncio.sleep 在这里不会真正休眠


    assert last_error is not None   # 能走到这说明循环内必然捕获过 ApiError
    raise RetriesExhausted(attempts, last_error)
