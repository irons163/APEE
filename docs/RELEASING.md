# 1.0.1 公開發佈候選

2026-10-16：公開版移除個人預設專案、採用使用者選定的 MIT 授權，並加入內含 Python 的可攜式 app。1.0 的功能驗收維持原始歷史；公開下載需要另外完成以下封裝與簽章條件。

## 可重建封裝

在 macOS 14+、已安裝 Xcode Command Line Tools 的原始碼 checkout 執行：

```sh
python3 -B tools/build_release.py --architecture arm64 --output dist/APEE-1.0.1-arm64
```

腳本從 `release/python-runtimes.json` 的固定 URL 下載 CPython 3.12.15，先核對 SHA-256，再解壓。快取位置為 `~/Library/Caches/APEE/release-inputs`。`--architecture x86_64` 可交叉編譯 Intel 版本，但必須另在 Intel Mac 實際驗收後才可發佈該架構；Apple Silicon 上通過不代表 Intel 已測試。

也可自行提供已核對的 runtime：

```sh
python3 -B -m apee desktop package \
  --runtime-archive /absolute/path/python-runtime.tar.gz \
  --runtime-sha256 VERIFIED_SHA256 \
  --architecture arm64 --output dist/APEE-1.0.1-arm64
```

輸出包含 `APEE.app`、ZIP、`SHA256SUMS`、`release.json` 及安裝說明。app 只包含固定程式、空白初始設定、Python 與原始授權聲明；不讀取建置者的 `apee.local.json`，不保存其 PATH、HOME、CLI 登入或執行紀錄。路徑在使用者開啟 app 時才依 bundle 與使用者目錄決定。

程式與授權檔只從 Git 追蹤的檔案清單複製，不掃入 ignored 設定、virtualenv 或其他私人檔案。封裝包含 `source-payload.json` 的檔案雜湊，`release.json` 核對封裝內容與 checkout 是否一致；未知來源、工作目錄未提交或內容不符時，不會標示為可正式公開。

未指定簽章時，輸出明確命名為 `-preview.zip`，`notarized` 與 `public_app_ready` 為 `false`。這種封裝可供維護者驗收，不能標示為已通過 Gatekeeper 的正式下載版。

## 正式簽章與 Apple 公證

需要可用的 **Developer ID Application** 憑證，以及事先儲存在本人 Keychain 的 `notarytool` 設定檔。Apple ID 密碼、私鑰及 app-specific password 不應寫入 repository、命令紀錄或聊天。

```sh
python3 -B tools/build_release.py --architecture arm64 \
  --signing-identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notary-profile APEE-NOTARY \
  --output dist/APEE-1.0.1-arm64-notarized
```

流程先由內而外簽署 Python、原生 extension 與 app，啟用 hardened runtime 與時間戳，再用 `notarytool` 提交。只有 Apple 回報 `Accepted`、staple／validate 成功及 `spctl --assess` 通過，才輸出沒有 `-preview` 的 ZIP。不接受其他類型憑證，也不在公證失敗後降級為可公開版本。

Apple 的要求見 [Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)。目前這台建置 Mac 查到 **0 個可用簽章身分**；正式公證尚未執行，不能只因本機 ad-hoc 驗證通過就宣稱完成。

## 驗收與公開條件

```sh
APEE_NATIVE_TESTS=1 \
APEE_RELEASE_RUNTIME=/absolute/path/verified-python-runtime.tar.gz \
python3 -B -m unittest discover -s tests -v
```

原生封裝驗收真正編譯、簽署、壓縮並解壓 app；刪除建置資料夾後啟動，再搬移一次，核對首次空白狀態、既有預算保留、環境不包含測試憑證、無排程或模型嘗試，以及搬移後簽章仍有效。

公開原始碼前，核對完整 Git refs／歷史、預設設定、授權、文件及即將公開的提交。模式掃描只提供初步憑證檢查，不能視為完整安全審查。

正式 app 發佈還必須符合：

- 發佈提交已合併並通過回歸；`source_dirty` 為 `false`，archive SHA-256 對應同一封裝。
- 指定架構的原生封裝與首次啟動驗收通過；使用者安裝與更新文件一致。
- `release.json` 中 `license_included`、`notarized` 及 `public_app_ready` 均為 `true`。
- GitHub Release 的版本、原始碼提交、安裝包與 `SHA256SUMS` 相符。

在上述條件達成前，保留 Draft PR／Release 及預覽標示。尚未設定正式簽章不妨礙公開 MIT 原始碼，但不能把預覽 app 當成已公證下載版。
