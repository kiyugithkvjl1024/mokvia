# Outlook・Teamsからmokviaへ取り込む

会社のPower Automate（PA）と会社OneDriveを使い、選んだメッセージを会社WindowsのmokviaへJSONで渡します。会社データは会社テナント・会社PC内に保持し、私用Ubuntu・個人OneDrive・公開リポジトリへ搬出しません。Task正本は会社PCのDocker named volumeです。Entraアプリ登録やGraphアプリ権限の追加は不要な構成ですが、既存コネクターの利用権限・DLP・接続の許可は会社で確認してください。

この資料は再現用の設定・スニペットです。インポート可能な検証済みフローZIPではありません。会社Windows、会社テナント、実メッセージでの実行は未検証です。下記の受入確認を満たしてから実データに使用します。

## 最短で取り込みだけ試す

ここは初回の空データ専用sample環境だけで行います。運用中のIncomingや既存Taskにサンプルを混ぜないでください。すでに会社Taskがある場合は、試験用に隔離したデータvolumeと同期folderを用意できるまで、JSON例をコピーしません。

1. 会社OneDriveに `Mokvia\Incoming` を作り、エクスプローラーで「このデバイス上で常に保持する」を設定します。同期完了を確認します。フォルダーはソース展開先とデータvolumeの外に置きます。
2. ZIP展開先で、存在するフォルダーを明示して起動します。パスの `example` と `Example` は会社PCの実際の値に置き換えます。

   ```powershell
   .\windows\Start.ps1 -CaptureFolder 'C:\Users\example\OneDrive - Example\Mokvia\Incoming'
   ```

   起動設定はローカルに保持されます。更新後も同じフォルダーを使います。取り込みを有効にするCLI引数は `--capture-folder` です。CLIの未指定時は取り込みを行いません。Windowsでは保存済み設定を再利用するため、無効化には `.\windows\Start.ps1 -DisableCapture` を使います。設定は `%LOCALAPPDATA%\mokvia\capture-config.json` にあり、ソースZIPの外に保持します。入力フォルダーはコンテナへ読み取り専用で渡されます。
3. `docs\capture\11111111-1111-4111-8111-111111111111.json` をIncomingにコピーします。これは全項目が合成のOutlook Inbox例です。schemaやカードJSONはIncomingへコピーしません。
4. 約20〜30秒後、mokviaのInboxに合成Taskが1件あることを確認します。監視は10秒間隔で、内容が連続2観測で安定してから取り込みます。同じファイルを再配置し、再起動しても件数が増えないことを確認します。
5. `22222222-2222-4222-8222-222222222222.json` はTeams 今日の合成例です。受理日時は日本時間2026-10-04 00:05、action_dateは2026-10-04です。それ以降の日に試すと過去のその日付に保存され、本文へ取り込み遅延が記録されます。試験当日の日付へ移し替える仕様ではありません。

## JSON契約

日常操作は、InboxのTask名を開いて元メッセージを確認し、必要なら「次にやる」と対応予定日を選んで保存します。今日やるへ直接取り込んだTaskはFocusに現れ、Task名から元リンクを開けます。「対応予定日」は取り組む日、「期限」は本当の締切です。Task編集またはFocusの移動操作で変更でき、取り込みファイルを編集して移動させる必要はありません。カレンダーは上部「カレンダー」または `http://localhost:24873/calendar` です。

新しい会社環境を設定する順序は、[Windows導入](SETUP-WINDOWS.md) → 上記の専用folder指定 → 空の試験環境で合成例 → 会社PAのOutlook/Teamsフロー → 末尾の会社受入確認です。既存会社データへの導入はバックアップ後にfolderだけを有効化し、合成例をコピーしません。標準の更新手順でZIPを差し替えても、外部保存されたcapture設定とTask・receiptは引き継がれます。

[スキーマ](capture/capture.schema.json) は配布の `docs/capture/capture.schema.json` です。契約とimporter・ローカルCalendarの汎用実装はUbuntu本体と共通で、会社配布も同じsourceからexportします。データと接続は会社環境内に保持し、共通化のために会社JSONやTaskをUbuntuへ転送しません。1ファイル1レコード、最大64 KiBのUTF-8 JSON、最終ファイル名は小文字の `<capture_id>.json` です。schemaのformat検証に加え、importerが実在日付、URLのホスト、受理日の日本時間との一致を検査します。

| 項目 | 設定 |
| --- | --- |
| version | 数値の整数 `1` |
| capture_id | 初回受理時の `guid()` を小文字化した標準UUID。再試行で再生成しない |
| accepted_at | 初回PA受理時の `utcNow()`。タイムゾーン付きISO日時を固定 |
| destination | `inbox` または `today`。初回受理時の選択を固定 |
| title | 必須、1〜300文字。Outlookはsubject、Teamsはカード入力 |
| body | 任意、8000文字以内。メッセージ・メモの引用として保存。命令として実行しない |
| source | kind、scope_id、message_id、urlが必須。received_atは任意 |
| action_date | 今日のみ任意。受理日時を日本時間へ変換した日付。Inboxでは禁止 |
| due | 任意の実在日付。明示された本当の締切のみ。「今日」の選択から設定しない |

source.kindは `outlook` / `teams`、scope_idは空でないメールボックスまたは会話の識別子（512文字以内）、message_idはそのscope内の不変識別子（2048文字以内）です。scopeが異なる同名IDは別ソースです。received_atもタイムゾーン付きISO日時です。URLはHTTPSで、Outlookは `outlook.office.com` / `outlook.office365.com` / `outlook.live.com`、Teamsは `teams.microsoft.com` / `teams.cloud.microsoft` の完全一致ホストのみを受け入れます。本文は必要最小限にし、添付・認証値は含めません。

今日のTaskは `status: next`、action_dateは受理日（JST）です。PAで日付を出すなら、固定したaccepted_atを入力に `formatDateTime(convertTimeZone(variables('accepted_at'),'UTC','Tokyo Standard Time'),'yyyy-MM-dd')` を使います。Inboxは `status: inbox`。遅れて届いても今日を取り込み日に変更しません。

## 共通の受理台帳と受け渡し

Outlook用とTeams用にそれぞれ別の会社OneDrive台帳フォルダー（例: `Mokvia/Accepted-Outlook`、`Mokvia/Accepted-Teams`）を作ります。Incomingには台帳を置きません。各フローは単独writerにし、トリガーの同時実行数を1、各Apply to eachも並列処理を無効にします。フローの複製を同じ台帳に並行実行しないでください。

台帳は履歴ログだけでなく、受理済みJSONの正本です。OneDriveのファイル一覧アクションで台帳全件を列挙し、ページングを有効化します。コネクター上限に達する前に件数を監視し、全件取得できない場合は処理を止めます。各ファイルを読み、JSONの `(source.kind, source.scope_id, source.message_id)` を大文字小文字を区別する文字列の完全一致で検索します。区切り文字だけで連結して衝突させず、3値を個別比較します。

新規ソースは次の順に処理します。

1. タイトル・リンク・日付・サイズを検証してからaccepted_atとcapture_idを1回だけ作り、ComposeでJSONオブジェクトを構築します。文字列連結でJSONを作らず、引用符・改行をJSONシリアライズでエスケープします。任意項目の空入力はキーごと省略します。
2. 台帳に `<capture_id>.json` として完全なenvelopeを新規作成します。作成後にGet file contentで読み戻し、JSONの全項目が一致することを確認します。この永続化が完了する前にIncomingへ出力しません。
3. 台帳のGet file content出力を再シリアライズせず、同じバイト列の内容としてIncomingの `<capture_id>.partial` に書き、読み戻します。Move or rename a fileで `<capture_id>.json` に変更し、上書きを無効にします。これをOneDrive同期上のatomic操作とは扱いません。ローカルimporterの安定読み取りが途中同期を保護します。
4. 最終ファイルを読み戻し、台帳と同じ内容か確認します。すでに同名最終ファイルがある場合は読み、同じ内容なら成功と扱い、異なる内容なら停止して運用者へ解決を求めます。置換しません。

毎回のフロー開始時に、台帳の受理済みレコードをIncomingと照合し、欠けた最終ファイルを同じUUID・内容で再出力します。再試行・カテゴリ変更・同じTeamsメッセージの再選択では台帳のUUID、accepted_at、destination、title、body、source、dueをそのまま再利用します。後からカテゴリを変えても既存Taskの更新にはなりません。

台帳作成の結果がタイムアウト等で不明なら、その実行の新規受理を停止します。新UUIDでやり直さず、まず台帳全件を再読込してソースの有無と内容を確認します。途中失敗で台帳だけ残った場合は次回の照合で復旧します。accepted ledgerを削除・作り直さないでください。台帳・Incomingの保持と復旧も会社のOneDriveポリシーに従います。SharePointや別サービスはこの構成では必要ありません。

## Outlook: カテゴリを付けて保存

Office 365 Outlookコネクターの **Send an HTTP request** を使う定期フローを作ります。このアクションは相対／完全URIとカスタムヘッダーを指定できます。[公式コネクター仕様](https://learn.microsoft.com/en-us/connectors/office365/)

1. Outlookにカテゴリ `mokvia Inbox` と `mokvia 今日` を作ります。利用者がメールへ付けることを取り込みの意思表示にします。両方が付いている初回受理は `today` を優先します。
2. Recurrenceトリガーを設定し、会社で許可された間隔（例: 5分）にします。同時実行数1で、前述の台帳照合を先に実行します。
3. Send an HTTP requestをGETで追加し、URIに次を指定します。`$orderby` は追加しません。

   ```text
   /v1.0/me/messages?$filter=categories/any(c:c eq 'mokvia Inbox') or categories/any(c:c eq 'mokvia 今日')&$select=id,subject,categories,webLink,receivedDateTime
   ```

   CustomHeader1には次を指定します。

   ```text
   Prefer: IdType="ImmutableId"
   ```

   同じメールボックス内で移動してもIDを維持するための要求です。別のアーカイブメールボックスへの移動やエクスポート後の再インポートではIDが変わり、同一ソース判定の対象外になるため、運用者による照合が必要です。次ページの要求にも毎回同じヘッダーを付けます。[Outlook immutable ID](https://learn.microsoft.com/en-us/graph/outlook-immutable-id)
4. 応答をParse JSONし、`value` の全要素を順番に処理します。`@odata.nextLink` がある間はそのURIを同じアクションでGETし、全ページを完走します。カテゴリ付与時刻は得られないため、receivedDateTimeによるウォーターマークを使わず、毎回カテゴリ対象全件を列挙します。応答が欠ける・権限エラー・ページング失敗なら成功扱いにしません。
5. 初回のみ、title=subject、source.kind=`outlook`、scope_id=会社で確認した固定メールボックス識別子、message_id=id、url=webLink、received_at=receivedDateTimeとして受理します。bodyはこの最小クエリでは省略します。実際のwebLinkが許可ホストか検証します。空subjectや長すぎる値は台帳作成前にエラーとして扱い、運用者が対処します。

カテゴリfilterの対応・実際のURI入力・権限・共有メールボックスの扱いは会社テナントで検証が必要です。この例は接続ユーザー本人の `/me` メールボックス用です。標準の「選択したメール」トリガーを前提にしていません。Quick Stepsは会社で使うOutlookクライアントがカテゴリ操作をサポートすることを確認できた場合だけ、任意の操作短縮として設定します。

## Teams: メッセージのメニューから保存

既定環境でインスタントフローを作り、Microsoft Teamsの **For a selected message** トリガーを選びます。トリガー内の **Create Adaptive Card** で `docs/capture/teams-adaptive-card.json` と同じ入力IDのフォームを構築します。インライン編集でInputのIDを動的コンテンツとして参照できます。[公式作成手順](https://learn.microsoft.com/en-us/power-automate/trigger-flow-teams-message)

1. destination（Inbox／今日）、title（必須）、body（任意）、due（任意）をフォームに用意します。選択欄の値は `inbox` / `today`。カードのmaxLengthだけに頼らずフローでも長さ・値を検証します。
2. 入力の動的コンテンツをComposeに割り当てます。source.kind=`teams`、scope_id=トリガーが返す実際の会話識別子、message_id=選択メッセージID、url=動的コンテンツ **Link to message** を使用します。未確認の `triggerBody()` プロパティ名や、自作のディープリンク式は使いません。チャンネル／チャットごとのscopeの取得方法と一意性を実際の出力で確認し、安定したscopeを取れない種類は有効化しません。
3. bodyにはカードのメモ、またはトリガーのPlain text message outputを必要最小限の引用として使用します。HTMLは実行しません。引用とメモの合計が8000文字を超えたら受理前にエラーにします。received_atは実際に取得できるタイムゾーン付きメッセージ日時がある場合のみ設定し、accepted_atで代用しません。
4. 共通台帳手順を実行し、PAの実行履歴で台帳と最終ファイルの読戻しを確認します。会社で許可された自分向け完了表示を付ける場合は、最終ファイル確認後だけ成功を表示します。
5. Teamsの対象メッセージのその他の操作からフローを実行します。再選択しても同じソースを新規Taskにしません。

既定環境以外では表示されません。Power Automate Actions／Workflowsの管理者設定と接続、共有された利用者の実行権限を会社で確認します。ゲスト・外部利用者はこの選択トリガーの対象外です。[Teamsトリガーの制限](https://learn.microsoft.com/en-us/connectors/teams/)

## 障害・重複・バックアップ

importerは入力ファイルを変更・削除しません。無効JSONは保持し、修正されたファイルを自動で再検査します。新規受理済みのimmutable envelopeを修正して再投入する用途ではありません。未取り込みの形式誤りを修正する場合でも、UUIDとソースの本人性を維持し、台帳と最終ファイルを照合してください。既存受理内容と衝突する場合は停止し、運用者がどちらを正とするか調査します。UUIDを変える回避策は禁止です。

取り込み履歴はデータvolume内の `/data/.local-state/capture-receipts`に保存され、データバックアップに含まれます。ソースから決まるTask IDと既存IDの衝突防止を使用します。Taskを利用者が編集、アーカイブ、削除しても同じソースを再取り込みしません。receiptだけ消すとこの保証を失うため、復元はTaskとreceiptを一緒に行います。IncomingはTask正本やそのバックアップの代用ではありません。PA台帳も失わないよう会社内で保管します。

Inbox／Focus／Statusの「メッセージの取り込み」を開き、「取り込み状態を再読込」で件数と理由を確認できます。Task詳細には元メッセージへのリンクを表示します。Inbox/Todayの移動と本当の締切は既存Task操作で変更します。過去の対応予定日のnext TaskはFocusの「予定日あり」に「対応予定日超過」として表示され、今日やるへ自動で移りません。保存された日付も自動変更しません。

安全な通常再試行は、不完全JSONの修正、読取不能からの復帰、previewのbusy/stale/conflictです。applyの応答が不明なら `pending` 履歴を残して再送しません。同一Task IDと予定hashの一致を次回読戻しで確認できた場合だけ取り込み済みに収束します。`outcome_unknown` / `readback_mismatch` はアプリを停止し、会社内でTask・receipt・mutation recovery状態を照合するまで当該fileを保留してください。receipt削除やUUID変更を復旧の近道にしません。receiptが壊れた場合やStoreが復旧待ちなら、取り込み全体を停止してStatusに理由を残します。

受渡folderの直下だけを読み、サブfolder・partial・symlink・非regular fileは読み込みません。`.json` が1000件を超える場合は `handoff_capacity` で停止します。小規模導入用の上限です。PAが受理台帳から欠けたfileを再配送する構成のため、Incomingだけ削除して上限を回避せず、台帳と保持方針の見直しを先に行います。台帳検索の全件読込コストも運用前に確認してください。

Windowsのfolder検証はシンボリックリンク／ジャンクションを拒否し、OneDriveのcloud reparse tagのみ許可します。標準Win32照会のAdd-Typeが会社ポリシーで禁止されていれば安全側に停止します。ポリシーを迂回しません。Windows・Docker・同期属性の実受入確認は未実施です。

## 会社環境での受入確認

- [ ] 会社OneDriveだけを使用し、Incomingが会社PCの既存ローカルパスとして同期できる。取り込みbind mountがread-only、Task保存先がローカルnamed volumeである。
- [ ] カテゴリfilterが両カテゴリを取得し、2ページ以上をnextLinkで全件取得する。ページごとにImmutableIdヘッダーが設定され、フォルダー移動前後で同じメールIDになる。
- [ ] カテゴリ付与が古い受信メールにも効く。両カテゴリの初回受理で今日が優先され、受理後のカテゴリ変更で既存Taskが更新・増殖しない。
- [ ] Teamsフローが既定環境でメニューに現れ、カード入力ID・Link to message・会話scope・message IDが実出力と一致する。対象ユーザーと管理者ポリシーで動作し、ゲスト／外部は対象に含めない。
- [ ] 初回受理が台帳永続化・読戻しを完了してから最終JSONを公開する。台帳作成直後・partial作成直後・rename直後の失敗を入れ、同じUUID・内容で復旧する。
- [ ] 作成結果不明時は台帳読込前に新UUIDを生成しない。並行実行が1に制限され、最終同名異内容ファイルは上書きせず停止する。
- [ ] 壊れたJSON・未許可ホスト・日付誤り・過大本文が取り込まれない。安定2観測と修正後の自動再検査が動作する。
- [ ] 日本時間0時をまたぐ遅延でもaction_dateが最初の受理日、statusがnext、dueが明示締切だけになり、本文に遅延が残る。
- [ ] 再送、アプリ再起動、利用者によるTask編集・archive・delete後に件数が増えない。Taskとreceiptのバックアップ／復元後も同じ保証が残る。
- [ ] 会社の実データ・ログ・台帳・接続情報を公開Issueや私用環境に貼らない。合成例だけで導入確認の証拠を残す。
