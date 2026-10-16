# 安裝與首次啟動

APEE 需要 macOS 14 或更新版本。可攜式 app 內含 Python 與 APEE 引擎，不需要另裝 Python 或 Swift，也不依賴建置者的工作目錄。

## 使用公開下載版

正式下載版必須有 Developer ID 簽章與 Apple 公證，且 `release.json` 的 `public_app_ready` 必須為 `true`。檔名有 `-preview` 的封裝只供維護者驗收，尚未完成公證，不是正式下載版。

1. 下載符合 Mac 架構的 ZIP 與 `SHA256SUMS`，在下載目錄執行 `shasum -a 256 -c SHA256SUMS`。
2. 解壓縮，將 `APEE.app` 放到 `~/Applications` 或 `/Applications`，再開啟。
3. 自行安裝 Git、[GitHub CLI](https://cli.github.com/)、[Codex CLI](https://learn.chatgpt.com/docs/codex/cli)，分別完成 `gh auth login` 與 `codex login`。APEE 使用你的本機登入，不向發佈者取得帳號。
4. 在「專案設定」加入自己的 repository，設定準備與測試命令；從「加入任務」輸入具體需求與完成條件。
5. 在「用量與預算」設定需要的限制。模型與單價未設定時，費用保持未定價。
6. 從「政策與服務」明確安裝及啟動背景服務；需要通知時，再按「允許並啟用桌面通知」。

初次啟動只建立空白設定，不啟用任何專案、不登入服務、不安裝排程，也不呼叫模型。未啟用專案前，可以開啟所有本機管理介面。

Codex CLI 必須支援 `exec --ignore-user-config`、`sandbox --permission-profile` 及 `sandbox --include-managed-config`；[官方命令參考](https://learn.chatgpt.com/docs/developer-commands?surface=cli) 描述這些介面。使用原始碼時可執行 `python3 -B -m apee doctor` 核對工具與登入。

## 設定、歷史與更新

可攜式 app 的預設設定為 `~/Library/Application Support/APEE/instances/default/apee.local.json`，任務與用量依該設定的 `state_dir` 保存。已存在的設定不會被新版本預設值覆寫；無法解析時顯示錯誤並保留原檔。

更新前備份設定與 SQLite。使用背景服務時先從舊 app 停止並移除服務，完成替換後再由新 app 安裝服務。服務移除會保留任務與歷史。背景程序固定安裝當時的 app 路徑；搬移 app 後也需要重新安裝服務。

維護者可用絕對路徑的 `APEE_INSTANCE_ROOT` 指定隔離測試資料夾。這個變數不寫入 app，也不寫入服務環境。不要以它切換仍在執行中的正式排程。

## 從原始碼執行

需要 Python 3.9+、Git，以及已登入且相容的 GitHub／Codex CLI；自行編譯 SwiftUI app 另需 Xcode Command Line Tools。

```sh
git clone https://github.com/irons163/APEE.git
cd APEE
cp apee.example.json apee.local.json
python3 -B -m apee doctor
python3 -B -m apee snapshot
python3 -B -m apee desktop build --open
```

範例專案保持停用。從原始碼編譯的 `desktop build` 維持本機專用模式；它固定目前的 Python 與設定路徑，並非可攜式對外安裝包。
