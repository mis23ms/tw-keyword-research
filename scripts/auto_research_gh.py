"""
auto_research_gh.py — GitHub Actions 版關鍵字爬蟲 v5 (final)

v5 changes:
- 30-day URL + title dedup: 不重複收錄已出現過的內容
- min_hit = 2: 命中不足時標記 LOW-HIT，不硬塞垃圾
- 三輪搜尋: site-specific → filetype:pdf → broad (逐步放寬)
- Domain whitelist / blacklist
- pdf_only mode
- 年份過濾 (排除 2 年以上舊內容)

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
MIN_HIT = 2  # below this → mark as LOW-HIT, still generate summary
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
# Domain whitelist — trusted sources (always accept)
# ---------------------------------------------------------------------------
DOMAIN_WHITELIST = {
    # Big 4 + consulting
    "deloitte.com", "mckinsey.com", "kpmg.com", "ey.com", "pwc.com",
    "bcg.com", "bain.com", "accenture.com", "oliverwyman.com",
    # Banks / asset managers
    "goldmansachs.com", "morganstanley.com", "jpmorgan.com",
    "blackrock.com", "ark-invest.com", "vanguard.com",
    "ubs.com", "barclays.com", "citi.com", "creditsights.com",
    "nomura.com", "jefferies.com",
    # Semiconductor / tech IR
    "tsmc.com", "investor.tsmc.com", "semi.org",
    "asml.com", "intel.com", "nvidia.com", "amd.com",
    # Research firms
    "iqvia.com", "evaluate.com", "trendforce.com",
    "idc.com", "gartner.com", "forrester.com",
    "spglobal.com", "fitchratings.com", "moodys.com",
    # Government / intl orgs
    "imf.org", "worldbank.org", "oecd.org", "iea.org",
    "fda.gov", "sec.gov", "bis.org", "wto.org",
    "nist.gov", "energy.gov", "commerce.gov",
    # Defense think tanks
    "csis.org", "rand.org", "iiss.org", "sipri.org",
    "aerospace.org", "aiaa.org",
    # ETF providers
    "globalxetfs.com", "etf.com", "ishares.com",
}

# ---------------------------------------------------------------------------
# Domain blacklist
# ---------------------------------------------------------------------------
DOMAIN_BLACKLIST = {
    # Social
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
    # News aggregators / repackagers
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
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else d


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
    for w in DOMAIN_WHITELIST:
        if d.endswith("." + w):
            return True
    return False


def is_pdf_url(url: str) -> bool:
    lower = url.lower().split("?")[0].split("#")[0]
    return lower.endswith(".pdf") or "/pdf/" in url.lower()


def has_old_year(text: str) -> bool:
    years = re.findall(r"\b(20[0-2]\d)\b", text[:2000])
    if not years:
        return False
    return all(int(y) < MIN_YEAR for y in years)


def normalize_title(title: str) -> str:
    """Normalize title for dedup comparison."""
    t = (title or "").lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


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
# 30-day dedup: load all URLs + titles from existing reports
# ---------------------------------------------------------------------------

def load_existing_dedup() -> tuple[set, set]:
    """Scan reports/ for all items.json, return (urls_set, normalized_titles_set)."""
    urls = set()
    titles = set()

    if not os.path.isdir(REPORTS_DIR):
        return urls, titles

    for name in os.listdir(REPORTS_DIR):
        items_path = os.path.join(REPORTS_DIR, name, "items.json")
        if not os.path.isfile(items_path):
            continue
        try:
            with open(items_path, "r", encoding="utf-8") as f:
                items = json.load(f)
            for it in items:
                if it.get("url"):
                    urls.add(normalize_url(it["url"]))
                if it.get("title"):
                    nt = normalize_title(it["title"])
                    if len(nt) > 10:  # skip very short titles
                        titles.add(nt)
        except Exception:
            continue

    return urls, titles


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
    for be in ["auto", "lite", "html"]:
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
    all_results = []
    seen_urls = set()

    def add(results):
        for r in results:
            url = (r.get("href") or r.get("url") or "").strip()
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append(r)

    # R1: exact keyword from config
    print(f"  [R1] {keyword[:80]}...")
    add(ddg_search(keyword, region, timelimit, max_results))
    time.sleep(random.uniform(3.0, 5.0))

    # R2: add filetype:pdf if not present
    if "filetype:pdf" not in keyword.lower():
        q2 = f"{keyword} filetype:pdf"
        print(f"  [R2] +filetype:pdf ...")
        add(ddg_search(q2, region, timelimit, max_results))
        time.sleep(random.uniform(3.0, 5.0))

    # R3: broaden (strip site: / filetype:, add generic terms)
    if len(all_results) < max_results:
        core = re.sub(r"site:\S+", "", keyword, flags=re.IGNORECASE)
        core = re.sub(r"filetype:\S+", "", core, flags=re.IGNORECASE)
        core = re.sub(r"\bOR\b", "", core)
        core = re.sub(r"\s+", " ", core).strip()
        if core:
            q3 = f"{core} report pdf -wikipedia -linkedin -reddit"
            print(f"  [R3] broad: {q3[:70]}...")
            add(ddg_search(q3, region, timelimit, max_results))

    print(f"  [TOTAL] {len(all_results)} unique candidates")
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
            html_text, include_comments=False,
            include_tables=False, favor_recall=True,
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
        cl = r.headers.get("content-length")
        if cl and int(cl) > 15_000_000:
            print(f"    [SKIP] PDF too large")
            return None
        content = r.content
        ct = (r.headers.get("content-type") or "").lower()
        if "pdf" not in ct and not url.lower().endswith(".pdf"):
            return extract_article(content.decode("utf-8", errors="replace"))
        text = trafilatura.extract(content, include_comments=False, favor_recall=True)
        if text and len(text.strip()) > 100:
            fname = url.split("/")[-1].split("?")[0]
            title = fname.replace(".pdf", "").replace("-", " ").replace("_", " ").strip()
            return {"title": title, "text": text.strip()}
        return None
    except Exception as e:
        print(f"    [WARN] PDF error: {e}")
        return None


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

def should_accept(url: str, pdf_only: bool) -> tuple[bool, str]:
    if is_blacklisted(url):
        return False, "blacklisted"
    if is_pdf_url(url):
        return True, "PDF"
    if is_whitelisted(url):
        if pdf_only:
            return False, "whitelist-but-not-pdf"
        return True, "Trusted"
    if pdf_only:
        return False, "not-pdf"
    return False, "not-trusted"


# ---------------------------------------------------------------------------
# Single keyword job
# ---------------------------------------------------------------------------

def run_one_keyword(
    job: dict, date_str: str,
    prev_urls: set, prev_titles: set,
) -> dict | None:
    keyword = job["keyword"]
    label = job.get("label", keyword[:50])
    lang = job.get("lang", "en")
    region = job.get("region", "us-en")
    timelimit = job.get("timelimit", "y")
    target = job.get("target", 5)
    minlen = job.get("minlen", 400)
    max_per_domain = job.get("max_per_domain", 2)
    pdf_only = job.get("pdf_only", True)

    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  pdf_only={pdf_only} target={target} minlen={minlen}")

    candidates = multi_round_search(keyword, region, timelimit, max_results=max(30, target * 6))

    if not candidates:
        print(f"  [FAIL] No search results.")
        return _write_low_hit(label, keyword, date_str, [], "No search results returned")

    seen = set()
    per_domain = {}
    items = []
    skip_reasons = {"blacklisted": 0, "not-pdf": 0, "dedup-url": 0, "dedup-title": 0,
                    "too-short": 0, "old-content": 0, "lang-mismatch": 0, "extract-fail": 0}

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

        # Quality gate
        accept, reason = should_accept(nurl, pdf_only)
        if not accept:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue

        # 30-day URL dedup
        if nurl in prev_urls:
            skip_reasons["dedup-url"] += 1
            print(f"  [DEDUP] URL already in past 30d: {nurl[:60]}")
            continue

        # 30-day title dedup
        nt = normalize_title(title)
        if nt and len(nt) > 10 and nt in prev_titles:
            skip_reasons["dedup-title"] += 1
            print(f"  [DEDUP] Title already in past 30d: {title[:50]}")
            continue

        d = get_domain(nurl)
        if per_domain.get(d, 0) >= max_per_domain:
            continue

        print(f"  Fetch [{reason}]: {title[:50]}...")

        if is_pdf_url(nurl):
            data = fetch_pdf_text(nurl, timeout=20)
        else:
            html_text = fetch_html(nurl, timeout=15, retries=1)
            data = extract_article(html_text) if html_text else None

        if not data:
            skip_reasons["extract-fail"] += 1
            continue

        text = (data.get("text") or "").strip()
        if len(text) < minlen:
            skip_reasons["too-short"] += 1
            continue

        if has_old_year(text):
            skip_reasons["old-content"] += 1
            continue

        dl = detect_lang(text[:1500])
        if not lang_ok(dl, lang):
            skip_reasons["lang-mismatch"] += 1
            continue

        final_title = (title or data.get("title") or "No Title").strip()
        summary = make_summary(text)

        items.append({
            "title": final_title,
            "url": nurl,
            "domain": d,
            "lang": dl,
            "type": reason,
            "trusted": is_whitelisted(nurl),
            "summary": summary,
        })
        per_domain[d] = per_domain.get(d, 0) + 1

        # Add to dedup sets for this run
        prev_urls.add(nurl)
        if nt and len(nt) > 10:
            prev_titles.add(nt)

        trust = " ✅" if is_whitelisted(nurl) else ""
        print(f"  [ADD] {len(items)}/{target} [{reason}]{trust} {d}")

        time.sleep(random.uniform(0.8, 1.5))

    # Print skip summary
    skips = {k: v for k, v in skip_reasons.items() if v > 0}
    if skips:
        print(f"  [SKIPS] {skips}")

    # Check min_hit
    if len(items) < MIN_HIT:
        return _write_low_hit(label, keyword, date_str, items,
                              f"Only {len(items)} hits (min={MIN_HIT}). Skips: {skips}")

    # Write output
    return _write_output(label, keyword, date_str, items, pdf_only)


def _write_output(label, keyword, date_str, items, pdf_only) -> dict:
    slug = slugify(label or keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    with open(os.path.join(folder_path, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(os.path.join(folder_path, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")
        f.write(f"- Results: {len(items)} | PDF-only: {pdf_only}\n\n---\n\n")
        for i, it in enumerate(items, 1):
            trust = " ✅" if it.get("trusted") else ""
            f.write(f"## {i}. {it['title']} `[{it['type']}]`{trust}\n\n")
            f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
            f.write(f"{it['summary']}\n\n---\n\n")

    print(f"  [DONE] {len(items)} sources → {folder_name}/")
    return {"keyword": label, "folder": folder_name, "count": len(items), "status": "OK"}


def _write_low_hit(label, keyword, date_str, items, reason) -> dict:
    """Still write a summary, but mark as LOW-HIT."""
    slug = slugify(label or keyword)
    folder_name = f"{date_str}_{slug}"
    folder_path = os.path.join(REPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    with open(os.path.join(folder_path, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(os.path.join(folder_path, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")
        f.write(f"- ⚠️ **LOW-HIT**: {reason}\n")
        f.write(f"- Results: {len(items)}\n\n---\n\n")
        if items:
            for i, it in enumerate(items, 1):
                trust = " ✅" if it.get("trusted") else ""
                f.write(f"## {i}. {it['title']} `[{it['type']}]`{trust}\n\n")
                f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
                f.write(f"{it['summary']}\n\n---\n\n")
        else:
            f.write("_No qualified sources found this run._\n\n")
            f.write(f"Search attempted: `{keyword[:100]}`\n")

    print(f"  [LOW-HIT] {len(items)} sources → {folder_name}/ — {reason}")
    return {"keyword": label, "folder": folder_name, "count": len(items), "status": "LOW-HIT"}


# ---------------------------------------------------------------------------
# Cleanup old reports (>30 days)
# ---------------------------------------------------------------------------

def cleanup_old_reports():
    if not os.path.isdir(REPORTS_DIR):
        return
    cutoff_str = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    removed = 0
    for name in sorted(os.listdir(REPORTS_DIR)):
        fp = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(fp):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
        if m and m.group(1) < cutoff_str:
            shutil.rmtree(fp)
            removed += 1
            print(f"  [CLEANUP] {name}")
    if removed:
        print(f"  [CLEANUP] Removed {removed} old reports")


# ---------------------------------------------------------------------------
# Generate index.md
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

            # Read items for stats
            count = 0
            trusted = 0
            low_hit = False
            ip = os.path.join(fp, "items.json")
            if os.path.isfile(ip):
                try:
                    with open(ip, "r", encoding="utf-8") as jf:
                        items = json.load(jf)
                    count = len(items)
                    trusted = sum(1 for it in items if it.get("trusted"))
                except Exception:
                    pass
            # Check if LOW-HIT
            with open(sp, "r", encoding="utf-8") as f:
                content = f.read(500)
                if "LOW-HIT" in content:
                    low_hit = True

            entries.append({
                "date": date_str, "folder": name, "keyword": first_line,
                "count": count, "trusted": trusted, "low_hit": low_hit,
            })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("# 📊 Auto Keyword Research — Report Index\n\n")
        f.write(f"Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write("Schedule: Every **Saturday** at 12:00 Taiwan Time\n\n")
        f.write("Sources: PDF reports from trusted institutions (Big 4, banks, think tanks, IR)\n\n")
        f.write("---\n\n")
        if not entries:
            f.write("_No reports yet._\n")
        else:
            current_date = ""
            for e in entries:
                if e["date"] != current_date:
                    current_date = e["date"]
                    f.write(f"### {current_date}\n\n")
                badge = f" ({e['trusted']}✅)" if e["trusted"] else ""
                warn = " ⚠️" if e["low_hit"] else ""
                f.write(f"- [{e['keyword']}](reports/{e['folder']}/summary.md)"
                        f" — {e['count']} sources{badge}{warn}\n")
            f.write("\n")
    print(f"\n[INDEX] {len(entries)} entries")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Auto Keyword Research v5 (GitHub Actions)")
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

    # Load 30-day dedup
    prev_urls, prev_titles = load_existing_dedup()
    print(f"Dedup loaded: {len(prev_urls)} URLs, {len(prev_titles)} titles from past {RETENTION_DAYS}d")

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

    # Cleanup
    print(f"\n{'='*60}")
    print(f"Cleanup: >30 days...")
    cleanup_old_reports()
    generate_index()

    # Summary
    print(f"\n{'='*60}")
    ok = [r for r in results if r["status"] == "OK"]
    low = [r for r in results if r["status"] == "LOW-HIT"]
    print(f"FINISHED: {len(ok)} OK, {len(low)} LOW-HIT, {len(jobs)-len(results)} FAILED")
    for r in results:
        icon = "✓" if r["status"] == "OK" else "⚠️"
        print(f"  {icon} {r['keyword']} → {r['count']} sources [{r['status']}]")
    print("=" * 60)


if __name__ == "__main__":
    main()
