"""
auto_research_gh.py — GitHub Actions 版關鍵字爬蟲 v6

v6 changes:
- Strict PDF: Content-Type 必須是 application/pdf 才收，非 PDF 一律跳
- target=3 per group, 5 groups = 最多 15 PDF
- SKIP: 命中 <2 → 標記 SKIP，不硬塞新聞/PR
- 30-day URL + title dedup
- Domain whitelist/blacklist
- 年份過濾

安全性：
- 無 shell=True / os.startfile / subprocess
- 無對外上傳（僅本地寫檔）
- 僅清理 reports/ 下超過 30 天的子資料夾
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
MAX_SUMMARY_CHARS = 500
MIN_HIT = 2
CURRENT_YEAR = datetime.now(timezone.utc).year
MIN_YEAR = CURRENT_YEAR - 2

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Domain blacklist
# ---------------------------------------------------------------------------
DOMAIN_BLACKLIST = {
    "wikipedia.org", "en.wikipedia.org",
    "linkedin.com", "www.linkedin.com",
    "reddit.com", "www.reddit.com",
    "quora.com", "medium.com",
    "facebook.com", "twitter.com", "x.com",
    "youtube.com", "instagram.com", "tiktok.com", "pinterest.com",
    # PR wire
    "globenewswire.com", "www.globenewswire.com",
    "prnewswire.com", "www.prnewswire.com",
    "businesswire.com", "www.businesswire.com",
    "accesswire.com", "newswire.com",
    # Aggregators
    "markets.financialcontent.com", "financialcontent.com",
    "markets.businessinsider.com",
    "researchandmarkets.com", "www.researchandmarkets.com",
    # General news
    "cnbc.com", "www.cnbc.com",
    "investorplace.com", "tomshardware.com",
    "scmp.com", "buzzfeed.com", "huffpost.com", "dailymail.co.uk",
    # Low-quality
    "goodfirms.co", "tradingkey.com",
    "scribd.com", "slideshare.net", "issuu.com", "academia.edu",
    # China-based
    "baidu.com", "zhihu.com", "weibo.com", "bilibili.com",
    "sohu.com", "sina.com.cn", "163.com", "qq.com",
    "csdn.net", "tencent.com", "xinhuanet.com",
    "people.com.cn", "chinadaily.com.cn",
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
        q = [(k, v) for (k, v) in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in {"utm_source", "utm_medium", "utm_campaign",
                                   "utm_term", "utm_content", "gclid", "fbclid"}]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), ""))
    except Exception:
        return url


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def get_root_domain(d: str) -> str:
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else d


def is_blacklisted(url: str) -> bool:
    d = get_domain(url)
    return d in DOMAIN_BLACKLIST or get_root_domain(d) in DOMAIN_BLACKLIST


def is_pdf_url(url: str) -> bool:
    lower = url.lower().split("?")[0].split("#")[0]
    return lower.endswith(".pdf") or "/pdf/" in url.lower()


def has_old_year(text: str) -> bool:
    years = re.findall(r"\b(20[0-2]\d)\b", text[:2000])
    if not years:
        return False
    return all(int(y) < MIN_YEAR for y in years)


def normalize_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", "", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len] if s else "untitled"


def make_summary(text: str, max_chars: int = MAX_SUMMARY_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    for sep in ["。", ". ", "！", "! ", "？", "? ", "\n"]:
        idx = cut.rfind(sep)
        if idx > max_chars // 3:
            return cut[: idx + len(sep)].strip()
    return cut.strip() + "…"


# ---------------------------------------------------------------------------
# 30-day dedup
# ---------------------------------------------------------------------------

def load_existing_dedup() -> tuple[set, set]:
    urls = set()
    titles = set()
    if not os.path.isdir(REPORTS_DIR):
        return urls, titles
    for name in os.listdir(REPORTS_DIR):
        ip = os.path.join(REPORTS_DIR, name, "items.json")
        if not os.path.isfile(ip):
            continue
        try:
            with open(ip, "r", encoding="utf-8") as f:
                items = json.load(f)
            for it in items:
                if it.get("url"):
                    urls.add(normalize_url(it["url"]))
                nt = normalize_title(it.get("title", ""))
                if len(nt) > 10:
                    titles.add(nt)
        except Exception:
            continue
    return urls, titles


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _do_ddgs_search(keyword, region, timelimit, max_results):
    from ddgs import DDGS
    with DDGS() as ddgs:
        try:
            return list(ddgs.text(keywords=keyword, region=region,
                                  timelimit=timelimit, max_results=max_results))
        except TypeError:
            try:
                return list(ddgs.text(query=keyword, region=region,
                                      timelimit=timelimit, max_results=max_results))
            except TypeError:
                return list(ddgs.text(keyword, region=region,
                                      timelimit=timelimit, max_results=max_results))


def _do_legacy_search(keyword, region, timelimit, max_results):
    from duckduckgo_search import DDGS
    for be in ["auto", "lite", "html"]:
        try:
            with DDGS() as ddgs:
                res = list(ddgs.text(keywords=keyword, region=region,
                                     timelimit=timelimit, max_results=max_results, backend=be))
            if res:
                print(f"    [OK] {be} ({len(res)}) legacy")
                return res
        except Exception as e:
            print(f"    [WARN] {be}: {e}")
            time.sleep(2.0)
    return []


def ddg_search(query, region, timelimit, max_results=20):
    try:
        res = _do_ddgs_search(query, region, timelimit, max_results)
        if res:
            print(f"    [OK] {len(res)} results")
            return res
    except ImportError:
        pass
    except Exception as e:
        print(f"    [WARN] ddgs: {e}")
        time.sleep(2.0)
    try:
        return _do_legacy_search(query, region, timelimit, max_results)
    except (ImportError, Exception):
        pass
    return []


def multi_round_search(keyword, region, timelimit, max_results=20):
    all_results = []
    seen = set()

    def add(results):
        for r in results:
            url = (r.get("href") or r.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                all_results.append(r)

    # R1: exact keyword
    print(f"  [R1] {keyword[:80]}...")
    add(ddg_search(keyword, region, timelimit, max_results))
    time.sleep(random.uniform(3.0, 5.0))

    # R2: broaden (strip site:, keep filetype:pdf + core terms)
    if len(all_results) < max_results:
        core = re.sub(r"site:\S+", "", keyword, flags=re.IGNORECASE)
        core = re.sub(r"\bOR\b", " ", core)
        core = re.sub(r"\s+", " ", core).strip()
        if core and core != keyword:
            print(f"  [R2] broad: {core[:70]}...")
            add(ddg_search(core, region, timelimit, max_results))

    print(f"  [TOTAL] {len(all_results)} candidates")
    return all_results


# ---------------------------------------------------------------------------
# Fetch PDF — STRICT Content-Type check
# ---------------------------------------------------------------------------

def fetch_pdf_text(url: str, timeout: int = 20) -> dict | None:
    """
    Download URL. ONLY accept if Content-Type is application/pdf.
    Non-PDF responses are rejected entirely (no HTML fallback).
    """
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code >= 400:
            return None

        content_type = (r.headers.get("content-type") or "").lower()

        # STRICT: must be PDF
        if "application/pdf" not in content_type:
            # Exception: URL ends in .pdf but server sends wrong content-type
            if not url.lower().split("?")[0].endswith(".pdf"):
                print(f"    [REJECT] Not PDF: Content-Type={content_type[:40]}")
                return None

        # Size check
        cl = r.headers.get("content-length")
        if cl and int(cl) > 15_000_000:
            print(f"    [SKIP] PDF too large: {int(cl)//1_000_000}MB")
            return None

        content = r.content

        # Verify it's actually PDF binary (starts with %PDF)
        if not content[:5].startswith(b"%PDF"):
            print(f"    [REJECT] Not valid PDF binary")
            return None

        text = trafilatura.extract(content, include_comments=False, favor_recall=True)
        if text and len(text.strip()) > 50:
            fname = url.split("/")[-1].split("?")[0]
            title = fname.replace(".pdf", "").replace("-", " ").replace("_", " ").strip()
            return {"title": title, "text": text.strip()}

        print(f"    [WARN] PDF extracted but too short or empty")
        return None
    except Exception as e:
        print(f"    [WARN] PDF error: {e}")
        return None


# ---------------------------------------------------------------------------
# Single keyword job
# ---------------------------------------------------------------------------

def run_one_keyword(job, date_str, prev_urls, prev_titles) -> dict | None:
    keyword = job["keyword"]
    label = job.get("label", keyword[:50])
    lang = job.get("lang", "en")
    region = job.get("region", "us-en")
    timelimit = job.get("timelimit", "y")
    target = job.get("target", 3)
    minlen = job.get("minlen", 300)
    max_per_domain = job.get("max_per_domain", 2)

    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  target={target} minlen={minlen}")

    candidates = multi_round_search(keyword, region, timelimit, max_results=max(30, target * 10))

    if not candidates:
        print(f"  [FAIL] No search results.")
        return _write_skip(label, keyword, date_str, [], "No search results")

    seen = set()
    per_domain = {}
    items = []
    skips = {}

    def skip(reason):
        skips[reason] = skips.get(reason, 0) + 1

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

        # Blacklist
        if is_blacklisted(nurl):
            skip("blacklisted")
            continue

        # 30-day URL dedup
        if nurl in prev_urls:
            skip("dedup-url")
            print(f"  [DEDUP] {nurl[:60]}")
            continue

        # 30-day title dedup
        nt = normalize_title(title)
        if nt and len(nt) > 10 and nt in prev_titles:
            skip("dedup-title")
            continue

        d = get_domain(nurl)
        if per_domain.get(d, 0) >= max_per_domain:
            continue

        print(f"  Fetch: {title[:50]}...")

        # STRICT PDF fetch — no HTML fallback
        data = fetch_pdf_text(nurl, timeout=20)

        if not data:
            skip("fetch-fail-or-not-pdf")
            continue

        text = (data.get("text") or "").strip()
        if len(text) < minlen:
            skip("too-short")
            continue

        if has_old_year(text):
            skip("old-content")
            continue

        dl = detect_lang(text[:1500])
        if not lang_ok(dl, lang):
            skip("lang-mismatch")
            continue

        final_title = (title or data.get("title") or "No Title").strip()
        summary = make_summary(text)

        items.append({
            "title": final_title,
            "url": nurl,
            "domain": d,
            "lang": dl,
            "type": "PDF",
            "summary": summary,
        })
        per_domain[d] = per_domain.get(d, 0) + 1
        prev_urls.add(nurl)
        if nt and len(nt) > 10:
            prev_titles.add(nt)

        print(f"  [ADD] {len(items)}/{target} ✅ {d}")
        time.sleep(random.uniform(0.8, 1.5))

    if skips:
        print(f"  [SKIPS] {skips}")

    # Min hit check
    if len(items) < MIN_HIT:
        return _write_skip(label, keyword, date_str, items,
                           f"Only {len(items)} PDFs (need {MIN_HIT}). Skips: {skips}")

    return _write_output(label, keyword, date_str, items)


def _write_output(label, keyword, date_str, items) -> dict:
    slug = slugify(label or keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    with open(os.path.join(folder_path, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(os.path.join(folder_path, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")
        f.write(f"- Sources: {len(items)} PDF(s)\n\n---\n\n")
        for i, it in enumerate(items, 1):
            f.write(f"## {i}. {it['title']} `[PDF]`\n\n")
            f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
            f.write(f"{it['summary']}\n\n---\n\n")

    print(f"  [DONE] {len(items)} PDFs → {folder_name}/")
    return {"keyword": label, "folder": folder_name, "count": len(items), "status": "OK"}


def _write_skip(label, keyword, date_str, items, reason) -> dict:
    slug = slugify(label or keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    with open(os.path.join(folder_path, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(os.path.join(folder_path, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")
        f.write(f"- ⚠️ **SKIP**: {reason}\n")
        f.write(f"- Sources: {len(items)} PDF(s)\n\n---\n\n")
        if items:
            for i, it in enumerate(items, 1):
                f.write(f"## {i}. {it['title']} `[PDF]`\n\n")
                f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
                f.write(f"{it['summary']}\n\n---\n\n")
        else:
            f.write("_No qualified PDFs found this run._\n\n")
            f.write(f"Query: `{keyword[:120]}`\n")

    print(f"  [SKIP] {len(items)} PDFs → {folder_name}/ — {reason}")
    return {"keyword": label, "folder": folder_name, "count": len(items), "status": "SKIP"}


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_old_reports():
    if not os.path.isdir(REPORTS_DIR):
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for name in sorted(os.listdir(REPORTS_DIR)):
        fp = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(fp):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
        if m and m.group(1) < cutoff:
            shutil.rmtree(fp)
            removed += 1
            print(f"  [CLEANUP] {name}")
    if removed:
        print(f"  [CLEANUP] Removed {removed}")


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def generate_index():
    entries = []
    if os.path.isdir(REPORTS_DIR):
        for name in sorted(os.listdir(REPORTS_DIR), reverse=True):
            fp = os.path.join(REPORTS_DIR, name)
            if not os.path.isdir(fp):
                continue
            sp = os.path.join(fp, "summary.md")
            if not os.path.isfile(sp):
                continue
            with open(sp, "r", encoding="utf-8") as f:
                first_line = f.readline().strip().lstrip("# ")
            m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
            date_str = m.group(1) if m else "unknown"

            count = 0
            is_skip = False
            ip = os.path.join(fp, "items.json")
            if os.path.isfile(ip):
                try:
                    with open(ip, "r", encoding="utf-8") as jf:
                        count = len(json.load(jf))
                except Exception:
                    pass
            with open(sp, "r", encoding="utf-8") as f:
                if "SKIP" in f.read(500):
                    is_skip = True

            # Only list in index if count > 0
            if count > 0:
                entries.append({
                    "date": date_str, "folder": name,
                    "keyword": first_line, "count": count, "skip": is_skip,
                })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("# 📊 Auto Keyword Research — Report Index\n\n")
        f.write(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write("Schedule: Every **Saturday** at 12:00 Taiwan Time\n\n")
        f.write("Sources: PDF-only from trusted institutions\n\n")
        f.write("---\n\n")
        if not entries:
            f.write("_No reports yet._\n")
        else:
            current_date = ""
            for e in entries:
                if e["date"] != current_date:
                    current_date = e["date"]
                    f.write(f"### {current_date}\n\n")
                warn = " ⚠️" if e["skip"] else ""
                f.write(f"- [{e['keyword']}](reports/{e['folder']}/summary.md)"
                        f" — {e['count']} PDF(s){warn}\n")
            f.write("\n")
    print(f"\n[INDEX] {len(entries)} entries")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Auto Keyword Research v6 (PDF-only)")
    print("=" * 60)

    if not os.path.isfile(CONFIG_PATH):
        print(f"[ERROR] Config not found: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    jobs = config.get("jobs", [])
    if not jobs:
        print("[ERROR] No jobs.")
        sys.exit(1)

    print(f"Loaded {len(jobs)} jobs")
    os.makedirs(REPORTS_DIR, exist_ok=True)

    prev_urls, prev_titles = load_existing_dedup()
    print(f"Dedup: {len(prev_urls)} URLs, {len(prev_titles)} titles")

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    results = []

    for idx, job in enumerate(jobs):
        try:
            result = run_one_keyword(job, date_str, prev_urls, prev_titles)
            if result:
                results.append(result)
        except Exception as e:
            print(f"  [ERROR] {job.get('label', '?')}: {e}")

        if idx < len(jobs) - 1:
            wait = random.uniform(5.0, 10.0)
            print(f"  [WAIT] {wait:.1f}s...")
            time.sleep(wait)

    print(f"\n{'='*60}")
    cleanup_old_reports()
    generate_index()

    ok = [r for r in results if r["status"] == "OK"]
    skip = [r for r in results if r["status"] == "SKIP"]
    fail = len(jobs) - len(results)
    print(f"\n{'='*60}")
    print(f"DONE: {len(ok)} OK | {len(skip)} SKIP | {fail} FAIL")
    for r in results:
        icon = "✅" if r["status"] == "OK" else "⚠️"
        print(f"  {icon} {r['keyword']} → {r['count']} PDF(s) [{r['status']}]")
    print("=" * 60)


if __name__ == "__main__":
    main()
