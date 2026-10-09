# DailyReview の昨日・今日表示

DailyReview の「変化を整理する」に、Asia/Tokyo の昨日と今日の Progress を表示します。「昨日（YYYY-MM-DD）」「今日（YYYY-MM-DD）」の順に分け、それぞれ記録がない場合も空表示を残します。

対象日は `occurred_on`（変化が発生した日）です。登録日時や Progress の ID の日付は分類に使いません。たとえば翌朝03:00に登録した昨日の変化は、昨日の欄に表示されます。

各記録の「編集」と既存のプレビュー・保存手順は維持します。発生日を昨日から今日へ変更して保存すると、一覧も今日の欄へ移動します。新規記録の日付の初期値は引き続き今日です。WeeklyReview は従来どおり東京日付の月曜〜日曜を表示し、編集リンクから Daily を開きます。

## ローカル検証

```sh
python3 -m unittest tests.test_progress_ui_runtime tests.test_progress_report
GTD_E2E_PLAYWRIGHT_MODULE=/path/to/playwright python3 tests/run_daily_review_ui_e2e.py --output /tmp/daily-review-qa
```

Node ランタイム検証は東京の深夜前後、年跨ぎ、閏日、Weekly の範囲、発生日による分類、日別の空表示と編集入口を確認します。画面検証は合成データだけの loopback サーバー、新規 headless Chromium、外部通信遮断で実施します。1280px、390px、320px で分類・日付編集後の再表示・空表示・Weekly からの編集・横溢れ・JavaScript エラーを確認します。

既存 `progress.js` へ表示処理を分離し、`app.js` のサイズ予算を増やしていません。変更済み静的ファイルを既存 PWA が取得できるよう shell cache は v73 に更新します。

## 受入手順

1. PC とスマホで DailyReview を開き、東京日付の昨日と今日の欄があることを確認する。
2. 昨日発生・今日登録の合成記録が昨日に、今日発生・以前登録の合成記録が今日に分類されることを確認する。
3. 片方または両方が空でも、それぞれの空表示があることを確認する。
4. 編集で発生日を別の欄の日付へ変更し、保存後の移動と内容を確認する。
5. WeeklyReview の一週間の一覧と、Daily での編集リンクを確認する。
6. 更新前 PWA の会社 Windows ブラウザ／実機スマホは、公開後に新 cache へ更新されることを確認する。今回のローカル検証では実機・本番配信は未検証。

この変更は自動登録の時刻・登録処理・Scheduled Task・Focus の操作 UI・通知設定を変更しません。ローカル検証と、公開・本番反映は別の操作です。
