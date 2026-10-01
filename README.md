# mokvia

mokviaは、タスクの収集・整理・見直しを支える個人向けタスク管理アプリです。会社Windowsのlocalhostで動作し、データはMarkdownで保存します。Docker Linuxコンテナ、Python標準ライブラリ、vanilla JavaScriptを使用します。実行時の外部API・AIモデル・クラウド保存は不要です。

**ソースは公開、利用は個別許可制です。** 実行・改変は著作権者と個別に許可された利用者が行えます。閲覧・検討用の取得は誰でも可能です。

[公開リポジトリ](https://github.com/kiyugithkvjl1024/mokvia)の指定版ZIPと[Windows導入手順](docs/SETUP-WINDOWS.md)を使い、会社が許可したDocker/PowerShell環境で起動します。初回は空の会社ローカル記録から始めます。

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

## ライセンス

現行の自作部分には [mokvia 個別許可利用条件](LICENSE) が適用されます。ソースは公開していますが、一般に実行・改変・再配布を許可するオープンソースライセンスではありません。許可された本人の個人利用・勤務先での本人の業務利用と必要な複製・非公開保管、許可された利用を補助する会社承認Copilot等のコード処理は認められます。許可は他社員へ自動的に拡張されず、再配布・販売・再許諾・他人向けのサービス提供（社内共用サーバーを含む）は別途許可が必要です。

本条件を付した版以降に適用し、[直前のMIT版](https://github.com/kiyugithkvjl1024/mokvia/blob/cb76b9375286c2c6f48579cba8ddef19d6621d68/LICENSE)を含む過去のMIT版・コードの許諾を遡及的に変更しません。GitHub規約によるサービス内の閲覧・fork等の権利を妨げません。ベースイメージ同梱ソフトウェアのライセンスは各権利者の条件に従います。

新版への更新は [Windows ZIP更新手順](docs/UPDATE-WINDOWS.md) を参照してください。会社側は取得のみで、会社データを公開repoや個人環境へ送信しません。
