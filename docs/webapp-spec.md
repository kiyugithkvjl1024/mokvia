# mokvia Web/API仕様

## 境界

Python標準ライブラリのThreadingHTTPServer、vanilla JavaScript/CSS/HTML、Markdownファイルを使います。SQLite、Node runtime、外部AI APIは不要です。Linux専用のファイルロック・安全なrenameを保持するためWindowsではDocker Linuxコンテナ内で動かします。

ホスト公開は `127.0.0.1:24873` のみ。コンテナ内は `0.0.0.0:24873` ですがComposeでloopbackへ限定します。Hostは `localhost:24873` または `127.0.0.1:24873` のみ。変更APIは同じ2つのHTTP Origin、`X-Mokvia-Web: 1`、JSON Content-Typeを検査します。ログイン機能はありません。同じPCを利用できるユーザーからのアクセスを防ぐものではないため、会社PCのアクセス制御を前提とします。LAN・公開ホスティング・proxyへの露出は対象外です。

## 画面とAPI

Capture/Inbox、Clarify、Focus、Task検索、Project、Direction、Roadmap/Cycle、時間配分、Progress、Reviewに既存UIを使用します。`/calendar` はJSTの日・週・月表示を追加し、予定と実績を区別します。月は月曜始まり42日、週は7日、日は1日です。

- `GET /api/v1/snapshot`：検証された現在の記録。
- `GET /api/v1/entities/{kind}/{id}`：個別entityと最新content_hash。
- `POST /api/v1/mutations/preview`：対象、最新base_hash、変更項目から検証・diff・preview_hashを得る。
- `POST /api/v1/mutations/apply`：明示確認したpreviewとpreview_hashを一度だけ適用。
- Task作業操作も同じmutation preview/apply経路で複数Taskへの整合した変更を扱う。
- `GET /api/v1/calendar?view=day|week|month&date=YYYY-MM-DD`：予定・実績の読取投影。
- `GET /api/v1/local-notifications`：最新通知とWindows helper heartbeat状態。
- `POST /api/v1/local-notifications/heartbeat`：同一Originで空JSONを送る。Taskは変更しない。
- `GET /api/v1/health`：distribution=`mokvia`、mode=`local` を確認。

詳しい既存APIのpayloadとエラー契約は `webapp/api.py` と `tests/test_webapp_api.py` が正本です。Copilotはコード・合成テストを読み、まず最新detail、次にpreview、ユーザーの確認後にapply、最後にdetailを読戻します。stale hash、validation、recovery、結果不明に対し推測した再送を行いません。

## 保存と復旧

正本は `/data` named volumeです。`base_hash`、同一プロセス内ロック、Linux flock、ファイルパス・symlink・重複ID検査、atomic publication、mutation recovery markerを保持します。runtime lockで二重起動や実行中のbackup/restoreを拒否します。会社データを配布Gitへ置きません。

バックアップは停止後に正本Markdownとmutation状態だけをtarへ出します。復元は一時領域でarchiveのパス・種類・サイズ・スキーマを検証し、現データの安全コピーと中断markerを作成してから入れ替えます。復元中断markerがある場合は自動起動を拒否します。Windowsからの操作は `windows/` のスクリプトを使用します。

外部カレンダー識別フィールドは互換スキーマに残りますが、この配布は新規空データから開始し、バックグラウンド同期・OAuth経路を持ちません。明示設定した会社PCのヘルパーだけが手動要求でOutlook COMまたはMicrosoft Graphを読み取り、共通差分処理へ渡します。PWA/service worker、外部画像、認証情報は含めません。

### ローカルCalendarとメッセージcapture

汎用Task createの明示IDは従来の `task-calendar-<24 hex>` に加え、`task-capture-<32 hex>` を許可する。新しいfrontmatter項目は追加しない。共通importerは初回受付日時・source identityから重複を防ぎ、既存preview/apply経由でInboxまたはnext/action_dateを作成する。schema versionと元メッセージmetadataは本文先頭の `mokvia-capture-v1` コメント、元リンクは本文Markdownに記録する。本文プレビューではこの機械metadataコメントのみを非表示にし、通常の本文とリンクを表示する。本体serverは明示した専用folderがある場合だけ取り込み、会社版も同じimporterを使う。ローカルCalendarは既存Taskの対応予定日・時刻付き予定・実績・締切を表示し、既存APIで予定を操作する。Google取得・同期は起動せず、既存個人Google同期・固定host/rootは維持する。OneDrive・Power Automate・Windows設定だけを会社adapterへ分離する。


### Manual dependency-blocked Next planning

Every general Task update entry (Project board status action, detail-panel editor,
Focus list Edit, and direct Clarify) uses the same pre-preview warning. Cancel
sends no mutation preview/apply. OK explicitly sets `confirm_blocked_next: true`.
General editing may save title/body/estimate/dates at the same time, but the
Project and prerequisite IDs must remain unchanged; waiting_for and availability
are still gates. Preview binds prerequisite hashes/statuses and apply rejects
concurrent changes. Actual start remains blocked. Unrelated edits preserve an
already manually planned Next.

The fixed API CLI uses this same update contract. Unconfirmed blocked transitions
resolve to Waiting; a save receipt alone is not confirmation. Sibling moves cannot
change status or dependency links. The dedicated tree replacement API keeps its
root/child contracts and has no blanket bulk override. Automatic suggestions and
bulk planning must not synthesize the explicit confirmation flag.

Daily Review today's commitments include unfinished Tasks whose due, action_date,
or scheduled_start falls today, regardless of Next/Waiting/Scheduled status. Each
Task appears once even when several dates match. The entire list and estimated
minutes total remain visible after expanding the step, including more than five
items.

Optional real UI verification (installed Playwright only; no production data):
`GTD_E2E_PLAYWRIGHT_MODULE=/path/to/playwright python3 tests/run_next_planning_ui_e2e.py`.
The runner makes fresh synthetic repositories, isolated loopback servers and a new
headless profile, replays the v64 general editor failure, and verifies actual
buttons/modal controls, Cancel/OK, blocked start, stale dependency rejection and
nine-task Review totals. Evidence is written outside the repository.

### Reviewの共有作業画面とDaily Reviewの記録

Review手順から開くInbox・Task・Project/Goalは通常画面と同じsingleton DOM、handler、preview/applyを使う。通常の振分けや編集をパネル内で行い、ReviewのNotes・チェック・スクロール・下書きを保持する。パネル専用の種別切替は置かない。Close/Backは一階層ずつ戻り、Forwardも対象階層を復元する。保存中の終了抑止とパネル内の未保存確認は、背後のReview draftを破棄対象にしない。

Daily画面は変化の整理、直近の予定と次の行動の確認、任意の一言、確認状況の保存の順にする。Progressは任意の独立した実績記録であり、Task更新の代わりでも必須の転記先でもない。新規Daily Notesの初期値は空欄とし、確認チェックと対象日を既存Review本文に保存できる。定型の振返り文を記入済みとして自動保存しない。既存Review本文と日付別の下書きは変更・移行せず保持する。朝/夜のどちらでも使える案内とし、GTDの日次確認と週次の全リスト見直しを区別する。アプリ内マニュアルは通常版、2分版、朝/夜/変更なしの例、終了条件を示し、調査Pagesへリンクする。

Outlook manual import is opt-in and company-local. Graph uses only separately approved read-only Microsoft 365 access; classic Outlook uses local COM. Neither writes to Outlook. No OAuth/token setup is performed by this distribution. See docs/OUTLOOK-CALENDAR.md for deferred company acceptance.
