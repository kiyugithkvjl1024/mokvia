# mokvia

頭の中の「やること」を集め、今日進める一つを選ぶ、一人用のタスク管理アプリです。会社Windowsのlocalhostで動作し、Task・予定・作業実績を会社PC内に保存します。

## 最初はこれ

**[はじめてのmokvia（QUICKSTART）](docs/QUICKSTART.md)** を開いてください。手入力Taskを一つ作り、今日の作業から完了まで進める3ページの入口です。最初はOutlook・Teams・AIの設定は不要です。

| 必要なとき | 読む資料 |
| --- | --- |
| 毎日の使い方、Calendar、Task操作、症状別FAQ | [利用マニュアル](docs/USER-MANUAL.md) |
| Docker／PowerShellの準備、図付きWindows導入 | [SETUP-WINDOWS](docs/SETUP-WINDOWS.md) |
| 任意のOutlook／Teams連携 | [Power Automateガイド](docs/POWER-AUTOMATE-CAPTURE.md) |
| 新版ZIPへの更新、Backup・復元 | [UPDATE-WINDOWS](docs/UPDATE-WINDOWS.md) |

会社の利用許可と著作権者の個別利用許可がある本人向けです。許可済みの環境で、指定版ZIPの展開フォルダーから起動します。

```powershell
.\windows\Start.ps1
```

ブラウザは `http://localhost:24873`。停止は `windows/Stop.ps1`、Backupは `windows/Backup.ps1` です。Backup取得後もアプリは停止したままです。データはソース外のDocker named volume、バックアップは会社承認済みローカル場所へ保持し、会社データを公開repoや個人環境へ送りません。

## 利用条件

ソースは公開、実行・改変は著作権者または個別許可済みの本人が行います。[LICENSE](LICENSE) と会社の利用規則に従ってください。許可は他社員へ自動的に広がらず、社内共用サーバー・再配布等は別途許可が必要です。過去MIT版と第三者の条件は維持されます。

## 開発資料

[製品仕様](docs/product-spec.md)、[Web/API契約](docs/webapp-spec.md)、[データスキーマ](docs/data-schema.md)、[公開・依存チェック](docs/DISTRIBUTION-CHECKLIST.md)。実装はDocker Linuxコンテナ・Python標準ライブラリ・vanilla JavaScriptです。Nodeは開発時のJS検査用で、Windowsの実行に直接導入する必要はありません。
