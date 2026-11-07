import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .credentials import CredentialStore
from .brokers.ls import LSClient, LSHTTP
from .brokers.sync import BrokerSync
from .market.indicators import MarketIndicators
from .market.sectors import SectorData, holdings_breakdown
from .portfolio import holdings


WEB_ROOT = Path(__file__).parent / "web"
MAX_BODY = 16_384


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def script_policy_hash(html):
    # 브라우저는 HTML 파싱 시 CRLF·CR을 LF로 바꾼 스크립트 텍스트로 해시를 검사한다.
    script = re.search(rb"<script>(.*?)</script>", html, re.S).group(1)
    script = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return base64.b64encode(hashlib.sha256(script).digest()).decode()


def initial_status(credentials=None, market=None, force=False, sectors=None):
    status = {
        "checked_at": utc_now(),
        "brokers": {
            name: {"state": "not_configured", "stage": "documentation",
                   "message": "API 신청·키 발급 상태 확인이 필요합니다."}
            for name in ("kiwoom", "ls")
        },
        "research": {
            "bigdata": {"state": "cost_unverified", "message": "MCP 조회도 토큰을 소비합니다. 계정 과금 조건 확인 전 호출을 보류합니다."},
            "quartr": {"state": "access_unverified", "message": "플러그인 이용 권한·비용·실행 경로 확인이 필요합니다."},
        },
        "market": {
            "fear_greed": {"state": "unavailable", "message": "CNN 자동 수집 경로 미확인"},
            "vix": {"state": "not_configured", "message": "FRED_API_KEY 설정 전"},
            "sectors": {"state": "unverified",
                        "message": "보유 종목 섹터 비중과 섹터 ETF 대용 흐름은 구현 예정입니다. 전체 거래소 섹터 수익률은 제공하지 않습니다."},
            "usd_krw": {"state": "not_configured", "message": "ECOS_API_KEY 설정 전"},
        },
    }
    if market is not None:
        status["market"].update(market.snapshot(force=force))
    if sectors is not None:
        status["market"]["sectors"] = sectors.status_item()
    if credentials:
        for broker, item in status["brokers"].items():
            if credentials.is_configured(broker):
                item.update(state="configured", stage="configuration",
                            message="키를 이 프로세스의 메모리에 입력했습니다. 실제 인증·조회 결과는 아래 잔고 조회 영역에서 확인하세요.")
    return status


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "MarketMonitor"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format, *args):
        pass

    def send_content(self, code, body, content_type="application/json; charset=utf-8", policy=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", policy or "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, code, data):
        self.send_content(code, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def permitted(self, api=False, mutation=False):
        port = self.server.server_address[1]
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.json_response(403, {"error": "로컬 주소로 접속해 주세요."})
            return False
        origin = self.headers.get("Origin")
        if (origin is not None and origin != f"http://{host}") or (mutation and not origin):
            self.json_response(403, {"error": "다른 출처의 요청을 허용하지 않습니다."})
            return False
        if api and self.headers.get("X-Market-Monitor") != "local":
            self.json_response(403, {"error": "앱에서 요청해 주세요."})
            return False
        return True

    def do_GET(self):
        if not self.permitted(api=self.path.startswith("/api/")):
            return
        if self.path == "/api/status":
            self.json_response(200, initial_status(self.server.credentials, self.server.market, sectors=self.server.sectors))
        elif self.path == "/api/portfolio":
            self.json_response(200, holdings.build(self.broker_statuses(), self.server.market.snapshot().get("usd_krw"),
                                                   self.server.sectors.profiles()))
        elif self.path == "/api/sectors":
            self.json_response(200, self.sectors_payload())
        elif self.path in {"/api/brokers/kiwoom", "/api/brokers/ls"}:
            self.json_response(200, self.server.broker_syncs[self.path.rsplit("/", 1)[1]].status())
        elif self.path == "/":
            body = (WEB_ROOT / "index.html").read_bytes()
            digest = script_policy_hash(body)
            policy = (f"default-src 'none'; script-src 'sha256-{digest}'; "
                      "style-src 'unsafe-inline'; connect-src 'self'; "
                      "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_content(200, body, "text/html; charset=utf-8", policy)
        else:
            self.json_response(404, {"error": "없는 경로입니다."})

    def broker_statuses(self):
        return {b: sync.status() for b, sync in self.server.broker_syncs.items()}

    def sectors_payload(self):
        profiles = self.server.sectors.profiles()
        portfolio = holdings.build(self.broker_statuses(), self.server.market.snapshot().get("usd_krw"), profiles)
        return {"holdings": holdings_breakdown(portfolio, profiles),
                "holdings_complete": portfolio["complete"], "holdings_basis": portfolio["basis"],
                **self.server.sectors.snapshot()}

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            self.json_response(400, {"error": "지원하지 않는 전송 방식입니다."})
            return None
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.json_response(413 if length > MAX_BODY else 411, {"error": "요청 크기를 확인해 주세요."})
            return None
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.json_response(415, {"error": "JSON 형식이 필요합니다."})
            return None
        try:
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeError):
            self.json_response(400, {"error": "올바른 JSON 객체가 필요합니다."})
            return None

    def do_POST(self):
        if not self.permitted(api=True, mutation=True):
            return
        data = self.read_json()
        if data is None:
            return
        if self.path == "/api/refresh" and not data:
            self.json_response(200, initial_status(self.server.credentials, self.server.market, force=True,
                                                   sectors=self.server.sectors))
        elif self.path in {"/api/brokers/kiwoom/refresh", "/api/brokers/ls/refresh"} and not data:
            sync = self.server.broker_syncs[self.path.split("/")[3]]
            result = sync.refresh()
            code = {"busy": 409, "not_configured": 409, "rate_limited": 429,
                    "failed": 502}.get(result, 200)
            self.json_response(code, {"result": result, **sync.status()})
        elif self.path == "/api/sectors/refresh" and not data:
            symbols = {position["symbol"] for status in self.broker_statuses().values()
                       for position in (status.get("snapshot") or {}).get("positions", [])}
            result = self.server.sectors.refresh(symbols)
            code = {"busy": 409, "not_configured": 409, "rate_limited": 429, "failed": 502}.get(result, 200)
            self.json_response(code, {"result": result, **self.sectors_payload()})
        else:
            self.json_response(404, {"error": "지원하지 않는 작업입니다."})


def create_server(host="127.0.0.1", port=8765, credentials=None, data_keys=None, market=None, sectors=None):
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("로컬 접속만 지원합니다.")
    server = LocalServer((host, port), Handler)
    server.credentials = credentials if credentials is not None else CredentialStore()
    server.market = market or MarketIndicators(data_keys or {})
    # LS 잔고와 섹터 조회가 한 전송 계층을 공유해 호출 간격 제한을 함께 지킨다.
    ls_transport = LSHTTP()
    server.broker_syncs = {"kiwoom": BrokerSync(server.credentials, broker="kiwoom"),
                           "ls": BrokerSync(server.credentials, LSClient(ls_transport), broker="ls")}
    server.sectors = sectors or SectorData(server.credentials, LSClient(ls_transport))
    return server
