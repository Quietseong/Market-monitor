"""Kiwoom read-only US holdings. Raw responses and credentials are never logged."""
import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import require_currency


ERROR_CATALOG = json.loads(Path(__file__).with_name("kiwoom_errors.json").read_text(encoding="utf-8"))
PUBLIC_FIELDS = frozenset({"stex_tp", "stk_cd", "api-id", "authorization", "cont-yn", "next-key",
                           "appkey", "secretkey", "grant_type", "token"})


# 실패사유 원문 대신 내보내는 사전 정의 분류. 일치하지 않으면 unclassified만 표시한다.
REASON_CLASSES = {
    "required": r"필수|누락|미입력|(?i:null|empty|blank|required|missing)",
    "length": r"길이|자리|바이트|초과|미만|(?i:byte|length|size)",
    "allowed_chars": r"문자|숫자|영문|대문자|소문자|특수|공백|(?i:character|alpha|numeric|digit)",
    "format": r"형식|포맷|(?i:format|pattern)",
    "allowed_values": r"허용|유효하지|잘못된|범위|코드값|(?i:invalid|not allowed|range)",
    "dependency": r"함께|같이|동시|입력\s?시|경우|조합|(?i:together|combination|when)",
}


def mentioned_fields(text):
    return [field for field in sorted(PUBLIC_FIELDS)
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(field) + r"(?![A-Za-z0-9_])", text)]


def slot_diagnostic(template, message):
    """Classify the dynamic {?} slots in-process; raw slot text never leaves this function."""
    literals = template.split("{?}")
    if len(literals) < 2:
        return {}
    pattern = "(.*?)".join(re.escape(part) for part in literals[:-1]) + "(.*)" + re.escape(literals[-1])
    match = re.search(pattern, message, re.S)
    if not match:
        return {}
    result = {}
    for label, value in zip(literals, match.groups()):
        value = value.strip()
        if label.rstrip().endswith("파라미터="):
            result["parameter_fields"] = mentioned_fields(value)
        elif label.rstrip().endswith("실패사유="):
            classes = [name for name, regex in REASON_CLASSES.items() if re.search(regex, value)]
            result["reason"] = {"classes": classes, "fields": mentioned_fields(value),
                                "empty": not value, "unclassified": bool(value) and not classes}
    return result


def public_diagnostic(message):
    """Match official public templates; never return the provider's raw text."""
    if not isinstance(message, str):
        return {"details": [], "fields": []}
    message = message[:2000]
    details = []
    for item in ERROR_CATALOG:
        literals = item["template"].split("{?}")
        pattern = ".*?".join(re.escape(part) for part in literals)
        if re.search(pattern, message):
            details.append({"code": item["code"], "description": item["template"],
                            **slot_diagnostic(item["template"], message)})
    return {"details": details, "fields": mentioned_fields(message)}


class BrokerError(Exception):
    def __init__(self, kind, operation=None, provider_code=None, hints=None):
        self.kind = kind
        # LS 오류 코드 형식은 공식 확인 전이다. 숫자 또는 짧은 영문 접두어+숫자만 남기고 나머지는 버린다.
        self.diagnostic = {"operation": operation if operation in {"au10001", "ust21070", "ust21110", "ust21160",
                                                                   "token", "COSOQ00201", "COSOQ02701",
                                                                   "g3104", "g3204"} else None,
                           "provider_code": str(provider_code) if re.fullmatch(r"-?\d{1,6}|[A-Z]{1,4}\d{1,6}", str(provider_code)) else None,
                           "hints": hints or []}
        super().__init__(kind)


def rejection_hints(message):
    if not isinstance(message, str):
        return []
    patterns = {"no_data": r"(자료|내역|데이터|잔고).{0,12}(없|존재하지)",
                "ip": r"(?i)\bip\b|아이피", "token": r"토큰|(?i:token)",
                "account": r"계좌", "registration": r"신청|등록|약정|동의",
                "permission": r"권한|허용|승인", "input": r"입력|파라미터|필수",
                "limit": r"초과|제한", "service_time": r"시간|점검|중단"}
    return [key for key, pattern in patterns.items() if re.search(pattern, message[:2000])]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class KiwoomHTTP:
    def __init__(self):
        self._opener = build_opener(NoRedirect())
        self._lock = Lock()
        self._last = 0.0

    def __call__(self, operation, payload, token="", continuation=""):
        if operation not in {"au10001", "ust21070", "ust21110", "ust21160"}:
            raise BrokerError("operation_denied")
        path = "/oauth2/token" if operation == "au10001" else "/api/us/acnt"
        headers = {"Content-Type": "application/json;charset=UTF-8"}
        if operation != "au10001":
            headers.update({"authorization": "Bearer " + token, "api-id": operation,
                            "cont-yn": "Y" if continuation else "N", "next-key": continuation})
        with self._lock:
            time.sleep(max(0, 0.4 - (time.monotonic() - self._last)))
            self._last = time.monotonic()
            try:
                request = Request("https://api.kiwoom.com" + path,
                                  json.dumps(payload).encode(), headers, method="POST")
                with self._opener.open(request, timeout=15) as response:
                    raw = response.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise BrokerError("invalid_response")
                    body = json.loads(raw, parse_float=Decimal)
                    return body, {k.lower(): v for k, v in response.headers.items()}
            except HTTPError as error:
                kind = {401: "authentication_failed", 403: "permission_denied",
                        429: "rate_limited"}.get(error.code, "http_error")
                error.close()
                raise BrokerError(kind) from None
            except (URLError, TimeoutError, OSError):
                raise BrokerError("network_error") from None
            except (ValueError, UnicodeError):
                raise BrokerError("invalid_response") from None


def number(value):
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()):
        raise BrokerError("invalid_response")
    result = Decimal(value.strip())
    if not result.is_finite():
        raise BrokerError("invalid_response")
    return format(result, "f")


def optional_number(value):
    """시세·평가 필드는 비어 있으면 None(미평가)으로 두고, 형식이 틀리면 실패한다."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return number(value)


def settlement_date(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d{8}", value) else None


def settled_cash(body, krw, cash):
    """ust21160 결제일별 USD 예수금으로 결제 후 현금을 만든다. (settled_cash, 사유) 중 하나만 값이 있다.
    실조회로 확인한 관계(d0 = ust21110 USD fc_entra, d{i} = d{i-1} + 정산금액, 정산금액 = 매도 - 매수)가
    깨지면 마지막 결제일 값을 믿지 않는다."""
    try:
        deposits = [Decimal(number(body.get(f"d{i}_usd_fx_entr"))) for i in range(5)]
        for i in range(1, 5):
            net = Decimal(number(body.get(f"d{i}_usd_exct_amt")))
            if (deposits[i] != deposits[i - 1] + net
                    or net != Decimal(number(body.get(f"d{i}_usd_sell_excta"))) - Decimal(number(body.get(f"d{i}_usd_buy_excta")))):
                return None, "inconsistent_cash"
    except BrokerError:
        return None, "invalid_cash"
    current = next((Decimal(c["amount"]) for c in cash if c["currency"] == "USD"), Decimal(0))
    if deposits[0] != current:
        return None, "inconsistent_cash"
    result = [{"currency": "USD", "amount": format(deposits[4], "f"), "settles_on": settlement_date(body.get("d4_setl_dt")),
               "source": "ust21160", "source_field": "d4_usd_fx_entr"},
              {"currency": "KRW", "amount": krw, "settles_on": None, "source": "ust21110", "source_field": "krw_entra"}]
    result += [{"currency": c["currency"], "amount": c["amount"], "settles_on": None, "source": "ust21110",
                "source_field": "fc_entra"} for c in cash if c["currency"] not in {"USD", "KRW"}]
    return result, None


def display_name(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= 80 and value.isprintable() else None


def currency(value):
    try:
        require_currency(value)
    except ValueError:
        raise BrokerError("invalid_response") from None
    return value


class KiwoomClient:
    def __init__(self, transport=None):
        self._transport = transport or KiwoomHTTP()

    def _call(self, operation, payload, token="", continuation=""):
        body, headers = self._transport(operation, payload, token, continuation)
        if not isinstance(body, dict) or not isinstance(headers, dict):
            raise BrokerError("invalid_response")
        if "return_code" not in body:
            raise BrokerError("invalid_response")
        if str(body["return_code"]) != "0":
            error = BrokerError("authentication_failed" if operation == "au10001" else "provider_rejected",
                                operation, body["return_code"], rejection_hints(body.get("return_msg")))
            error.diagnostic.update(public_diagnostic(body.get("return_msg")))
            raise error
        return body, headers

    def _pages(self, operation, payload, token):
        continuation, seen = "", set()
        for _ in range(100):
            body, headers = self._call(operation, payload, token, continuation)
            if not isinstance(body.get("result_list"), list):
                raise BrokerError("invalid_response")
            yield body
            flag = headers.get("cont-yn", "N")
            if flag in {"N", ""}:
                return
            if flag != "Y":
                raise BrokerError("invalid_response")
            continuation = headers.get("next-key", "")
            if (not isinstance(continuation, str) or not continuation or len(continuation) > 50
                    or not continuation.isascii() or any(ord(c) < 32 for c in continuation)
                    or continuation in seen):
                raise BrokerError("pagination_error")
            seen.add(continuation)
        raise BrokerError("pagination_error")

    def fetch(self, credentials):
        started = datetime.now(timezone.utc).isoformat()
        auth, _ = self._call("au10001", {"grant_type": "client_credentials",
                            "appkey": credentials.app_key, "secretkey": credentials.app_secret})
        token = auth.get("token")
        if (not isinstance(token, str) or not token or len(token) > 4096 or
                not token.isascii() or any(c.isspace() or ord(c) < 33 for c in token)
                or str(auth.get("token_type", "")).lower() != "bearer"):
            raise BrokerError("invalid_response")
        positions, cash, seen = [], [], set()
        # 실조회: stex_tp=ND에 stk_cd 빈값·생략 모두 1517(파라미터 stk_cd, 허용 값)로 거절됐다.
        # 공식 요청 예제 그대로 전체 조회한다. 응답 행에는 거래소 구분이 없어 exchange는 미제공으로 둔다.
        for page in self._pages("ust21070", {"stex_tp": "", "stk_cd": ""}, token):
            for row in page["result_list"]:
                if not isinstance(row, dict):
                    raise BrokerError("invalid_response")
                symbol = row.get("stk_cd")
                if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.:/\-]{1,32}", symbol):
                    raise BrokerError("invalid_response")
                code = currency(row.get("crnc_code"))
                key = (symbol, code)
                if key in seen:
                    raise BrokerError("duplicate_position")
                seen.add(key)
                cost = number(row.get("frgn_stk_book_uv"))
                if Decimal(cost) < 0:
                    raise BrokerError("invalid_response")
                positions.append({"symbol": symbol, "exchange": None, "currency": code,
                                  "quantity": number(row.get("poss_qty")),
                                  "settled_quantity": number(row.get("qty")), "average_cost": cost,
                                  "name": display_name(row.get("frgn_stk_nm")),
                                  "price": optional_number(row.get("now_pric")),
                                  "market_value": optional_number(row.get("evlt_amt")),
                                  "mapping": "pending", "source": "ust21070"})
        seen_cash, krw = set(), None
        for page in self._pages("ust21110", {}, token):
            value = number(page.get("krw_entra"))
            if krw is not None and krw != value:
                raise BrokerError("inconsistent_cash")
            krw = value
            for row in page["result_list"]:
                if not isinstance(row, dict):
                    raise BrokerError("invalid_response")
                code = currency(row.get("crnc_code"))
                if code in seen_cash or code == "KRW":
                    raise BrokerError("duplicate_cash")
                seen_cash.add(code)
                cash.append({"currency": code, "amount": number(row.get("fc_entra")),
                             "basis": "unverified", "source_field": "fc_entra"})
        settlement, _ = self._call("ust21160", {}, token)
        settled, settled_issue = settled_cash(settlement, krw, cash)
        cash.insert(0, {"currency": "KRW", "amount": krw, "basis": "unverified",
                        "source_field": "krw_entra"})
        return {"broker": "kiwoom", "started_at": started,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "provider_as_of": None, "positions": positions, "cash": cash,
                "settled_cash": settled, "settled_cash_issue": settled_issue,
                "pages_complete": True, "reconciled": False, "valuation_ready": False,
                "note": "순차 조회 결과입니다. 비중 계산은 결제 후 예수금(settled_cash)을 씁니다. 동일 시점 잔고 대조·매입단가 수수료 포함 여부는 미확인입니다."}
