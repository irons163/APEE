# APEE 驗證紀錄

最新日期：2026-10-13，Asia/Taipei。下方保留 2026-10-11 的歷史驗證結果。

## 結果

目前 **0.4.0 通過 77 項測試**，包含 3 項原生 sandbox 驗證，沒有跳過項目。已加入本機程序租約、異常中斷的明確恢復，以及未完成嘗試／殘留程序對新派工的阻擋。

**真實「模型自主修改 → 獨立驗證 → 提交 → Draft PR」已完成一次任務驗收**，成果為 [APEE PR #1](https://github.com/irons163/APEE/pull/1)。較早的 [codex-pool-manager PR #9](https://github.com/irons163/codex-pool-manager/pull/9) 保留為外部修補驗證與交付的證據。

## 2026-10-13：0.4 異常中斷恢復

執行：`APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v`

結果：**77 tests，26.916 秒，OK，沒有跳過項目**。完整本機輸出位於 `.apee/test-results-v0.4.log`。

10 項新增測試包含：

- 在準備、開發、測試三個階段以 SIGKILL 終止真實主程序；由 Runner 開始、分屬獨立程序群組的命令繼續存活，並保留嘗試的執行鎖。
- 主程序的全域鎖已釋放，但命令仍持有嘗試鎖時，恢復、重試與新派工受阻擋。原生 Codex sandbox 也實際通過這項鎖繼承測試。
- 命令結束後仍需明確 recover；原工作副本與嘗試記錄保留，重複恢復不新增事件、不增加嘗試數。retry 後在新的 checkout 完成真正的測試與提交。
- 即使目前嘗試已失敗，較早嘗試仍持有鎖時，也會阻擋重試。無法讀取的鎖採保留狀態處理。
- 沒有租約資料的舊版嘗試需要明確人工確認，恢復紀錄保存 `legacy_manually_checked` 來源；此選項無法略過新版遺失或仍被持有的鎖。
- 交付結果不明的任務保留 publishing，沿用 publish 核對既有 PR；已驗證、待審、合併與關閉的成果不被重排。
- 巢狀交付、逾時與程序啟動失敗的鎖釋放。

上述故障情境的 GitHub 邊界與開發內容採本機替身，Git clone、SQLite、檔案保留、程序中斷與原生 sandbox 均實際執行。這一版的開發完成後，重新檢查本機工作目錄、完整測試與 CLI 恢復查詢；沒有對既有真實任務執行恢復或重試。

## 2026-10-13：自主開發完整驗收

### 環境與任務

本機為 macOS arm64、Python `3.9.6`、Git `2.54.0`、Codex CLI `0.162.0-alpha.17.2`。GitHub CLI `2.102.0` 已從官方 release 安裝至使用者工具目錄並核對 SHA-256；使用者完成裝置登入後，`doctor` 全部通過，GitHub 確認帳號有 APEE repository 的讀寫權限。

這台電腦原先沒有 `.apee`，因此初始化新的 SQLite 任務資料，沒有重建或假定舊機器的執行紀錄已遷移。下方歷史章節中的 `.apee` 檔案路徑屬於當時的原始工作區。既有 PR #9 另以 GitHub CLI 核對仍為 OPEN、Draft，head 維持 `bfe3a907db96aba3fc124f1e5138b5319ff3f635`。

本次任務修正追蹤設定中的舊使用者絕對路徑。`tests` 與 `setup` 命令支援字面 `{config_dir}`，以設定檔所在目錄替換，原始 JSON 保留佔位符。允許修改範圍為 `apee/config.py`、`tests/test_engine.py`、`apee.json`、`README.md`。

### 成功的正式 CLI 流程

- 任務：`irons163/apee@bf98d79e2e3f864e`
- Run：`6aaadec006df4595bdd94f9916ed3261`
- 執行：`python3 -B -m apee run irons163/apee@bf98d79e2e3f864e --local-only`
- 起始提交：`8ec71b43d260da8ec2f61b4d6e252e9383659b37`
- 驗證與交付提交：`621b60c59778186b6c3dd8baf0ce1fd39ecb2f1c`
- 模型來源：`agent.backend = codex`，模型事件包含 `turn.completed`。
- 交付：`python3 -B -m apee publish irons163/apee@bf98d79e2e3f864e` 建立 [Draft PR #1](https://github.com/irons163/APEE/pull/1)。

這次使用正式 GitHub adapter 讀取 metadata 與 clone。Codex 執行器產生四個檔案的變更，引擎另外執行測試與獨立驗收、核對內容版本與工作範圍，再建立提交及審查包。GitHub 寫入後已確認 OPEN、Draft、base `main`，遠端 head 與驗證提交相同。`refresh` 成功更新任務為 `review`，回報「草稿待審；未回報檢查；尚無審查結論」。

### 驗證結果

1. APEE 原生 sandbox 內的套件：67 項，65 項通過、2 項原生 sandbox 測試依條件跳過。獨立搬移驗收另行通過。紀錄為 `.apee/runs/6aaadec006df4595bdd94f9916ed3261/test-1.log` 與 `test-2.log`。
2. 在同一驗證提交的 checkout 額外執行 `APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v`：**67 項全數通過，21.595 秒，沒有跳過項目**。完整輸出為 `.apee/runs/6aaadec006df4595bdd94f9916ed3261/native-full-suite.log`。
3. 開發前準備的獨立檢查實際執行 setup 與測試腳本，驗證含空白的設定目錄、不同工作目錄、參數不額外展開，以及 `project-add` 重寫後仍保存佔位符。原版會失敗，修正版通過。
4. Git whitespace、工作範圍、測試前後內容一致性及交付提交核對均通過。

模型事件、來源、patch、審查包與驗證輸出保留於該 run 目錄。遠端核對保存為 `.apee/pr-1-verified.json`。本機程式碼分支為 `codex/portable-validation-paths`，以同一驗證提交建立。

### 本次兩次嘗試的紀錄

第一次 `0744be824624472c8c0ae057f794663b` 在等待 GitHub CLI 登入期間使用既有真實 SSH origin 讀取與 clone，模型已產生修正並通過一般套件。獨立驗收腳本卻把 macOS 的 `/var` 與 `/private/var` 別名視為不同路徑，引擎因此停止交付並保存為失敗。

將獨立檢查的預期路徑正規化後，再確認原版仍會失敗、第一次修正版可通過。接著保留第一次紀錄，使用 `retry` 與正式 CLI 從新的 checkout 重新執行，完成上述第二次自主開發與交付。這份新狀態資料共記錄 2 次嘗試；目前沒有連續執行器運作。

## 2026-10-11：0.3 成果追蹤驗證

當時版本通過 65 項測試。真實 Codex 開發回合因用量上限失敗，當時完整自主流程尚未驗收，已交付成果為外部修補的 PR #9。

執行：`APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v`

結果：65 tests，68.804 秒，OK。完整本機輸出位於 `.apee/test-results-v0.3.log`。

16 項新增測試涵蓋：

- 沒有回報檢查、未知資料、等待、通過、失敗、取消與略過的區分；混合結果不會忽略失敗或未知項目。
- 審查要求修改、合併衝突，以及 PR 版本或目標變動；原本機測試與提交保持不變。
- 合併與未合併關閉分開記錄，跨重新開啟保留；明確更新可辨識 PR 重開，不自動重新開發。
- 固定 PR 編號與 repository 核對，錯誤或不完整資料不能覆寫最後成功的狀態。
- 讀取故障保留上次觀察並標示過期；恢復後更新，單一故障不影響其他成果。
- 相同觀察不新增重複事件，重複讀取錯誤不反覆輸出；檢查順序不同不算成果變動。
- 每輪 PR 查詢上限與輪替，停用專案不查詢；模型派工暫停時仍可追蹤成果。
- 真實 Git checkout、測試、提交與模擬 PR 交付之後，讀取狀態不會再次呼叫模型、推送或增加開發嘗試。

上述故障與遠端狀態轉換使用 GitHub 替身，未為測試去合併或關閉真實 PR。檢查 enum 另透過 GitHub GraphQL schema 實際核對。

### 真實 GitHub 讀取

以 `python3 -B -m apee refresh` 成功讀取既有 [PR #9](https://github.com/irons163/codex-pool-manager/pull/9)：

- OPEN、Draft；尚無審查結論，GitHub 未回報檢查。
- head 與原驗證提交 `bfe3a907db96aba3fc124f1e5138b5319ff3f635` 一致；分支與 base `main` 一致。
- 任務保留 `review`，總覽顯示「草稿待審；未回報檢查；尚無審查結論」。
- 原始本機測試紀錄保持不變；沒有把 GitHub 無檢查當成通過。

之後實際短跑 `work --interval 1 --max-cycles 2 --max-tasks 1 --local-only`，在 Codex 用量暫停下仍讀取同一 PR。兩輪正常結束，開發嘗試總數維持 2、狀態事件數維持 12，原提交與測試紀錄均保持一致。短跑已結束，目前沒有常駐執行器。

本機證據：`.apee/pr-refresh-v0.3.log`、`.apee/worker-smoke-v0.3.log`、`.apee/tracking-smoke-v0.3.json`。

## 0.2 連續執行驗證

執行：`APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v`

結果：49 tests，62.481 秒，OK。完整本機輸出位於 `.apee/test-results-v0.2.log`。

新增涵蓋：

- 本機程序以非零結束碼回報結構化用量錯誤時，當前任務等待、後續佇列保留。
- 額度、請求頻率與登入錯誤分類；任務文字與一般命令輸出不會觸發服務暫停。
- 暫停原因跨重新開啟保存，僅明確 `resume --codex` 解除；舊嘗試保留。
- 每日上限包含失敗，重新開啟不歸零，UTC 換日恢復容量。
- 另一個視窗可在執行中暫停後續任務；當前任務仍完成原定交付。
- 外部 patch 可在 Codex 暫停時使用，但仍受使用者暫停與每日上限限制。
- 連續模式受總嘗試上限限制，每輪重新載入設定，等待期間釋放任務執行鎖。
- 第二個連續執行器不能進入，另有手動任務時推遲該輪，重複空輪次不重複輸出訊息。
- SIGTERM 可停止等待並釋放鎖；開發中斷仍計入已開始的嘗試數。

以上額度故障情境由本機測試程序回傳固定事件；實際的 clone、測試、提交、SQLite 與程序中斷是真的執行。這些結果不代表真實模型已完成開發。

另以首跑的真實 `agent-events.log` 回放分類，確認辨識為 `usage_limit`。將這份既有證據保存成 Codex 派工暫停原因，未修改已交付任務的 `review` 狀態或既有嘗試。

在目前專案資料上實際執行 `work --interval 1 --max-cycles 2 --max-tasks 1 --local-only`：完成兩輪後以 `cycle_limit` 正常結束，新增嘗試為 0，保留額度暫停，原有每日嘗試數仍為 2。輸出位於 `.apee/worker-smoke-v0.2.log`；短跑已結束，目前沒有常駐執行器。

再次讀取 GitHub 確認 PR #9 仍為 OPEN、Draft，head 為 `bfe3a907db96aba3fc124f1e5138b5319ff3f635`，沒有 GitHub status checks 回報。原本 `codex-pool-manager` 工作目錄的既有修改保持原狀。

## 0.1 首次引擎驗證

執行：`APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v`

結果：31 tests，22.711 秒，OK。完整本機輸出位於 `.apee/test-results.log`。

涵蓋：

- 真實 Git checkout、測試前失敗／修補後通過、提交與審查包。
- Issue 與直接任務的去重、跨開啟週期的狀態保存。
- 已停用專案、派工標籤移除、來源更新與工作範圍限制。
- 測試失敗、無修改、測試期間改寫內容與驗證後修改。
- 單一 worker 鎖、執行期間查看狀態、逾時及子程序清理。
- 遠端分支衝突、PR 版本不符、寫入回應不明後的核對與去重。
- 外部 patch 同樣需要通過驗證，並記錄其來源。
- 原生 sandbox 的工作副本寫入／父目錄禁止寫入，以及測試禁止連線本機測試服務、明確啟用 setup network 後可連線。

模型事件與 GitHub 寫入的故障情境使用替身；Git 操作與 Python 測試是真的執行。原生 sandbox 情境使用本機 Codex CLI。

另外已實際驗證命令列的專案加入、任務入列、狀態保存、取消與中文總覽。GitHub 讀取已成功取得 `irons163` 的 331 個 repositories；其中 265 個不是 fork、也未封存。

## 首個真實試點

- Repository：`irons163/codex-pool-manager`
- 任務：修正 Sparkle appcast XML 的特殊字元輸出，並補上 parser 回歸測試。
- 起始提交：`6e64554a779b352c3cba6a364a7e7666866cdf5b`
- 交付提交：`bfe3a907db96aba3fc124f1e5138b5319ff3f635`
- 成果：[Draft PR #9](https://github.com/irons163/codex-pool-manager/pull/9)
- 已核對遠端為 OPEN、Draft、base `main`，head 與已驗證提交一致。
- 變更只包含 `scripts/generate_sparkle_appcast.sh` 與 `scripts/test_generate_sparkle_appcast.sh`。

先在未修改版本重現 XML parser 失敗，之後在 APEE 新的 checkout 套用修補，通過以下檢查：

1. 原有 localized notes 測試與新增的 XML round-trip 測試。
2. APEE 獨立的 `pilot_checks/check_appcast_xml.py`，驗證兩個架構、特殊字元、CDATA、網址與普通輸入。
3. Git whitespace 檢查，以及交付前的提交／工作副本核對。

所有 appcast 檢查使用合成資料與暫存輸出。未執行 Xcode App 測試、簽章、Release 發佈、合併或部署。原有本機 `codex-pool-manager` 工作目錄未由本次工作修改。

## 兩次嘗試的來源

第一次 `f6b9883711cd46ec97f5bd5af94ea1ee`：實際啟動 Codex CLI，收到用量上限錯誤及 `turn.failed`，任務保存為失敗，未交付程式變更。記錄位於 `.apee/runs/<run-id>/agent-events.log`。

第二次 `a6e26abba3e8481a821dfb0a65e37abb`：由目前對話完成修補，透過 `--patch` 交給 APEE。執行來源記為 `imported_patch`，兩項獨立命令通過後完成 GitHub Draft PR 交付。記錄、patch、測試輸出與審查包位於 `.apee/runs/<run-id>/`。

這證明驗證與交付流程已在真實 repository 跑通；模型自主修改仍需在可用的 CLI 用量下，以另一個明確任務補驗。本版不會在背景自動重跑或等待用量重置。
