"""
auto_research_gh.py — GitHub Actions 版關鍵字爬蟲
基於 auto_research_v2.py 改造

安全性：
- 無 shell=True
- 無 os.startfile / subprocess 開檔
- 無對外上傳（僅本地寫檔，由 Actions workflow git push）
- 無刪除使用者檔案（僅清理 reports/ 下超過 30 天的子資料夾）

輸出：
- 每組關鍵字 → reports/YYYY-MM-DD_<slug>/summary.md + items.json
- 全站 → index.md（最近 30 天報告索引）
"""

import json
import os
import re
import shutil
import sys
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

import requests
import trafilatura

from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config", "keywords.json")
REPORTS_DIR = os.path.join(REPO_ROOT, "reports")
INDEX_PATH = os.path.join(REPO_ROOT, "index.md")
RETENTION_DAYS = 30
MAX_SUMMARY_CHARS = 500  # 摘要長度上限

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def detect_lang(text: str) -> str:
    try:
        return detect(text)
    except Exception:
        return "unknown"


def lang_ok(detected: str, wanted: str) -> bool:
    d = (detected or "").lower().strip()
    w = (wanted or "").lower().strip()
    if not w or w == "auto":
        return True
    if w.startswith("zh"):
        return d.startswith("zh")
    return d == w


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urlparse(url)
        q = [
            (k, v)
            for (k, v) in parse_qsl(p.query, keep_blank_values=True)
            if k.lower()
            not in {
                "utm_source", "utm_medium", "utm_campaign",
                "utm_term", "utm_content", "gclid", "fbclid",
            }
        ]
        new_query = urlencode(q, doseq=True)
        return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, ""))
    except Exception:
        return url


def domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def slugify(text: str, max_len: int = 60) -> str:
    """Convert text to filesystem-safe slug."""
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len] if s else "untitled"


def make_summary(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Extract first N chars as summary, cut at sentence boundary."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # try to cut at last sentence-ending punctuation
    for sep in ["。", ". ", "！", "! ", "？", "? ", "\n"]:
        idx = cut.rfind(sep)
        if idx > max_chars // 3:
            return cut[: idx + len(sep)].strip()
    return cut.strip() + "…"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def ddg_search_candidates(keyword: str, region: str, timelimit: str, max_results: int = 30):
    last_err = None

    # 1) duckduckgo_search
    try:
        from duckduckgo_search import DDGS
        backends = ["auto", "lite", "html"]
        for be in backends:
            try:
                with DDGS() as ddgs:
                    res = list(
                        ddgs.text(
                            keywords=keyword,
                            region=region,
                            timelimit=timelimit,
                            max_results=max_results,
                            backend=be,
                        )
                    )
                if res:
                    print(f"  [OK] backend={be} candidates={len(res)}")
                    return res
            except Exception as e:
                last_err = e
                print(f"  [WARN] backend={be}: {e}")
                time.sleep(1.0)
    except ImportError as e:
        last_err = e

    # 2) ddgs fallback
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            try:
                res = list(ddgs.text(query=keyword, region=region, timelimit=timelimit, max_results=max_results))
            except TypeError:
                try:
                    res = list(ddgs.text(keywords=keyword, region=region, timelimit=timelimit, max_results=max_results))
                except TypeError:
                    res = list(ddgs.text(keyword, region=region, timelimit=timelimit, max_results=max_results))
        if res:
            print(f"  [OK] candidates={len(res)} (ddgs)")
            return res
    except Exception as e:
        last_err = e

    raise RuntimeError(f"Search failed: {last_err!r}")


# ---------------------------------------------------------------------------
# Fetch & Extract
# ---------------------------------------------------------------------------

def fetch_html(url: str, timeout: int = 15, retries: int = 1):
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
            if r.status_code >= 400:
                return None
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception:
            if attempt < retries:
                time.sleep(1.0)
    return None


def extract_article(html_text: str):
    try:
        text = trafilatura.extract(
            html_text,
            include_comments=False,
            include_tables=False,
            favor_recall=True,
        )
        if not text:
            return None
        m = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.I | re.S)
        title = (re.sub(r"\s+", " ", m.group(1)).strip() if m else "").strip()
        return {"title": title, "text": text.strip()}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Single keyword job
# ---------------------------------------------------------------------------

def run_one_keyword(job: dict, date_str: str) -> dict | None:
    keyword = job["keyword"]
    lang = job.get("lang", "en")
    region = job.get("region", "us-en")
    timelimit = job.get("timelimit", "w")
    target = job.get("target", 5)
    minlen = job.get("minlen", 800)
    max_per_domain = job.get("max_per_domain", 2)

    print(f"\n{'='*60}")
    print(f"Keyword: {keyword}")
    print(f"  lang={lang} region={region} timelimit={timelimit} target={target}")

    try:
        max_results = max(40, target * 8)
        candidates = ddg_search_candidates(keyword, region, timelimit, max_results=max_results)
    except RuntimeError as e:
        print(f"  [FAIL] {e}")
        return None

    seen = set()
    per_domain = {}
    items = []

    for r in candidates:
        if len(items) >= target:
            break

        url = (r.get("href") or r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url:
            continue

        nurl = normalize_url(url)
        if not nurl or nurl in seen:
            continue
        seen.add(nurl)

        d = domain(nurl)
        if per_domain.get(d, 0) >= max_per_domain:
            continue

        print(f"  Reading: {title[:55]}...")
        html_text = fetch_html(nurl, timeout=15, retries=1)
        if not html_text:
            continue

        data = extract_article(html_text)
        if not data:
            continue

        text = (data.get("text") or "").strip()
        if len(text) < minlen:
            continue

        dl = detect_lang(text[:1500])
        if not lang_ok(dl, lang):
            continue

        final_title = (title or data.get("title") or "No Title").strip()
        summary = make_summary(text)

        items.append({
            "title": final_title,
            "url": nurl,
            "domain": d,
            "lang": dl,
            "summary": summary,
        })
        per_domain[d] = per_domain.get(d, 0) + 1
        print(f"  [ADD] {len(items)}/{target} domain={d}")

        time.sleep(random.uniform(0.8, 1.5))

    if not items:
        print(f"  [FAIL] No articles matched filters.")
        return None

    # --- Write output ---
    slug = slugify(keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    # items.json
    items_path = os.path.join(folder_path, "items.json")
    with open(items_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    # summary.md
    md_path = os.path.join(folder_path, "summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {keyword}\n\n")
        f.write(f"- Date: {date_str}\n")
        f.write(f"- Lang: {lang} | Region: {region} | Time: {timelimit}\n")
        f.write(f"- Results: {len(items)}\n\n---\n\n")
        for i, it in enumerate(items, 1):
            f.write(f"## {i}. {it['title']}\n\n")
            f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
            f.write(f"{it['summary']}\n\n---\n\n")

    print(f"  [DONE] {len(items)} articles → {folder_name}/")
    return {
        "keyword": keyword,
        "folder": folder_name,
        "count": len(items),
    }


# ---------------------------------------------------------------------------
# Cleanup old reports (>30 days)
# ---------------------------------------------------------------------------

def cleanup_old_reports():
    if not os.path.isdir(REPORTS_DIR):
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed = 0

    for name in sorted(os.listdir(REPORTS_DIR)):
        folder_path = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(folder_path):
            continue
        # Extract date from folder name: YYYY-MM-DD_slug
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
        if not m:
            continue
        if m.group(1) < cutoff_str:
            shutil.rmtree(folder_path)
            removed += 1
            print(f"  [CLEANUP] Removed old report: {name}")

    if removed:
        print(f"  [CLEANUP] Total removed: {removed}")


# ---------------------------------------------------------------------------
# Generate index.md
# ---------------------------------------------------------------------------

def generate_index():
    entries = []

    if os.path.isdir(REPORTS_DIR):
        for name in sorted(os.listdir(REPORTS_DIR), reverse=True):
            folder_path = os.path.join(REPORTS_DIR, name)
            if not os.path.isdir(folder_path):
                continue
            summary_path = os.path.join(folder_path, "summary.md")
            if not os.path.isfile(summary_path):
                continue

            # Read first line for keyword
            with open(summary_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip().lstrip("# ")

            m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
            date_str = m.group(1) if m else "unknown"
            entries.append({
                "date": date_str,
                "folder": name,
                "keyword": first_line,
            })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("# 📊 Auto Keyword Research — Report Index\n\n")
        f.write(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(f"Schedule: Every **Wednesday & Saturday** at 12:00 Taiwan Time\n\n")
        f.write("---\n\n")

        if not entries:
            f.write("_No reports yet._\n")
        else:
            current_date = ""
            for e in entries:
                if e["date"] != current_date:
                    current_date = e["date"]
                    f.write(f"### {current_date}\n\n")
                f.write(f"- [{e['keyword']}](reports/{e['folder']}/summary.md)\n")
            f.write("\n")

    print(f"\n[INDEX] Generated index.md with {len(entries)} entries")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Auto Keyword Research (GitHub Actions)")
    print("=" * 60)

    # Load config
    if not os.path.isfile(CONFIG_PATH):
        print(f"[ERROR] Config not found: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    jobs = config.get("jobs", [])
    if not jobs:
        print("[ERROR] No jobs in config.")
        sys.exit(1)

    print(f"Loaded {len(jobs)} keyword jobs")

    os.makedirs(REPORTS_DIR, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = []

    for job in jobs:
        try:
            result = run_one_keyword(job, date_str)
            if result:
                results.append(result)
        except Exception as e:
            print(f"  [ERROR] {job.get('keyword', '?')}: {e}")

        # pause between keywords to avoid rate limit
        time.sleep(random.uniform(2.0, 4.0))

    # Cleanup old reports
    print(f"\n{'='*60}")
    print("Cleanup: removing reports older than {RETENTION_DAYS} days...")
    cleanup_old_reports()

    # Generate index
    generate_index()

    # Summary
    print(f"\n{'='*60}")
    print(f"FINISHED: {len(results)}/{len(jobs)} keywords succeeded")
    for r in results:
        print(f"  ✓ {r['keyword']} → {r['count']} articles")
    print("=" * 60)


if __name__ == "__main__":
    main()
