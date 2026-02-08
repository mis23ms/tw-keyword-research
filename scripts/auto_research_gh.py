"""
auto_research_gh.py — GitHub Actions 版關鍵字爬蟲 v4

v4 changes:
- Domain whitelist: 信任的大機構/顧問/政府/IR 優先
- Expanded blacklist: PR wire / news aggregator / 轉載站全擋
- pdf_only mode: config 可設定只收 PDF
- 三輪搜尋策略: site-specific → filetype:pdf → broad report (逐步放寬)
- label 欄位: 報告顯示更清楚的分類標題
- 年份過濾: 預設排除 3 年以上舊內容

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
CURRENT_YEAR = datetime.now(timezone.utc).year
MIN_YEAR = CURRENT_YEAR - 2  # reject content older than 2 years

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Domain whitelist — trusted report sources (always accept)
# ---------------------------------------------------------------------------
DOMAIN_WHITELIST = {
    # Big 4 + top consulting
    "deloitte.com", "mckinsey.com", "kpmg.com", "ey.com", "pwc.com",
    "bcg.com", "bain.com", "accenture.com", "oliverwyman.com",
    # Banks / asset managers
    "goldmansachs.com", "morganstanley.com", "jpmorgan.com",
    "blackrock.com", "ark-invest.com", "vanguard.com",
    "credit-suisse.com", "ubs.com", "barclays.com", "citi.com",
    "nomura.com", "jefferies.com", "creditsights.com",
    # Semiconductor / tech specific
    "investor.tsmc.com", "tsmc.com", "semi.org", "semiengineering.com",
    "asml.com", "intel.com", "nvidia.com", "amd.com",
    # Industry research
    "iqvia.com", "evaluate.com", "trendforce.com",
    "idc.com", "gartner.com", "forrester.com",
    "statista.com", "spglobal.com", "fitchratings.com",
    "moodys.com", "bloombergadria.com",
    # Government / intl orgs
    "imf.org", "worldbank.org", "oecd.org", "iea.org",
    "fda.gov", "sec.gov", "bis.org", "wto.org",
    "nist.gov", "energy.gov", "commerce.gov",
    # Defense think tanks
    "csis.org", "rand.org", "iiss.org", "sipri.org",
    "aerospace.org", "aiaa.org",
}

# ---------------------------------------------------------------------------
# Domain blacklist — reject these entirely
# ---------------------------------------------------------------------------
DOMAIN_BLACKLIST = {
    # Social media
    "wikipedia.org", "en.wikipedia.org",
    "linkedin.com", "www.linkedin.com",
    "reddit.com", "www.reddit.com",
    "quora.com", "www.quora.com",
    "medium.com",
    "facebook.com", "www.facebook.com",
    "twitter.com", "x.com",
    "youtube.com", "www.youtube.com",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "pinterest.com",
    # PR wire / press release distributors
    "globenewswire.com", "www.globenewswire.com",
    "prnewswire.com", "www.prnewswire.com",
    "businesswire.com", "www.businesswire.com",
    "accesswire.com", "www.accesswire.com",
    "newswire.com", "www.newswire.com",
    # News aggregators / repackagers
    "markets.financialcontent.com",
    "markets.businessinsider.com",
    "financialcontent.com",
    # General news (not report-grade)
    "cnbc.com", "www.cnbc.com",
    "investorplace.com", "www.investorplace.com",
    "tomshardware.com", "www.tomshardware.com",
    "scmp.com", "www.scmp.com",
    "buzzfeed.com", "huffpost.com",
    "dailymail.co.uk",
    # Low-quality aggregators / list sites
    "goodfirms.co", "www.goodfirms.co",
    "tradingkey.com", "www.tradingkey.com",
    "scribd.com", "slideshare.net",
    "issuu.com", "academia.edu",
    "researchandmarkets.com", "www.researchandmarkets.com",
    # China-based (per user preference)
    "baidu.com", "zhihu.com", "weibo.com", "bilibili.com",
    "sohu.com", "sina.com.cn", "163.com", "qq.com",
    "csdn.net", "tencent.com", "xinhuanet.com",
    "people.com.cn", "chinadaily.com.cn",
}

# ---------------------------------------------------------------------------
# URL report patterns — for non-PDF, non-whitelist URLs
# ---------------------------------------------------------------------------
REPORT_URL_PATTERNS = re.compile(
    r"(presentation|factsheet|fact-sheet|methodology|outlook|forecast|"
    r"whitepaper|white-paper|annual-report|quarterly-report|"
    r"10-k|10-q|10k|10q|earnings|investor-relations|"
    r"research-report|market-report|industry-report|industry-outlook|"
    r"supply-chain|executive-summary|briefing|"
    r"\.pdf)",
    re.IGNORECASE,
)

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


def get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def get_root_domain(d: str) -> str:
    """finance.yahoo.com → yahoo.com"""
    parts = d.split(".")
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return d


def is_blacklisted(url: str) -> bool:
    d = get_domain(url)
    return d in DOMAIN_BLACKLIST or get_root_domain(d) in DOMAIN_BLACKLIST


def is_whitelisted(url: str) -> bool:
    d = get_domain(url)
    if d in DOMAIN_WHITELIST:
        return True
    root = get_root_domain(d)
    if root in DOMAIN_WHITELIST:
        return True
    # Also check if any whitelist entry is a suffix of the domain
    for w in DOMAIN_WHITELIST:
        if d.endswith("." + w) or d == w:
            return True
    return False


def is_pdf_url(url: str) -> bool:
    return url.lower().rstrip("/").endswith(".pdf") or "/pdf/" in url.lower()


def is_report_url(url: str) -> bool:
    return bool(REPORT_URL_PATTERNS.search(url))


def has_old_year(text: str) -> bool:
    """Check if content is dominated by old year references."""
    years_found = re.findall(r"\b(20[0-2]\d)\b", text[:2000])
    if not years_found:
        return False
    recent = sum(1 for y in years_found if int(y) >= MIN_YEAR)
    return recent == 0  # all year mentions are old


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
# Search
# ---------------------------------------------------------------------------

def _do_ddgs_search(keyword: str, region: str, timelimit: str, max_results: int):
    from ddgs import DDGS
    with DDGS() as ddgs:
        try:
            return list(ddgs.text(
                keywords=keyword, region=region,
                timelimit=timelimit, max_results=max_results,
            ))
        except TypeError:
            try:
                return list(ddgs.text(
                    query=keyword, region=region,
                    timelimit=timelimit, max_results=max_results,
                ))
            except TypeError:
                return list(ddgs.text(
                    keyword, region=region,
                    timelimit=timelimit, max_results=max_results,
                ))


def _do_legacy_search(keyword: str, region: str, timelimit: str, max_results: int):
    from duckduckgo_search import DDGS
    backends = ["auto", "lite", "html"]
    for be in backends:
        try:
            with DDGS() as ddgs:
                res = list(ddgs.text(
                    keywords=keyword, region=region,
                    timelimit=timelimit, max_results=max_results,
                    backend=be,
                ))
            if res:
                print(f"    [OK] backend={be} ({len(res)}) (legacy)")
                return res
        except Exception as e:
            print(f"    [WARN] legacy {be}: {e}")
            time.sleep(2.0)
    return []


def ddg_search(query: str, region: str, timelimit: str, max_results: int = 20):
    """Single search call with ddgs → legacy fallback."""
    try:
        res = _do_ddgs_search(query, region, timelimit, max_results)
        if res:
            print(f"    [OK] {len(res)} results (ddgs)")
            return res
    except ImportError:
        pass
    except Exception as e:
        print(f"    [WARN] ddgs: {e}")
        time.sleep(2.0)

    try:
        return _do_legacy_search(query, region, timelimit, max_results)
    except ImportError:
        pass
    except Exception as e:
        print(f"    [WARN] legacy: {e}")
    return []


def multi_round_search(keyword: str, region: str, timelimit: str, max_results: int = 20):
    """
    Three-round search, each progressively broader:
    1. Original keyword (already contains site: and filetype: from config)
    2. Keyword + filetype:pdf (if not already included)
    3. Broader: core terms + report outlook pdf
    Deduplicate across rounds.
    """
    all_results = []
    seen_urls = set()

    def add_results(results):
        for r in results:
            url = (r.get("href") or r.get("url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)

    # Round 1: exact keyword from config (includes site: restrictions)
    print(f"  [R1] {keyword[:90]}...")
    r1 = ddg_search(keyword, region, timelimit, max_results)
    add_results(r1)
    time.sleep(random.uniform(3.0, 5.0))

    # Round 2: if keyword doesn't have filetype:pdf, add it
    if "filetype:pdf" not in keyword.lower():
        q2 = f"{keyword} filetype:pdf"
        print(f"  [R2] {q2[:90]}...")
        r2 = ddg_search(q2, region, timelimit, max_results)
        add_results(r2)
        time.sleep(random.uniform(3.0, 5.0))

    # Round 3: broaden — strip site: restrictions, add generic report terms
    # Extract core topic words (remove site:, filetype:, OR, etc.)
    core = re.sub(r"site:\S+", "", keyword, flags=re.IGNORECASE)
    core = re.sub(r"filetype:\S+", "", core, flags=re.IGNORECASE)
    core = re.sub(r"\bOR\b", "", core)
    core = re.sub(r"\s+", " ", core).strip()
    if core and len(all_results) < max_results:
        q3 = f"{core} report outlook pdf -wikipedia -linkedin -reddit"
        print(f"  [R3] {q3[:90]}...")
        r3 = ddg_search(q3, region, timelimit, max_results)
        add_results(r3)

    print(f"  [SEARCH] Total unique candidates: {len(all_results)}")
    return all_results


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


def fetch_pdf_text(url: str, timeout: int = 20) -> dict | None:
    try:
        r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True, stream=True)
        if r.status_code >= 400:
            return None

        # Check size — skip huge PDFs (>15MB)
        content_length = r.headers.get("content-length")
        if content_length and int(content_length) > 15_000_000:
            print(f"    [SKIP] PDF too large: {int(content_length)//1_000_000}MB")
            return None

        content = r.content
        content_type = (r.headers.get("content-type") or "").lower()

        if "pdf" not in content_type and not url.lower().endswith(".pdf"):
            # Not actually a PDF — treat as HTML
            return extract_article(content.decode("utf-8", errors="replace"))

        text = trafilatura.extract(content, include_comments=False, favor_recall=True)
        if text and len(text.strip()) > 100:
            fname = url.split("/")[-1].split("?")[0]
            title = fname.replace(".pdf", "").replace("-", " ").replace("_", " ").strip()
            return {"title": title, "text": text.strip()}

        return None
    except Exception as e:
        print(f"    [WARN] PDF fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# URL quality check
# ---------------------------------------------------------------------------

def should_accept(url: str, pdf_only: bool) -> tuple[bool, str]:
    """
    Returns (accept, reason).
    Logic:
    1. Blacklisted → reject
    2. PDF URL → accept (tagged as PDF)
    3. Whitelisted domain → accept (tagged as Trusted)
    4. If pdf_only=True and not PDF → reject
    5. URL matches report pattern → accept (tagged as Report)
    6. Otherwise → reject
    """
    if is_blacklisted(url):
        return False, "blacklisted"

    if is_pdf_url(url):
        return True, "PDF"

    if is_whitelisted(url):
        return True, "Trusted"

    if pdf_only:
        return False, "not-pdf (pdf_only mode)"

    if is_report_url(url):
        return True, "Report"

    return False, "not-report-like"


# ---------------------------------------------------------------------------
# Single keyword job
# ---------------------------------------------------------------------------

def run_one_keyword(job: dict, date_str: str) -> dict | None:
    keyword = job["keyword"]
    label = job.get("label", keyword[:50])
    lang = job.get("lang", "en")
    region = job.get("region", "us-en")
    timelimit = job.get("timelimit", "w")
    target = job.get("target", 5)
    minlen = job.get("minlen", 600)
    max_per_domain = job.get("max_per_domain", 2)
    pdf_only = job.get("pdf_only", False)

    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  keyword: {keyword[:80]}...")
    print(f"  lang={lang} region={region} time={timelimit} target={target} pdf_only={pdf_only}")

    candidates = multi_round_search(keyword, region, timelimit, max_results=max(30, target * 6))

    if not candidates:
        print(f"  [FAIL] No search results.")
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

        # --- Quality gate ---
        accept, reason = should_accept(nurl, pdf_only)
        if not accept:
            d = get_domain(nurl)
            print(f"  [SKIP] {reason}: {d} / {nurl[:60]}")
            continue

        d = get_domain(nurl)
        if per_domain.get(d, 0) >= max_per_domain:
            continue

        print(f"  Reading [{reason}]: {title[:50]}...")

        # Fetch
        if is_pdf_url(nurl):
            data = fetch_pdf_text(nurl, timeout=20)
        else:
            html_text = fetch_html(nurl, timeout=15, retries=1)
            data = extract_article(html_text) if html_text else None

        if not data:
            continue

        text = (data.get("text") or "").strip()
        if len(text) < minlen:
            print(f"  [SKIP] too short: {len(text)} < {minlen}")
            continue

        # Year freshness check
        if has_old_year(text):
            print(f"  [SKIP] content too old (pre-{MIN_YEAR})")
            continue

        dl = detect_lang(text[:1500])
        if not lang_ok(dl, lang):
            continue

        final_title = (title or data.get("title") or "No Title").strip()
        summary = make_summary(text)

        source_type = reason  # PDF / Trusted / Report
        trusted = "✅" if is_whitelisted(nurl) else ""

        items.append({
            "title": final_title,
            "url": nurl,
            "domain": d,
            "lang": dl,
            "type": source_type,
            "trusted": bool(is_whitelisted(nurl)),
            "summary": summary,
        })
        per_domain[d] = per_domain.get(d, 0) + 1
        print(f"  [ADD] {len(items)}/{target} [{source_type}]{trusted} {d}")

        time.sleep(random.uniform(0.8, 1.5))

    if not items:
        print(f"  [FAIL] No articles passed all filters.")
        return None

    # --- Write output ---
    slug = slugify(label or keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    items_path = os.path.join(folder_path, "items.json")
    with open(items_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(folder_path, "summary.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")
        f.write(f"- Search: `{keyword[:80]}...`\n")
        f.write(f"- Lang: {lang} | Region: {region} | Time: {timelimit}\n")
        f.write(f"- Results: {len(items)} | PDF-only: {pdf_only}\n\n---\n\n")
        for i, it in enumerate(items, 1):
            trust_badge = " ✅" if it.get("trusted") else ""
            f.write(f"## {i}. {it['title']} `[{it.get('type', '?')}]`{trust_badge}\n\n")
            f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
            f.write(f"{it['summary']}\n\n---\n\n")

    print(f"  [DONE] {len(items)} articles → {folder_name}/")
    return {
        "keyword": label,
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
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
        if not m:
            continue
        if m.group(1) < cutoff_str:
            shutil.rmtree(folder_path)
            removed += 1
            print(f"  [CLEANUP] Removed: {name}")

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

            with open(summary_path, "r", encoding="utf-8") as f:
                first_line = f.readline().strip().lstrip("# ")

            m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
            date_str = m.group(1) if m else "unknown"

            # Count items
            items_path = os.path.join(folder_path, "items.json")
            count = 0
            trusted_count = 0
            if os.path.isfile(items_path):
                try:
                    with open(items_path, "r", encoding="utf-8") as jf:
                        items_data = json.load(jf)
                    count = len(items_data)
                    trusted_count = sum(1 for it in items_data if it.get("trusted"))
                except Exception:
                    pass

            entries.append({
                "date": date_str,
                "folder": name,
                "keyword": first_line,
                "count": count,
                "trusted": trusted_count,
            })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("# 📊 Auto Keyword Research — Report Index\n\n")
        f.write(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write("Schedule: Every **Wednesday & Saturday** at 12:00 Taiwan Time\n\n")
        f.write("Sources: PDF reports, trusted institutions (Big 4, top banks, think tanks, govt)\n\n")
        f.write("---\n\n")

        if not entries:
            f.write("_No reports yet._\n")
        else:
            current_date = ""
            for e in entries:
                if e["date"] != current_date:
                    current_date = e["date"]
                    f.write(f"### {current_date}\n\n")
                trust_info = f" ({e['trusted']}✅)" if e["trusted"] else ""
                f.write(f"- [{e['keyword']}](reports/{e['folder']}/summary.md) — {e['count']} sources{trust_info}\n")
            f.write("\n")

    print(f"\n[INDEX] Generated index.md with {len(entries)} entries")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Auto Keyword Research v4 (GitHub Actions)")
    print("=" * 60)

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

    for idx, job in enumerate(jobs):
        try:
            result = run_one_keyword(job, date_str)
            if result:
                results.append(result)
        except Exception as e:
            print(f"  [ERROR] {job.get('label', job.get('keyword', '?'))}: {e}")

        if idx < len(jobs) - 1:
            wait = random.uniform(5.0, 10.0)
            print(f"  [WAIT] {wait:.1f}s before next keyword...")
            time.sleep(wait)

    # Cleanup
    print(f"\n{'='*60}")
    print(f"Cleanup: removing reports older than {RETENTION_DAYS} days...")
    cleanup_old_reports()

    generate_index()

    # Summary
    print(f"\n{'='*60}")
    print(f"FINISHED: {len(results)}/{len(jobs)} keywords succeeded")
    for r in results:
        print(f"  ✓ {r['keyword']} → {r['count']} sources")
    print("=" * 60)


if __name__ == "__main__":
    main()
