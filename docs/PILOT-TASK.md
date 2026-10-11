# 首個試點：codex-pool-manager

使用者已選擇 `irons163/codex-pool-manager` 作為首個試點。根據專案現況，選擇一項可獨立驗證、無須操作帳號資料的發佈工具修補：**讓更新資訊中的特殊字元不會破壞 Sparkle appcast XML**。

## 任務規格

修正 `scripts/generate_sparkle_appcast.sh` 中的 XML 輸出：

- App 名稱與 release 標題中的 `&`、`<`、`>`、引號應正確保留。
- 下載網址與 localized release notes URL 的多個查詢參數應在解析後保持原值。
- 發佈說明包含 `]]>` 時仍產生合法 XML，解析後文字不可遺失。
- 既有普通輸入、arm64／x86_64 兩份 feed、版號與檔案長度維持原意。
- 在既有測試腳本補上使用 XML parser 的回歸測試，不能只搜尋字串。

範圍只限 `scripts/generate_sparkle_appcast.sh` 與 `scripts/test_generate_sparkle_appcast.sh`。不涉及 Swift App、Xcode 設定、帳號、憑證、簽章或發佈作業。

## 驗收

1. 既有測試腳本通過。
2. 新增的 parser 回歸測試通過。
3. APEE 保存的獨立驗收腳本 `pilot_checks/check_appcast_xml.py` 通過。
4. 審查 diff 只包含上述兩個腳本。

所有驗證使用合成資料，在暫存目錄產生 XML；不建立 GitHub Release、不合併 PR、不部署應用程式。
