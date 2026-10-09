# Outlook予定の手動取り込みと会議の完了

会社Windowsで使用する機能です。育休復帰後、会社PCで設定します。現在、会社アカウント接続・Windows Outlook実機は未検証です。認証やアプリ登録を自動で準備する機能はありません。

## 日常操作

1. 朝などに上部「カレンダー」を開きます。`http://localhost:24873/calendar` です。
2. 取得方式と取り込み期間を選び、「Outlookから取り込む」を押します。取得ヘルパーは、この要求があるときだけ予定を取得します。定期同期はしません。
3. 会議が終わったら、会議のドラッグハンドルから「完了」にドロップします。「…」の操作メニュー内の「完了」も同じ操作です。事前の開始・完了ごとの時刻入力は不要です。
4. 初回完了で予定開始・終了をローカル実績の初期値にし、「予定由来」と表示します。これは実測値ではありません。
5. 日・週表示の時間枠で、ハンドルをドラッグすると実績の枠全体を移動できます。枠の上端・下端は開始・終了を5分単位で調整します。Outlookの元の予定は変更しません。スマホでもpointerによるドラッグを使えます。
6. 操作メニューの「実績時刻」はキーボード・タッチ用の補助です。月表示もこの入力で調整します。入力の取消では保存しません。
7. 直前の完了・実績修正は30秒以内の「元に戻す」で戻せます。その後に別の変更や取り込みがあると古いUndoは使えません。完了取消は実績を保持し、計測を再開しません。

時刻指定の会議が対象です。終日予定の完了は24時間の作業実績を作りません。実測済み／計測中の記録がある場合、予定由来の値やドラッグで置き換えません。実績の実測用timerとの自動連携は今回追加していません。

## データ保全

取得を全ページ・全件検査した後、ローカル状態1ファイルを原子的に差し替えます。取得失敗・解析不備では既存状態を変更しません。単に範囲内から消えた予定は「今回未確認」で、取消と断定しません。明示された取消は履歴として保持します。

同じ予定の再取り込みで重複させません。繰り返しの各回は元の各回の識別子で分離し、変更後の現在の日時を識別子にしません。予定由来・手修正・実測の実績、完了日時、ローカルメモは再取り込みで上書きしません。完了済みの件名・日時変更は「変更あり・要確認」です。

GraphとCOMは取得元の識別子体系が異なります。初回成功で方式と予定表を固定します。既存の完了を守るため、方式・予定表の変更はそのまま追加せず拒否します。移行は将来の照合手順を要します。両方式の検証は別の合成データ環境で行います。

保存先はDocker named volumeの `.local-state/outlook-import/state.json` です。Task正本と別の外部予定・注釈です。会社版Backup/Restoreの対象に含めます。OAuth token・取得要求／応答ファイルはバックアップへ含めません。

## 復帰後に会社内で確認すること

- Docker・ローカルスクリプト・Outlook COM利用の会社ルール。実行ポリシーは変更しません。
- Graphの場合、会社Microsoft 365への通信・Entraアプリ利用・同意が許可されるか。個人クラウドには接続しません。
- 対象は自分の予定表。共有予定表はこの初期版の受入範囲に含めません。
- 認証・予定情報・handoff・backupを個人Ubuntu、個人OneDrive、公開repoへ持ち出さないこと。

## 従来版Outlookの設定

会社承認済みの、既存・非同期・非共有の専用ローカルフォルダーを用意します。以下のパスは説明用です。ソースフォルダー・ドライブ直下・OneDrive・ネットワークパスは使いません。

```powershell
.\windows\Start.ps1 -OutlookFolder 'C:\MokviaLocal\OutlookHandoff'
.\windows\OutlookImport.ps1 -Folder 'C:\MokviaLocal\OutlookHandoff' -Source classic
```

二つ目は別のログイン済みPowerShellウィンドウで起動し、使用中だけ開いておきます。Outlookへ書き戻さず、既定の予定表を読み取ります。従来版ではWindowsとOutlookの表示タイムゾーンが一致することを確認します。不一致なら日時を推測せず取得を拒否します。新しいOutlookにはこのCOM方式を使用できません。変更済みの繰り返し回を元の回へ確実に照合できない場合は取得全体を失敗させ、既存状態を保持します。

## Graphの設定（会社での別途承認後）

必要なのは会社アカウントの委任された読み取り権限です。今回の取得項目は基本情報だけなので、最小権限候補は `Calendars.ReadBasic`。`Calendars.ReadWrite`、送信、連絡先、アプリ全組織権限は要求しません。管理者が必要と判断する同意・アプリ登録・認証方式は会社で決めます。

取得ヘルパーはOAuthログイン・refresh token保存を実装していません。会社で承認された認証経路で得た短期access tokenを、そのPowerShellセッション内だけで渡します。コマンド行・ファイル・ログへ平文tokenを置きません。会社側の認証方式を決めるまでは接続を実行しません。

```powershell
# 復帰後、会社で許可された手順により取得済みのtokenだけを使う例
$sessionToken = Read-Host '承認済み短期access token' -AsSecureString
.\windows\OutlookImport.ps1 -Folder 'C:\MokviaLocal\OutlookHandoff' -Source graph `
  -CalendarId 'approved-calendar-id' -MailboxScope 'approved-mailbox-scope' -AccessToken $sessionToken
Remove-Variable sessionToken
```

`MailboxScope`と`CalendarId`は実際の会社内の対象を確認して指定します。自動探索しません。期限切れ・権限不足ならデータを保持して取得失敗になります。認証の利便性向上は、会社の方式が決まった後の別変更です。

公式仕様: [calendarView](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview)、[Graph event](https://learn.microsoft.com/en-us/graph/api/resources/event)、[COM GlobalAppointmentID](https://learn.microsoft.com/en-us/office/vba/api/outlook.appointmentitem.globalappointmentid)、[繰り返しのOriginalDate](https://learn.microsoft.com/en-us/office/vba/api/outlook.exception.originaldate)。

## 会社PCでの疎通・受入と切り戻し

1. 既存のBackupを会社内に取得し、停止したままの状態と復元手順を確認します。
2. 空の試験データvolumeで、会社内の試験予定表の短い期間を取り込みます。実会社予定はUbuntu検証へコピーしません。
3. 再取り込みで重複しないこと、各回・移動済み回・取消・終日・日跨ぎ・タイムゾーンを確認します。
4. 完了、延長、Undo、再取り込み後の実績保持、取得ヘルパー停止時の保全を確認します。スマホ相当幅・キーボードでも補助操作を確認します。
5. 問題があれば取得ヘルパーをCtrl+Cで止め、`Start.ps1 -DisableOutlook` で連携を無効化します。保存済み予定・完了は削除しません。アプリを止めて会社内Backupへ戻す場合は、Taskと外部予定状態を同じ時点へ復元します。volume削除・stateファイル削除はしません。
6. handoffの要求／応答残骸は、ヘルパーとアプリを停止して状態を確認した後、会社内の運用担当が整理します。残った応答から自動で遅延適用しません。

Ubuntuでは共通処理・Graphの合成応答・HTTP・ブラウザを検証します。Windows PowerShell構文実行、COM、Docker Desktop bind、会社テナント権限、条件付きアクセス、実token、会社実機のタッチは別途受入が必要です。

開発者向けの合成QAは `python3 -m unittest tests.test_outlook_import`、`node tests/outlook_time_runtime.js`。既存のPlaywrightがある開発環境では `GTD_E2E_PLAYWRIGHT_MODULE=/existing/playwright python3 tests/run_outlook_import_ui_e2e.py --output /tmp/outlook-ui-evidence` でPC・スマホ・touch・Undo・失敗保全を再検証できます。試験サーバーとデータは一時領域・loopback内だけで、会社PCでNodeを導入する必要はありません。
