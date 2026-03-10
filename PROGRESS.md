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
