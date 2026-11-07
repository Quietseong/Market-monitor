"""LS read-only overseas holdings, cash, instrument classification (g3104) and daily closes (g3204). Raw responses, OutBlock1 and credentials are never logged."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .contracts import require_currency
from .kiwoom import BrokerError, NoRedirect, rejection_hints, display_name


BASE_URL = "https://openapi.ls-sec.co.kr:8080"
# 허용 목록. 주문·정정·취소 TR은 넣지 않는다.
PATHS = {"token": "/oauth2/token", "COSOQ00201": "/overseas-stock/accno", "COSOQ02701": "/overseas-stock/accno",
         "g3104": "/overseas-stock/market-data", "g3204": "/overseas-stock/chart"}
# 공식 응답 예제(조회가 완료되었습니다)의 rsp_cd. TR마다 다르다. 전체 성공 코드표가 미확인이므로
# 이 코드만 성공으로 보고, 공식 근거로 다른 성공 코드가 확인될 때만 늘린다. g3104·g3204는 2026-11-07 실응답 확인.
SUCCESS_CODES = {"COSOQ00201": frozenset({"00001"}), "COSOQ02701": frozenset({"00136"}),
                 "g3104": frozenset({"00000"}), "g3204": frozenset({"00000"})}
# g3104 거래소 코드. 나스닥을 먼저 찾고 없으면 뉴욕·아멕스(81)를 찾는다.
PROFILE_EXCHANGES = ("82", "81")
INSTRUMENT_KINDS = {"주식": "stock", "ETF,ETN": "etf"}
LEVERAGE = re.compile(r"ETF\(([+-]?\d+(?:\.\d+)?)배\)")
SYMBOL = re.compile(r"[A-Z0-9.:/\-]{1,32}")
KST = timezone(timedelta(hours=9))
# RFC 6749 5.2 토큰 오류 코드. 이 목록 밖의 값은 노출하지 않는다.
OAUTH_ERRORS = frozenset({"invalid_request", "invalid_client", "invalid_grant", "unauthorized_client",
                          "unsupported_grant_type", "invalid_scope"})


# 토큰 거절 문구를 원문 대신 이 분류로만 내보낸다. 패턴은 원인 후보 이름이며 확정 판단이 아니다.
AUTH_HINTS = {
    "appkey": r"(?i)app\s?key|앱\s?키",
    "appsecret": r"(?i)secret|시크릿|비밀\s?키",
    "invalid": r"(?i)유효하지|잘못|올바르지|invalid|not valid",
    "expired": r"(?i)만료|기한|기간|expire",
    "scope": r"(?i)scope",
    "grant": r"(?i)grant",
    "not_found": r"(?i)존재하지|없는|not found",
    "blocked": r"(?i)차단|정지|중지|block|suspend",
}


def auth_hints(message):
    if not isinstance(message, str):
        return []
    return [name for name, pattern in AUTH_HINTS.items() if re.search(pattern, message[:2000])]


def provider_code(body):
    # TR 응답은 rsp_cd, 게이트웨이 오류는 error_code(가짜 키 대조로 관측: 403 IGW00103)로 온다.
    if not isinstance(body, dict):
        return None
    code = body.get("rsp_cd", body.get("error_code"))
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z]{0,4}\d{1,6}", code) else None


class LSHTTP:
    def __init__(self):
        self._opener = build_opener(NoRedirect())
        self._lock = Lock()
        self._last = 0.0

    def __call__(self, operation, payload, token="", continuation=""):
        if operation not in PATHS:
            raise BrokerError("operation_denied")
        path = PATHS[operation]
        if operation == "token":
            data = urlencode(payload).encode()
            headers = {"content-type": "application/x-www-form-urlencoded"}
        else:
            # 공식 sample.py 헤더 그대로. mac_address는 법인 계좌만 필수라 빈값.
            data = json.dumps(payload).encode()
            headers = {"content-type": "application/json; charset=utf-8", "authorization": "Bearer " + token,
                       "tr_cd": operation, "tr_cont": "Y" if continuation else "N",
                       "tr_cont_key": continuation, "mac_address": ""}
        with self._lock:
            # TR별 초당 1회 제한. 모든 호출을 1.1초 이상 띄운다.
            time.sleep(max(0, 1.1 - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            try:
                request = Request(BASE_URL + path, data, headers, method="POST")
                with self._opener.open(request, timeout=15) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise BrokerError("invalid_response", operation)
                    body = json.loads(raw, parse_float=Decimal)
                    return body, {k.lower(): v for k, v in response.headers.items()}
            except HTTPError as error:
                kind = {401: "authentication_failed", 403: "permission_denied",
                        429: "rate_limited"}.get(error.code, "http_error")
                if operation == "token" and kind == "http_error":
                    kind = "authentication_failed"
                try:
                    raw = error.read(20_000)
                except OSError:
                    raw = b""
                media = (error.headers.get("Content-Type", "") if error.headers else "").split(";")[0].strip().lower()
                error.close()
                try:
                    body = json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    body = None
                message = body.get("rsp_msg") if isinstance(body, dict) else None
                if isinstance(body, dict) and not isinstance(message, str):
                    message = body.get("error_description")
                failure = BrokerError(kind, operation, provider_code(body), rejection_hints(message) + auth_hints(message))
                # 원문 대신 HTTP 상태·본문 종류·허용 키 존재 여부·OAuth 표준 오류 코드만 남긴다.
                oauth = body.get("error") if isinstance(body, dict) else None
                failure.diagnostic.update(
                    http_status=error.code,
                    body_kind="empty" if not raw else "json" if isinstance(body, dict) else
                              "html" if media == "text/html" or raw.lstrip()[:1] == b"<" else "other",
                    body_keys=sorted(k for k in ("rsp_cd", "rsp_msg", "error", "error_code", "error_description")
                                     if isinstance(body, dict) and k in body),
                    oauth_error=oauth if oauth in OAUTH_ERRORS else None)
                raise failure from None
            except (URLError, TimeoutError, OSError):
                raise BrokerError("network_error", operation) from None
            except (ValueError, UnicodeError):
                raise BrokerError("invalid_response", operation) from None


def amount(value, operation):
    """십진 문자열 또는 정수(예제의 원화 금액)만 받는다. 누락·빈값은 0으로 바꾸지 않는다."""
    if isinstance(value, bool):
        raise BrokerError("invalid_response", operation)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal) and value.is_finite():
        return format(value, "f")
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()):
        raise BrokerError("invalid_response", operation)
    return format(Decimal(value.strip()), "f")


def optional_amount(value, operation):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return amount(value, operation)


def currency(value, operation):
    try:
        require_currency(value)
    except ValueError:
        raise BrokerError("invalid_response", operation) from None
    return value


def rows(body, block, operation):
    # 명세표는 Object로 적었지만 공식 응답 예제는 배열이다. 예제 형태만 받고 다른 형태는 실응답 확인 대상으로 남긴다.
    value = body.get(block)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        error = BrokerError("invalid_response", operation, provider_code(body))
        error.diagnostic["block"] = block
        error.diagnostic["shape"] = type(value).__name__ if value is not None else "missing"
        raise error
    return value


def settled_cash(estimates, deposits, krw):
    """COSOQ02701 OutBlock2 추정외화예수금으로 결제 후 현금을 만든다. (settled_cash, 사유) 중 하나만 값이 있다.
    deposits: {통화: OutBlock3 FcurrDps}. 실조회로 확인한 관계(추정1 = FcurrDps, 추정i = 추정(i-1) - 매수i + 매도i)가
    깨지면 추정4를 믿지 않는다. T4FcurrDps는 대기 거래가 없어도 FcurrDps·추정4와 달라 쓰지 않는다."""
    result, seen = [{"currency": "KRW", "amount": krw, "settles_on": None, "source": "COSOQ02701",
                     "source_field": "WonDpsBalAmt"}], set()
    try:
        for row in estimates:
            if not isinstance(row, dict):
                return None, "invalid_cash"
            code = currency(row.get("CrcyCode"), "COSOQ02701")
            if code in seen or code == "KRW":
                return None, "invalid_cash"
            seen.add(code)
            value = lambda field, i: Decimal(amount(row.get(f"{field}{i}"), "COSOQ02701"))
            chain = [value("PrsmptFcurrDps", 1)]
            for i in range(2, 5):
                chain.append(value("PrsmptFcurrDps", i))
                if chain[-1] != chain[-2] - value("FcurrBuyAdjstAmt", i) + value("FcurrSellAdjstAmt", i):
                    return None, "inconsistent_cash"
            if code in deposits and chain[0] != Decimal(deposits[code]):
                return None, "inconsistent_cash"
            result.append({"currency": code, "amount": format(chain[3], "f"), "settles_on": None,
                           "source": "COSOQ02701", "source_field": "PrsmptFcurrDps4"})
    except BrokerError:
        return None, "invalid_cash"
    result += [{"currency": code, "amount": value, "settles_on": None, "source": "COSOQ02701", "source_field": "FcurrDps"}
               for code, value in deposits.items() if code not in seen and Decimal(value) != 0]
    return result, None


class LSClient:
    def __init__(self, transport=None, today=None):
        self._transport = transport or LSHTTP()
        self._today = today or (lambda: datetime.now(KST).strftime("%Y%m%d"))

    def _call(self, operation, payload, token="", continuation=""):
        body, headers = self._transport(operation, payload, token, continuation)
        if not isinstance(body, dict) or not isinstance(headers, dict):
            raise BrokerError("invalid_response", operation)
        if operation in SUCCESS_CODES and body.get("rsp_cd") not in SUCCESS_CODES[operation]:
            # 블록 모양이 정상이어도 미확인·누락 코드는 성공으로 보지 않는다(오류 응답의 빈 블록을 빈 잔고로 오인 방지).
            # 공식 오류표로 의미를 확인하기 전에는 거절·권한·한도로 단정하지 않고 수집 보류로 둔다.
            raise BrokerError("unconfirmed_response", operation, provider_code(body), rejection_hints(body.get("rsp_msg")))
        return body, headers

    def _pages(self, operation, payload, token):
        continuation, seen = "", set()
        for _ in range(100):
            body, headers = self._call(operation, payload, token, continuation)
            yield body
            flag = headers.get("tr_cont", "N")
            if flag in {"N", ""}:
                return
            if flag != "Y":
                raise BrokerError("invalid_response", operation)
            continuation = headers.get("tr_cont_key", "")
            if (not isinstance(continuation, str) or not continuation or len(continuation) > 18
                    or not continuation.isascii() or any(ord(c) < 32 for c in continuation)
                    or continuation in seen):
                raise BrokerError("pagination_error", operation)
            seen.add(continuation)
        raise BrokerError("pagination_error", operation)

    def token(self, credentials):
        auth, _ = self._call("token", {"grant_type": "client_credentials", "appkey": credentials.app_key,
                                       "appsecretkey": credentials.app_secret, "scope": "oob"})
        token = auth.get("access_token")
        if (not isinstance(token, str) or not token or len(token) > 4096 or
                not token.isascii() or any(c.isspace() or ord(c) < 33 for c in token)
                or str(auth.get("token_type", "")).lower() != "bearer"):
            raise BrokerError("authentication_failed", "token", provider_code(auth))
        return token

    def stock_profile(self, token, symbol):
        """g3104 종목 분류. 두 거래소 모두 OutBlock이 없으면 None(해당 종목 없음)."""
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise BrokerError("invalid_request", "g3104")
        for exchange in PROFILE_EXCHANGES:
            body, _ = self._call("g3104", {"g3104InBlock": {"delaygb": "R", "keysymbol": exchange + symbol,
                                                            "exchcd": exchange, "symbol": symbol}}, token)
            # 거래소가 틀리면 성공 코드와 "해당종목이 없습니다." 문구만 오고 OutBlock이 없다(실응답 확인).
            block = body.get("g3104OutBlock")
            if block is None or block == {}:
                continue
            if (not isinstance(block, dict) or not isinstance(block.get("symbol"), str)
                    or block["symbol"].strip() != symbol or str(block.get("exchcd", "")).strip() != exchange):
                raise BrokerError("invalid_response", "g3104", provider_code(body))
            instrument = display_name(block.get("instname"))
            kind = INSTRUMENT_KINDS.get(instrument, "other")
            industry = display_name(block.get("induname"))
            leverage = None
            if kind == "etf" and industry:
                match = LEVERAGE.fullmatch(industry)
                leverage = format(Decimal(match.group(1)), "f") if match else None
            return {"symbol": symbol, "exchcd": exchange, "exchange_name": display_name(block.get("exchange_name")),
                    "name": display_name(block.get("korname")), "kind": kind, "leverage": leverage,
                    "industry": industry if kind == "stock" else None}
        return None

    def daily_closes(self, token, symbol, exchange, start, end):
        """g3204 일별 수정종가 [(date, 십진 문자열)] 날짜 오름차순. 한 번만 조회한다(qrycnt 60이면 약 3개월)."""
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol) or exchange not in PROFILE_EXCHANGES:
            raise BrokerError("invalid_request", "g3204")
        # 연속 조회 신호가 와도 따라가지 않는다. 요청 기간이 qrycnt보다 짧아 한 쪽이면 충분하다.
        body, _ = self._call("g3204", {"g3204InBlock": {
            "sujung": "Y", "delaygb": "R", "keysymbol": exchange + symbol, "exchcd": exchange, "symbol": symbol,
            "gubun": "2", "qrycnt": 60, "comp_yn": "N", "sdate": start, "edate": end,
            "cts_date": "", "cts_info": ""}}, token)
        result, previous = [], None
        for row in rows(body, "g3204OutBlock1", "g3204"):
            raw_date = row.get("date")
            if not isinstance(raw_date, str) or not re.fullmatch(r"\d{8}", raw_date):
                raise BrokerError("invalid_response", "g3204")
            try:
                day = datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                raise BrokerError("invalid_response", "g3204") from None
            close = amount(row.get("close"), "g3204")
            if (previous is not None and day <= previous) or Decimal(close) <= 0:
                raise BrokerError("invalid_response", "g3204")
            previous = day
            result.append((day, close))
        return result

    def fetch(self, credentials):
        started = datetime.now(timezone.utc).isoformat()
        token = self.token(credentials)
        codes = {}
        base_date = self._today()
        positions, seen = [], set()
        # 공식 예제 본문 그대로. BaseDt는 필수이며 예제가 날짜라 당일(KST)을 넣는다.
        holdings = {"COSOQ00201InBlock1": {"RecCnt": 1, "BaseDt": base_date, "CrcyCode": "ALL",
                                           "AstkBalTpCode": "00"}}
        provider_date = None
        for page in self._pages("COSOQ00201", holdings, token):
            codes["COSOQ00201"] = provider_code(page)
            # OutBlock1에는 계좌번호·비밀번호가 있다. 응답 기준일자(형식 검증된 8자리)만 꺼낸다.
            # 공식 예제는 요청 20250217에 응답 20250428을 돌려줘 서버가 기준일을 바꾸는지 실응답으로 확인한다.
            echoed = page.get("COSOQ00201OutBlock1", {}).get("BaseDt") if isinstance(page.get("COSOQ00201OutBlock1"), dict) else None
            provider_date = echoed if isinstance(echoed, str) and re.fullmatch(r"20\d{6}", echoed) else provider_date
            for row in rows(page, "COSOQ00201OutBlock4", "COSOQ00201"):
                symbol = row.get("ShtnIsuNo")
                if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.:/\-]{1,32}", symbol):
                    raise BrokerError("invalid_response", "COSOQ00201")
                code = currency(row.get("CrcyCode"), "COSOQ00201")
                kind = row.get("AstkBalTpCode")
                if not isinstance(kind, str) or not re.fullmatch(r"\d{2}", kind):
                    raise BrokerError("invalid_response", "COSOQ00201")
                key = (symbol, code, kind)
                if key in seen:
                    raise BrokerError("duplicate_position", "COSOQ00201")
                seen.add(key)
                cost = amount(row.get("FcstckUprc"), "COSOQ00201")
                if Decimal(cost) < 0:
                    raise BrokerError("invalid_response", "COSOQ00201")
                market = row.get("MktTpNm")
                isin = row.get("IsuNo")
                positions.append({
                    "symbol": symbol, "isin": isin if isinstance(isin, str) and re.fullmatch(r"[A-Z0-9]{12}", isin) else None,
                    "exchange": market if isinstance(market, str) and re.fullmatch(r"[A-Za-z ]{1,20}", market) else None,
                    "currency": code, "balance_type": kind,
                    "quantity": amount(row.get("AstkBalQty"), "COSOQ00201"),
                    "settled_quantity": amount(row.get("AstkSettQty"), "COSOQ00201"),
                    "average_cost": cost,
                    "name": display_name(row.get("JpnMktHanglIsuNm")),
                    "price": optional_amount(row.get("OvrsScrtsCurpri"), "COSOQ00201"),
                    "market_value": optional_amount(row.get("FcurrEvalAmt"), "COSOQ00201"),
                    "mapping": "pending", "source": "COSOQ00201"})
        cash, seen_cash, krw, estimates, deposits = [], set(), None, [], {}
        for page in self._pages("COSOQ02701", {"COSOQ02701InBlock1": {"RecCnt": 1, "CrcyCode": "ALL"}}, token):
            codes["COSOQ02701"] = provider_code(page)
            block = page.get("COSOQ02701OutBlock4")
            if not isinstance(block, dict):
                error = BrokerError("invalid_response", "COSOQ02701", provider_code(page))
                error.diagnostic.update(block="COSOQ02701OutBlock4", shape=type(block).__name__ if block is not None else "missing")
                raise error
            value = amount(block.get("WonDpsBalAmt"), "COSOQ02701")
            if krw is not None and krw != value:
                raise BrokerError("inconsistent_cash", "COSOQ02701")
            krw = value
            block2 = page.get("COSOQ02701OutBlock2")
            estimates.extend([block2] if isinstance(block2, dict) else block2 if isinstance(block2, list) else [None])
            for row in rows(page, "COSOQ02701OutBlock3", "COSOQ02701"):
                code = currency(row.get("CrcyCode"), "COSOQ02701")
                if code in seen_cash or code == "KRW":
                    raise BrokerError("duplicate_cash", "COSOQ02701")
                seen_cash.add(code)
                deposits[code] = amount(row.get("FcurrDps"), "COSOQ02701")
                # 외화예수금·T4외화예수금·외화주문가능금액은 포함 관계가 미확인이라 서로 더하지 않는다.
                for field in ("FcurrDps", "T4FcurrDps", "FcurrOrdAbleAmt"):
                    cash.append({"currency": code, "amount": amount(row.get(field), "COSOQ02701"),
                                 "basis": "unverified", "source_field": field})
        cash.insert(0, {"currency": "KRW", "amount": krw, "basis": "unverified", "source_field": "WonDpsBalAmt"})
        settled, settled_issue = settled_cash(estimates, deposits, krw)
        return {"broker": "ls", "started_at": started,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "provider_as_of": None, "base_date": base_date, "provider_base_date": provider_date,
                "provider_codes": codes,
                "positions": positions, "cash": cash,
                "settled_cash": settled, "settled_cash_issue": settled_issue,
                "pages_complete": True, "reconciled": False, "valuation_ready": False,
                "note": "순차 조회 결과입니다. 비중 계산은 추정외화예수금4(settled_cash)를 씁니다. 아래 현금 필드 간 포함 관계·매입단가 수수료 포함 여부는 미확인입니다."}
