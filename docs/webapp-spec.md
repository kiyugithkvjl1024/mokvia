# mokvia Web/API仕様

## 境界

Python標準ライブラリのThreadingHTTPServer、vanilla JavaScript/CSS/HTML、Markdownファイルを使います。SQLite、Node runtime、外部AI APIは不要です。Linux専用のファイルロック・安全なrenameを保持するためWindowsではDocker Linuxコンテナ内で動かします。

ホスト公開は `127.0.0.1:24873` のみ。コンテナ内は `0.0.0.0:24873` ですがComposeでloopbackへ限定します。Hostは `localhost:24873` または `127.0.0.1:24873` のみ。変更APIは同じ2つのHTTP Origin、`X-GTD-Web: 1`、JSON Content-Typeを検査します。ログイン機能はありません。同じPCを利用できるユーザーからのアクセスを防ぐものではないため、会社PCのアクセス制御を前提とします。LAN・公開ホスティング・proxyへの露出は対象外です。

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
- `GET /api/v1/health`：distribution=`gtd-local`、mode=`local` を確認。

詳しい既存APIのpayloadとエラー契約は `webapp/api.py` と `tests/test_webapp_api.py` が正本です。Copilotはコード・合成テストを読み、まず最新detail、次にpreview、ユーザーの確認後にapply、最後にdetailを読戻します。stale hash、validation、recovery、結果不明に対し推測した再送を行いません。

## 保存と復旧

正本は `/data` named volumeです。`base_hash`、同一プロセス内ロック、Linux flock、ファイルパス・symlink・重複ID検査、atomic publication、mutation recovery markerを保持します。runtime lockで二重起動や実行中のbackup/restoreを拒否します。会社データを配布Gitへ置きません。

バックアップは停止後に正本Markdownとmutation状態だけをtarへ出します。復元は一時領域でarchiveのパス・種類・サイズ・スキーマを検証し、現データの安全コピーと中断markerを作成してから入れ替えます。復元中断markerがある場合は自動起動を拒否します。Windowsからの操作は `windows/` のスクリプトを使用します。

外部カレンダー識別フィールドは互換スキーマに残りますが、この配布は新規空データから開始し、同期・OAuth・外部API経路を持ちません。PWA/service worker、外部画像、認証情報は含めません。
