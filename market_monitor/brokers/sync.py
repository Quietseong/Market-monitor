from copy import deepcopy
import time
from threading import Lock

from .kiwoom import BrokerError, KiwoomClient
from .ls import LSClient


class BrokerSync:
    """증권사 하나의 조회 상태. 증권사마다 별도 인스턴스라 한쪽 실패가 다른 쪽 스냅샷을 지우지 않는다."""

    def __init__(self, credentials, client=None, broker="kiwoom"):
        if broker not in {"kiwoom", "ls"}:
            raise ValueError("지원하지 않는 증권사입니다.")
        self._broker = broker
        self._credentials = credentials
        self._client = client or (KiwoomClient() if broker == "kiwoom" else LSClient())
        self._lock = Lock()
        self._next_attempt = 0.0
        self._snapshot = None
        self._state = "not_queried"
        self._error = None
        self._diagnostic = None

    def status(self):
        with self._lock:
            return deepcopy({"state": self._state, "error": self._error,
                             "diagnostic": self._diagnostic, "snapshot": self._snapshot})

    def refresh(self):
        with self._lock:
            if self._state == "syncing":
                return "busy"
            credential = self._credentials.get(self._broker)
            if credential is None:
                return "not_configured"
            if time.monotonic() < self._next_attempt:
                return "rate_limited"
            self._next_attempt = time.monotonic() + 60
            self._state, self._error = "syncing", None
            self._diagnostic = None
        try:
            snapshot = self._client.fetch(credential)
        except BrokerError as error:
            with self._lock:
                self._state, self._error = "failed", error.kind
                self._diagnostic = error.diagnostic
            return "failed"
        except Exception:
            with self._lock:
                self._state, self._error = "failed", "internal_error"
            return "failed"
        with self._lock:
            self._snapshot = snapshot
            self._state = "fetched_unreconciled"
        return "fetched_unreconciled"
