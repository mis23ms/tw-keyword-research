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
