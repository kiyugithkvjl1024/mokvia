"use strict";

window.ReviewHelp = Object.freeze({
  daily: {
    title: "Daily Review（5〜10分）",
    purpose: "いまの状況に合わせて気がかりと直近の予定を確認し、必要な変更を反映します。朝は今日の約束を整え、夜は残ったことと次の行動を確認します。全リストの棚卸しはWeeklyで行います。",
    alt: "Inbox・直近の予定を確認する従来のDaily Review参考図。現在の操作手順は下のテキスト参照",
    flow: ["変化・気がかりを整理", "予定と次の行動を確認", "必要ならひとこと", "確認状況を保存して終了"],
    steps: [
      {label: "変化・気がかりを整理", purpose: "現実と記録のずれを減らすため。", action: "新しい気がかりはInboxへ、完了や変更は該当Taskへ反映します。実績台帳に残したい事実だけProgressへ任意で記録します。記録済み事項の転記は不要です。"},
      {label: "予定と次の行動を確認", purpose: "今と次の約束を実行可能にするため。", action: "Inbox、Doing、今日から7日内の予定・期限を見ます。必要なら振分け、対応予定日、次の行動を画面内で更新します。朝は今日、夜は次に取り組むことを整えます。"},
      {label: "必要ならひとこと", purpose: "次に役立つ気づきだけ残すため。", action: "『進んだこと／引っかかり／次の一手』の必要分を1行だけ。例：『返信待ち。返答が来たら資料を更新する』。Notesは任意で、空欄でも保存できます。"},
      {label: "確認状況を保存して終了", purpose: "何を確認したか残し、見直しを閉じるため。", action: "確認した項目にチェックし、最後に『確認状況を保存』を押して保存完了を確認します。変更不要なら何も更新しなくて構いません。"},
      {label: "時間がない日の2分版", purpose: "最低限の信頼を保つため。", action: "新しい気がかりをInboxへ入れる → 次の予定と期限を確認 → 今すぐ必要な一件だけ更新 → 確認した項目を保存。任意のProgressとNotesは省略できます。"},
    ],
    example: ["朝：期限の近いTaskを今日の予定に入れる。夜：終わったTaskを完了し残りの次の行動を決める。変更なし：確認した項目だけチェックしてNotes空欄で保存する。", "全タスクを毎日見直す、長い日記を書く、記録済みの変更をNotesへ転記する。", "『進んだ／詰まった／次の一手』やif–thenを日次の任意メモに使うのはmokviaの提案です。Dラボの週次実践をGTD公式の日次必須手順とは扱いません。"],
    stuck: "判断できない一件は、次に誰へ何を確認するか決めてInboxやTaskへ残します。全部解決するまで終えない運用にしません。",
    done: "気がかりと直近の予定・次の行動を確認し、必要な変更を反映して、確認状況の保存完了が見えた。ProgressとNotesの記入は任意です。",
    back: "/reviews/weekly?tab=daily",
  },
  weekly: {
    title: "Weekly Review（30〜45分）",
    purpose: "約束・Project・Goalの全体を信頼できる状態に戻します。",
    alt: "ClearからWeekly Reviewを記録するまでの6段階の流れ",
    flow: ["Clear", "Current", "Creative", "Goal整合", "必要な変更を反映", "Weekly Reviewを記録"],
    steps: [
      {label: "Clear", purpose: "未整理の気がかりを信頼できる一覧へ戻すため。", action: "Inboxを確認し、捨てるもの、次の判断が必要なもの、予定へ置くものを分けます。"},
      {label: "Current", purpose: "現在の約束と滞留を現実に合わせるため。", action: "Next、Doing、Waiting、Scheduled、Somedayを確認し、止まったものや期限を見直します。"},
      {label: "Creative", purpose: "Active Projectを次の物理的行動へ接続するため。", action: "NextがないProjectとSomedayを見て、必要な次の行動や再開条件を決めます。"},
      {label: "Goal整合", purpose: "今週の行動が大事な方向から外れていないか確かめるため。", action: "PurposeとActive Goalを見て、続ける約束と見直す約束を決めます。"},
      {label: "必要な変更を反映", purpose: "気づきをメモだけで終わらせず、mokviaを信頼できる状態にするため。", action: "決めたTask、Project、日付、紐づけの変更を該当画面で反映し、結果を確認します。"},
      {label: "Weekly Reviewを記録", purpose: "判断と変更の結果を次週へ引き継ぐため。", action: "確認状態とNotesを見直し、Weekly Reviewを保存して保存完了を確認します。"},
    ],
    example: ["NextのないProjectに一つの物理的行動を置く。", "Project名だけを『進める』にする。", "Waitingは相手と次の確認日を残せば十分です。"],
    stuck: "全てを直そうとせず、今週の信頼を落とす一件から変更します。",
    done: "Clear、Current、Creative、Goal整合を確認し、必要な変更を反映して、Weekly Reviewの保存完了を確認できた。",
    back: "/reviews/weekly?tab=weekly",
  },
  progress: {
    title: "Progressの詳しいやり方",
    alt: "人が小さな変化を記録し、Weeklyで意味・紐づけ・証拠を補完してmokviaへ蓄積する。mokviaは月次実績台帳Markdownへ整形し、人が用途別に選択・言い換えて、実績報告・職務経歴書・面接・ライフプランへ分岐する流れ。",
    flow: ["人が小さな変化を記録する", "mokviaが原子的な事実として蓄積する", "Weeklyで意味・紐づけ・証拠を補完し、蓄積へ戻す", "mokviaが月次実績台帳Markdownへ整形する", "人が用途別に選択・言い換える", "実績報告・職務経歴書・面接・ライフプランへ分岐する"],
    steps: [
      {label: "小さな変化を記録", purpose: "後で思い出そうとせず、気づいた変化を事実として残すため。", action: "変化に気づいた日、またはその日を振り返るときに、前と比べて変わったことを一文で書き、発生日を選びます。"},
      {label: "原子的な事実として蓄積", purpose: "用途が変わっても組み替えられる材料にするため。", action: "記録するたびに、一つの記録には一つの変化だけを書きます。複数の変化があれば、記録を分けます。"},
      {label: "Weeklyで補完", purpose: "記録時に分からなかった意味や背景を、振り返ってから補うため。", action: "週ごとのWeekly Reviewを行うときに、その週のProgressを見ます。紐づけ先、メリット・学び、証拠は必要な記録だけ補います。毎件を埋める必要はありません。"},
      {label: "月次実績台帳へ整形", purpose: "散らばった事実を月単位で見渡すため。", action: "月末の振り返りや報告の準備をするときに、月次実績台帳Markdownを表示し、候補を一覧で確認します。"},
      {label: "人が用途別に選択・言い換え", purpose: "同じ事実を相手と目的に合う説明へ変えるため。", action: "実績を伝える必要があるときに、使う記録だけを選び、誇張せず伝わる表現に整えます。"},
      {label: "報告や転職活動へ利用", purpose: "事実と証拠に基づく説明をいつでも作れるようにするため。", action: "実績報告、職務経歴書、面接、ライフプランを準備するときに、必要な記録を必要な形で使います。"},
    ],
    example: ["問い合わせテンプレートを更新した。必要なら「返信時の迷いが減った」「更新後のテンプレートURL」を補足する。", "仕事を頑張った。", "メリット・学びは良くなった点や次回に使う気づき、証拠はURL・数値・成果物など後から確かめられる根拠です。どちらも記録時に不明なら空欄で、Weekly Reviewで必要なものだけ補います。"],
    stuck: "大きく書こうとせず、一日の変化を一文に分けます。意味や用途は、週ごとのWeekly Reviewで必要なものから補えます。",
    done: "一つの変化を日付付きの事実として記録し、任意の補足を無理に埋めていない。Weekly Reviewでは必要な記録だけを補えている。",
    purpose: "小さな変化を、あとから説明できる原子的な事実として残します。記録時に分からない意味・紐づけ・証拠は、Weekly Reviewで振り返ってから補えます。",
    back: "/reviews/weekly",
  },
});

window.ReviewHelpUI = {
  layout(e, kind, editing = false) {
    const daily = kind === "daily", guide = e.reviewGuide, progress = document.getElementById("progress-review"), past = document.getElementById("review-history"), home = guide && guide.parentNode;
    if (home && progress && past) {
      if (daily) { home.insertBefore(progress, guide); home.insertBefore(e.reviewDisclosure, past); if (e.reviewSaveStatus) home.insertBefore(e.reviewSaveStatus, past); }
      else { home.insertBefore(guide, progress); home.insertBefore(past, e.reviewDisclosure); }
    }
    const recordDetails = document.getElementById("review-record-details"); if (recordDetails) recordDetails.open = editing || !daily;
    const progressEntry = document.getElementById("progress-entry"); if (progressEntry) { progressEntry.hidden = !daily; if (e.progressCancel && !e.progressCancel.hidden) progressEntry.open = true; }
    const summary = e.reviewDisclosure?.querySelector("summary"), heading = document.getElementById("review-guide-heading"), help = document.getElementById("review-notes-help");
    if (summary) summary.textContent = daily ? "3. 確認を終えて保存" : "＋ Weekly Reviewを記録";
    if (heading) heading.textContent = daily ? "2. 予定と次の行動を確認" : "Weekly Reviewの進め方";
    if (progress) { document.getElementById("progress-review-heading").textContent = daily ? "1. 変化を整理する" : "今日の変化を記録"; document.getElementById("progress-purpose").textContent = daily ? "新しい気がかりや変更はInbox・Taskへ反映。実績台帳に残したい事実があれば、ここで記録します（任意）。記録済み事項の転記は不要です。" : "小さな変化を、あとで使える事実として残します。"; }
    if (!editing) { e.reviewBodyLabel.textContent = daily ? "ひとこと振返り（任意）" : "メモ"; e.reviewBody.rows = daily ? 3 : 8; e.reviewBody.placeholder = daily ? "例：返信待ち。返答が来たら資料を更新する。" : ""; e.reviewPreview.textContent = daily ? "確認状況を保存" : "作成"; }
    if (help) help.hidden = !daily || editing;
  }
};
