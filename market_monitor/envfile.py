"""로컬 .env에서 증권사 키 쌍과 시장 지표 공급원 키(FRED·ECOS)만 읽는다. 셸 실행·변수 확장 없이 데이터로만 해석하며 값은 어떤 메시지에도 넣지 않는다.

형식 정책
- UTF-8(BOM 허용). 빈 줄과 `#`로 시작하는 줄은 건너뛴다.
- `NAME=VALUE` 한 줄에 하나. `export ` 접두어·줄 끝 주석·이스케이프·`$VAR` 확장은 지원하지 않는다(값의 `#`, `$`도 그대로 값이다).
- 값 앞뒤 공백은 지운다. 같은 따옴표(' 또는 ")로 감싼 값은 따옴표만 벗긴다.
- 빈 값은 미설정으로 본다. 한 증권사의 키와 시크릿 중 하나만 있으면 실패한다.
- 아래 여섯 이름 외의 변수는 무시하고 이름도 출력하지 않는다. 여섯 이름의 중복 정의는 실패한다.
- FRED_API_KEY·ECOS_API_KEY는 영문·숫자만 허용하며 증권사 저장소가 아닌 별도 사전에 담는다.
"""
import re
from pathlib import Path


PAIRS = {"kiwoom": ("KIWOOM_APP_KEY", "KIWOOM_APP_SECRET"), "ls": ("LS_APP_KEY", "LS_APP_SECRET")}
DATA_NAMES = ("FRED_API_KEY", "ECOS_API_KEY")
NAMES = frozenset([*(name for pair in PAIRS.values() for name in pair), *DATA_NAMES])
MAX_BYTES = 64 * 1024


class EnvFileError(Exception):
    """메시지에는 줄 번호·변수 이름만 들어간다."""


def parse(text):
    values, lines = {}, {}
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, value = stripped.partition("=")
        name = name.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise EnvFileError(f"{number}번째 줄은 NAME=VALUE 형식이 아닙니다.")
        if name not in NAMES:
            continue
        if name in values:
            raise EnvFileError(f"{name}이(가) {lines[name]}번째 줄과 {number}번째 줄에 중복 정의되었습니다.")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            if len(value) < 2 or value[-1] != value[0]:
                raise EnvFileError(f"{number}번째 줄({name})의 따옴표가 닫히지 않았습니다.")
            value = value[1:-1]
        values[name], lines[name] = value, number
    return values


def read(path):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise EnvFileError(f"{path.name} 파일을 읽을 수 없습니다.") from None
    if len(raw) > MAX_BYTES:
        raise EnvFileError(f"{path.name} 파일이 너무 큽니다.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise EnvFileError(f"{path.name} 파일은 UTF-8이어야 합니다.") from None
    return parse(text)


def load(store, path, data_keys=None):
    """키 쌍이 모두 채워진 증권사만 store에 넣고 그 이름 목록을 돌려준다. data_keys가 있으면 채워진 지표 키를 넣는다."""
    values = read(path)
    loaded = []
    for name in DATA_NAMES:
        value = values.get(name, "")
        if value and not re.fullmatch(r"[A-Za-z0-9]{8,64}", value):
            raise EnvFileError(f"{name} 값은 영문·숫자 8~64자여야 합니다.")
        if value and data_keys is not None:
            data_keys[name] = value
    for broker, (key_name, secret_name) in PAIRS.items():
        key, secret = values.get(key_name, ""), values.get(secret_name, "")
        if not key and not secret:
            continue
        if not key or not secret:
            missing = key_name if not key else secret_name
            raise EnvFileError(f"{broker}: {missing} 값이 비어 있습니다. 키와 시크릿을 함께 입력하거나 둘 다 비워 두세요.")
        if key == secret:
            # 실조회 2026-10-31: 같은 값을 두 칸에 넣어 LS 토큰이 IGW00105로 거절됐다. 인증 호출 전에 막는다.
            raise EnvFileError(f"{broker}: {key_name}·{secret_name} 두 칸에 같은 값이 들어 있습니다. 시크릿 칸에 발급된 Secret Key를 넣어 주세요.")
        try:
            store.configure(broker, key, secret)
        except ValueError:
            raise EnvFileError(f"{broker}: {key_name}/{secret_name} 값의 길이·공백·문자 형식을 확인해 주세요.") from None
        loaded.append(broker)
    return loaded
