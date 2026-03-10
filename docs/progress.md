# Progress

## 2026/03/10

#### 1. Space name ヘッダー追加 & 404 時の出力改善 (v0.2.0)

**Changes**

- テキスト出力の冒頭に `[Space name]` ヘッダーを追加。`--room-info` / `--room-info-file` 使用時に room info テキストから Space name を抽出して表示する。`--room-id` 直接指定時は表示なし。
- メッセージ取得 API で 404 エラーが返った場合、エラー終了せず `0 messages found ...` として正常出力するように変更。
- 空メッセージ時のフッターテキストを `"No messages found"` → `"0 messages found"` に統一。

**Changed files**

- get_messages.py
- .gitignore
