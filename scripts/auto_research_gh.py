"""
auto_research_gh.py — GitHub Actions 版關鍵字爬蟲 v7

v7 changes:
- min_hit=1: 有 1 篇就出報告，0 篇才 SKIP（附前 5 個被拒 URL+原因）
- PDF 文字抽不出來 → 保留為 link-only（不丟掉）
- PDF 判定: Content-Type OR .pdf OR %PDF header
- timeout 拉到 45s，retry 1 次
- index 只列本次 run 的資料夾（_latest_run.json）
- 30-day dedup (URL + title)

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
LATEST_RUN_PATH = os.path.join(REPORTS_DIR, "_latest_run.json")
RETENTION_DAYS = 30
MAX_SUMMARY_CHARS = 500
CURRENT_YEAR = datetime.now(timezone.utc).year
MIN_YEAR = CURRENT_YEAR - 2
PDF_TIMEOUT = 45
PDF_RETRIES = 1

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
    "globenewswire.com", "www.globenewswire.com",
    "prnewswire.com", "www.prnewswire.com",
    "businesswire.com", "www.businesswire.com",
    "accesswire.com", "newswire.com",
    "markets.financialcontent.com", "financialcontent.com",
    "markets.businessinsider.com",
    "researchandmarkets.com", "www.researchandmarkets.com",
    "cnbc.com", "www.cnbc.com",
    "investorplace.com", "tomshardware.com",
    "scmp.com", "buzzfeed.com", "huffpost.com", "dailymail.co.uk",
    "goodfirms.co", "tradingkey.com",
    "scribd.com", "slideshare.net", "issuu.com", "academia.edu",
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


def is_pdf_url_hint(url: str) -> bool:
    """URL looks like it might be a PDF (heuristic)."""
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
# Fetch PDF — resilient, keeps link-only on extraction failure
# ---------------------------------------------------------------------------

def is_actual_pdf(content_type: str, url: str, content_bytes: bytes) -> bool:
    """
    Three-way PDF check:
    1. Content-Type contains application/pdf
    2. URL ends with .pdf
    3. First bytes are %PDF
    """
    ct = (content_type or "").lower()
    if "application/pdf" in ct:
        return True
    if is_pdf_url_hint(url):
        return True
    if content_bytes[:5].startswith(b"%PDF"):
        return True
    return False


def fetch_pdf(url: str) -> dict:
    """
    Fetch URL and determine if it's a PDF.
    Returns dict with:
      - is_pdf: bool
      - text: extracted text or None
      - content_type: str
      - size_bytes: int
      - status: 'ok' | 'text-too-short' | 'extraction-failed' | 'not-pdf' | 'timeout' | 'blocked' | 'error'
      - error: error message or None
    """
    result = {
        "is_pdf": False, "text": None, "content_type": "",
        "size_bytes": 0, "status": "error", "error": None,
    }

    for attempt in range(PDF_RETRIES + 1):
        try:
            r = requests.get(url, headers=UA, timeout=PDF_TIMEOUT,
                             allow_redirects=True, stream=True)

            if r.status_code == 403 or r.status_code == 401:
                result["status"] = "blocked"
                result["error"] = f"HTTP {r.status_code}"
                return result

            if r.status_code >= 400:
                result["status"] = "error"
                result["error"] = f"HTTP {r.status_code}"
                if attempt < PDF_RETRIES:
                    time.sleep(2.0)
                    continue
                return result

            content = r.content
            ct = r.headers.get("content-type", "")
            result["content_type"] = ct
            result["size_bytes"] = len(content)

            if not is_actual_pdf(ct, url, content):
                result["status"] = "not-pdf"
                result["error"] = f"Content-Type: {ct[:60]}"
                return result

            result["is_pdf"] = True

            # Skip huge PDFs
            if len(content) > 20_000_000:
                result["status"] = "text-too-short"
                result["error"] = f"PDF too large ({len(content)//1_000_000}MB), kept as link"
                return result

            # Try text extraction
            try:
                text = trafilatura.extract(content, include_comments=False, favor_recall=True)
                if text and len(text.strip()) > 50:
                    result["text"] = text.strip()
                    result["status"] = "ok"
                else:
                    result["status"] = "text-too-short"
                    result["error"] = f"Extracted only {len(text.strip()) if text else 0} chars"
            except Exception as e:
                result["status"] = "extraction-failed"
                result["error"] = str(e)[:100]

            return result

        except requests.exceptions.Timeout:
            result["status"] = "timeout"
            result["error"] = f"Timeout {PDF_TIMEOUT}s (attempt {attempt+1})"
            if attempt < PDF_RETRIES:
                time.sleep(3.0)
                continue
            return result

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)[:100]
            if attempt < PDF_RETRIES:
                time.sleep(2.0)
                continue
            return result

    return result


# ---------------------------------------------------------------------------
# Single keyword job
# ---------------------------------------------------------------------------

def run_one_keyword(job, date_str, prev_urls, prev_titles) -> dict:
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
    print(f"  target={target}")

    candidates = multi_round_search(keyword, region, timelimit, max_results=max(30, target * 10))

    rejected = []  # track rejected URLs + reasons (for stub)
    seen = set()
    per_domain = {}
    items = []

    if not candidates:
        print(f"  [FAIL] No search results.")
        return _write_report(label, keyword, date_str, items, target, rejected)

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
            rejected.append({"url": nurl, "title": title, "reason": "blacklisted"})
            continue

        # 30-day dedup
        if nurl in prev_urls:
            rejected.append({"url": nurl, "title": title, "reason": "dedup-url (seen in past 30d)"})
            continue

        nt = normalize_title(title)
        if nt and len(nt) > 10 and nt in prev_titles:
            rejected.append({"url": nurl, "title": title, "reason": "dedup-title (seen in past 30d)"})
            continue

        d = get_domain(nurl)
        if per_domain.get(d, 0) >= max_per_domain:
            rejected.append({"url": nurl, "title": title, "reason": f"max_per_domain ({max_per_domain})"})
            continue

        print(f"  Fetch: {title[:50]}...")

        # Fetch PDF
        pdf = fetch_pdf(nurl)

        if not pdf["is_pdf"]:
            rejected.append({"url": nurl, "title": title, "reason": f"not-pdf: {pdf.get('error','')}"})
            continue

        # PDF is confirmed — now decide: full text or link-only
        final_title = (title or "").strip()
        text = pdf.get("text") or ""
        link_only = False

        if pdf["status"] == "ok" and len(text) >= minlen:
            # Full extraction success
            # Year check
            if has_old_year(text):
                rejected.append({"url": nurl, "title": title, "reason": f"old content (pre-{MIN_YEAR})"})
                continue
            # Lang check
            dl = detect_lang(text[:1500])
            if not lang_ok(dl, lang):
                rejected.append({"url": nurl, "title": title, "reason": f"lang mismatch: {dl}"})
                continue
            summary = make_summary(text)
        else:
            # Extraction failed or too short — keep as link-only
            link_only = True
            summary = f"_(PDF text extraction: {pdf['status']}. {pdf.get('error','')}. Kept as link-only.)_"
            dl = lang

        if not final_title:
            fname = nurl.split("/")[-1].split("?")[0]
            final_title = fname.replace(".pdf", "").replace("-", " ").replace("_", " ").strip() or "Untitled PDF"

        items.append({
            "title": final_title,
            "url": nurl,
            "domain": d,
            "lang": dl,
            "type": "PDF",
            "link_only": link_only,
            "content_type": pdf["content_type"][:60],
            "size_bytes": pdf["size_bytes"],
            "fetch_status": pdf["status"],
            "summary": summary,
        })
        per_domain[d] = per_domain.get(d, 0) + 1
        prev_urls.add(nurl)
        if nt and len(nt) > 10:
            prev_titles.add(nt)

        tag = "📄" if not link_only else "🔗"
        print(f"  [ADD] {len(items)}/{target} {tag} {d} [{pdf['status']}]")
        time.sleep(random.uniform(0.8, 1.5))

    return _write_report(label, keyword, date_str, items, target, rejected)


def _write_report(label, keyword, date_str, items, target, rejected) -> dict:
    slug = slugify(label or keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    count = len(items)
    full_text_count = sum(1 for it in items if not it.get("link_only"))
    link_only_count = sum(1 for it in items if it.get("link_only"))
    is_skip = count == 0
    status = "SKIP" if is_skip else "OK"

    # items.json
    with open(os.path.join(folder_path, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    # summary.md
    with open(os.path.join(folder_path, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")

        if is_skip:
            f.write(f"- ⚠️ **SKIP**: No qualified PDFs found\n")
        elif count < target:
            f.write(f"- ℹ️ Only {count} PDF(s) found (target={target})\n")

        if full_text_count:
            f.write(f"- 📄 Full-text PDFs: {full_text_count}\n")
        if link_only_count:
            f.write(f"- 🔗 Link-only PDFs: {link_only_count}\n")

        f.write(f"\n---\n\n")

        if items:
            for i, it in enumerate(items, 1):
                tag = "📄" if not it.get("link_only") else "🔗 link-only"
                f.write(f"## {i}. {it['title']} `[{tag}]`\n\n")
                f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
                if it.get("link_only"):
                    size_kb = it.get("size_bytes", 0) // 1024
                    f.write(f"- Size: {size_kb} KB | Status: {it.get('fetch_status','?')}\n")
                f.write(f"\n{it['summary']}\n\n---\n\n")
        else:
            f.write("_No qualified PDFs found this run._\n\n")
            f.write(f"Query: `{keyword[:120]}`\n\n")

        # Rejected URLs (top 5) — always show for transparency
        if rejected:
            f.write("## Rejected candidates (top 5)\n\n")
            for rj in rejected[:5]:
                f.write(f"- ❌ `{rj['reason']}` — [{get_domain(rj['url'])}]({rj['url']})")
                if rj.get("title"):
                    f.write(f" — {rj['title'][:60]}")
                f.write("\n")
            if len(rejected) > 5:
                f.write(f"\n_...and {len(rejected)-5} more rejected._\n")
            f.write("\n")

    icon = "✅" if not is_skip else "⚠️"
    print(f"  [{status}] {count} PDFs ({full_text_count} full, {link_only_count} link-only)"
          f" → {folder_name}/  |  {len(rejected)} rejected")

    return {
        "keyword": label, "folder": folder_name,
        "count": count, "full_text": full_text_count,
        "link_only": link_only_count, "status": status,
    }


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
# Index — only shows current run's folders
# ---------------------------------------------------------------------------

def generate_index(run_folders: list[dict]):
    """Generate index.md from this run's results only (via _latest_run.json)."""

    # Save latest run manifest
    with open(LATEST_RUN_PATH, "w", encoding="utf-8") as f:
        json.dump(run_folders, f, ensure_ascii=False, indent=2)

    # Also collect all existing reports for the full index
    all_entries = []
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
            link_only = 0
            is_skip = False
            ip = os.path.join(fp, "items.json")
            if os.path.isfile(ip):
                try:
                    with open(ip, "r", encoding="utf-8") as jf:
                        items_data = json.load(jf)
                    count = len(items_data)
                    link_only = sum(1 for it in items_data if it.get("link_only"))
                except Exception:
                    pass
            with open(sp, "r", encoding="utf-8") as f:
                if "SKIP" in f.read(500):
                    is_skip = True

            all_entries.append({
                "date": date_str, "folder": name, "keyword": first_line,
                "count": count, "link_only": link_only, "skip": is_skip,
                "is_current_run": name in [r["folder"] for r in run_folders],
            })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("# 📊 Auto Keyword Research — Report Index\n\n")
        f.write(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write("Schedule: Every **Saturday** at 12:00 Taiwan Time\n\n")
        f.write("Sources: PDF-only from institutional sites\n\n")
        f.write("---\n\n")

        if not all_entries:
            f.write("_No reports yet._\n")
        else:
            current_date = ""
            for e in all_entries:
                if e["count"] == 0 and not e["is_current_run"]:
                    continue  # hide old empty reports
                if e["date"] != current_date:
                    current_date = e["date"]
                    f.write(f"### {current_date}\n\n")
                full = e["count"] - e["link_only"]
                parts = []
                if full:
                    parts.append(f"{full}📄")
                if e["link_only"]:
                    parts.append(f"{e['link_only']}🔗")
                info = " + ".join(parts) if parts else "0"
                warn = " ⚠️ SKIP" if e["skip"] else ""
                new = " 🆕" if e["is_current_run"] else ""
                f.write(f"- [{e['keyword']}](reports/{e['folder']}/summary.md)"
                        f" — {info}{warn}{new}\n")
            f.write("\n")

    print(f"\n[INDEX] {len(all_entries)} entries ({len(run_folders)} from this run)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Auto Keyword Research v7")
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
            results.append(result)
        except Exception as e:
            print(f"  [ERROR] {job.get('label', '?')}: {e}")

        if idx < len(jobs) - 1:
            wait = random.uniform(5.0, 10.0)
            print(f"  [WAIT] {wait:.1f}s...")
            time.sleep(wait)

    # Cleanup
    print(f"\n{'='*60}")
    cleanup_old_reports()
    generate_index(results)

    # Summary
    ok = [r for r in results if r["status"] == "OK"]
    skipped = [r for r in results if r["status"] == "SKIP"]
    total_full = sum(r.get("full_text", 0) for r in results)
    total_link = sum(r.get("link_only", 0) for r in results)

    print(f"\n{'='*60}")
    print(f"DONE: {len(ok)} OK | {len(skipped)} SKIP | {total_full}📄 full + {total_link}🔗 link-only")
    for r in results:
        icon = "✅" if r["status"] == "OK" else "⚠️"
        print(f"  {icon} {r['keyword']} → {r['count']} ({r.get('full_text',0)}📄 {r.get('link_only',0)}🔗) [{r['status']}]")
    print("=" * 60)


if __name__ == "__main__":
    main()
