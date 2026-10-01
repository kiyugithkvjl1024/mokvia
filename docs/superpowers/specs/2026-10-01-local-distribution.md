# mokvia 配布仕様

承認済みの目的: Windows会社PCだけで既存GTDの主要操作を利用する。Docker上のLinux/Python、localhost Web UI、Markdown正本を維持する。会社PCのNode導入は不要。

コードとデータは分離する。データはLinux永続volume、バックアップは会社PC内の承認済み場所。Git同期・Google同期・Codex自律処理・Tailscaleは含めない。外部通信は導入時の依存取得と承認済みCopilot手動補助に限る。Teamsは初回範囲外。

既存UIの階層・Task/Project・Review・Progress・作業セッション・時間配分を維持する。ローカルカレンダーは日/週/月を表示し、予定Taskの追加/変更を既存preview/applyで行う。予定と実績は別表示、日本時間。外部Calendarは同期しない。

ローカル決定的処理で休憩期限完了、着手条件解放、25分超過等を判断する。Windows通知補助は表示のみ。PowerShell制限時はブラウザ通知を使い、タブ閉時に動かないことを説明する。スリープ復帰時は現在状態を再判定して通知を最大1件へまとめる。

Windows側公開は127.0.0.1:24873だけ。コンテナ内の待受はDockerネットワーク内だけ。Host/Origin、競合検査、ロック、復旧境界を保持する。多重起動を拒否し停止時に永続volumeを消さない。

配布物は汎用コード・導入資料・架空テストだけ。個人データ、履歴、秘密、権利未確認画像を持ち込まない。公開ライセンスは権利者承認済みのMIT。検証はPython/API/状態遷移/カレンダー/Docker/保存復元/UI/通知fallback。Windows表示は実環境での受入を明記する。
