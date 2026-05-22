# Progress

## 2026-05-22

#### 1. `--limit` デフォルト上限 1000 件撤廃（無制限化）(v0.6.1)

**Issue**

- `--limit` を指定せずに実行すると、内部デフォルト値 1000 に達した時点でメッセージ取得が打ち切られ、「1000 messages found」と表示されるだけで残りのメッセージが取得されなかった。

**Changes**

- `--limit` のデフォルト値を `1000` から `None`（無制限）に変更。明示指定しない場合は `after_dt` 以降の全メッセージを取得する。
- `fetch_messages()` の `limit` 引数型を `int | None` に変更し、`None` の場合は件数チェックをスキップするよう修正。
- `--limit` のヘルプ文を `default: unlimited` に更新。

**Changed files**

- get_messages.py

## 2026-05-07

#### 1. メッセージ分割ファイル出力モード `--output-dir` 追加 (v0.6.0)

**Issue**

- Webex メッセージ量が多い Space を要約 AI（Circuit Headless）に投げるとコンテキスト長で処理がスタックする。
- 複数 Space の取得結果をクリップボード経由で受け渡していたため、並行運用するとクリップボードが上書きされてメッセージ取得からやり直しになっていた。

**Changes**

- `--output-dir <path>` を追加。指定するとメッセージを投稿（4 行ブロック）境界で分割し、`<yyyymmdd_HHmmss>_<space>__NNofMM.txt`（ゼロ埋め 2 桁固定）の連番テキストファイルを書き出す。各チャンクの 1 行目には `[Space名]` を必ず付与（プロンプト `01_Chatナレッジ収集` 等が要求する形式に合わせる）。最終チャンクのみ末尾に `--- N messages found ...` フッターを付ける。
- 取りこぼし検証用に分割前の全文を `<prefix>__full.txt` として併せて保存。
- `--max-chars <int>`（デフォルト 50000）を追加。1 チャンクの最大文字数。1 投稿単独で超過する場合は警告を stderr に出して単独チャンクとして出力（投稿途中での分割はしない）。
- `--output-dir` モードでは stdout には `M messages found, N chunks` の 1 行サマリのみを出し、クリップボードへのコピーは行わない（既存の `--output-dir` 未指定フローはそのまま）。
- `--output-dir` + `--format json` の併用は `EXIT_ARG_ERROR` で拒否。
- `<output_dir>` 配下の `*.txt` のうち、ファイル名 prefix `yyyymmdd_HHmmss` が 7 日より古いものを実行開始時に自動削除（mtime 非依存で予測可能）。
- 共通ヘルパー `_format_message_block_lines()`、新関数 `format_text_chunks()`、`_sanitize_filename()`（AHK 側 `SanitizeFileName` と同等ルール）、`_cleanup_old_output_files()` を追加。`format_text_output()` は内部で `_format_message_block_lines()` を呼ぶ形に整理し、出力結果は従来と同一。
- `messages/` ディレクトリを `.gitignore` に追加（生成物。7 日経過で自動削除）。

**Changed files**

- get_messages.py
- .gitignore

## 2026-04-13

#### 1. `--after` 指定時に古い日付のメッセージが欠落する問題の修正 (v0.5.2)

**Issue**

- `-a 2026-04-01` のように日付を指定しても、実在する 04-01〜04-04 のメッセージが取得できず、04-05 以降しか取れないケースがあった。
- 原因は Webex API `messages.list` が基本は `created` 降順で返すものの、特定のスレッド親メッセージが本来の時系列順を逸脱した位置に単発で挿入されるケースがあること。従来は「最初に `created < after_dt` なメッセージを見たら即 `break`」していたため、この例外的な 1 件で早期終了し、後続の該当メッセージを取り逃していた。

**Changes**

- `fetch_messages()` を単発の順序逸脱に耐えるロジックに変更。`created < after_dt` でも即 `break` せず `continue` でスキップし、`OLD_RUN_CUTOFF` (=50) 件連続で古いメッセージが続いたときに初めて真の終端と判断して打ち切る。新しいメッセージが現れた時点でカウンタはリセット。
- SDK の自動ページング（ドキュメント通り動作することを確認済み）はそのまま利用。

**Changed files**

- get_messages.py

## 2026-03-24

#### 1. roomId 正規化による messages.list 404 問題の修正 (v0.5.1)

**Issue**

- UUID から生成した roomId（`ciscospark://us/ROOM/...`）と、API が返す正規の roomId（`ciscospark://urn:TEAM:us-west-2_r/ROOM/...`）のリージョン部分が異なる Space が存在した。`rooms.get()` は内部で名寄せされるため成功するが、`messages.list()` は正確な roomId が必要で 404 を返していた。

**Changes**

- `validate_room()` の戻り値を `str`（タイトル）から `tuple[str, str]`（正規 roomId, タイトル）に変更。`room.id` を取得して後続 API 呼び出しに使用するようにした。
- `--room-id` 直接指定時に `space_name` が `None` だった問題を修正。`room_title` をフォールバックとして設定し、テキスト出力に Space 名ヘッダーが常に表示されるようにした。
- verbose モードで正規 roomId が入力と異なる場合にログ出力を追加。

**Changed files**

- get_messages.py

## 2026/03/13

#### 1. Content-Disposition ヘッダーによるファイル名解決 (v0.5.0)

**Issue**

- Webex のファイル添付URLは `https://webexapis.com/v1/contents/<content-id>` 形式のため、URLパスからファイル名を抽出できず、すべて `(file)` と表示されていた。

**Changes**

- HEADリクエストで `Content-Disposition` ヘッダーからファイル名を取得するロジックを実装。`filename*=UTF-8''...` (RFC 5987) を優先し、`filename="..."` にフォールバック。
- `ThreadPoolExecutor(max_workers=25)` による並列HEADリクエストでパフォーマンスを確保。
- URLエンコードされた日本語ファイル名を `unquote_plus` で正しくデコード（`+` → スペース対応）。
- HEADリクエスト失敗時はURLパスからのフォールバックを維持。

**Changed files**

- get_messages.py

#### 2. 添付ファイル名表示 (v0.4.0)

**Changes**

- 添付ファイル付きメッセージでファイル名を表示する機能を追加。URLパスからファイル名を抽出し、テキスト出力では `[Files: filename.png]` として表示。
- 本文なし＋ファイルありの場合は `[Files: ...]` を本文として表示。本文あり＋ファイルありの場合は本文の次行に表示。
- JSON出力に `filenames` フィールドを追加（既存の `files` (生URL) は後方互換のため維持）。
- ファイル名が取れないURL（Webex contents API URLなど拡張子なし）は `(file)` にフォールバック。

**Changed files**

- get_messages.py
- docs/progress.md

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
