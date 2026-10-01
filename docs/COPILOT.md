# VS Code Copilotへの開始文

会社が承認したVS Code Copilotを、このZIP展開済みのソースフォルダーで開き、次を渡します。

> このGTD Localを会社Windowsのlocalhostで起動してください。まず .github/copilot-instructions.md と docs/SETUP-WINDOWS.md を読み、会社が許可したDocker Linux containers/Composeを確認してください。Nodeの直接導入や外部ホスティングは不要です。PowerShellポリシーを変更せず、制限があればその場で知らせてください。会社データ・バックアップをソース外に保ち、Gitへの送信や外部カレンダー同期を行わず、空のデータで起動・停止・バックアップ復元・通知の受入確認をしてください。会社データ本文へのアクセスは別途指示するまで行わないでください。

Copilotは手動指示による導入、明確化、日次/週次レビュー、文章整理に使います。無人AI実行、APIキー、M365 Copilot、Copilot CLIは必須ではありません。

Task更新はWeb APIのpreview/apply境界と最新内容の読戻しを使用します。復旧状態がある場合は停止して表示された手順に従います。直接Markdownを上書きして復旧検査を飛ばさないでください。APIの契約は `docs/webapp-spec.md`、データ契約は `docs/data-schema.md` を読みます。

会話レビューでは、実績を想像して追加せず、ユーザーが確認した事実とローカル記録を根拠にします。会社情報をCopilotへ送信できる範囲は会社の承認に従います。承認された範囲が不明なら、UIだけでレビューできます。
