# Progress

## 2026/03/10

#### 1. Webex Room メッセージ取得CLIツール 初期実装 (v0.1.0)

**Changes**

- 指定日時以降のWebex Roomメッセージを一括取得するCLIツールを実装。
- Space情報テキストからUUID抽出 → API用roomIdへのBase64エンコード変換に対応。
- `--room-info` / `--room-info-file` / `--room-id` の3種類のRoom指定方式（排他）。
- `--after` で日付のみ（JST midnight→UTC変換）/ ISO8601完全形式に対応。
- メッセージを新しい順にイテレートし、指定日時以降のものを収集して時系列順に出力。
- `people.get()` による投稿者名解決（キャッシュ付き、失敗時はemail/personIdフォールバック）。
- text / JSON 出力形式、クリップボードコピー（UTF-16LE）。
- exit code分類（0:成功 / 2:引数エラー / 3:環境変数不足 / 4:Room解決失敗 / 5:API失敗）。
- `--verbose` で詳細ログをstderrに出力。
- Windows Python 3.13 の `zoneinfo` 対応で `tzdata` を依存に追加。

**Changed files**

- get_messages.py
- requirements.txt
- .env.example
- .gitignore

#### 2. Space name ヘッダー追加 & 404 時の出力改善 (v0.2.0)

**Changes**

- テキスト出力の冒頭に `[Space name]` ヘッダーを追加。`--room-info` / `--room-info-file` 使用時に room info テキストから Space name を抽出して表示する。`--room-id` 直接指定時は表示なし。
- メッセージ取得 API で 404 エラーが返った場合、エラー終了せず `0 messages found ...` として正常出力するように変更。
- 空メッセージ時のフッターテキストを `"No messages found"` → `"0 messages found"` に統一。

**Changed files**

- get_messages.py
- .gitignore

#### 3. Webex OAuth 認証の実装 (v0.3.0)

**Changes**

- Webex OAuth 2.0 (Authorization Code Grant) による認証フローを `webex_auth.py` として新規実装。
- トークンの取得・保存(`.webex_tokens.json`)・自動リフレッシュに対応。Personal Access Token の12時間制限から、OAuth Access Token 14日間 + Refresh Token 90日間に延長。
- トークン未取得・期限切れ時は自動でブラウザ認証を起動し、認証後そのまま元の処理を継続。AHK経由でも再実行不要。
- `--auth` フラグで明示的な認証実行にも対応。環境変数 `WEBEX_ACCESS_TOKEN` による後方互換フォールバックを維持。

**Changed files**

- webex_auth.py (新規)
- get_messages.py
- .env.example
- .gitignore
- requirements.txt

#### 4. 添付ファイル名表示 (v0.4.0)

**Changes**

- 添付ファイル付きメッセージでファイル名を表示する機能を追加。URLパスからファイル名を抽出し、テキスト出力では `[Files: filename.png]` として表示。
- 本文なし＋ファイルありの場合は `[Files: ...]` を本文として表示。本文あり＋ファイルありの場合は本文の次行に表示。
- JSON出力に `filenames` フィールドを追加（既存の `files` (生URL) は後方互換のため維持）。
- ファイル名が取れないURL（Webex contents API URLなど拡張子なし）は `(file)` にフォールバック。

**Changed files**

- get_messages.py
- docs/progress.md
