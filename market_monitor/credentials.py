from dataclasses import dataclass, field
from threading import RLock


BROKERS = frozenset({"kiwoom", "ls"})


@dataclass(frozen=True, slots=True)
class Credentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)


class CredentialStore:
    def __init__(self):
        self._items = {}
        self._lock = RLock()

    def configure(self, broker, app_key, app_secret):
        if broker not in BROKERS:
            raise ValueError("지원하지 않는 증권사입니다.")
        for value in (app_key, app_secret):
            if (not isinstance(value, str) or not value or len(value) > 2048
                    or not value.isascii() or any(c.isspace() or ord(c) < 33 for c in value)):
                raise ValueError("키의 길이·공백·문자 형식을 확인해 주세요.")
        with self._lock:
            self._items[broker] = Credentials(app_key, app_secret)

    def get(self, broker):
        with self._lock:
            return self._items.get(broker)

    def is_configured(self, broker):
        return self.get(broker) is not None

    def clear(self):
        with self._lock:
            self._items.clear()
