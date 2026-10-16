# APEE — Autonomous Project Evolution Engine

把多個 GitHub 專案的任務排進同一個循環：**取得任務 → 獨立 checkout → 自動開發 → 測試與審查 → 交付 Draft PR → 追蹤成果**。

目前為 **1.0.1 公開發佈候選版本**。1.0 的完整功能已驗收並合併；1.0.1 整理公開預設值、安裝文件及內含 Python 的可攜式 macOS app。公開下載狀態以 [發佈紀錄](docs/RELEASING.md) 與 Release 的 `release.json` 為準；`-preview.zip` 是尚未公證的驗收包。

## 功能

- 多專案持久佇列、Issue／直接任務去重、獨立工作副本與指定測試。
- 全新唯讀模型審查、有限測試／審查修復，以及同一 APEE PR 的有限 GitHub CI 修復。
- 前景循環與 macOS launchd、暫停恢復、執行鎖、有限異常恢復、每日嘗試上限。
- 所有模型回合的 token 與估算費用紀錄、UTC 每日預算、未知用量阻擋及人工核對。
- 跨專案目標與任務依賴、回饋與指標、有限候選規劃、接受後核對成效。
- 原生 SwiftUI 桌面管理、專案與任務入口、歷史用量、服務管理與 macOS 成果通知。

工作只針對你明確啟用的專案與政策。模型完成後仍須通過引擎指定的測試、範圍與內容版本核對。預算控制新模型回合的開始，進行中的回合可能超額；估價不代表訂閱帳單。未提供真實指標時，不宣稱產品成效。

## 安裝

完整說明見 [安裝與首次啟動](docs/INSTALL.md)。桌面需要 macOS 14+；可攜式 app 內含 Python，不需要 Swift 或建置者的原始碼目錄。Git、GitHub CLI 與相容的 Codex CLI 由使用者自行安裝及登入。

正式下載版須完成 Developer ID 簽章與 Apple 公證。驗收預覽包會明確標示 `notarized: false`、`public_app_ready: false`；目前本機 ad-hoc 簽章不能視為對外公證完成。

從原始碼執行需要 Python 3.9+，編譯本機桌面 app 另需 Xcode Command Line Tools：

```sh
git clone https://github.com/irons163/APEE.git
cd APEE
cp apee.example.json apee.local.json
python3 -B -m apee doctor
python3 -B -m apee snapshot
python3 -B -m apee desktop build --open
```

`apee.json` 沒有任何專案；範例專案保持停用，也不自動建立 PR。新使用者不會連到作者的試點。可攜式 app 在第一次開啟時建立自己的空白設定，已存在的設定及歷史會保留。

## 加入專案與任務

桌面可加入、編輯專案，設定測試、準備命令、優先順序、修改範圍及 Draft PR 政策。也能使用 CLI：

```sh
python3 -B -m apee project-add your-account/your-project \
  --test '["npm", "test"]' \
  --setup '["npm", "ci", "--ignore-scripts"]' \
  --setup-network

python3 -B -m apee enqueue your-account/your-project \
  --title '修正空搜尋結果' \
  --body '沒有結果時顯示空狀態，補上空結果與正常結果的回歸測試。'

python3 -B -m apee run --local-only
```

命令使用 JSON 參數陣列，不經 shell 展開。`{config_dir}` 以設定檔所在目錄替換，原始字面模板仍保留。`allowed_paths` 可限制允許修改的 repository 相對檔名。啟用 `--setup-network` 只開放準備步驟連網，模型 shell 與測試保持限制。

省略 `--publish-draft` 時保留本機審查包；只有明確啟用的專案才自動推送並建立 Draft PR。直接任務可用 `--body-file` 提供長篇需求；Issue 派工使用專案指定的標籤，預設 `apee:ready`。

## 日常操作

```sh
python3 -B -m apee status
python3 -B -m apee refresh
python3 -B -m apee cycle
python3 -B -m apee work --interval 300 --max-cycles 12 --max-tasks 5
python3 -B -m apee pause --reason '先審查成果'
python3 -B -m apee resume
```

每項任務保留需求、起始提交、每次嘗試、模型記錄、測試結果與 patch。一般重試建立新的 checkout；交付回應不明時先核對既有 PR。遠端 head、來源或 base 改變時不沿用舊驗證，也不覆寫他人提交。

桌面可以編輯專案、加入任務、管理目標與依賴、保存回饋及指標、接受或拒絕候選、調整審查／CI／規劃政策、管理服務與通知，以及查看或核對歷史用量。

設定 `daily_tokens` 可限制當日新回合；費用預算另需明確的模型與 input／cached_input／output 每百萬 token USD 單價。不推測模型或價格，完整已知 token 不接受覆寫。用量以模型開始的 UTC 日期歸戶，不包含其他 Codex 對話。

背景服務由桌面明確安裝及啟動，也可使用：

```sh
python3 -B -m apee service install --interval 300 --max-tasks 1
python3 -B -m apee service start
python3 -B -m apee service status
python3 -B -m apee service stop
python3 -B -m apee service uninstall
```

安裝先核對工具、登入與真實背景存取。服務每次一輪，沿用暫停、租約與每日容量；登出或睡眠時不補跑。移除保留設定、SQLite 及成果；更新或搬移 app 前先移除舊服務，再從新位置安裝。

遇到可辨識的模型登入、額度或頻率問題會保存原因並停止新的模型派工。確認服務可用後，明確 `resume --codex`。一般異常中斷可先 `recover` 檢查，再針對任務恢復或重試；完整規則見 [工作流程](docs/WORKFLOWS.md)。

## 驗證與發佈

```sh
python3 -B -m unittest discover -s tests -v
APEE_NATIVE_TESTS=1 python3 -B -m unittest discover -s tests -v
```

原生測試使用隔離設定，驗證 sandbox、程序租約、launchd 與 SwiftUI app。可攜式封裝測試另需指定已核對的 `APEE_RELEASE_RUNTIME`；測試不派工正式專案或呼叫模型。

- [完整功能驗收](docs/COMPLETION.md)：1.0 的逐項證據與已知邊界。
- [驗證歷史](docs/VALIDATION.md)：保留真實模型、GitHub CI、原生操作及各版本測試結果。
- [發佈程序](docs/RELEASING.md)：可重建的 runtime 來源、SHA-256、簽章、公證與公開條件。
- [第三方聲明](THIRD_PARTY_NOTICES.md)：內含 Python 與相依元件的授權來源。

原始碼採用 [MIT License](LICENSE)。內含的第三方元件保留各自授權。

目前只支援 macOS 與 GitHub.com 同一 repository 的任務分支；不自動合併、部署或發佈產品。GitHub 未回報檢查不代表檢查通過；付款／支出容量造成工作未啟動時保存平台診斷，不以程式修復掩蓋。
