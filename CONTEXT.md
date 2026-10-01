# mokvia

個人のコミットメントを信頼できる行動体系として扱うための共通語彙です。

## Language

**Task**:
行動、コミットメント、または明確化前の入力を表す1件の記録。
_Avoid_: Todo, Item

**Update**:
既存entityを更新する会話workflow。これは新しいstatusではなく、新しいentityでもない。
_Avoid_: Task Status, Entity Type

**Inbox**:
意味や次の行動がまだ十分に明確化されていないTaskの集合。
_Avoid_: Backlog, Unsorted Tasks

**Next Action**:
望む結果へ進むために、現時点で実行可能な次の具体的行動。
_Avoid_: Priority Task, Step

**Planned**:
Projectに紐づけて保存済みで、まだ実行候補にしないTaskの状態。計画ツリー内の将来工程にも、ツリーに含めない単独Taskにも使える。外部待ちを表すWaitingとは異なり、計画ツリーへの所属そのものは表さない。
_Avoid_: Waiting, Backlog, Priority

**計画ツリー**:
Project内で前後関係または明示的な並びを持つTaskの構造。Taskのstatusとは独立しており、`project_position`、primary `depends_on`、または同一Projectの別Taskからprimary parentとして参照されることで所属を判定する。
_Avoid_: Planned Tasks, Priority Order

**単独Task**:
Projectには属するが、計画ツリーには含めないTask。作成時は`planned`または`next`で、後のstatus変更後も単独Taskとしての所属を維持する。Project詳細では`done`以外の全statusで表示する。
_Avoid_: Other Task, Unplanned Task

**Doing**:
今まさに着手中で、注意を向けているTaskの状態。
_Avoid_: In Progress, Active Task

**Waiting**:
他者からの返答や外部の出来事を待っており、自分だけでは次へ進めないTaskの状態。
_Avoid_: Blocked, Pending

**Scheduled**:
実行する日時または時間帯を明示的に約束したTaskの状態。
_Avoid_: Due, Calendar Task

**Someday**:
実行を約束していないが、将来再検討する価値があるTaskの状態。
_Avoid_: Maybe, Low Priority

**Project**:
完了までに複数の行動を要する、明確な完了条件を持つ成果。必要に応じて、
開始日と終了日の対で「目安期間（見込み期間）」を持つ。これは期限や優先度、
自動的な順序付け、状態変更を表さない。
_Avoid_: Category, Task Group

**Purpose**:
何のために行動するかと、判断で守る原則を表す最上位の方向性。現在の記録は1件だけを持つ。
_Avoid_: Goal, Project, Mission Statement Collection

**Vision**:
Purposeに沿って実現したい、数年先の暮らしや仕事の具体的な全体像。
_Avoid_: Goal, Forecast, Progress Target

**Goal**:
複数のProjectや行動に方向を与える中長期の成果。
_Avoid_: Vision, Priority

**Roadmap Outcome**:
1つのGoalに属し、複数のProjectを束ねてNow / Next / Laterの順序を与える成果。実行期間そのものではない。
_Avoid_: Goal, Project, Cycle, Priority

**Cycle**:
開始日と終了日を持ち、最大2件のRoadmap Outcomeへ短期的にコミットする期間。成果やProjectそのものではない。
_Avoid_: Sprint Task, Roadmap Outcome, Review

**Now**:
現在のactive Cycleに含まれるRoadmap Outcomeの集合。独立した永続priorityではない。
_Avoid_: Priority, Doing, Active Status

**Next / Later**:
active Cycleに含まれないactive Roadmap Outcomeを並べる、順序付きの2つのRoadmapレーン。
_Avoid_: Task Status, Priority

**Area**:
完了させる対象ではなく、継続して維持する責任領域。維持基準と手動確認の事実を持つ。
_Avoid_: Project, Category, Status

**Resource Allocation Plan**:
期間内にどの対象へ何分または何ベーシスポイントを配分するかを、ユーザーが明示した計画。`time_allocation_plan` Entityとして保存し、実績や優先度を自動生成しない。
_Avoid_: Priority, Forecast, Score

**Allocation Axis**:
配分と実績を集計する対象の軸。`area`、`goal`、`roadmap_outcome`のいずれかで、Taskの帰属は現在の階層事実から求める。
_Avoid_: Category, Ranking

**Attribution**:
作業セッションを現在のArea、Goal、Roadmap Outcome、または帰属なしの`none`へ割り当てる事実。過去の階層を復元せず、`none`も実績分母に含める。
_Avoid_: Historical Reconstruction, Priority

**Daily Review**:
直近の予定、着手中のTask、Inboxを整え、その日に信頼できる行動候補を確認する短周期の見直し。
_Avoid_: Daily Report, Stand-up

**Weekly Review**:
すべてのTask、Project、Goalを一巡し、体系全体への信頼を回復する定期的な見直し。
_Avoid_: Weekly Report, Retrospective

**Progress**:
昨日までと比べて何が変わったかを、一度だけ保存する日々の事実記録。Reviewや月次実績台帳そのものではなく、それらが参照する一次記録。
_Avoid_: Task Completion, Daily Report, Achievement

**Context**:
Taskを実行できる場所、道具、状況などの条件。
_Avoid_: Category, Tag

**AI Proposal**:
手動で利用するCopilot等が提示し、ユーザーの明示的な確認を受けるまで記録へ適用されない変更候補。
_Avoid_: Automation, Decision
