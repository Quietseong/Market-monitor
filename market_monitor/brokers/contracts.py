import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


READ_OPERATIONS = frozenset({"authenticate", "positions", "cash", "account_info"})


def require_read_operation(broker, operation):
    if broker not in {"kiwoom", "ls"} or operation not in READ_OPERATIONS:
        raise ValueError("허용된 증권사 조회 작업이 아닙니다.")
    return operation


def require_decimal(value):
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("유한한 Decimal 값이 필요합니다.")


def require_currency(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("통화 코드가 필요합니다.")


@dataclass(frozen=True, slots=True)
class Position:
    source_symbol: str
    currency: str
    quantity: Decimal
    average_cost: Decimal | None

    def __post_init__(self):
        if (not isinstance(self.source_symbol, str)
                or not re.fullmatch(r"[A-Z0-9.:/\-]{1,32}", self.source_symbol)):
            raise ValueError("원천 종목 코드가 필요합니다.")
        require_currency(self.currency)
        require_decimal(self.quantity)
        if self.average_cost is not None:
            require_decimal(self.average_cost)
            if self.average_cost < 0:
                raise ValueError("매입단가를 확인해 주세요.")


@dataclass(frozen=True, slots=True)
class CashBalance:
    currency: str
    amount: Decimal
    basis: str

    def __post_init__(self):
        require_currency(self.currency)
        require_decimal(self.amount)
        if self.basis not in {"settled", "total", "available", "unverified"}:
            raise ValueError("잔액의 의미를 구분해야 합니다.")


@dataclass(frozen=True, slots=True)
class Snapshot:
    broker: str
    account_ref: str
    collected_at: datetime
    positions: tuple[Position, ...]
    cash: tuple[CashBalance, ...]
    complete: bool = True

    def __post_init__(self):
        require_read_operation(self.broker, "positions")
        if not isinstance(self.account_ref, str) or not self.account_ref:
            raise ValueError("내부 계좌 참조가 필요합니다.")
        if (not isinstance(self.collected_at, datetime)
                or self.collected_at.utcoffset() is None):
            raise ValueError("수집 시각에 시간대가 필요합니다.")
        if (not isinstance(self.positions, tuple) or not isinstance(self.cash, tuple)
                or not all(isinstance(p, Position) for p in self.positions)
                or not all(isinstance(c, CashBalance) for c in self.cash)
                or not isinstance(self.complete, bool)):
            raise ValueError("스냅샷 형식을 확인해 주세요.")
        keys = [(p.source_symbol, p.currency) for p in self.positions]
        if len(keys) != len(set(keys)):
            raise ValueError("중복 종목은 원천 페이지를 먼저 확인해야 합니다.")
