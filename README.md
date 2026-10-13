# APEE — Autonomous Project Evolution Engine

把多個 GitHub 專案的開發任務排進同一個工作循環：**取得任務 → 獨立工作副本 → 自動開發 → 執行測試 → 交付待審變更**。

這是 **0.3.0 引擎原型**。已有可執行的命令列、持久任務佇列、有限的連續執行模式、Codex 執行介面、原生 sandbox 測試、Draft PR 交付與交付後狀態追蹤。2026-10-13 已透過 [APEE PR #1](https://github.com/irons163/APEE/pull/1) 完成一次真實「模型自主修改 → 獨立驗證 → 提交 → Draft PR」驗收，修正設定搬移後的驗證路徑，67 項測試全數通過。較早的 [codex-pool-manager PR #9](https://github.com/irons163/codex-pool-manager/pull/9) 則驗證外部修補的套用與交付。詳見 [驗證紀錄](docs/VALIDATION.md)。

## 第一版提供什麼

- 讀取 GitHub 帳號的專案清單，產生繁體中文總覽。
- 只處理設定中啟用的 repositories；每個專案有自己的優先順序、準備步驟與測試命令。
- 同步有 `apee:ready` 標籤的開放 Issues，也能直接輸入任務，不必先建立 Issue。
- 同一項 Issue 或同內容的直接任務不會重複入列。重開後保留任務、嘗試與狀態事件。
- 每項任務使用新的 checkout 與分支，保留原始任務、起始提交、模型記錄、測試輸出與 patch。
- 模型回合成功後，引擎另外執行設定的測試；測試失敗、沒有變更或驗證期間內容被修改，都不會交付 PR。
- 可先保留本機審查包，也可依專案設定自動推送任務分支並建立 Draft PR。
- GitHub 寫入回應不明時，重試交付會先核對既有 PR，避免重複建立。不會自動合併或部署。
- 可在前景連續檢查任務，限制輪數、總嘗試數與每日嘗試數；等待期間可查看狀態、加入任務、暫停或恢復。
- 遇到已辨識的 Codex 額度、請求頻率或登入錯誤，保存原因並停止新的模型派工；重新開啟仍保留暫停原因。
- 追蹤已交付 PR 的 GitHub 檢查、審查要求、衝突、合併及關閉狀態；總覽集中列出需處理的成果。

## 執行需求

- macOS、Python 3.9+、Git、已登入的 GitHub CLI，以及已登入的 Codex CLI。
- 2026-10-13 本機驗證版本為 Codex CLI `0.162.0-alpha.17.2`、GitHub CLI `2.102.0`、Python `3.9.6`。需支援 `codex exec --ignore-user-config` 及 `codex sandbox --permission-profile`。
- 各專案的開發工具須在 `PATH` 中可執行；例如 Node、npm、Swift 或 Python。

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

設定中的 `daily_attempt_limit` 預設為 10，限制同一份狀態資料每天開始的嘗試數，包含 `run`、`cycle`、`work` 與外部 patch。以 **UTC 日期** 計算，台灣時間上午 8 點換日；失敗仍計入，重啟不歸零。這是工作量限制，不是 token 或費用上限。

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

外部 patch 不需要模型額度，因此可在 Codex 暫停期間使用；仍受使用者暫停、每日上限及相同驗證規則限制。已驗證成果的 `publish` 可獨立執行。

每次執行的 `.apee/runs/<run-id>/` 包含 `repo/`、固定需求、模型事件、測試記錄，以及通過後的 `changes.patch` 與 `review.md`。SQLite 保存所有任務與狀態事件。不要在工作仍執行時刪除這些資料。

## 驗證

```sh
python3 -B -m unittest discover -s tests -v
APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v
```

測試包含真實的 Git clone、故障範例修補後的 Python 回歸測試、審查包生成、去重、停用專案、Issue 變更、重複 worker、逾時清理、測試改寫內容、遠端分支衝突與 PR 寫入結果核對。模型和 GitHub 寫入採替身；原生 sandbox 測試另外驗證工作副本可寫、父目錄不可寫。

連續模式另驗證非零結束碼的額度錯誤、跨重啟暫停、明確恢復、每日上限換日、執行中暫停、設定重新載入、單一連續執行器、安靜等待及 SIGTERM 結束。額度情境使用固定錯誤的本機程序，不算真實模型自主開發。

PR 追蹤驗證檢查狀態分類、版本漂移、要求修改、衝突、合併／關閉／重開、連線故障與恢復、跨重啟保存、觀察去重及有上限的輪詢。故障情境使用 GitHub 替身；真實試點則另有實際讀取證據。

## 目前限制

- 目前是循序執行的本機引擎，支援有限的前景連續模式；沒有桌面介面、背景常駐排程、跨主機 worker、功能探索或自動產品決策。
- 自主範圍為執行已提供的任務與追蹤已交付成果。尚無獨立模型程式碼審查、GitHub CI 自動修復或自動 merge。
- 只接 GitHub.com 同一 repository 的任務分支；尚未支援 Enterprise、fork PR 或 submodules 的特殊準備流程。
- 專案準備與測試使用 Codex sandbox，允許 checkout 及系統暫存目錄寫入。這不是容器／VM；適用於你信任的程式庫，不能當作任意不可信程式碼的隔離服務。
- 模型沿用 CLI 登入，忽略個人 `config.toml` 並明確指定 workspace-write；未指定 `model` 時使用 CLI 預設。專案與管理政策仍可能限制執行。原始碼與任務內容會交由設定的模型服務處理。
- 目前限制每次模型時間、每個命令時間、每輪／連續執行總嘗試數與每日嘗試數，尚未提供可保證的 token／費用上限。失敗不會無限重試。
- Codex 全域暫停依據已辨識的結構化服務錯誤；未知錯誤保存為一般失敗。跨日不會自動解除模型服務的暫停，需要明確恢復。
- 正常中斷會記錄；若主機斷電或程序被強制殺死，未完成狀態會保留，避免自動重播。需先檢查工作副本與殘留程序；目前未提供自動恢復這種嘗試的功能。
- discovery 最多讀取 1,000 個 repositories；Issue 同步使用分頁。

## 設計與介面依據

產品方向見 [docs/PRODUCT.md](docs/PRODUCT.md)。Codex 執行介面依據 [官方 non-interactive mode 文件](https://learn.chatgpt.com/docs/non-interactive-mode) 與本機 CLI help；測試隔離依據 [permission profiles 文件](https://learn.chatgpt.com/docs/permissions)。PR 交付使用 [GitHub CLI 的 Draft PR 介面](https://cli.github.com/manual/gh_pr_create)。

PR 追蹤使用 [GitHub CLI 的 PR JSON 欄位](https://cli.github.com/manual/gh_pr_view)，並以 GitHub GraphQL schema 的 `CheckConclusionState`、`CheckStatusState`、`StatusState` 核對檢查分類；與 [GitHub CLI 的檢查結果分類](https://cli.github.com/manual/gh_pr_checks) 一樣區分通過、失敗、等待、略過與取消。
