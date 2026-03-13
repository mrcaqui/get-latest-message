"""Webex Room メッセージ取得CLIツール。

指定日時以降のメッセージを取得し、stdout に出力する。
結果はクリップボードにもコピーされる（--no-copy で無効化可能）。
"""

import argparse
import base64
import concurrent.futures
import json
import posixpath
import re
import subprocess
import sys
import threading
from urllib.parse import unquote, unquote_plus, urlparse
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from dotenv import load_dotenv
from webexpythonsdk import WebexAPI
from webexpythonsdk.exceptions import ApiError

import webex_auth

# Windows console の文字化け対策
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- Exit codes ---
EXIT_OK = 0
EXIT_ARG_ERROR = 2
EXIT_ENV_ERROR = 3
EXIT_ROOM_ERROR = 4
EXIT_API_ERROR = 5

# --- UUID / Room info patterns ---
SPACE_ID_PATTERN = re.compile(
    r"Space\s*ID\s*[:：]\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
SPACE_URI_PATTERN = re.compile(
    r"space=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
UUID_PATTERN = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
SPACE_NAME_PATTERN = re.compile(
    r"Space\s*name\s*[:：]\s*(.+)",
    re.IGNORECASE,
)

LOCAL_TZ = ZoneInfo("Asia/Tokyo")


# ======================================================================
# Room ID helpers
# ======================================================================

def encode_room_id(room_uuid: str, region: str = "us") -> str:
    """UUID から Webex API の roomId を生成する。"""
    u = _uuid.UUID(room_uuid)
    plain = f"ciscospark://{region}/ROOM/{str(u).upper()}"
    return base64.urlsafe_b64encode(plain.encode("utf-8")).decode("ascii").rstrip("=")


def extract_space_name(text: str) -> str | None:
    """Space 情報テキストから Space name を抽出する。"""
    match = SPACE_NAME_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return None


def extract_uuid_from_room_info(text: str) -> str:
    """Space 情報テキストから UUID を抽出する。"""
    # 1. Space ID: 行から抽出
    match = SPACE_ID_PATTERN.search(text)
    if match:
        return match.group(1).strip()

    # 2. Space URI の space=<uuid> から抽出
    match = SPACE_URI_PATTERN.search(text)
    if match:
        return match.group(1).strip()

    # 3. テキスト全体から UUID 候補を列挙。1件のみなら採用
    candidates = list(set(UUID_PATTERN.findall(text)))
    if len(candidates) == 1:
        return candidates[0]
    elif len(candidates) > 1:
        raise ValueError(
            "Could not determine a unique Space ID. Multiple UUIDs found in the text."
        )

    raise ValueError("Could not find a Space ID in the provided text.")


def resolve_room_id_direct(room_id_str: str, region: str) -> str:
    """--room-id の値を解釈して API 用 roomId を返す。"""
    # UUID 形式か判定
    try:
        u = _uuid.UUID(room_id_str)
        return encode_room_id(str(u), region=region)
    except ValueError:
        pass

    # エンコード済み ID 判定（URL-safe Base64 + 一定の長さ）
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", room_id_str):
        return room_id_str

    raise ValueError(
        f"Invalid --room-id value: '{room_id_str}'. "
        "Provide a UUID (e.g. 041ff480-acf9-...) or an encoded room ID."
    )


def resolve_room_id(args, verbose: bool) -> tuple[str, str | None]:
    """CLI 引数から API 用 roomId を解決する。Space name も返す。"""
    if args.room_id:
        room_id = resolve_room_id_direct(args.room_id, region=args.region)
        if verbose:
            print(f"[verbose] Room ID resolved from --room-id: {room_id}", file=sys.stderr)
        return (room_id, None)

    # room-info / room-info-file からテキストを取得
    if args.room_info_file:
        try:
            text = Path(args.room_info_file).read_text(encoding="utf-8")
        except Exception as e:
            print(f"Error: Failed to read room-info-file: {e}", file=sys.stderr)
            sys.exit(EXIT_ARG_ERROR)
    else:
        text = args.room_info

    space_name = extract_space_name(text)
    if verbose and space_name:
        print(f"[verbose] Space name: {space_name}", file=sys.stderr)

    room_uuid = extract_uuid_from_room_info(text)
    if verbose:
        print(f"[verbose] Extracted UUID: {room_uuid}", file=sys.stderr)

    room_id = encode_room_id(room_uuid, region=args.region)
    if verbose:
        print(f"[verbose] Encoded roomId: {room_id}", file=sys.stderr)
    return (room_id, space_name)


# ======================================================================
# Datetime helpers
# ======================================================================

def parse_after_datetime(after_str: str) -> datetime:
    """--after の値をパースし、UTC の aware datetime を返す。"""
    # 日付のみ (YYYY-MM-DD)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", after_str):
        dt = datetime.strptime(after_str, "%Y-%m-%d")
        dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt.astimezone(timezone.utc)

    # ISO8601 パース試行
    try:
        dt = datetime.fromisoformat(after_str)
    except ValueError:
        raise ValueError(
            f"Invalid --after value: '{after_str}'. Use YYYY-MM-DD or ISO8601 datetime."
        )

    # ナイーブならローカルタイムとして解釈
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)

    return dt.astimezone(timezone.utc)


def format_utc_iso(dt: datetime) -> str:
    """UTC datetime を Webex API 形式の ISO8601 文字列に変換する。"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ======================================================================
# API helpers
# ======================================================================

def _parse_created(value) -> datetime:
    """msg.created を aware datetime に変換する（str / datetime 両対応）。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    # 文字列の場合
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _created_to_isostr(value) -> str:
    """msg.created を ISO8601 文字列に変換する（JSON出力用）。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def validate_room(api: WebexAPI, room_id: str, verbose: bool) -> str:
    """Room の存在とアクセス権を事前確認し、Room タイトルを返す。"""
    try:
        room = api.rooms.get(room_id)
        if verbose:
            print(f"[verbose] Room validated: {room.title}", file=sys.stderr)
        return room.title
    except ApiError as e:
        status = e.status_code if hasattr(e, "status_code") else None
        if status == 401:
            msg = "Access token is invalid or expired. Check WEBEX_ACCESS_TOKEN."
        elif status == 403:
            msg = "No permission to access this room. Check that the token has access."
        elif status == 404:
            msg = (
                "Room not found. The Space ID may be invalid, or the inferred roomId "
                "format may be incorrect. Try --room-id with the API room ID directly."
            )
        else:
            msg = f"API error ({status}): {e}"
        print(f"Error: {msg}", file=sys.stderr)
        sys.exit(EXIT_ROOM_ERROR)


def fetch_messages(
    api: WebexAPI,
    room_id: str,
    after_dt: datetime,
    limit: int,
    verbose: bool,
) -> list:
    """指定日時以降のメッセージを取得し、時系列順のリストで返す。"""
    collected = []
    count = 0
    for msg in api.messages.list(roomId=room_id, max=200):
        created = _parse_created(msg.created)
        if created < after_dt:
            break
        collected.append(msg)
        count += 1
        if count >= limit:
            if verbose:
                print(f"[verbose] Reached limit ({limit}), stopping.", file=sys.stderr)
            break

    if verbose:
        print(f"[verbose] Fetched {len(collected)} messages.", file=sys.stderr)

    collected.reverse()  # 時系列順に
    return collected


def resolve_names(api: WebexAPI, messages: list, verbose: bool) -> dict:
    """personId → displayName のマッピングを構築する。"""
    cache: dict[str, str | None] = {}
    for msg in messages:
        pid = msg.personId
        if pid in cache:
            continue
        try:
            person = api.people.get(pid)
            cache[pid] = person.displayName
        except Exception:
            # personEmail フォールバック
            email = getattr(msg, "personEmail", None)
            cache[pid] = email  # None の場合もキャッシュ

    if verbose:
        resolved = sum(1 for v in cache.values() if v is not None)
        print(
            f"[verbose] Name resolution: {resolved}/{len(cache)} resolved.",
            file=sys.stderr,
        )
    return cache


def get_sender_name(msg, name_cache: dict) -> str:
    """メッセージの送信者名を返す。"""
    pid = msg.personId
    cached = name_cache.get(pid)
    if cached:
        return cached
    email = getattr(msg, "personEmail", None)
    if email:
        return email
    if pid:
        return pid
    return "Unknown Sender"


# ======================================================================
# Output
# ======================================================================

def _resolve_filename_from_url(url: str, auth_headers: dict, verbose: bool) -> str | None:
    """HEAD リクエストで Content-Disposition からファイル名を取得する。"""
    try:
        resp = requests.head(url, headers=auth_headers, timeout=10, allow_redirects=True)
        resp.raise_for_status()
        cd = resp.headers.get("Content-Disposition", "")
        if not cd:
            return None
        # filename*=UTF-8''<encoded> を優先
        m = re.search(r"filename\*\s*=\s*UTF-8''(.+?)(?:;|$)", cd, re.IGNORECASE)
        if m:
            return unquote_plus(m.group(1).strip())
        # filename="<name>" フォールバック
        m = re.search(r'filename\s*=\s*"([^"]+)"', cd)
        if m:
            return unquote_plus(m.group(1).strip())
        # filename=<name> (引用符なし)
        m = re.search(r'filename\s*=\s*([^;\s]+)', cd)
        if m:
            return unquote_plus(m.group(1).strip())
        return None
    except requests.exceptions.RequestException as e:
        if verbose:
            print(f"[verbose] HEAD request failed for {url}: {e}", file=sys.stderr)
        return None


def _fallback_filename_from_url(url: str) -> str:
    """URL パスからファイル名を抽出するフォールバック。"""
    path = urlparse(url).path
    name = posixpath.basename(unquote(path))
    # クエリパラメータ除去
    name = name.split("?")[0]
    if not name or "." not in name:
        return "(file)"
    return name


def resolve_filenames_batch(
    messages: list, auth_headers: dict, verbose: bool,
) -> dict[str, list[str]]:
    """全メッセージの添付ファイル名を並列 HEAD リクエストで一括解決する。"""
    # URL → メッセージID のマッピング（重複排除）
    url_to_msg_ids: dict[str, list[str]] = {}
    for msg in messages:
        files = getattr(msg, "files", None) or []
        for url in files:
            url_to_msg_ids.setdefault(url, []).append(msg.id)

    if not url_to_msg_ids:
        return {}

    url_to_name: dict[str, str] = {}
    sem = threading.Semaphore(25)
    head_success = 0
    lock = threading.Lock()

    def _resolve(url: str) -> None:
        nonlocal head_success
        with sem:
            name = _resolve_filename_from_url(url, auth_headers, verbose)
        if name:
            with lock:
                head_success += 1
            url_to_name[url] = name
        else:
            url_to_name[url] = _fallback_filename_from_url(url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=25) as executor:
        futures = [executor.submit(_resolve, url) for url in url_to_msg_ids]
        concurrent.futures.wait(futures)

    if verbose:
        total = len(url_to_msg_ids)
        print(
            f"[verbose] File name resolution: {head_success}/{total} resolved via HEAD.",
            file=sys.stderr,
        )

    # メッセージ単位の結果を構築
    result: dict[str, list[str]] = {}
    for msg in messages:
        files = getattr(msg, "files", None) or []
        if files:
            result[msg.id] = [url_to_name.get(url, "(file)") for url in files]
    return result


def format_text_output(
    messages: list, name_cache: dict, room_id: str, after_dt: datetime,
    space_name: str | None = None,
    filename_cache: dict[str, list[str]] | None = None,
) -> str:
    """text 形式の出力文字列を生成する。"""
    after_utc_str = format_utc_iso(after_dt)
    after_local = after_dt.astimezone(LOCAL_TZ)
    after_local_str = after_local.strftime("%Y-%m-%d %H:%M %Z")

    lines = []
    if space_name:
        lines.append(f"[{space_name}]")
    for msg in messages:
        created = _parse_created(msg.created)
        local_created = created.astimezone(LOCAL_TZ)
        time_str = local_created.strftime("%Y-%m-%d %H:%M:%S %z")
        # %z は +0900 形式なので +09:00 に変換
        time_str = time_str[:-2] + ":" + time_str[-2:]

        sender = get_sender_name(msg, name_cache)

        body = getattr(msg, "text", None) or getattr(msg, "markdown", None)

        # 添付ファイル名
        files = getattr(msg, "files", None) or []
        if filename_cache is not None:
            filenames = filename_cache.get(msg.id, [])
        else:
            filenames = [_fallback_filename_from_url(u) for u in files]

        if not body:
            if filenames:
                body = f"[Files: {', '.join(filenames)}]"
            else:
                body = "[Attachment only]"
            filenames = []  # bodyに含めたので重複表示しない

        lines.append(f"{time_str} [{sender}]")
        lines.append(body)
        if filenames:
            lines.append(f"[Files: {', '.join(filenames)}]")
        lines.append("")

    # フッター
    if messages:
        lines.append("---")
        lines.append(
            f"{len(messages)} messages found with created >= {after_utc_str} "
            f"(= {after_local_str})"
        )
    else:
        lines.append(
            f"0 messages found with created >= {after_utc_str} "
            f"(= {after_local_str})"
        )

    return "\n".join(lines)


def format_json_output(
    messages: list, name_cache: dict, room_id: str, after_dt: datetime,
    filename_cache: dict[str, list[str]] | None = None,
) -> str:
    """JSON 形式の出力文字列を生成する。"""
    after_utc_str = format_utc_iso(after_dt)

    msg_list = []
    for msg in messages:
        files = getattr(msg, "files", None) or []
        if filename_cache is not None:
            filenames = filename_cache.get(msg.id, [])
        else:
            filenames = [_fallback_filename_from_url(u) for u in files]
        entry = {
            "id": msg.id,
            "created": _created_to_isostr(msg.created),
            "personId": msg.personId,
            "personEmail": getattr(msg, "personEmail", None),
            "displayName": get_sender_name(msg, name_cache),
            "text": getattr(msg, "text", None),
            "markdown": getattr(msg, "markdown", None),
            "files": files,
            "filenames": filenames,
        }
        msg_list.append(entry)

    result = {
        "roomId": room_id,
        "after_utc": after_utc_str,
        "count": len(msg_list),
        "messages": msg_list,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def copy_to_clipboard(text: str) -> None:
    """クリップボードにテキストをコピー（ベストエフォート）。"""
    try:
        subprocess.run(
            ["clip"],
            input=text.encode("utf-16-le"),
            check=True,
        )
    except Exception as e:
        print(f"Warning: Failed to copy to clipboard: {e}", file=sys.stderr)


# ======================================================================
# CLI
# ======================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Webex room messages since a given datetime."
    )

    room_group = parser.add_mutually_exclusive_group(required=False)
    room_group.add_argument(
        "--room-info", "-ri",
        help="Space info text (for dev/debug; use --room-info-file for production).",
    )
    room_group.add_argument(
        "--room-info-file", "-rif",
        help="Path to a file containing Space info text (recommended for AHK).",
    )
    room_group.add_argument(
        "--room-id", "-r",
        help="Room ID directly (UUID or encoded ID).",
    )

    parser.add_argument(
        "--after", "-a",
        help="Fetch messages created on or after this datetime (YYYY-MM-DD or ISO8601).",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Authenticate with Webex via OAuth (opens browser).",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=1000,
        help="Maximum number of messages to fetch (default: 1000).",
    )
    parser.add_argument(
        "--region",
        default="us",
        help="Webex region for roomId encoding (default: us).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print verbose logs to stderr.",
    )
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="Disable clipboard copy.",
    )

    return parser.parse_args()


def validate_environment() -> str:
    """WEBEX_ACCESS_TOKEN を検証して返す。"""
    import os

    token = os.environ.get("WEBEX_ACCESS_TOKEN")
    if not token:
        print(
            "Error: WEBEX_ACCESS_TOKEN is not set. "
            "Set it in .env or as an environment variable.",
            file=sys.stderr,
        )
        sys.exit(EXIT_ENV_ERROR)
    return token


def main() -> None:
    # .env をスクリプト配置ディレクトリから読み込み
    load_dotenv(Path(__file__).parent / ".env")

    args = parse_args()
    verbose = args.verbose

    # --- Auth mode ---
    if args.auth:
        webex_auth.run_oauth_flow(verbose)
        return

    # --- Normal mode: 必須引数の検証 ---
    if not (args.room_info or args.room_info_file or args.room_id):
        print("Error: --room-info, --room-info-file, --room-id のいずれかが必要です。", file=sys.stderr)
        sys.exit(EXIT_ARG_ERROR)
    if not args.after:
        print("Error: --after は必須です。", file=sys.stderr)
        sys.exit(EXIT_ARG_ERROR)

    # --- トークン解決 ---
    token = webex_auth.resolve_access_token(verbose)

    # Room ID 解決
    try:
        room_id, space_name = resolve_room_id(args, verbose)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_ROOM_ERROR)

    # --after パース
    try:
        after_dt = parse_after_datetime(args.after)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(EXIT_ARG_ERROR)

    if verbose:
        print(f"[verbose] after (UTC): {format_utc_iso(after_dt)}", file=sys.stderr)

    # API 初期化
    try:
        api = WebexAPI(access_token=token)
    except Exception as e:
        print(f"Error: Failed to initialize Webex API: {e}", file=sys.stderr)
        sys.exit(EXIT_API_ERROR)

    # Room 検証
    validate_room(api, room_id, verbose)

    # メッセージ取得
    try:
        messages = fetch_messages(api, room_id, after_dt, args.limit, verbose)
    except ApiError as e:
        status = e.status_code if hasattr(e, "status_code") else None
        if status == 404:
            if verbose:
                print("[verbose] 404 on message fetch, treating as 0 messages.", file=sys.stderr)
            messages = []
        else:
            print(f"Error: Failed to fetch messages: {e}", file=sys.stderr)
            sys.exit(EXIT_API_ERROR)

    # 投稿者名解決
    name_cache = resolve_names(api, messages, verbose)

    # 添付ファイル名解決
    auth_headers = dict(api._session.headers)
    filename_cache = resolve_filenames_batch(messages, auth_headers, verbose)

    # 出力生成
    if args.format == "json":
        result = format_json_output(messages, name_cache, room_id, after_dt, filename_cache=filename_cache)
    else:
        result = format_text_output(messages, name_cache, room_id, after_dt, space_name=space_name, filename_cache=filename_cache)

    # stdout 出力（先に実行）
    print(result)

    # クリップボードコピー
    if not args.no_copy:
        copy_to_clipboard(result)
        if verbose:
            print("[verbose] Result copied to clipboard.", file=sys.stderr)


if __name__ == "__main__":
    main()
