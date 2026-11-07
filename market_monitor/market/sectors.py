"""보유 종목 업종 비중과 섹터 ETF 대용 흐름.

업종은 LS g3104 업종명(규칙 기반)이다. 성장·가치 분류는 하지 않는다. ETF는 구성 종목을 분해하지 않고 한 묶음으로 둔다.
시장 흐름은 SPDR 섹터 ETF 11개의 LS g3204 일별 수정종가 변화이며, 전체 거래소 섹터 수익률이 아니다.
"""
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from threading import Lock

from ..brokers.kiwoom import BrokerError
from ..brokers.ls import LSClient
from .indicators import CACHE_SECONDS, KST, RETRY_SECONDS, STALE_DAYS


SECTOR_ETFS = {
    "XLK": "기술", "XLF": "금융", "XLV": "헬스케어", "XLY": "경기소비재", "XLP": "필수소비재", "XLE": "에너지",
    "XLI": "산업재", "XLB": "소재", "XLU": "유틸리티", "XLRE": "부동산", "XLC": "커뮤니케이션",
}
SECTOR_EXCHANGE = "81"
ASSET_CLASSES = {"stock": "개별주", "etf": "ETF", "leveraged_etf": "레버리지 ETF", "inverse_etf": "인버스 ETF",
                 "etf_unrated": "ETF (배율 미확인)", "unknown": "분류 미확인"}
ETF_GROUP = "ETF (구성 종목 미분해)"
UNKNOWN_GROUP = "분류 미확인"
CASH_GROUP = "현금"
MONTH_DAYS = 30
MONTH_LABEL = "약 1개월(30일)"
HISTORY_DAYS = 45
PCT = Decimal("0.01")
NOTE = ("업종은 LS g3104 업종명(규칙 기반)이며 ETF 구성 종목은 분해하지 않습니다. 시장 흐름은 SPDR 섹터 ETF 11개의 "
        "LS 일별 수정종가 변화로, 전체 거래소 섹터 수익률이 아닌 대용치입니다.")


def asset_class(profile):
    if not profile:
        return "unknown"
    if profile.get("kind") == "stock":
        return "stock"
    if profile.get("kind") == "etf":
        # LS는 일부 레버리지 ETF의 업종을 배율 없이 준다(실응답: 2X 상품이 "자본시장 서비스").
        # 배율을 모르면 1배로 단정하지 않는다.
        if profile.get("leverage") is None:
            return "etf_unrated"
        leverage = Decimal(profile["leverage"])
        if leverage < 0:
            return "inverse_etf"
        return "leveraged_etf" if leverage > 1 else "etf"
    return "unknown"


def _change(last, base):
    return format(((Decimal(last) / Decimal(base) - 1) * 100).quantize(PCT, ROUND_HALF_EVEN), "f")


def etf_flow(closes):
    """closes: [(date, 십진 문자열)] 날짜 오름차순."""
    if len(closes) < 2:
        return {"close": closes[-1][1] if closes else None, "as_of_date": closes[-1][0].isoformat() if closes else None,
                "change_1d_pct": None, "change_1m_pct": None, "base_date": None, "period_1m": MONTH_LABEL,
                "reason": "insufficient_history"}
    last_day, last = closes[-1]
    floor = last_day - timedelta(days=MONTH_DAYS)
    base_day, base = next((day, close) for day, close in closes if day >= floor)
    month = _change(last, base) if base_day < last_day else None
    return {"close": last, "as_of_date": last_day.isoformat(), "change_1d_pct": _change(last, closes[-2][1]),
            "change_1m_pct": month, "base_date": base_day.isoformat() if month is not None else None,
            "period_1m": MONTH_LABEL, "reason": None if month is not None else "insufficient_history"}


def _group(holding, profiles):
    profile = profiles.get(holding["symbol"])
    kind = asset_class(profile)
    if kind == "stock" and profile.get("industry"):
        return profile["industry"], "industry"
    if kind in {"etf", "leveraged_etf", "inverse_etf", "etf_unrated"}:
        return ETF_GROUP, "etf"
    return UNKNOWN_GROUP, "unknown"


def holdings_breakdown(portfolio, profiles):
    """portfolio.holdings.build 결과의 업종 비중. 분모는 포트폴리오와 같다. total_assets 기준이면 현금을 한 묶음으로 더한다."""
    valued = [h for h in portfolio["holdings"] if h.get("market_value") is not None]
    cash = portfolio.get("cash") if portfolio.get("basis") == "total_assets" else None
    total = (Decimal(portfolio["total_assets"]) if cash else
             sum((Decimal(h["market_value"]) for h in valued), Decimal(0)))
    if total <= 0:
        return []
    groups = {}
    for holding in valued:
        name, kind = _group(holding, profiles or {})
        group = groups.setdefault(name, {"group": name, "kind": kind, "value": Decimal(0), "symbols": []})
        group["value"] += Decimal(holding["market_value"])
        group["symbols"].append(holding["symbol"])
    if cash:
        groups[CASH_GROUP] = {"group": CASH_GROUP, "kind": "cash", "value": Decimal(cash["total_usd"]), "symbols": []}
    result = [{"group": g["group"], "kind": g["kind"],
               "weight_pct": format((g["value"] / total * 100).quantize(PCT, ROUND_HALF_EVEN), "f"),
               "symbols": sorted(g["symbols"])} for g in groups.values()]
    # 반올림 전 값으로 정렬하고, 같으면 분류 미확인을 뒤에 둔다.
    order = {g["group"]: g["value"] for g in groups.values()}
    result.sort(key=lambda g: (-order[g["group"]], g["kind"] == "unknown", g["group"]))
    return result


def _parts_state(total, failed):
    if failed == 0:
        return "available"
    return "error" if failed == total else "partial"


class SectorData:
    """프로세스 메모리 캐시. 업종 분류는 바뀌지 않으므로 프로세스 수명 동안 보관하고 '해당 종목 없음'(None)도 보관한다.
    ETF 하나의 실패가 다른 ETF나 이전 정상값을 지우지 않는다."""

    def __init__(self, credentials, client=None, clock=time.monotonic, today=None):
        self._credentials = credentials
        self._client = client or LSClient()
        self._clock = clock
        self._today = today or (lambda: datetime.now(KST).date())
        self._lock = Lock()
        self._running = False
        self._attempted = None
        self._profiles, self._profile_errors = {}, {}
        self._flows, self._good = {}, {}
        self._state = {"profiles": "not_queried", "etfs": "not_queried"}
        self._error = None
        self._collected_at = None

    def profiles(self):
        with self._lock:
            return {symbol: dict(p) if p else None for symbol, p in self._profiles.items()}

    def snapshot(self):
        with self._lock:
            etfs = [{"symbol": symbol, "label": label,
                     **(self._flows.get(symbol) or {"state": "not_queried"})} for symbol, label in SECTOR_ETFS.items()]
            return {"configured": self._credentials.get("ls") is not None, "state": dict(self._state),
                    "error": self._error, "profile_errors": dict(self._profile_errors), "etfs": etfs,
                    "collected_at": self._collected_at, "note": NOTE}

    def status_item(self):
        """연결 상태 표의 시장 항목."""
        with self._lock:
            state = self._state["etfs"]
        if self._credentials.get("ls") is None:
            return {"state": "not_configured", "message": "LS 키가 필요합니다. " + NOTE}
        messages = {"not_queried": "섹터 조회 전입니다.", "syncing": "섹터 자료를 조회 중입니다.",
                    "available": "섹터 ETF 일별 자료를 수집했습니다.", "partial": "일부 섹터 ETF 수집에 실패했습니다.",
                    "error": "섹터 ETF 수집에 실패했습니다."}
        return {"state": state, "message": messages[state] + " " + NOTE}

    def refresh(self, symbols, etfs=True):
        """etfs=False는 보유 종목 업종만 조회하고 ETF 상태와 캐시는 그대로 둔다."""
        with self._lock:
            if self._running:
                return "busy"
            credential = self._credentials.get("ls")
            if credential is None:
                return "not_configured"
            now = self._clock()
            if self._attempted is not None and now - self._attempted < RETRY_SECONDS:
                return "rate_limited"
            self._attempted, self._running = now, True
            previous = dict(self._state)
            self._state = {"profiles": "syncing", "etfs": "syncing" if etfs else previous["etfs"]}
            self._error = None
        try:
            return self._collect(credential, symbols, now, previous, etfs)
        except Exception:
            with self._lock:
                self._state = {"profiles": "error", "etfs": "error" if etfs else previous["etfs"]}
                self._error = "internal_error"
            return "failed"
        finally:
            with self._lock:
                self._running = False

    def _collect(self, credential, symbols, now, previous, etfs):
        symbols = sorted({s for s in symbols if isinstance(s, str)})
        with self._lock:
            missing = [s for s in symbols if s not in self._profiles]
            due = [s for s in SECTOR_ETFS if s not in self._good or now - self._good[s][1] >= CACHE_SECONDS] if etfs else []
        token = None
        if missing or due:
            try:
                token = self._client.token(credential)
            except BrokerError as error:
                with self._lock:
                    self._error = error.kind
                    self._state = {"profiles": "error" if missing else previous["profiles"],
                                   "etfs": "error" if due else previous["etfs"]}
                    for symbol in due:
                        self._flows[symbol] = self._failure(symbol, error.kind)
                return "failed"
        for symbol in missing:
            try:
                profile = self._client.stock_profile(token, symbol)
            except BrokerError as error:
                kind = error.kind
            except Exception:
                kind = "internal_error"
            else:
                with self._lock:
                    self._profiles[symbol] = profile
                    self._profile_errors.pop(symbol, None)
                continue
            with self._lock:
                self._profile_errors[symbol] = kind
        today = self._today()
        start, end = (today - timedelta(days=HISTORY_DAYS)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        for symbol in due:
            collected_at = datetime.now(timezone.utc).isoformat()
            try:
                flow = etf_flow(self._client.daily_closes(token, symbol, SECTOR_EXCHANGE, start, end))
            except BrokerError as error:
                item = self._failure(symbol, error.kind)
            except Exception:
                item = self._failure(symbol, "internal_error")
            else:
                stale = flow["as_of_date"] is not None and (today - datetime.fromisoformat(flow["as_of_date"]).date()).days > STALE_DAYS
                item = {"state": "no_data" if flow["as_of_date"] is None else "stale" if stale else "available",
                        **flow, "collected_at": collected_at, "error": "no_data" if flow["as_of_date"] is None else None}
                if flow["as_of_date"] is not None:
                    with self._lock:
                        self._good[symbol] = (item, now)
            with self._lock:
                self._flows[symbol] = item
        with self._lock:
            failed_profiles = sum(1 for s in symbols if s not in self._profiles)
            failed_etfs = sum(1 for s in SECTOR_ETFS if (self._flows.get(s) or {}).get("error"))
            self._state = {"profiles": _parts_state(len(symbols), failed_profiles) if symbols else "not_queried",
                           "etfs": _parts_state(len(SECTOR_ETFS), failed_etfs) if etfs else previous["etfs"]}
            self._collected_at = datetime.now(timezone.utc).isoformat()
            states = {self._state[part] for part in (("profiles", "etfs") if etfs else ("profiles",))} - {"not_queried"}
        if states == {"error"}:
            return "failed"
        return "available" if states <= {"available"} else "partial"

    def _failure(self, symbol, kind):
        good = self._good.get(symbol)
        if good:
            return dict(good[0], state="stale", error=kind)
        return {"state": "error", "error": kind}
