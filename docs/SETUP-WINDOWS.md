# 会社Windowsで使う

本配布は会社PC内のDocker Linuxコンテナで動作します。ブラウザのURLは `http://localhost:24873` です。Google Calendar同期、Slack、外部ホスティング、自宅接続、GitHubへのデータバックアップは使いません。Node.jsの直接導入は不要です。

## 必要条件

- 会社が許可したDocker（Linux containers、Compose v2）。製品の契約・利用条件を会社で確認してください。
- Dockerイメージ取得と、この配布コードの取得が許可されていること。
- 通常のログイン済みWindowsセッション、Windows PowerShell 5.1以上。
- PowerShellが会社ポリシーで許可されること。スクリプト実行制限や署名要求を変更・迂回しないでください。
- VS Codeの承認済みGitHub Copilotは導入支援・手動レビューに任意で利用できます。会社Task本文を送信できる範囲は会社で確認してください。

Windows通知・Docker Desktop・復帰時の実際の挙動は、会社PCで下記の受入確認が必要です。Linux上の検査結果をWindowsでの実動作確認と読み替えないでください。

## 初回起動

公開リポジトリからZIPを取得して、会社が許可したソース用フォルダーに展開します。ZIP利用では `.git` がなく、会社データをGitで送る経路を作りません。会社データやバックアップをこのフォルダーに置かないでください。

PowerShellで展開フォルダーへ移動し、次を実行します。

```powershell
.\windows\Start.ps1
```

初回はイメージを構築し、空のデータを初期化します。初期化は既存データを上書きしません。表示されたブラウザで通知許可を与えます。日本時間のローカルカレンダー（日・週・月）で予定を管理し、作業実績は予定と区別して表示します。外部カレンダー同期はありません。

データはソース外のDocker named volume `mokvia_data`（コンテナの `/data`）です。Docker Desktopの会社PC内のLinuxストレージに保持されます。会社の保存場所ルールで別の承認済みドライブが必要なら、Dockerの保存先を承認された手順で設定してから利用してください。volume削除・Docker reset・アンインストールはデータを失わせるため、先にバックアップが必要です。

Startは同じComposeプロジェクトを再利用します。24873番ポートが他のプロセスに使われている場合は停止せずエラーにします。別の配布コピーを並行起動しないでください。

## 通知と停止

Windows通知補助は10秒間隔でlocalhostを確認し、Windows標準通知を表示します。第三者PowerShellモジュールは不要です。タブを閉じても、Dockerと通知補助が動いていれば監視が続きます。通知補助は同一ユーザー内のmutexで重複起動を防ぎます。補助の状態は `%LOCALAPPDATA%\mokvia` に保存し、Taskファイルを書き換えません。

補助が起動できない・heartbeatが途絶える場合、開いているブラウザが通知を担当します。この場合はタブを開いたままにしてください。ブラウザ通知許可と会社のブラウザ設定が必要です。会社がPowerShellを禁止している場合、ポリシー変更をせず、承認されたDocker操作で起動し、ブラウザのみを利用します。

スリープ、電源断、ログアウト、アプリ停止中は通知できません。復帰時は現在状態を読み直し、過去の通知を連続再生せず現在の通知を最大1件にまとめます。集中モード・通知設定・会社ポリシーが表示を抑制する場合があります。Windowsの通知センターへの履歴保存・表示成功は保証しません。UIにも状態を残します。

```powershell
.\windows\Stop.ps1
```

Stopはこのアプリの通知補助とComposeサービスだけを停止し、保存volumeを保持します。停止後の再開はStartです。Windows全体の他プロセスは終了しません。

## バックアップと復元

バックアップは停止したスナップショットです。既定の保存場所は `%LOCALAPPDATA%\mokvia\Backups`。会社の保持期間・保管規則に従い、承認されたローカルフォルダーへ変更できます。OneDrive等へ同期される場所を選ばないでください。バックアップは会社データを含む平文tarです。

```powershell
.\windows\Backup.ps1
.\windows\Backup.ps1 -Destination 'D:\ApprovedLocalData\mokvia-backups'
```

スクリプトはアプリを停止し、動作中の専用一時コンテナ内で検証したアーカイブを作り、volume内の一時ファイルを `docker cp` で取得し、一時ファイルと専用コンテナを削除します。Docker cpはtmpfsを読み出せないため、転送にはvolumeを使用します。PowerShell 5の標準出力リダイレクトでバイナリを壊さない構成です。取得後も停止したままです。必要時にStartしてください。

```powershell
.\windows\Restore.ps1 -Archive 'D:\ApprovedLocalData\mokvia-backups\mokvia-example.tar' -BackupDirectory 'D:\ApprovedLocalData\mokvia-backups'
```

復元前に現データをソース外へバックアップします。復元先は停止中のvolumeに限定し、アーカイブ内容を検証します。失敗時はエラーを確認し、むやみに再実行しないでください。復元後も停止したままです。Start後に件数・代表レコード・作業実績・予定を確認してから利用を再開してください。

定期バックアップを会社が許可する場合のみ、Windowsタスクスケジューラでログインユーザーの `Backup.ps1` を実行できます。実行中のアプリを停止して停止したままにするため、利用していない時刻を選びます。既定では登録しません。スリープ中は実行できません。管理者権限・ポリシー変更を要求しません。

## 会社PCの受入確認

1. Startを2回実行してもコンテナと通知が重複しない。他プロセスで24873が占有される場合、そのプロセスが停止されない。
2. `docker compose -p mokvia port app 24873` が127.0.0.1限定。別PC・LANアドレスから到達しない。
3. 架空Task・Projectを追加、変更し、再起動後に残る。日・週・月の予定と作業実績、日本時間の日付境界が正しい。
4. 作業開始・中断・完了、休憩期限、着手条件解放、レビューの操作を確認する。
5. Windows通知が表示される。タブを閉じても通知する。通知補助だけを終了（`%LOCALAPPDATA%\mokvia\notify-stop` に停止信号を書き込む）すると、heartbeat失効後にブラウザfallbackへ切り替わる。Stop.ps1はアプリ自体も停止するためfallbackの確認には使わない。会社設定で非表示ならUIで確認する。
6. スリープ復帰時に過去の通知が連続表示されない。
7. Backup、架空データ変更、Restore、読戻しで復元を確認する。バックアップがソース外にある。
8. 会社実データ、秘密、バックアップがソース・Git・Copilot入力へ混入しない。

## 障害時

```powershell
docker compose -p mokvia logs --tail 80 app
docker compose -p mokvia ps
```

ログに会社情報が含まれ得るので、公開IssueやCopilotへ無確認で貼り付けないでください。データvolume削除やDocker resetを復旧手順として使わないでください。

## 新版への更新

[UPDATE-WINDOWS.md](UPDATE-WINDOWS.md) の停止バックアップ・別フォルダー展開・同じvolumeでの読戻しを使用します。会社側からのコード・データ送信は行いません。

## 初回導入と旧名データの検出

取得元は https://github.com/kiyugithkvjl1024/mokvia です。指定commitのZIPを新しいmokviaソース用フォルダーへ展開します。Gitログイン・clone・pushは不要です。

`Start.ps1` がDocker volume一覧と旧Windows状態を確認し、問題がなければ外部volume `mokvia_data` を明示作成してCompose project `mokvia` を起動します。Compose単独のupは未作成volumeを自動作成しません。`MOKVIA_DATA_ROOT=/data`、ヘッダー`X-Mokvia-Web`、通知補助mutex `Local\mokviaNotify`、Windows状態 `%LOCALAPPDATA%\mokvia` を使用します。

旧名の`gtd-local_gtd_data`等のvolumeや `%LOCALAPPDATA%\GtdLocal` が見つかった場合は、具体的な場所を示して停止します。既存データ・バックアップの削除、コピー、自動移行、新しい空volumeの起動はしません。会社版は未導入を前提とし、Ubuntu私用データを持ち込む手順はありません。検出された保存先を確認するまで、そのまま保全してください。
