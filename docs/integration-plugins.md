# 組み込み連携契約 v1

コア契約は `webapp/integrations/contracts.py`、実績read modelは `webapp/integrations/actuals.py`。固有アダプタは `webapp/integration_plugins/<provider>.py`。共通router・registry・reflection serviceが本人の正規化DTOから明示操作を解決する。

capabilityは `calendar.read` / `calendar.write` / `actual.read` / `actual.write`。組み込みの登録は全て既定ON。enabled、環境上のavailable、configured、connection_stateは別々で、登録・ONだけで認証・通信・定期処理を開始しない。OFFでもコア保存済み予定・実績・完了・注釈・書込receiptを削除しない。

ActualRecordは、reference_id/source_kind/source_id/source_provider/title/start/end/origin/completed/provisional/revision。reference_idは `task:<Task ID>` または `external:<安定した外部記録key>`。日時はUTC ISO、provisionalのendはnull。revisionはDTOの意味内容のSHA-256で、開始終了を修正しても参照IDは変わらない。Outlookの取得元IDや内部ファイルはTimeTrackerへ渡さない。Task実績のprovenanceは既存スキーマにないためrecorded、Outlookはplanned/manual/measuredを保持する。予定だけ・終日の予定由来24時間は実績DTOを作らない。

ActualWriter.write_actual(ActualWriteRequest)が新規/更新を扱う。createはremote_idなし、updateはremote_idと事前readback由来expected_remote_revisionを必須にする。native versionがない連携は正規化remote内容のhashを使う。idempotency_keyを受け取り、ActualWriteReceiptを返す。applied/unchangedはexact remote_id・remote_revision付きのreadback確認後だけ。例外・結果不明はunknownで、同じwriteを再送しない。Coreがローカルrevision/CAS、対応付け、pending/unknown/反映済み状態を保有し、固有adapterはOutlook状態やTask Markdownへ直接書かない。

Googleの専用API/OAuth/serviceと既存CLI/定期処理は当面互換adapterで包む。固定対象・権限・journal・readback・非再送を維持し、登録だけではtimerを開始しない。会社版は環境availableで切替え、私用設定・認証・会社データをソースへ含めない。認証は各provider内の既存境界に残す。

固有moduleは `describe(PluginContext) -> {available,configured,connection_state}`（読み取りのみ）と `build_plugin(PluginContext)`（認証開始なし）を公開する。PluginContext.root は環境データroot、settings は注入された非秘密設定、services は既存の境界の注入。Registryは必要capabilityを検証してからfactoryを生成する。describeは例外・認証要求を起こさず未接続を返す。credentialそのものを設定・DTO・receiptへ保存しない。

HTTPは `GET /api/v1/integrations/status`、`POST /api/v1/integrations/settings {id,enabled,revision}`、`GET /api/v1/actuals`。NXの明示操作は `POST /api/v1/integrations/task {id,reference_id,task_url,work_item_id,categories}`、`actual-preview {id,references}`、`actual-apply {id,references,token}`、`actual-reconcile {id,reference_id}`（同じintegrations prefix）。旧actual-write routeはpreview_requiredで拒否する。remote ID・owner・任意DTOをHTTP入力から受けず、本人local catalogとCore receiptから解決する。

Previewはコア実績revision・連携設定revision・NX最新全件検査へ束縛される。Applyは行ごとに再検査し、書込前にCore pending receiptを保存する。分類だけの変更もprovider payloadを含む操作keyで更新対象となる。provider journalにも同じ操作keyのhashとlocal revisionを保存するため、Core pendingの直後・provider intent保存前の停止を、古い成功と誤照合しない。結果不明は自動再送せず、同一参照・同一revision・同一操作keyのexact readbackでCore receiptも更新する。既知NX拒否はsnake_caseの非秘密エラーコードを持つfailed receiptとし、修正後の明示再操作を許す。

書込はOFF/未接続・途中実績・stale preview・結果不明で停止する。途中失敗では後続を止め、先行成功を巻き戻さない。設定・receipt・NX対応表・未確定intentは `.local-state/integrations` に保存し、会社版Backup/Restoreへ一緒に含む。backupは両state lockを取得してvalidated snapshotを作り、復元前には現状backupを保存する。未定義フィールド、秘密情報入りstate、symlinkを受け入れない。

Calendarの共通連携設定は全provider既定ON、未設定/未接続を別表示し、ONだけでは認証・同期を始めない。OFFでも保存済みCalendar・実績・Core反映状態を表示する。NX専用操作は設定済みmock transportの時だけ開く。PC1440px/スマホ390px・320pxでは合成データで登録→複数新規→変更なし→PUT→応答不明→照合→競合→OFF保全を検証する。

既存Outlookのstate_namespace・key・binding・Undoは移行せず保持する。取得実装は `webapp/integration_plugins/outlook_graph.py`、旧moduleは互換import。共通保存・差分・完了は `webapp/integrations/external_events.py`。Calendar行のactual_reference_idは同じDTO参照へつながる。表示rendererはprovider_idで登録し、コアCalendarがOutlookのUIに直接依存しない。

NXは専用本番HTTPS transportを追加。非秘密profileの明示起動引数と環境変数による秘密注入が揃うまで未接続。資格情報生成・自動同期はなく、status/factory/起動は通信しない。設定・TLS・本人照合・失敗境界・Windows helperの手順は会社版 TIMETRACKER-NX.md を参照する。
