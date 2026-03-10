"""Webex OAuth 2.0 認証モジュール。

Authorization Code Grant フローによるトークンの取得・保存・自動リフレッシュを提供する。
"""

import http.server
import json
import os
import secrets
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

# --- Exit codes (get_messages.py と共通) ---
EXIT_ENV_ERROR = 3

# --- 定数 ---
TOKEN_ENDPOINT = "https://webexapis.com/v1/access_token"
AUTHORIZE_URL = "https://webexapis.com/v1/authorize"
REDIRECT_PORTS = [8888, 8889, 8890]
SCOPES = "spark:messages_read spark:rooms_read spark:people_read"
TOKEN_FILE_NAME = ".webex_tokens.json"
CALLBACK_TIMEOUT = 120  # seconds


def _get_token_file_path() -> Path:
    """トークンファイルのパスを返す（スクリプトと同じディレクトリ）。"""
    return Path(__file__).parent / TOKEN_FILE_NAME


def _get_oauth_credentials() -> tuple[str, str]:
    """環境変数から WEBEX_CLIENT_ID, WEBEX_CLIENT_SECRET を取得する。"""
    client_id = os.environ.get("WEBEX_CLIENT_ID")
    client_secret = os.environ.get("WEBEX_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Error: WEBEX_CLIENT_ID と WEBEX_CLIENT_SECRET を .env に設定してください。\n"
            "Webex Developer Portal で Integration を作成し、発行された値を設定します。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ENV_ERROR)
    return client_id, client_secret


# ======================================================================
# OAuth Callback Server
# ======================================================================

class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """OAuth コールバックを受信する一時的な HTTP リクエストハンドラ。"""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        self.server.callback_params = params

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = (
            "<html><body>"
            "<h2>認証成功</h2>"
            "<p>このタブを閉じてください。</p>"
            "</body></html>"
        )
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        """サーバーログを抑制する。"""
        pass


def _wait_for_callback(port: int) -> dict:
    """ローカルサーバーを起動し、OAuth コールバックを1件受信して返す。"""
    server = http.server.HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    server.timeout = CALLBACK_TIMEOUT
    server.callback_params = None
    server.handle_request()

    if server.callback_params is None:
        print(
            "Error: コールバックがタイムアウトしました "
            f"({CALLBACK_TIMEOUT}秒以内にブラウザで認証してください)。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ENV_ERROR)

    return server.callback_params


# ======================================================================
# Token persistence
# ======================================================================

def _save_tokens(token_data: dict) -> None:
    """トークン情報を .webex_tokens.json に保存する。"""
    now = datetime.now(timezone.utc)

    data = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "access_token_expires_at": (
            now + timedelta(seconds=token_data["expires_in"])
        ).isoformat(),
        "refresh_token_expires_at": (
            now + timedelta(seconds=token_data["refresh_token_expires_in"])
        ).isoformat(),
        "obtained_at": now.isoformat(),
    }

    path = _get_token_file_path()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_tokens() -> dict | None:
    """トークンファイルを読み込む。存在しない/不正な場合は None を返す。"""
    path = _get_token_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # 必須キーの存在確認
        for key in ("access_token", "refresh_token", "access_token_expires_at", "refresh_token_expires_at"):
            if key not in data:
                return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _is_expired(expires_at_str: str, buffer_seconds: int = 300) -> bool:
    """トークンが期限切れ（バッファ付き）かどうかを判定する。"""
    expires_at = datetime.fromisoformat(expires_at_str)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires_at - timedelta(seconds=buffer_seconds)


# ======================================================================
# Token refresh
# ======================================================================

def _refresh_tokens(client_id: str, client_secret: str, refresh_token: str, verbose: bool) -> dict:
    """Refresh Token を使って新しいトークンペアを取得する。

    失敗時は requests.RequestException を送出する（呼び出し元でフォールバック可能）。
    """
    if verbose:
        print("[verbose] Access token expired, refreshing...", file=sys.stderr)

    response = requests.post(TOKEN_ENDPOINT, data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    })
    response.raise_for_status()

    token_data = response.json()
    _save_tokens(token_data)

    if verbose:
        print("[verbose] Token refreshed successfully.", file=sys.stderr)

    return token_data


# ======================================================================
# OAuth flow
# ======================================================================

def run_oauth_flow(verbose: bool) -> None:
    """ブラウザベースの OAuth フローを実行し、トークンを保存する。"""
    client_id, client_secret = _get_oauth_credentials()
    state = secrets.token_urlsafe(32)

    # ポートを試行
    port = None
    server_error = None
    for candidate_port in REDIRECT_PORTS:
        try:
            # ポートが使えるかテスト
            test_server = http.server.HTTPServer(("127.0.0.1", candidate_port), _OAuthCallbackHandler)
            test_server.server_close()
            port = candidate_port
            break
        except OSError as e:
            server_error = e
            if verbose:
                print(f"[verbose] Port {candidate_port} is in use, trying next...", file=sys.stderr)

    if port is None:
        print(
            f"Error: コールバック用ポート {REDIRECT_PORTS} がすべて使用中です。\n"
            "他のアプリケーションを終了してから再試行してください。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ENV_ERROR)

    redirect_uri = f"http://localhost:{port}/callback"

    auth_url = (
        f"{AUTHORIZE_URL}"
        f"?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={SCOPES}"
        f"&state={state}"
    )

    if verbose:
        print(f"[verbose] Using callback port: {port}", file=sys.stderr)

    # ブラウザを開く
    print("ブラウザで Webex 認証ページを開いています...", file=sys.stderr)
    if not webbrowser.open(auth_url):
        print(
            "ブラウザを自動で開けませんでした。以下の URL を手動で開いてください:\n"
            f"{auth_url}",
            file=sys.stderr,
        )

    # コールバック待機
    params = _wait_for_callback(port)

    # state 検証
    returned_state = params.get("state", [None])[0]
    if returned_state != state:
        print(
            "Error: state パラメータが一致しません（CSRF攻撃の可能性）。\n"
            "再度 `--auth` を実行してください。",
            file=sys.stderr,
        )
        sys.exit(EXIT_ENV_ERROR)

    # エラーチェック
    if "error" in params:
        error = params["error"][0]
        desc = params.get("error_description", [""])[0]
        print(f"Error: 認証に失敗しました: {error} - {desc}", file=sys.stderr)
        sys.exit(EXIT_ENV_ERROR)

    auth_code = params.get("code", [None])[0]
    if not auth_code:
        print("Error: 認可コードを取得できませんでした。", file=sys.stderr)
        sys.exit(EXIT_ENV_ERROR)

    # トークン交換
    if verbose:
        print("[verbose] Exchanging authorization code for tokens...", file=sys.stderr)

    try:
        response = requests.post(TOKEN_ENDPOINT, data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "redirect_uri": redirect_uri,
        })
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error: トークンの取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(EXIT_ENV_ERROR)

    token_data = response.json()
    _save_tokens(token_data)

    print("認証成功! トークンを保存しました。", file=sys.stderr)
    if verbose:
        expires_in_days = token_data.get("expires_in", 0) // 86400
        refresh_days = token_data.get("refresh_token_expires_in", 0) // 86400
        print(
            f"[verbose] Access token expires in {expires_in_days} days, "
            f"refresh token expires in {refresh_days} days.",
            file=sys.stderr,
        )


# ======================================================================
# Token resolution (main entry point)
# ======================================================================

def _auto_authenticate(verbose: bool, reason: str) -> str:
    """OAuth フローを自動実行し、新しい access_token を返す。"""
    print(f"{reason} 自動で認証を開始します...", file=sys.stderr)
    run_oauth_flow(verbose)
    tokens = _load_tokens()
    if tokens is None:
        print("Error: 認証後のトークン読み込みに失敗しました。", file=sys.stderr)
        sys.exit(EXIT_ENV_ERROR)
    print("認証完了。処理を続行します。", file=sys.stderr)
    return tokens["access_token"]


def resolve_access_token(verbose: bool) -> str:
    """有効な access_token を返す。必要に応じて自動で OAuth フローを実行する。"""
    # 1. 環境変数フォールバック（後方互換）
    env_token = os.environ.get("WEBEX_ACCESS_TOKEN")
    if env_token:
        if verbose:
            print("[verbose] Using WEBEX_ACCESS_TOKEN from environment.", file=sys.stderr)
        return env_token

    # 2. トークンファイルから読み込み
    tokens = _load_tokens()
    if tokens is None:
        return _auto_authenticate(verbose, "トークンがありません。")

    # 2a. access_token が有効期限内
    if not _is_expired(tokens["access_token_expires_at"]):
        if verbose:
            print("[verbose] Using valid access token from token file.", file=sys.stderr)
        return tokens["access_token"]

    # 2b. refresh_token が有効 → リフレッシュ
    if not _is_expired(tokens["refresh_token_expires_at"]):
        client_id, client_secret = _get_oauth_credentials()
        try:
            new_tokens = _refresh_tokens(client_id, client_secret, tokens["refresh_token"], verbose)
            return new_tokens["access_token"]
        except requests.RequestException as e:
            if verbose:
                print(f"[verbose] Refresh failed: {e}", file=sys.stderr)
            return _auto_authenticate(verbose, "トークンのリフレッシュに失敗しました。")

    # 2c. 両方期限切れ → 自動再認証
    return _auto_authenticate(verbose, "認証が切れています。")
