# GTD Local 製品仕様

会社PC内のDocker Linuxコンテナとブラウザで完結するMarkdownベースのGTDです。アプリ実行中の外部API、AIモデル、クラウド保存、外部カレンダー同期はありません。会社承認済みCopilotは手動補助として利用します。

## 保持する機能

- Capture、Inbox、Clarify、Focus、Task検索、Project、Direction、Roadmap/Cycle、Progress、時間配分、日次・週次Review。
- Purpose → Vision → Goal → Project → Task、およびAreaによる分類。自動で階層を推測しません。
- Taskの依存関係、開始可能日時、作業開始・中断・完了・継続Task、実績修正。
- 固定5分休憩と開始可能Taskのローカル自動解除。
- 日・週・月カレンダー（JST）。予定・行動日・締切と作業実績を区別し、予定の追加・変更はpreview後に明示保存。

Markdownを正本とし、最新hashと検証付きpreview/applyで変更します。結果不明時は再送せず読戻しと復旧判断を行います。Reviewの判断や文章整理は手動で、AIによる定期無人処理は含みません。

## 運用

Docker named volumeを永続データに使います。配布ソース内へ会社データを保存しません。停止後に会社承認済みローカル場所へtarバックアップし、復元前には現データもバックアップします。Gitは配布コードの取得に限り、会社データの送信には使いません。

監視は10秒間隔で現在状態を再読込します。通常Taskの25分超過・進行中なしなどの通知はJST 06:00–22:00に最大5分ごと、休憩終了は別途通知します。再開時に過去通知を大量再生せず最新状態へ集約します。Windows通知ヘルパーを標準とし、使用できない場合は開いたブラウザタブから通知します。OSの通知許可や集中モードの影響を受けます。

Teamsは初回配布に含めません。会社のクラウド禁止方針に対するCopilot/Teamsの承認範囲は会社に確認します。ユーザーが承認した範囲以上の外部送信は行いません。

導入・受入確認は [SETUP-WINDOWS.md](SETUP-WINDOWS.md)、Copilot開始文は [COPILOT.md](COPILOT.md) を参照してください。
