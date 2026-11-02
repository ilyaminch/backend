"""Окна ограничения входа для согласованного одного процесса backend.

Используется адрес из ASGI scope, а не произвольный X-Forwarded-For.
Настройка доверенного proxy выполняется отдельно при развёртывании.
Все методы синхронные и вызываются из одного event loop без await между
проверкой и резервированием. При нескольких workers нужен общий backend.
"""

import hashlib
import math
import time
from collections import deque
from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.errors import ApiError


@dataclass
class _Pair:
    failures: deque[float] = field(default_factory=deque)
    pending: int = 0


@dataclass
class LoginAttempt:
    pair: _Pair
    completed: bool = False


class LoginRateLimiter:
    def __init__(self, settings: Settings) -> None:
        self.ip_limit = settings.login_ip_attempts
        self.ip_window = settings.login_ip_window_seconds
        self.failure_limit = settings.login_failed_attempts
        self.failure_window = settings.login_failure_window_seconds
        self._ips: dict[str, deque[float]] = {}
        self._pairs: dict[tuple[str, bytes], _Pair] = {}

    @staticmethod
    def _trim(values: deque[float], threshold: float) -> None:
        while values and values[0] <= threshold:
            values.popleft()

    def _cleanup(self, now: float) -> None:
        for address, attempts in list(self._ips.items()):
            self._trim(attempts, now - self.ip_window)
            if not attempts:
                del self._ips[address]
        for key, pair in list(self._pairs.items()):
            self._trim(pair.failures, now - self.failure_window)
            if not pair.failures and not pair.pending:
                del self._pairs[key]

    @staticmethod
    def _reject(delay: float) -> None:
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Слишком много попыток входа. Повторите позже.",
            True,
            headers={"Retry-After": str(max(1, math.ceil(delay)))},
        )

    def begin(self, address: str, login: str) -> LoginAttempt:
        now = time.monotonic()
        self._cleanup(now)
        attempts = self._ips.setdefault(address, deque())
        if len(attempts) >= self.ip_limit:
            self._reject(attempts[0] + self.ip_window - now)
        # Rejected pair attempts still consume the IP budget.
        attempts.append(now)
        key = (address, hashlib.sha256(login.encode("utf-8", "surrogatepass")).digest())
        pair = self._pairs.setdefault(key, _Pair())
        # In-flight verifications reserve places so parallel requests cannot
        # bypass the failure limit. These places are released on every exit.
        if len(pair.failures) + pair.pending >= self.failure_limit:
            delay = pair.failures[0] + self.failure_window - now if pair.failures else 1
            self._reject(delay)
        pair.pending += 1
        return LoginAttempt(pair)

    def finish(self, attempt: LoginAttempt, *, success: bool | None) -> None:
        if attempt.completed:
            return
        attempt.completed = True
        attempt.pair.pending -= 1
        if success is True:
            attempt.pair.failures.clear()
        elif success is False:
            attempt.pair.failures.append(time.monotonic())
        # None means cancellation/technical failure, not invalid credentials.
