# 📊 tw-keyword-research

GitHub Actions 自動化關鍵字研究爬蟲 — 每週自動從機構級來源抓取 PDF 報告。

## 功能

- **每週六 12:00 台灣時間** 自動執行（GitHub Actions cron）
- **5 組產業主題**，鎖定機構級 PDF（Big 4、投行、研究機構、產業協會）
- **三重 PDF 驗證**：Content-Type + URL hint + `%PDF` header
- **雙重文字提取**：trafilatura → pdftotext (poppler) fallback
- **link-only 保留**：PDF 確認但抽不出字 → 保留連結 + metadata
- **30 天去重**：URL + 標題 normalize 比對，避免重複
- **自動清理**：超過 30 天的舊報告自動刪除
- 也可手動觸發 `workflow_dispatch`

## 主題涵蓋

| 主題 | 來源範例 |
|---|---|
| Semiconductor: CoWoS / HBM / Advanced Packaging | IEEE, PwC, TSMC, ASML, Samsung, IMEC |
| AI / Cloud Infrastructure | Deloitte, McKinsey, Gartner, IDC, NVIDIA, IEA |
| Robotics ETF Factsheets (BOTZ/ROBO/IRBO) | Global X, iShares, ROBO Global |
| Aerospace & Defense | Deloitte, RAND, CSIS, SIPRI, KPMG |
| BioPharma / Life Sciences | IQVIA, Evaluate, FDA, EY, McKinsey |

## 報告結構

```
reports/
├── 2026-02-08_semiconductor-cowos-hbm-advanced-packaging/
│   ├── summary.md          # 摘要 + rejected 清單
│   └── items.json          # 結構化資料（URL, domain, 摘要, metadata）
├── 2026-02-08_ai-cloud-infrastructure/
│   ├── summary.md
│   └── items.json
└── ...
index.md                    # 首頁索引（自動產生）
```

每份報告包含：
- 📄 **Full-text**：成功提取文字的 PDF（附摘要）
- 🔗 **Link-only**：確認為 PDF 但無法提取文字（保留連結 + 檔案大小）
- ❌ **Rejected**：被過濾的候選清單（附原因，方便 debug）

## 技術架構

```
DuckDuckGo Search (ddgs / duckduckgo_search)
  ↓
R1: broad keyword + filetype:pdf
R2: site-by-site (逐一嘗試 allowed_domains)
  ↓
Domain filter (per-job allowed_domains whitelist)
  ↓
Blacklist filter (60+ 低品質站點)
  ↓
30-day URL + title dedup
  ↓
fetch PDF (45s timeout, 403 fast-fail)
  ↓
Triple PDF verification
  ↓
Text extraction: trafilatura → pdftotext fallback
  ↓
Language check → Year check → Summary
  ↓
reports/YYYY-MM-DD_slug/summary.md + items.json
  ↓
index.md (scan ALL report folders)
```

## 設定

### `config/keywords.json`

每組 job 包含：
- `keyword`：搜尋關鍵字（含 `filetype:pdf`）
- `label`：顯示名稱
- `allowed_domains`：白名單網域（程式碼過濾，非搜尋引擎語法）
- `target`：每組目標 PDF 數量
- `minlen`：全文提取最低字數

### `requirements.txt`

```
ddgs
duckduckgo_search
requests
trafilatura
langdetect
```

系統依賴：`poppler-utils`（pdftotext）

## 使用方式

### GitHub Actions（主要）

Push 到 GitHub 後自動排程，或到 Actions → Auto Keyword Research → Run workflow 手動觸發。

### 本地測試

```bash
sudo apt-get install poppler-utils
pip install -r requirements.txt
python scripts/auto_research_gh.py
```

## 首頁

報告索引發布在 GitHub Pages：  
🔗 https://mis23ms.github.io/tw-keyword-research/

## 版本紀錄

- **v9**（2026-02-08）：修復 5 個 bug（dedup 自吃、index 只看當次 run、workflow 門檻過嚴），穩定版
- **v8**：per-job allowed_domains、R2 site-by-site、pdftotext fallback
- **v7**：link-only 保留、rejected 清單、MIN_HIT=1
- **v6**：5 組精準關鍵字、嚴格 PDF 驗證
- **v5**：週六排程、30 天去重
- **v4**：報告級搜尋、domain 黑白名單
