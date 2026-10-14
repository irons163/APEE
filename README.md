# APEE — Autonomous Project Evolution Engine

把多個 GitHub 專案的開發任務排進同一個工作循環：**取得任務 → 獨立工作副本 → 自動開發 → 執行測試 → 交付待審變更**。

這是 **1.0 本機完整功能版**。提供原生 macOS 桌面管理、持久任務、背景排程、有限自動恢復、所有模型回合的 token／估算費用預算、獨立程式碼審查、有限測試／審查修復、GitHub CI 修復、跨專案目標與依賴、回饋及指標、有限改進規劃、成效比較與 macOS 成果通知。完整驗收範圍与證據見 [docs/COMPLETION.md](docs/COMPLETION.md)。歷史的 [APEE PR #1](https://github.com/irons163/APEE/pull/1) 為真實模型開發；[codex-pool-manager PR #9](https://github.com/irons163/codex-pool-manager/pull/9) 為外部修補驗證與交付，來源分開保存。

## 提供什麼

- 讀取 GitHub 帳號的專案清單，產生繁體中文總覽。
- 只處理設定中啟用的 repositories；每個專案有自己的優先順序、準備步驟與測試命令。
- 同步有 `apee:ready` 標籤的開放 Issues，也能直接輸入任務，不必先建立 Issue。
- 同一項 Issue 或同內容的直接任務不會重複入列。重開後保留任務、嘗試與狀態事件。
- 每項任務使用新的 checkout 與分支，保留原始任務、起始提交、模型記錄、測試輸出與 patch。
- 模型回合成功後，引擎另外執行設定的測試；測試失敗、沒有變更或驗證期間內容被修改，都不會交付 PR。
- 可先保留本機審查包，也可依專案設定自動推送任務分支並建立 Draft PR。
- GitHub 寫入回應不明時，重試交付會先核對既有 PR，避免重複建立。不會自動合併或部署。
- 可在前景連續檢查任務，限制輪數、總嘗試數與每日嘗試數；等待期間可查看狀態、加入任務、暫停或恢復。
- 可安裝目前使用者的 macOS 背景排程，每次喚醒只跑一輪，沿用暫停、執行鎖與每日嘗試上限；提供預覽、安裝、啟停、狀態與移除指令。
- 遇到已辨識的 Codex 額度、請求頻率或登入錯誤，保存原因並停止新的模型派工；重新開啟仍保留暫停原因。
- 主程序意外結束後，未完成嘗試會阻擋新的開發派工；確認執行鎖已釋放後，可保留原工作副本並明確恢復為中斷狀態，再使用新的 checkout 重試。
- 可明確啟用自動恢復：新版模型嘗試的鎖持續釋放達觀察期後，每項任務最多自動重試指定次數，每輪處理數另有上限。
- 保存 Codex JSONL 用量、模型與設定單價快照；依 UTC 日期檢查 token／估算 USD 預算，用量不明時停止新的模型派工。
- 原生桌面介面顯示任務、PR、用量與排程，提供暫停、恢復、預算設定、人工用量核對與恢復政策。
- 追蹤已交付 PR 的 GitHub 檢查、審查要求、衝突、合併及關閉狀態；總覽集中列出需處理的成果。

## 執行需求

1.0 的完整操作規則、預算與來源邊界，見 [目標、修復、規劃與通知](docs/WORKFLOWS.md)。舊設定未指定新政策時維持停用；範例啟用審查及有限 CI 修復，規劃與通知由桌面明確設定。

- macOS、Python 3.9+、Git、已登入的 GitHub CLI，以及已登入的 Codex CLI。
- 2026-10-13 本機驗證版本為 Codex CLI `0.162.0-alpha.17.2`、GitHub CLI `2.102.0`、Python `3.9.6`。需支援 `codex exec --ignore-user-config` 及 `codex sandbox --permission-profile`。
- 各專案的開發工具須在 `PATH` 中可執行；例如 Node、npm、Swift 或 Python。
- 桌面 app 需要 macOS 14+；編譯需要可用的 Xcode Command Line Tools／Swift 編譯器。引擎本身不需要 Swift。

APEE 本身只使用 Python 標準函式庫，從此目錄即可執行，不需要安裝套件：

```sh
python3 -m apee doctor
python3 -m apee discover irons163
python3 -m apee report
```

輸出位於 `.apee/overview.md`。專案目錄清單不會自動啟用專案。此工作區的 `apee.json` 已啟用 `irons163/codex-pool-manager` 試點，測試與 `allowed_paths` 限定 appcast 腳本。要處理其他功能，必須先設定對應的驗證方式與工作範圍。

## 加入第一個專案

下列是使用 npm 的範例，請改成實際 repository 與該專案真正的測試命令：

```sh
python3 -m apee project-add your-account/your-project \
  --test '["npm", "test"]' \
  --setup '["npm", "ci", "--ignore-scripts"]' \
  --setup-network \
  --publish-draft
```

這會把設定寫入 `apee.json`。`--publish-draft` 表示每次成功驗證後自動交付 Draft PR；省略時保存本機審查包。`--setup-network` 只允許準備步驟連網，模型 shell 與測試仍關閉網路。準備步驟不得修改受版本控制的內容。

測試命令是參數陣列，不經 shell 展開。需要多個檢查時，重複使用 `--test`。專案若已有自己的環境啟動腳本，可直接將腳本設定為命令。檢查須能正確回報失敗；引擎判定的「通過」是命令結束碼為 0，不代表自動證明了所有產品行為。

專案的 `tests` 與 `setup` 命令支援字面佔位符 `{config_dir}`，例如 `["/usr/bin/python3", "-B", "{config_dir}/pilot_checks/check_appcast_xml.py"]`。載入時，每個參數中的所有 `{config_dir}` 都會替換成設定檔所在目錄的絕對路徑；基準是所選設定檔，不是目前執行目錄或目標 repository 的 checkout。路徑即使含空白也仍是一個參數，不經 shell、不展開環境變數、`~` 或萬用字元；沒有佔位符的相對與絕對參數維持原值。原始 JSON 會保留佔位符，`project-add` 重寫設定時不會存入替換後的機器專屬路徑。

專案可在設定中指定 `allowed_paths`，列出允許修改的完整 repository 相對檔名；超出範圍的變更會停止交付。省略此設定才允許 repository 內其他檔案。針對局部功能的測試，應搭配相同範圍的檔案限制。

也可以參考 `apee.example.json` 建立 `apee.local.json`；存在時會優先使用，且此檔不納入版本控制。`--config /absolute/path/config.json` 可明確選擇設定。

## 交給 APEE 一個任務

直接加入具體要求與完成條件：

```sh
python3 -m apee enqueue your-account/your-project \
  --title '修正空搜尋結果的顯示' \
  --body '搜尋沒有結果時顯示空狀態，不拋出錯誤；補上空結果與正常結果的回歸測試。'

python3 -m apee run --local-only
```

長篇需求可用 `--body-file task.md`。`run` 處理佇列下一項，也可帶入輸出的完整任務 ID。`--local-only` 會讓本次成果留在本機。

如果使用 GitHub Issues，在目標 Issue 加上設定的 `apee:ready` 標籤後執行：

```sh
python3 -m apee cycle
```

每一輪會同步已啟用專案並最多執行 `max_tasks_per_cycle` 項任務，預設為 1。排序先看專案 priority（1 最高、5 最低），再看入列時間。Issue 開始執行前會重新讀取並保存版本；若在交付前變更或移除派工標籤，會留下成果並停止新的上傳。

## 連續執行、暫停與恢復

在前景執行有限輪次，每輪完成後等五分鐘，最多檢查 12 輪、開始 5 次開發嘗試：

```sh
python3 -m apee work --interval 300 --max-cycles 12 --max-tasks 5
```

上述數字也是預設值。加上 `--local-only` 可讓所有成果留在本機。輪數包含沒有任務、暫停或被其他執行器占用的輪次；嘗試數包含失敗。達到任一上限即結束，不會自動安裝背景服務或在關閉視窗後重新啟動。Ctrl-C 可停止連續模式；SIGTERM 使用相同清理流程。

設定中的 `daily_attempt_limit` 預設為 10，限制同一份狀態資料每天開始的嘗試數，包含 `run`、`cycle`、`work` 與外部 patch。以 **UTC 日期** 計算，台灣時間上午 8 點換日；失敗仍計入，重啟不歸零。另可設定下方的模型用量預算。

從另一個視窗控制派工：

```sh
python3 -m apee pause --reason '先審查目前成果'
python3 -m apee status
python3 -m apee resume
```

`pause` 立即保存暫停設定，即使任務正在執行也可操作；當前任務會完成驗證及原定交付，後續任務保持未開始。`resume` 只解除使用者暫停，不會直接執行任務。連續模式在下一輪讀取新設定；若未啟動連續模式，需另外執行 `run`、`cycle` 或 `work`。

若 Codex 回報已辨識的用量上限、請求頻率限制或登入失效，當前任務轉為 `waiting`，其他任務保持 `queued`。引擎保存全域暫停原因，不會逐一讓其餘專案重複失敗，也不會反覆呼叫服務探測額度。**登入成功不代表有可用額度**；錯誤訊息中的時間不會自動推測為可恢復的時區。

確認服務可用、或完成重新登入後，明確恢復模型派工：

```sh
python3 -m apee resume --codex
python3 -m apee work
```

`resume --codex` 會清除 Codex 暫停原因及使用者暫停，將等待任務重新入列，保留舊 checkout 和失敗記錄。它不會重跑已交付、已取消或一般開發失敗的任務。一般失敗仍需針對任務使用 `retry`。

連續模式每輪重新讀取設定，等待時不占用任務執行鎖；同一份狀態資料只能有一個連續執行器，手動開始任務也受執行鎖與每日上限限制。運行期間不能更換 `state_dir`，需先停止後重新啟動。

## macOS 背景排程

先以明確的設定路徑產生預覽；此步只輸出設定，不會登錄或啟動排程：

```sh
python3 -m apee --config /absolute/path/settings.json service render \
  --interval 300 --max-tasks 1 --output /tmp/apee-service-preview.plist
```

確認後安裝目前桌面登入使用者的 LaunchAgent，並可立即執行一輪：

```sh
python3 -m apee --config /absolute/path/settings.json service install --interval 300 --max-tasks 1
python3 -m apee --config /absolute/path/settings.json service start
python3 -m apee --config /absolute/path/settings.json service status
```

`install` 啟用後續排程，預設每 300 秒喚醒一次，間隔至少 60 秒；安裝當下不執行任務。`start` 啟用排程並要求立即執行，已在執行時保持當前工作。每次喚醒執行 `work --max-cycles 1`，預設最多開始 1 次嘗試；`--max-tasks` 可調整，但仍受每輪設定與持久的每日上限限制。`install` 加上 `--local-only` 可讓每次成果留在本機。更換服務選項需先移除再安裝。

這是登入使用者的背景排程，登出後不執行。電腦睡眠期間或前一輪仍在執行時錯過的間隔不會補跑；失敗不會透過 KeepAlive 立即反覆重啟。每次新喚醒都沿用原狀態資料，已保存的暫停、每日嘗試數與異常中斷阻擋持續有效。

安裝與啟動前會以固定環境檢查 Git、GitHub CLI、Codex CLI 及登入，再以一次性的真實 launchd 程序檢查程式、設定檔與狀態目錄的背景存取；檢查不執行開發任務，結束後移除。macOS 隱私權可能阻擋背景讀取 `Documents`、`Desktop` 等位置。若檢查失敗，將程式、設定與狀態資料放在背景可存取的位置（例如 `~/Developer`），或依 macOS 隱私權設定允許存取後，再安裝服務。只有前景 `doctor` 通過不足以證明背景可用。

服務固定 Python、程式工作目錄、設定檔與 `state_dir` 的絕對路徑。保留程式所在位置；搬移程式、Python 或狀態目錄後需重新安裝。更改 `state_dir` 時，舊排程會在建立新狀態或開始工作前停止該輪，避免意外重設每日上限。一般專案設定仍在每輪重新載入。

背景環境使用白名單並清除其他繼承變數，保留明確的工具 PATH、HOME、語系與 CLI 設定目錄；包含 `~/.local/bin`。不將 `GH_TOKEN`、`GITHUB_TOKEN` 或 `OPENAI_API_KEY` 存入服務檔，沿用 CLI 的本機登入。服務檔與管理紀錄以使用者專用權限保存。

```sh
python3 -m apee --config /absolute/path/settings.json service stop
python3 -m apee --config /absolute/path/settings.json service uninstall
```

`stop` 停止目前程序並停用後續排程，直到明確 `start` 或 `install`；正在開發的任務可能因此中斷，後續依 `recover`／`retry` 處理。若只想讓當前任務完成並暫停後續派工，使用 `pause`。`uninstall` 先停止服務，只移除已核對的服務檔與安裝紀錄，保留 SQLite、checkout、嘗試與輸出記錄。

管理紀錄位於 `~/Library/Application Support/APEE/services`，獨立於設定及任務資料；設定檔損壞或遺失後，仍可帶入原本的絕對路徑執行 `stop`／`uninstall`。同一份狀態資料不接受第二個服務設定。登錄回應不明時保留紀錄，重試會核對既有服務；無法核對的檔案或服務不會被覆寫或移除。

`service status` 顯示是否已安裝、載入、正在執行、是否需要重新安裝，以及最近一次程序結束碼。這些是排程狀態；任務成果使用 `status`、`show` 與總覽確認。標準輸出與錯誤位於 `state_dir/service/stdout.log`、`stderr.log`，路徑也會由服務狀態回報。

## 桌面介面

從預定長期保留的程式目錄與設定檔編譯並開啟 app：

```sh
python3 -B -m apee --config /absolute/path/settings.json desktop build --open
```

預設輸出為 `~/Applications/APEE.app`，可用 `--output /absolute/path/APEE.app` 指定新位置。已存在的輸出不會覆寫。這是本機 ad-hoc 簽署的 SwiftUI app，尚未提供公證與對外發佈安裝包。app 固定編譯時的 Python、程式與設定路徑，搬移後應重新編譯；正式背景環境宜放在 `~/Library/Application Support/APEE` 等可存取位置。

app 與 CLI 共用同一份 SQLite、設定、執行鎖與背景服務。每十秒更新本機快照，不會因此呼叫模型或查 GitHub；「更新 GitHub 近況」才會執行遠端查詢。可以查看任務與 PR、暫停／恢復派工、啟停已安裝的背景排程，以及修改預算與恢復政策。首次安裝服務及不明交付核對仍由 CLI 操作。

0.6.1 的「專案設定」可查看已設定專案、原始測試／準備命令、修改範圍及交付方式。「加入專案」接受 repository、優先順序與每行一個 JSON 參數陣列的命令，測試必填；準備步驟連網與自動交付 Draft PR 需明確勾選。新設定會完整驗證後以 0600 權限原子儲存，既有政策與 `{config_dir}` 模板保留；既有專案不覆寫，修改它仍需編輯設定檔。

工作總覽的「加入任務」可選擇啟用專案，輸入標題、需求與完成條件。加入時只寫入持久佇列，不直接呼叫模型；背景排程在後續輪次依暫停、容量與預算處理。即使引擎正在執行其他任務，也能加入新任務；相同內容只保留一筆。沒有啟用專案時會先提示設定，儲存失敗時表單與輸入保留；已有執行紀錄的任務可直接開啟記錄目錄。

「用量與預算」的數值預設留空，代表未啟用該項限制；儲存前後會保留專案與命令模板。模型名稱與三項單價由使用者明確設定，不會猜測訂閱費用。日期選單顯示今天與最近 30 個有紀錄的 UTC 日期，可以開啟原始用量記錄並人工核對；已有完整 token 用量的欄位不能覆寫。查看或核對歷史回合不會把它移入今天的預算，下方設定始終修改目前的派工政策。

`python3 -B -m apee snapshot` 提供同一份本機 JSON，適合其他本機介面整合；不帶人類可讀的總覽結尾。

快照包含 `projects`（保留原始命令模板）及 `budget_history`（最近 30 個有紀錄日期）。更早日期可用 `python3 -B -m apee budget status --date 2026-10-13` 查詢；這些讀取都不修改用量帳冊或派工日期。

## Token 與估算費用預算

可以只設定 token 預算，例如：

```sh
python3 -B -m apee configure --settings-json '{"budget":{"daily_tokens":1000000}}'
python3 -B -m apee budget status
python3 -B -m apee budget reconcile
```

`configure` 只接受 `model`、`budget` 與 `auto_recovery`，在取得執行鎖並驗證完整設定後原子儲存。傳入的 `budget` 會取代整個預算物件；`{"budget":{}}` 清除預算。`daily_tokens` 為正整數，最多 `10^12`。`daily_estimated_usd` 是最多九位小數的正十進位字串，設定時須同時有明確的 `model` 與 `budget.prices_per_million`。單價物件恰含 `input`、`cached_input`、`output` 三個非負十進位字串，單位都是每百萬 token 的 USD。

模型開始前先保存待核對用量；結束後從 `agent-events.log` 的 `turn.completed.usage` 讀取輸入、快取輸入與輸出。總 token 為輸入加輸出，快取是輸入的一部分，不重複加總；reasoning 已包含在輸出，不另加。金額以非快取輸入、快取輸入及輸出分別乘單價，使用十進位運算並向上保留九位小數。每次回合保存當時的模型與單價，之後更改設定不會回算既有費用。

以**模型開始的 UTC 日期**歸戶，跨日回合仍歸原開始日；舊版紀錄只能依原嘗試開始日匯入。這是本狀態目錄內 APEE 已記錄的用量，不包含其他 Codex 對話，也不代表整個帳號的訂閱額度、credits 或實際帳單。

**預算在開始新的模型回合前檢查；進行中的回合可能超額。** Codex 在回合完成時才回報完整用量，因此這不是上游硬性 token／帳單上限。達到任一已設定上限即阻止後續模型派工；暫停不會自動解除 Codex 的服務額度暫停。仍可追蹤 PR、交付已驗證成果，以及套用不呼叫模型的外部 patch。

缺少記錄、失敗／未完成的事件流或無法辨識的用量會保留為未知；啟用任一預算後，未知用量會阻擋當日新模型回合。費用預算另外要求當日回合都有可核對的估價；沒有模型／單價的舊紀錄及未支援定價的非零 cache-write 用量不會當成零。`budget reconcile` 匯入舊記錄，並只在嘗試鎖釋放後核對待完成項目；`cycle` 會自動執行相同核對。

取得實際依據後，可從桌面或 CLI 保存人工核對與原因，例如：

```sh
python3 -B -m apee budget record RUN_ID \
  --input-tokens 100 --cached-input-tokens 20 --output-tokens 10 \
  --estimated-usd 0.01 --reason '依已核對的供應商用量紀錄補登'
```

上例數字僅示範格式。人工核對需先確認嘗試鎖已釋放；舊版完整紀錄可在沒有租約時補登費用。完整且已知的 token 數不接受改寫，原值與前次核對都會保留。省略金額仍屬未定價，不能解除費用預算的阻擋。

## 追蹤已交付的成果

```sh
python3 -m apee refresh
python3 -m apee status
```

`refresh` 讀取所有已啟用專案的待審 PR，更新 GitHub 近況與本機總覽。`status`、`show` 和 `report` 只讀取本機保存的狀態；每份近況會標示最後成功讀取時間。

`cycle` 和 `work` 每輪先更新 PR，再決定是否開始開發。Codex 額度不足、使用者暫停派工或達到每日嘗試上限時，PR 追蹤仍可進行；這不呼叫模型，也不計為開發嘗試。`max_pr_checks_per_cycle` 預設為 20，每輪優先讀取最久未更新的成果，避免專案多時一直只查前幾筆。手動 `refresh` 可一次更新全部待審 PR，已停用專案不會查詢。

總覽會區分以下情況：

- GitHub 未回報檢查、狀態不明、等待執行、通過、失敗、取消、皆略過／中立。**沒有檢查不代表通過**。
- 草稿、審查已核准、要求修改、合併衝突、已合併、已關閉但未合併。
- PR 提交版本、來源分支或合併目標已改變。原本機測試紀錄保持不變，改過的版本不會沿用已驗證的標示。
- 更新失敗時保留最後成功讀取的資訊，明確標示過期；連線恢復後解除錯誤。某個 PR 讀取失敗不會阻止其餘 PR 更新。

連續模式只在近況改變、恢復或新錯誤出現時輸出對應結果；相同觀察不重複新增狀態事件。這些訊息只出現在本機輸出與總覽，不會向 GitHub 留言或發送外部通知。

合併與關閉分別記為 `merged` 和 `closed`，從一般輪詢移除；不會重新入列、重新開發或自動重開 PR。需要確認已關閉 PR 是否重新開啟時，明確指定任務：

```sh
python3 -m apee refresh 'your-account/your-project#123'
```

若 GitHub 回報已重開，該任務恢復 `review` 並繼續追蹤。PR 追蹤只是讀取遠端狀態；不會自動修復 CI、要求審查、核准、合併或部署。檢查結果僅代表 GitHub 回傳的項目，尚未驗證所有必需檢查、分支保護規則或合併資格。

## 異常中斷與恢復

主機重開或主程序被強制終止後，任務可能仍停在準備、開發或測試中。先查看本機未完成嘗試，再指定要處理的任務：

```sh
python3 -m apee recover
python3 -m apee recover 'your-account/your-project#123'
python3 -m apee retry 'your-account/your-project#123'
python3 -m apee run 'your-account/your-project#123' --local-only
```

省略任務 ID 的 `recover` 只檢查，不改任務狀態。指定 ID 後，命令取得本機執行鎖，確認原嘗試的鎖已釋放，才將任務記為 `interrupted`。工作副本、原需求、模型記錄與既有測試結果會保留；重新入列及執行分別由 `retry`、`run` 控制，新嘗試使用新的 checkout。

主程序與它直接啟動的命令共同持有每次嘗試的執行鎖。即使主程序消失，仍有命令持鎖時也會阻擋恢復、重試和新的開發派工；先讓相關程序結束，再執行 `recover`。`status` 與總覽會指出待恢復或需檢查的紀錄。恢復及查詢本身不會增加開發嘗試數，每日上限仍包含原先已開始的嘗試。

任務若停在 `publishing`，`recover` 會指向 `publish`，由它沿用原驗證提交並核對既有 PR。已完成驗證或交付的成果保持原狀。

0.3 及更早版本的嘗試沒有執行鎖紀錄。人工確認相關程序全部結束後，可明確使用 `recover TASK_ID --legacy`，紀錄會標示人工確認來源。這個選項僅適用舊版資料；新版的鎖遺失、無法讀取或仍被持有時，仍需先檢查及處理。

### 有上限的自動恢復

預設關閉。可從桌面啟用，或明確設定：

```sh
python3 -B -m apee configure --settings-json '{"auto_recovery":{"enabled":true,"max_retries":1,"cooldown_seconds":300,"max_per_cycle":1}}'
```

每輪先核對用量，再觀察啟用專案中停在 `preparing`、`running` 或 `testing` 的新版 Codex 嘗試。只有執行來源、租約版本與實際工作目錄一致，且所有歷史嘗試均沒有仍被持有或無法讀取的鎖，才處理。必須連續觀察鎖釋放至少 `cooldown_seconds`；重獲鎖、暫停、停用或時間倒退會重新計時。背景每五分鐘檢查時，恢復時間也受輪詢間隔影響。

確認後記為 `interrupted`，保存自動來源與觀察證據，再於容量允許時重新入列；下一次執行使用全新 checkout。每項任務的 `max_retries` 是跨重新啟動的累計上限，允許 0–5 次；0 只標記中斷。`cooldown_seconds` 允許 60–86400 秒，`max_per_cycle` 允許 1–20 項。自動重新入列不另計開發嘗試，真正開始時仍受每日嘗試、worker 總嘗試與模型預算限制。

一般開發失敗、外部 patch、來源不明或租約遺失的舊版資料不自動重試；`publishing` 仍由 `publish` 核對。Codex 暫停、用量未知或預算不足時保留已中斷成果，容量恢復後才可在後續輪次入列；使用者暫停時不改任務。每次恢復都保留原 checkout 與記錄，達到重試上限後交由人工處理。

## 看成果與處理失敗

```sh
python3 -m apee status
python3 -m apee show 'your-account/your-project#123'
python3 -m apee publish 'your-account/your-project#123'
python3 -m apee retry 'your-account/your-project#123'
python3 -m apee cancel 'your-account/your-project#123'
```

- `show` 顯示固定的任務、嘗試、工作副本與錯誤。
- `publish` 只接受已驗證的成果；會核對提交與工作副本，再建立或核對 PR。交付失敗後再次執行此命令，不會重跑模型。
- `retry` 適用於失敗、已中斷或等待中的開發任務；會保留舊嘗試，下一次 `run` 使用新的 checkout。單一任務重試不會解除全域 Codex 暫停。
- `cancel` 取消待執行或等待中的任務。執行中使用 Ctrl-C，會停止程序群組並保留紀錄。

也能接收既有開發對話提供的修補，在新的 checkout 中套用後執行相同驗證：

```sh
python3 -m apee run 'your-account/your-project#123' --patch changes.patch --local-only
```

這必須明確指定待執行的任務 ID；若上次執行失敗，先使用 `retry`。紀錄會標示 `imported_patch`，不會將外部修補算成模型自主產生的成果。

套用外部 patch 不使用開發模型回合；啟用獨立審查或有限修復時仍會呼叫模型，遵守模型暫停與預算。未啟用審查時，可在 Codex 暫停期間套用及驗證外部 patch；使用者暫停、每日上限及內容核對規則持續有效。已驗證成果的 `publish` 可獨立執行。

每次執行的 `.apee/runs/<run-id>/` 包含 `repo/`、固定需求、模型事件、測試記錄，以及通過後的 `changes.patch` 與 `review.md`。SQLite 保存所有任務與狀態事件。不要在工作仍執行時刪除這些資料。

## 驗證

```sh
python3 -B -m unittest discover -s tests -v
APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v
```

測試包含真實的 Git clone、故障範例修補後的 Python 回歸測試、審查包生成、去重、停用專案、Issue 變更、重複 worker、逾時清理、測試改寫內容、遠端分支衝突與 PR 寫入結果核對。模型和 GitHub 寫入採替身；原生 sandbox 測試另外驗證工作副本可寫、父目錄不可寫。

連續模式另驗證非零結束碼的額度錯誤、跨重啟暫停、明確恢復、每日上限換日、執行中暫停、設定重新載入、單一連續執行器、安靜等待及 SIGTERM 結束。額度情境使用固定錯誤的本機程序，不算真實模型自主開發。

恢復機制以真實 `SIGKILL` 終止準備、開發與測試中的主程序，驗證存活子程序仍持有執行鎖、阻擋恢復及新派工。另涵蓋原生 sandbox 鎖繼承、明確恢復、歷史嘗試、舊版資料、交付結果核對、保留工作副本與新 checkout 重試。

背景服務測試涵蓋設定預覽、環境與登入檢查、背景存取失敗、登錄回應不明、啟停與移除失敗、狀態目錄變更、重複設定及資料保留。`APEE_NATIVE_TESTS=1` 另以隔離的程式、空專案設定與狀態目錄，實際登錄 launchd、執行一輪、核對 SQLite，再停止並移除；不會啟動正式專案排程。

0.6 另外驗證實際 SIGKILL 後的觀察期、自動恢復的累計上限、重啟後的未完成入列、暫停與容量阻擋，以及原生 sandbox 子程序存活時不恢復。用量測試涵蓋快取去重、十進位估價、失敗仍計用量、未知用量阻擋、單價快照、UTC 歸戶、舊版匯入與人工稽核。設定測試核對無效值不改檔、命令模板保留與單一 JSON 快照；原生桌面測試實際編譯、簽署並由 app 的診斷入口讀取隔離後端。測試單價與模型用量使用明確的合成資料。

PR 追蹤驗證檢查狀態分類、版本漂移、要求修改、衝突、合併／關閉／重開、連線故障與恢復、跨重啟保存、觀察去重及有上限的輪詢。故障情境使用 GitHub 替身；真實試點則另有實際讀取證據。

## 目前限制

- 開發執行維持循序處理，跨專案政策限制同時等待交付的專案數；沒有跨主機 worker。
- 規劃依已設定的目標與證據提出有限候選；自動入列必須由目標政策明確啟用。交付停在待審，不自動 merge 或部署。
- 只接 GitHub.com 同一 repository 的任務分支；尚未支援 Enterprise、fork PR 或 submodules 的特殊準備流程。
- 專案準備與測試使用 Codex sandbox，允許 checkout 及系統暫存目錄寫入。這不是容器／VM；適用於你信任的程式庫，不能當作任意不可信程式碼的隔離服務。
- 模型沿用 CLI 登入，忽略個人 `config.toml` 並明確指定 workspace-write；未指定 `model` 時使用 CLI 預設。專案與管理政策仍可能限制執行。原始碼與任務內容會交由設定的模型服務處理。
- 模型預算控制後續派工，進行中的回合可能超額；估算費用不等於帳單，未設定數值時不啟用限制。另限制每次模型時間、每個命令時間與嘗試數。
- Codex 全域暫停依據已辨識的結構化服務錯誤；未知錯誤保存為一般失敗。跨日不會自動解除模型服務的暫停，需要明確恢復。
- 正常中斷會記錄；強制終止後可手動恢復，或明確啟用有上限的自動恢復。依據本機可繼承的執行鎖判定，原工作副本及紀錄保留；尚無跨主機租約或任意脫離程序群組的背景程序追蹤。
- discovery 最多讀取 1,000 個 repositories；Issue 同步使用分頁。

## 設計與介面依據

產品方向見 [docs/PRODUCT.md](docs/PRODUCT.md)。Codex 執行介面依據 [官方 non-interactive mode 文件](https://learn.chatgpt.com/docs/non-interactive-mode) 與本機 CLI help；測試隔離依據 [permission profiles 文件](https://learn.chatgpt.com/docs/permissions)。PR 交付使用 [GitHub CLI 的 Draft PR 介面](https://cli.github.com/manual/gh_pr_create)。

模型用量欄位依據同一份 non-interactive 文件的 `turn.completed.usage`。訂閱額度、credits 與費用差異參照 [官方 pricing 文件](https://learn.chatgpt.com/docs/pricing)；APEE 不內建或猜測費率，使用明確設定的單價快照。

PR 追蹤使用 [GitHub CLI 的 PR JSON 欄位](https://cli.github.com/manual/gh_pr_view)，並以 GitHub GraphQL schema 的 `CheckConclusionState`、`CheckStatusState`、`StatusState` 核對檢查分類；與 [GitHub CLI 的檢查結果分類](https://cli.github.com/manual/gh_pr_checks) 一樣區分通過、失敗、等待、略過與取消。
