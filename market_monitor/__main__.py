import argparse
import getpass
import sys
from pathlib import Path

from . import envfile
from .credentials import CredentialStore

from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="Market Monitor 로컬 대시보드")
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--configure", choices=["kiwoom", "ls"], action="append", default=[],
                        help="키를 숨김 입력합니다. 같은 증권사의 .env 값보다 우선합니다.")
    env_group = parser.add_mutually_exclusive_group()
    env_group.add_argument("--env-file", type=Path,
                           help="키 쌍을 읽을 .env 경로. 생략하면 현재 작업 폴더의 .env가 있을 때만 읽습니다.")
    env_group.add_argument("--no-env-file", action="store_true", help=".env를 읽지 않습니다(키 없는 검증용).")
    args = parser.parse_args()
    credentials = CredentialStore()
    data_keys = {}
    if args.configure and not sys.stdin.isatty():
        parser.error("키 입력은 사용자가 직접 연 대화형 터미널에서 실행해 주세요.")
    env_path = None if args.no_env_file else args.env_file or Path.cwd() / ".env"
    if env_path is not None and (args.env_file or env_path.is_file()):
        try:
            loaded = envfile.load(credentials, env_path, data_keys)
        except envfile.EnvFileError as error:
            credentials.clear()
            parser.exit(1, f".env 오류: {error}\n")
        print(f"env: {env_path.name}에서 키 쌍을 읽은 증권사: {', '.join(loaded) or '없음'}", flush=True)
        print(f"env: 시장 지표 키: {', '.join(sorted(data_keys)) or '없음'}", flush=True)
    try:
        # 숨김 입력은 .env 다음에 적용되어 같은 증권사의 파일 값을 덮어쓴다.
        for broker in dict.fromkeys(args.configure):
            print(f"{broker}: 키는 메모리에만 보관합니다. 조회는 화면의 잔고 조회 버튼을 눌렀을 때만 실행합니다.", flush=True)
            credentials.configure(broker, getpass.getpass("App Key: "), getpass.getpass("App Secret: "))
    except (ValueError, EOFError, KeyboardInterrupt):
        credentials.clear()
        parser.exit(1, "키 입력이 취소되었거나 형식이 올바르지 않습니다.\n")
    server = create_server(args.host, args.port, credentials=credentials, data_keys=data_keys)
    print(f"Market Monitor: http://{args.host}:{server.server_address[1]}", flush=True)
    print("Stop: Ctrl+C. Broker queries run only on request. Keys are held in this process memory while running.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        credentials.clear()
        data_keys.clear()


if __name__ == "__main__":
    main()
