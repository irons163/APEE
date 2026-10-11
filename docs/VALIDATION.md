# APEE 驗證紀錄

日期：2026-10-11，Asia/Taipei。

## 結果

目前 **0.3.0 通過 65 項測試**，包含原生 sandbox 驗證，沒有跳過項目。在 0.2 連續執行控制之上新增 PR 檢查、審查、版本及合併／關閉追蹤。已實際完成的 GitHub 成果仍為 `codex-pool-manager` 外部修補的 Draft PR #9。

Codex CLI 的真實開發回合因用量上限失敗，因此尚不能將本版視為已通過「模型自主修改 → PR」完整驗收的系統。本輪 0.3 沒有重新消耗模型額度探測服務，也沒有新增 PR、合併、部署或安裝背景服務。

## 0.3 成果追蹤驗證

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
