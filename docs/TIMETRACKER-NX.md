# TimeTracker NX 確定実績反映（本番HTTP transport・合成検証済み）

本番HTTPS transportを同梱しています。実APIへの疎通・会社認証はまだ行っていません。非秘密設定と承認済み資格情報の注入がない初期状態は未接続です。任意URLへ資格情報を送る処理、認証生成、OAuth開始、予定の自動送信はありません。全連携を既定ONにする共通登録表へ載せても、登録・有効化・factory生成は認証や同期を開始しません。

## 操作

1. コアで終了時刻を確定し、完了した実績を選択する。Taskと外部予定由来実績は共通 `ActualRecord` を使用する。未完了・計測中・予定だけのレコードは送れない。
2. 実績参照IDに対応するタスクURLと、NXで確認したワークアイテムIDを登録する。URL形式は未確定なのでURLからIDを推測しない。作業分類・工程分類IDは社内設定に合わせて指定する。
3. 「選択した確定実績をプレビュー」で本人ID、開始終了、タスクID、新規・変更・変更なし・競合を確認する。
4. 「プレビューの実績を反映」で複数件を明示的に反映する。全選択の事前検査で一件でも反映不可なら開始しない。途中の拒否・結果不明では後続を止め、完了済みを巻き戻さない。各結果を確認する。
5. 結果不明の行は「送信せず照合」で本人対象日の全実績を再取得する。一致一件だけなら対応付けを保存。一致なし・複数候補は保留し、再POSTしない。照合で確認できない状態を消して再送してはいけない。

変更がない実績は送信しない。変更は対応済みNX IDにPUTする。NX側の値または `updatedAt` が前回から変わった・削除された場合は競合。タスク変更は削除再作成しない。分類の解除もNX版の挙動未検証なので停止する。削除機能はない。

## 所有・接続契約

- 固有プラグイン：`webapp/integration_plugins/timetracker_nx.py`、内部adapter：`webapp/timetracker_nx.py`。
- 共通契約v1：`webapp/integrations/contracts.py` の `ActualRecord` / `ActualWriteRequest` / `ActualWriteReceipt`。Outlookの `local.actual`、Task Markdown、provider固有原本には依存しない。
- `reference_id` は不変。開始終了修正で変わらない。Taskは `task:<ID>`、外部実績は `external:<stable key>`。NX ID・前回payload・`updatedAt`・未確定intentは、コアから渡したprivate journalに保存する。
- `describe(context)` / `build_plugin(context)` は共通lazy registry契約に適合する。IDは `timetracker_nx`、capabilitiesは `actual.read` / `actual.write`。factoryは専用 `NXHTTPTransport` または `synthetic_only=True` の合成transportだけを受け入れる。settingsは `api_base / timezone / granularity / required_categories`。資格情報項目はない。
- journal既定場所：`.local-state/integrations/timetracker-nx.json`。バックアップ・復元は対応表とunknown intentを一緒に保全する。journalを削除して再接続すると重複登録の危険がある。
- `preview_actuals(records)` / `apply_actuals(records, token)` / `reconcile(reference_id)` が明示操作用。`write_actual(request)` は共通単件writer契約。idempotency keyはhashで保存し、別参照・別revisionへの使い回しを拒否する。本人は必ずGET `/system/users/me`から解決し、書込先のuser IDをリクエスト入力にしない。
- コアDTOに所有者フィールドはない。呼出元は本人のlocal catalogだけから選択IDを解決し、ブラウザから任意DTO・owner・NX user IDを受け付けてはいけない。NX本人のremote実績取得 `read_actuals(first,last)` は競合・照合用で、export選択catalogに混ぜない。
- 共通router・lazy registry・ReflectionServiceへ組込み済み。実績参照IDとpreview tokenだけをHTTPで受け、最新コアDTOを解決する。照合成功はCore receiptにも保存する。分類だけの変更を含む操作keyを両stateへ記録し、provider intent前の停止を古い成功と混同しない。
- 専用画面は `webapp/static/timetracker-nx.js` とCSS。`MokviaTimeTrackerNX.mount(root, api)` に `listActuals / registerTask / previewActuals / applyActuals / reconcile` を供給する。preview/applyは参照IDだけを受け、serverが本人catalogから最新DTOを取得する。共通Calendarの連携設定と確定実績画面へ組込み済み。OFF/未接続でもCore反映状態を表示する。

共通 `ReflectionService` がunknownを保持している場合、provider側の照合成功だけでcoreのunknownを放置しない。照合readbackに対応する同一参照・同一local revisionのreceiptをコア側にも保存する必要がある。providerに達する前に「変更なし」を返す経路でも、明示プレビューはNX側の最新全実績を取得する。

## NX固有の安全策

- POST前にintentをatomic保存し、ファイルをfsyncする。途中終了・応答欠損・readback失敗はunknownに残す。結果不明を自動再送しない。
- POSTは `workItemId/startTime/finishTime` と明示分類。PUTには `workItemId` を送らない。成功はremote IDを指定してexact値をreadbackした後だけ確定する。
- 本人対象日ごとにGET全実績を `limit/offset/totalCount` で取得する。特定タスクだけに絞らない。ページ不足・総数変化・重複ID・別人データは失敗として止める。既存全実績と選択実績の時間重複を調べる。PUTで日付を変える場合は前回日も取得する。
- journalはNXの設定済みAPI baseと本人IDにscope固定。他host/本人への変更で既存対応を再利用しない。
- タスクURLは設定済みHTTPS hostに限定し、資格情報付きURL・query・fragment・非標準portは拒否。URLへアクセスせず、手動IDを対応付ける。現時点で通常のタスクURLがquery/fragment形式だった場合は受け入れず、実機確認後に安全な対応を追加する。
- 粒度は社内設定の5/6/10/15分。自動丸めしない。時刻はコアのUTCから社内timezoneへ変換してNXのoffsetなし日時形式にする。Asia/TokyoはWindowsのIANA DBなしでも利用可能。他地域はOSのIANA timezone DBが必要。
- 日跨ぎ、ロック、必須分類、本人未割当、終了project/入力不可タスク、重複を停止して表示する。serverの例外本文・headers・任意エラーメッセージをログやUIに出さず、既知コードと固定文だけ返す。
- NX APIに条件付きPUT/ETagの保証は確認できていない。事前再取得とPUTの間の同時編集を原子的に防げる保証はない。会社受入では競合条件を確認し、同時編集を避ける運用が必要。

## 合成検証

Python標準ライブラリと既存Nodeだけで実行できる。会社Windowsでは既存の `SETUP-WINDOWS.md` に従う承認済みDocker Linux container内でPythonを実行する（CoreのPOSIX lockに依存するため、native Windows Pythonでのアプリ起動は対象外）。NodeのQAは既存環境がある場合だけ実行し、会社への新規導入を前提にしない。

```sh
python -m unittest tests.test_timetracker_nx tests.test_timetracker_nx_plugin -v
node tests/timetracker_nx_runtime.js
python -m unittest tests.test_integration_nx_http tests.test_integration_plugins tests.test_timetracker_nx_http_transport -v
# Playwrightが既存環境にある場合のみ（新規install不要）
python tests/run_integration_ui_e2e.py --output <合成QA出力先>
python tests/run_timetracker_nx_demo.py --port 25187
```

demoはloopbackだけにbindし、一時journalを使う。例のhost `example.invalid` は合成用。実績AとBを同じワークアイテムIDへ登録し、両方選択→プレビュー→反映→再プレビューを試す。「合成実績Bを30分延長」→再読込→再選択で変更更新、「次の送信を結果不明にする」→送信→照合、「合成NX側を変更」→競合を確認できる。各操作の前に必要なURL/IDを登録する。停止時はCtrl+C。一時データは削除される。

合成DOMとfresh headless ChromiumでUIを検証した。1440px/390px/320pxで既定ON・未接続、URL/ID登録、複数新規、変更なし、PUT、結果不明の照合、競合、OFF保全、横スクロールなし、browser errorなしを確認。Outlook既存PC/スマホUI回帰も通過。実Windows/会社ブラウザでの確認は受入時に行う。

## 将来の本番設定インターフェイス（今は設定・接続しない）

本番transportは `webapp/timetracker_nx_transport.py`。Python標準HTTPSConnectionを使い、TLS証明書とhostnameを検証する。HTTP、非443、任意host、redirect、環境proxy、TLS検証無効化、自動retryは許可しない。社内がHTTPのみの場合、この方式は停止する。APIキー/Bearerの平文通信リスクを会社管理者と確認し、HTTPSを用意するまでは秘密を送らない。

非秘密JSONを会社承認済みの非同期ローカル保存先（source・Task/data・backupの外）に置く。以下は合成形式例で、実会社設定ではない。

```json
{
  "version": 1,
  "api_base": "https://example.invalid/nx/api",
  "allowed_host": "example.invalid",
  "user_id": "21",
  "timezone": "Asia/Tokyo",
  "granularity": 5,
  "required_categories": [],
  "auth_mode": "api_key",
  "credential_env": "MOKVIA_NX_CREDENTIAL",
  "company_approved": false
}
```

社内承認後にhost・API base・本人ID・粒度・分類を確認し、`company_approved`をtrueにする。未知フィールド（api_key/password/token等）・symlink・書換可能な不適切権限・未承認設定は拒否する。NX7以降のAPIキーは `api_key`、会社で承認済みの既存短期tokenは `bearer`。Basic認証・token生成・自動refreshは含めない。NX6未満で本人APIが使えない場合も書込しない。

資格情報は会社で承認されたOS保管・秘密注入手段から、起動プロセスの指定環境変数へ渡す。アプリは要求時だけ読む。設定JSON、Compose生成ファイル、Task、journal、receipt、export、backup、ログに値を保存しない。Docker daemon管理者やプロセス管理者は環境を閲覧できるため、この注入境界の会社承認が必要。コマンド行への秘密直書き、平文.env、docker compose config/inspect出力の保存を避ける。起動後の呼出側環境変数は会社の注入手順で解放する。承認前・未注入は未接続。

- 普段版: `python3 -m webapp.server --timetracker-config /approved-local/profile.json`。
- Linux runtime: `python3 -m local_runtime serve --container --timetracker-config /nx-config/profile.json`。
- 会社Windows: `windows\Start.ps1 -TimeTrackerConfigFile 'C:\ApprovedLocal\NX\profile.json'`。専用非秘密fileをread-only bindし、環境変数名の参照だけをComposeへ渡す。CaptureとOutlookの既存引数を保持する。`-DisableTimeTracker`は次回起動のNX設定mountを外す。CoreのON/OFFとは別で、保存済み実績・対応表を消さない。

Windows profileは本人と会社管理者以外が書き換えられないACLで保護する。Docker Desktopのread-only bind flagとfile modeは会社受入で確認する（POSIXでは書込可能modeを拒否し、read-only filesystemのmount metadata時だけ許す）。Windowsの参照設定は `%LOCALAPPDATA%\mokvia\timetracker-reference.json`、Composeは同ディレクトリ。保存内容はfile pathと環境変数の参照だけ。Backup/Restore/stop等のmaintenanceではprofileをmountせず資格情報を注入しない。資格情報がなくてもアプリ自体は起動し未接続を表示する。不正profileの指定時は起動を拒否するので、設定を解除して既存ローカル機能を利用できる。

本人GETのIDが設定本人IDと一致してからその本人パスだけを許す。資格情報変更時も再確認する。GET429/5xx/timeoutは固定エラーで停止。POST/PUTのredirect・408/425・429/5xx・切断/timeout・不正成功・未知4xxはunknownを保持し、照合のみ行う。401/403と既知NX拒否のみfailedにする。NXの任意message/header/bodyをエラー表示へ流さない。

## Windows会社での受入（復帰後）

1. 共通router・登録表・会社版manifestの組込みを確認する。ON/OFF、unconnected、factory生成が認証・同期を開始しないこと、OFFでも保存済み実績とreceiptが閲覧できることを確認する。
2. 合成テストとdemoを会社Windowsの承認済みDocker Linux containerで実行し、会社ブラウザからlocalhostを開く。日本語、キーボード、touch、1440px/390px/320pxの幅、横スクロールなし、未確定行の選択不可、反映ボタンの無効化、結果不明行の照合を確認する。journalのowner ACL、プロセス排他、停止後unknown保全も確認する。
3. 管理者とNXの版（NX/RX・NX6/7/7.1）とStd/Pro、HTTPS API base、本人権限、入力粒度、分類必須設定、実績入力ロックを確認する。会社host/資格情報はソース・平文settings・ログに書かない。会社承認済みcredential保管境界を別途設計する。実通信コードはあるが、会社設定・認証方式を決めるまでは未接続のまま使用する。
4. 通常のタスクURL形式とワークアイテムIDをNX画面で確認する。URL形式を推測せずID手動対応で照合する。host固定、redirect非追従、query/fragmentの安全な扱い、credential非転送を実transport導入時に確認する。
5. GET本人→本人日の全件ページングが正しいこと、UTC updatedAtとlocal開始終了の形式、作業分類/工程分類の空値挙動を会社版公式helpで確認する。
6. 会社で承認した検証用実績だけを使い、単件新規→readback→同じ実績の再操作で非送信→開始終了変更のPUT→複数件→本人以外拒否を確認する。NX側変更時の競合、日跨ぎ・ロック・未割当・必須分類・終了project・粒度・時間重複の表示も確認する。
7. POSTが成功した直後の応答断、未成立の応答断、PUT応答断、保存直後のプロセス停止を再現し、再送が起きず、一致一件だけの照合で解決し、一致なし/複数で停止することを確認する。coreのunknown receiptも同じ確認結果へ更新する。

実API疎通、会社の認証注入方式、会社host、Windows/Docker bind実機、Std/Pro実機、NX/RXの版差、タスクURL形式、同時編集、Windows/会社ブラウザ実機は未検証。自動削除・タスク変更で削除再作成・自動同期・timer・予定自動送信は初版に含めない。

合成TLS loopback試験は一時証明書・合成headerのみを使用し、本人固定、paging、新規/PUT、非再送照合、redirect、TLS不一致、429/5xx/408/未知4xxを検証する。OpenSSL CLIがない実行環境ではTLS double試験をskipとして記録する。PowerShell helperの実行・Docker Desktopは会社受入で確認する。

## 参照した公式資料

2026-10-09にNX公式helpを再確認した。NX7でAPIキー認証、NX6で本人取得が追加されている。APIキーheaderは `X-TT-ApiKey`。Std/Proの運用権限と導入版は会社で再確認する。

- `nx.timetracker.jp/support/help/webapi/docs/use-api/`
- `nx.timetracker.jp/support/help/webapi/docs/whats-new/`
- `nx.timetracker.jp/support/help/webapi/docs/common-specifications/`
- `docs.timetracker.jp/webapi/docs/reference/system/users/timeEntries/postTimeEntry`
- `docs.timetracker.jp/webapi/docs/reference/system/users/timeEntries/putTimeEntry`
- `docs.timetracker.jp/webapi/docs/reference/system/users/timeEntries/getTimeEntries`

公開docsの製品表記がRXへ切り替わる場合、対象NX版の組込みhelpとの一致を確認する。Web APIの編集権限は認証した本人権限に従う。
