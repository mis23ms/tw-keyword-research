# tw-keyword-research

自動關鍵字研究爬蟲 — GitHub Actions 版

## 功能

- 每週三、週六台灣時間 12:00 自動執行
- 搜尋 5 組投資相關關鍵字，擷取摘要精華
- 報告保留 30 天，自動清理舊報告
- GitHub Pages 可直接瀏覽 index + 報告

## 結構

```
config/keywords.json    ← 關鍵字設定（改這裡即可）
scripts/auto_research_gh.py  ← 主程式
reports/YYYY-MM-DD_slug/     ← 報告輸出
  summary.md                 ← 摘要報告
  items.json                 ← 結構化資料
index.md                     ← 報告索引
```

## 關鍵字設定

編輯 `config/keywords.json`，每組可設定：

| 欄位 | 說明 | 範例 |
|------|------|------|
| keyword | 搜尋關鍵字 | `"TSMC earnings presentation"` |
| lang | 語言過濾 | `"en"` / `"zh"` |
| region | DuckDuckGo 地區 | `"us-en"` / `"tw-zh"` |
| timelimit | 時間範圍 | `"d"` / `"w"` / `"m"` / `"y"` |
| target | 目標篇數 | `5` |
| minlen | 最短字數 | `800` |

## 手動執行

GitHub → Actions → Auto Keyword Research → Run workflow

## 啟用 GitHub Pages

Settings → Pages → Source: Deploy from a branch → Branch: `main` / `/(root)` → Save

v7 五大改動
1. 有 1 篇就出報告，0 篇才 SKIP
>= 1 PDF → 出報告（標示 "Only X PDF(s), target=Y"）
== 0 PDF → SKIP（但仍產 stub，列前 5 個被拒 URL + 原因）
首頁不會再空白。
2. 抽不出文字 → 保留為 link-only
PDF 確認 → 提取文字成功 → 📄 full-text
PDF 確認 → 提取失敗/太短 → 🔗 link-only（保留 URL/title/size/status）
你丟 NotebookLM 時，有連結+標題也能用。
3. PDF 三重判定
Content-Type 含 application/pdf? → ✅
URL 結尾 .pdf? → ✅
前 5 bytes 是 %PDF? → ✅
三個都不符 → ❌ 拒絕
4. Timeout 45s + retry 1 次
McKinsey/GS 等常擋的站不會卡住整個流程。超時就記入 rejected 清單。
5. Index 改進
每次 run 存 _latest_run.json，index 標記 🆕 當次結果，顯示 📄/🔗 數量。

v8 為什麼會比之前穩
根本問題：DuckDuckGo 的 site:A OR site:B 根本不可靠
之前每一版都在 query 裡塞 site:X OR site:Y OR site:Z，DuckDuckGo 常常無視這些 OR，結果不是跑到垃圾站就是 0 results。
v8 解法：搜尋歸搜尋，domain 過濾交給程式
keywords.json 新增 allowed_domains 欄位
  ↓
R1: DDG 搜「HBM CoWoS advanced packaging filetype:pdf」（不帶 site:）
  → 拿到 25 個候選
  → 程式碼比對 allowed_domains → 只留 tsmc.com / semi.org / deloitte.com 等
  ↓
R2: 如果不夠，逐一嘗試 site:DOMAIN（單一 site: DDG 能處理）
  → site:semi.org HBM advanced packaging filetype:pdf
  → site:tsmc.com HBM advanced packaging filetype:pdf
  → ...最多試 5 個 domain
單一 site: DuckDuckGo 可以處理，多個 OR 它不行。v8 拆開來做。
其他改進
問題v8 解法TSMC investor.tsmc.com 403allowed_domains 包含 tsmc.com + sec.gov（ADR 20-F/6-K）ETF 抓到財富管理報告allowed_domains 鎖 globalxetfs.com / roboglobal.com / ishares.comPDF 0 charstrafilatura → pdftotext (poppler) 雙重提取；失敗仍保留為 🔗 link-only403 卡住全流程fast-fail，不重試，記入 rejected整頁空白>= 1 PDF 就出報告，0 才 SKIP（附 rejected 清單你看得到原因）minlen 太嚴降到 100（link-only 不受此限）

三個檔案都要更新

scripts/auto_research_gh.py → 全選刪除 → 貼 v8
config/keywords.json → 全選刪除 → 貼新版（有 allowed_domains）
.github/workflows/auto_research.yml → 全選刪除 → 貼新版（加了 poppler-utils）

其實 v8 的 Python 程式碼已經符合 min_required=1（count == 0 才 SKIP），計數邏輯也沒問題。這次真的只改 keywords.json 一個檔案。整理了系統改進方案與優化策略。Presented fileLet me summarize what changed clearly.Done只改 config/keywords.json 這一個檔案就好。Python 和 YAML 都不動。

改了什麼（2 處）
1. Semiconductor：移除 ishares.com
diff  "allowed_domains": [
    "semi.org", "imec-int.com", "ieee.org", ...
    "tsmc.com", "asml.com", "micron.com", "samsung.com",
-   "skhynix.com", "nvidia.com", "amd.com", "intel.com", "ishares.com",
+   "skhynix.com", "nvidia.com", "amd.com", "intel.com",
    "spglobal.com", "idc.com", "gartner.com"
  ]
ETF factsheet 不會再混進半導體組。
2. TSMC IR：拿掉 investor.tsmc.com，只留 tsmc.com + sec.gov
之前 R2 會嘗試 site:investor.tsmc.com → 永遠 403。現在：

R1 broad search 找 TSMC quarterly results earnings filetype:pdf
R2 site-by-site 只試 site:tsmc.com 和 site:sec.gov（ADR 20-F/6-K）
investor.tsmc.com 是 tsmc.com 的子網域，domain_matches() 仍會接受它的 URL——但不會主動去戳它觸發 403


