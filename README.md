# ETF 配息自動追蹤（網頁版）

網頁管理持股 → GitHub 自動排程抓配息 → 推 Telegram。

## 檔案
- `index.html` — 網頁介面（填/看持股、看上次配息），部署到 GitHub Pages
- `etf_dividend.py` — 後端腳本（純標準庫，零依賴）
- `holdings.json` — 持股資料（網頁會自動寫入，初始可留範例）
- `last_result.json` — Actions 跑完寫回，網頁讀取顯示（自動產生）
- `.github/workflows/notify.yml` — 每月 5 號 09:00(台灣) 自動執行

## 運作流程
網頁(改持股) → GitHub API 寫回 holdings.json → Actions 排程讀取 → 抓配息 → 推 Telegram + 寫回 last_result.json → 網頁顯示結果

## 設定步驟（詳見對話）
1. 建 Public repo `etf-dividend`，推上全部檔案
2. Settings → Pages → 部署 main 分支 → 取得網址
3. 建 Telegram bot，設兩個 Secrets：TELEGRAM_TOKEN、TELEGRAM_CHAT_ID
4. 建 fine-grained PAT（此 repo 的 Contents 讀寫），在網頁「設定」貼入
5. 網頁填持股 → 儲存到 GitHub
6. Actions → Run workflow 測試

## 注意
- repo 為 Public，holdings.json 內容公開可見（已與使用者確認）
- PAT 只存瀏覽器 localStorage，不進 repo
- TWSE 現金股利欄位名稱偶有調整，金額抓到 0 時看 stderr 警告回報欄位名
