"""시장 지표 수집. VIX는 FRED VIXCLS(Cboe 일별 종가), USD/KRW는 한국은행 ECOS 731Y001/0000001(원/미국달러 매매기준율).

키는 요청 URL에만 넣는다. 오류·상태·로그에는 HTTP 상태와 제공자 공개 오류 코드만 남기고 키와 원문 응답은 넣지 않는다.
수집 실패는 0이나 추정값으로 채우지 않고, 마지막 정상값이 있으면 오래된 자료로 표시한다.
"""
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


KST = timezone(timedelta(hours=9))
HISTORY_DAYS = 120
STALE_DAYS = 5          # 주말·미국/한국 공휴일을 넘겨도 이 기간 안이면 정상 일별 자료로 본다.
CACHE_SECONDS = 3600    # 일별 자료라 접속마다 다시 받지 않는다.
RETRY_SECONDS = 60      # 강제 갱신도 이 간격 안에서는 외부 호출하지 않는다.
MAX_BODY = 2_000_000
KEY_PATTERN = re.compile(r"[A-Za-z0-9]{8,64}")
DECIMAL = re.compile(r"\d+(?:\.\d+)?")


class SourceError(Exception):
    def __init__(self, kind, http_status=None, provider_code=None):
        super().__init__(kind)
        self.kind, self.http_status, self.provider_code = kind, http_status, provider_code

    def diagnostic(self):
        return {k: v for k, v in (("http_status", self.http_status), ("provider_code", self.provider_code)) if v is not None}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, timeout=20):
    request = Request(url, headers={"User-Agent": "market-monitor-local/1.0", "Accept": "application/json"})
    try:
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY + 1)
    except HTTPError as error:
        return error.code, error.read(65_536)
    except (URLError, TimeoutError, OSError):
        raise SourceError("network_error") from None


def _json(body, http_status):
    if len(body) > MAX_BODY:
        raise SourceError("invalid_response", http_status)
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise SourceError("invalid_response", http_status) from None
    if not isinstance(data, dict):
        raise SourceError("invalid_response", http_status)
    return data


def _us_eastern_offset(day):
    # 2007년 이후 미국 일광절약시간: 3월 둘째 일요일 ~ 11월 첫째 일요일.
    march = date(day.year, 3, 8)
    start = march + timedelta((6 - march.weekday()) % 7)
    november = date(day.year, 11, 1)
    end = november + timedelta((6 - november.weekday()) % 7)
    return timezone(timedelta(hours=-4 if start <= day < end else -5))


def _vix_close_time(day):
    return datetime(day.year, day.month, day.day, 16, 15, tzinfo=_us_eastern_offset(day))


def _kst_day(day):
    return datetime(day.year, day.month, day.day, tzinfo=KST)


def _observations(points, low, high, http_status):
    """[(date, 값 문자열 또는 None)] → 날짜 오름차순·중복 없음·범위 검사."""
    result, previous = [], None
    for day, raw in points:
        if previous is not None and day <= previous:
            raise SourceError("invalid_response", http_status)
        previous = day
        if raw is None:
            result.append((day, None))
            continue
        if not isinstance(raw, str) or not DECIMAL.fullmatch(raw.strip()):
            raise SourceError("invalid_response", http_status)
        value = Decimal(raw.strip())
        if not low < value < high:
            raise SourceError("invalid_response", http_status)
        result.append((day, format(value, "f")))
    return result


def fetch_fred_vix(key, start, end, transport=http_get):
    query = urlencode({"series_id": "VIXCLS", "api_key": key, "file_type": "json",
                       "observation_start": start.isoformat(), "observation_end": end.isoformat()})
    status, body = transport("https://api.stlouisfed.org/fred/series/observations?" + query)
    data = _json(body, status)
    if status != 200:
        code = data.get("error_code")
        raise SourceError("provider_rejected" if status in {400, 401, 403, 429} else "http_error", status,
                          str(code) if isinstance(code, int) else None)
    rows = data.get("observations")
    if not isinstance(rows, list):
        raise SourceError("invalid_response", status)
    points = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise SourceError("invalid_response", status)
        try:
            day = date.fromisoformat(row["date"])
        except ValueError:
            raise SourceError("invalid_response", status) from None
        value = row.get("value")
        points.append((day, None if value == "." else value))  # FRED는 휴장·누락을 "."로 표시한다.
    return _observations(points, Decimal(0), Decimal(200), status)


def fetch_ecos_usd_krw(key, start, end, transport=http_get):
    if not KEY_PATTERN.fullmatch(key):
        raise SourceError("invalid_key_format")
    url = ("https://ecos.bok.or.kr/api/StatisticSearch/" + key + "/json/kr/1/1000/731Y001/D/"
           + start.strftime("%Y%m%d") + "/" + end.strftime("%Y%m%d") + "/0000001")
    status, body = transport(url)
    data = _json(body, status)
    if status != 200:
        raise SourceError("http_error", status)
    if "RESULT" in data:
        code = data["RESULT"].get("CODE") if isinstance(data["RESULT"], dict) else None
        code = code if isinstance(code, str) and re.fullmatch(r"(?:INFO|ERROR)-\d{3}", code) else None
        raise SourceError("no_data" if code == "INFO-200" else "provider_rejected", status, code)
    block = data.get("StatisticSearch")
    rows = block.get("row") if isinstance(block, dict) else None
    if not isinstance(rows, list) or block.get("list_total_count") != len(rows):
        raise SourceError("invalid_response", status)
    points = []
    for row in rows:
        if (not isinstance(row, dict) or row.get("STAT_CODE") != "731Y001" or row.get("ITEM_CODE1") != "0000001"
                or row.get("UNIT_NAME") != "원" or not isinstance(row.get("TIME"), str)
                or not re.fullmatch(r"\d{8}", row["TIME"])):
            raise SourceError("invalid_response", status)
        try:
            day = datetime.strptime(row["TIME"], "%Y%m%d").date()
        except ValueError:
            raise SourceError("invalid_response", status) from None
        points.append((day, row.get("DATA_VALUE")))
    return _observations(points, Decimal(100), Decimal(10_000), status)


INDICATORS = {
    "vix": {"key": "FRED_API_KEY", "fetch": fetch_fred_vix, "stamp": _vix_close_time,
            "source": "FRED VIXCLS (Cboe VIX 일별 종가)", "basis": "일별 종가, 미국 동부 16:15 기준",
            "source_url": "https://fred.stlouisfed.org/series/VIXCLS"},
    "usd_krw": {"key": "ECOS_API_KEY", "fetch": fetch_ecos_usd_krw, "stamp": _kst_day,
                "source": "한국은행 ECOS 731Y001 원/미국달러 매매기준율", "basis": "일별 매매기준율, 1달러당 원",
                "source_url": "https://ecos.bok.or.kr/"},
}
MESSAGES = {
    "network_error": "공급원 연결 실패", "http_error": "공급원 HTTP 오류", "provider_rejected": "공급원이 요청을 거절함(키·요청 확인)",
    "invalid_response": "응답 형식이 예상과 다름", "no_data": "조회 기간에 자료 없음", "invalid_key_format": "키 형식 확인 필요",
    "internal_error": "수집 처리 오류",
}


def build_item(name, observations, collected_at, today):
    spec = INDICATORS[name]
    known = [(day, value) for day, value in observations if value is not None]
    if not known:
        raise SourceError("no_data")
    day, value = known[-1]
    history = [{"at": spec["stamp"](d).isoformat(), "value": v} for d, v in observations if d <= day]
    stale = (today - day).days > STALE_DAYS
    return {"state": "stale" if stale else "available", "value": value, "as_of": spec["stamp"](day).isoformat(),
            "as_of_date": day.isoformat(), "as_of_precision": "date", "collected_at": collected_at,
            "delayed": True, "source": spec["source"], "basis": spec["basis"], "source_url": spec["source_url"],
            "history": history,
            "message": f"{spec['basis']}. 실시간 값이 아닙니다." + (f" 기준일이 {STALE_DAYS}일보다 오래되었습니다." if stale else "")}


class MarketIndicators:
    """지표별 마지막 결과를 메모리에 보관한다. 한 지표의 실패가 다른 지표나 이전 정상값을 지우지 않는다."""

    def __init__(self, keys=None, transport=http_get, clock=time.monotonic, today=None):
        self._keys = {name: value for name, value in (keys or {}).items() if value}
        self._transport, self._clock = transport, clock
        self._today = today or (lambda: datetime.now(KST).date())
        self._lock, self._fetch_lock = Lock(), Lock()
        self._items, self._good, self._fetched_at = {}, {}, None

    def configured(self, name):
        return INDICATORS[name]["key"] in self._keys

    def _collect(self):
        today = self._today()
        start = today - timedelta(days=HISTORY_DAYS)
        for name, spec in INDICATORS.items():
            key = self._keys.get(spec["key"])
            if not key:
                item = {"state": "not_configured", "message": f"{spec['key']}를 .env에 넣고 서버를 다시 실행해 주세요."}
            else:
                collected_at = datetime.now(timezone.utc).isoformat()
                try:
                    item = build_item(name, spec["fetch"](key, start, today, self._transport), collected_at, today)
                    with self._lock:
                        self._good[name] = item
                except SourceError as error:
                    item = self._failure(name, error.kind, error.diagnostic(), collected_at)
                except Exception:
                    item = self._failure(name, "internal_error", {}, collected_at)
            with self._lock:
                self._items[name] = item

    def _failure(self, name, kind, diagnostic, attempted_at):
        message = MESSAGES.get(kind, kind)
        with self._lock:
            good = self._good.get(name)
        if good:
            item = dict(good, state="stale", message=f"최근 조회 실패({message}). 이전 정상값을 표시합니다.")
        else:
            item = {"state": "error", "message": message + ". 값을 추정하지 않습니다."}
        item.update(error=kind, diagnostic=diagnostic, attempted_at=attempted_at)
        return item

    def snapshot(self, force=False):
        now = self._clock()
        with self._lock:
            age = None if self._fetched_at is None else now - self._fetched_at
        due = age is None or age >= CACHE_SECONDS or (force and age >= RETRY_SECONDS)
        if due and self._fetch_lock.acquire(blocking=False):
            try:
                with self._lock:
                    self._fetched_at = now
                self._collect()
            finally:
                self._fetch_lock.release()
        with self._lock:
            return {name: dict(self._items.get(name) or {"state": "not_queried", "message": "조회 중입니다."})
                    for name in INDICATORS}
