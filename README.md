# GTD Local

会社Windowsのlocalhostで動く、Markdownベースの個人GTDアプリです。Docker Linuxコンテナ、Python標準ライブラリ、vanilla JavaScriptを使用します。実行時の外部API・AIモデル・クラウド保存は不要です。

[Windows導入手順](docs/SETUP-WINDOWS.md)を読み、会社が許可したDocker/PowerShell環境でZIPを展開して起動します。

```powershell
.\windows\Start.ps1
```

ブラウザで `http://localhost:24873` を開きます。最初は空のデータです。配布ソースと会社データを分離し、会社データはDocker named volume、バックアップは承認済みローカル場所に保持します。

- GTD階層、Task/Project、日次・週次Review、Progress、時間配分。
- 開始・中断・完了と作業実績、5分休憩、着手条件の自動解除。
- JSTの日・週・月カレンダー、予定の追加・変更、予定と実績の区別。
- Windows通知ヘルパー、利用できない場合のブラウザ通知。
- 手動のVS Code Copilot補助。[開始文](docs/COPILOT.md)をそのまま使用できます。

停止・バックアップ・復元は `windows/Stop.ps1`、`Backup.ps1`、`Restore.ps1` を使用します。バックアップはアプリを停止したままにします。詳細と会社PCでの受入確認は導入手順を参照してください。会社のクラウド禁止に対するCopilot等の例外範囲は、会社の承認に従います。

## 開発と検証

```sh
python3 -m unittest discover -s tests
node tests/local_calendar_runtime.js
node tests/local_notifications_runtime.js
docker compose up -d --build
```

Nodeは開発時のJS単体検査用で、Windowsのアプリ実行には不要です。Linuxの安全なファイル操作を使用するため、PythonだけでWindowsネイティブ実行する構成は提供しません。

[製品仕様](docs/product-spec.md)、[Web/API契約](docs/webapp-spec.md)、[データスキーマ](docs/data-schema.md)、[公開・依存チェック](docs/DISTRIBUTION-CHECKLIST.md)を参照してください。
