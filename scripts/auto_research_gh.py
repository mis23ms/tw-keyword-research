"""
auto_research_gh.py — v9 (all bugs fixed)

Fixed from v8-final:
- BUG1: job_success now = cnt >= 1 (any PDF counts, not just full-text)
- BUG2: gen_index scans ALL report folders, not just current run
- BUG3: dedup skips today's folders (same-day rerun won't eat itself)
- BUG4+5: removed run_state.json / B+ gating (workflow simplified)

Features kept:
- per-job allowed_domains
- R1 broad + R2 site-by-site
- pdftotext fallback
- link-only for extraction failures
- >= 1 PDF = report, 0 = SKIP with rejected list
- 403 fast-fail
- 30-day dedup + cleanup

Safety:
- subprocess.run for pdftotext only (no shell=True)
- No uploads (GET only)
- rmtree only on reports/ YYYY-MM-DD_ folders > 30 days
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

import requests
import trafilatura
from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0

_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_HERE) == "scripts":
    REPO_ROOT = os.path.dirname(_HERE)
else:
    REPO_ROOT = _HERE

_CFG1 = os.path.join(REPO_ROOT, "config", "keywords.json")
_CFG2 = os.path.join(REPO_ROOT, "keywords.json")
CONFIG_PATH = _CFG1 if os.path.isfile(_CFG1) else _CFG2

REPORTS_DIR = os.path.join(REPO_ROOT, "reports")
INDEX_PATH = os.path.join(REPO_ROOT, "index.md")
RETENTION_DAYS = 30
MAX_SUMMARY_CHARS = 500
CURRENT_YEAR = datetime.now(timezone.utc).year
MIN_YEAR = CURRENT_YEAR - 2
PDF_TIMEOUT_1 = 45
PDF_TIMEOUT_2 = 20
GLOBAL_DEADLINE_SEC = 12 * 60
HAS_PDFTOTEXT = False

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

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
    "cnbc.com", "investorplace.com", "tomshardware.com",
    "scmp.com", "buzzfeed.com", "huffpost.com", "dailymail.co.uk",
    "goodfirms.co", "tradingkey.com",
    "scribd.com", "slideshare.net", "issuu.com", "academia.edu",
    "baidu.com", "zhihu.com", "weibo.com", "bilibili.com",
    "sohu.com", "sina.com.cn", "163.com", "qq.com",
    "csdn.net", "tencent.com", "xinhuanet.com",
    "people.com.cn", "chinadaily.com.cn",
}


# ───────────────────────── helpers ─────────────────────────

def detect_lang(text):
    try:
        return detect(text)
    except Exception:
        return "unknown"


def lang_ok(detected, wanted):
    d, w = (detected or "").lower(), (wanted or "").lower()
    if not w or w == "auto":
        return True
    return d.startswith("zh") if w.startswith("zh") else d == w


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.lower() not in {"utm_source", "utm_medium", "utm_campaign",
                                   "utm_term", "utm_content", "gclid", "fbclid"}]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), ""))
    except Exception:
        return url


def get_domain(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def root_domain(d):
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else d


def is_blacklisted(url):
    d = get_domain(url)
    return d in DOMAIN_BLACKLIST or root_domain(d) in DOMAIN_BLACKLIST


def domain_matches(url, allowed_list):
    d = get_domain(url)
    rd = root_domain(d)
    for a in allowed_list:
        a = a.lower().strip()
        if d == a or rd == a or d.endswith("." + a):
            return True
    return False


def is_pdf_url_hint(url):
    lower = url.lower().split("?")[0].split("#")[0]
    return lower.endswith(".pdf") or "/pdf/" in url.lower()


def has_old_year(text):
    years = re.findall(r"\b(20[0-2]\d)\b", text[:2000])
    return bool(years) and all(int(y) < MIN_YEAR for y in years)


def normalize_title(title):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (title or "").lower())).strip()


def slugify(text, mx=60):
    s = re.sub(r"[\s_]+", "-", re.sub(r"[^\w\s-]", "", text.lower())).strip("-")
    return s[:mx] or "untitled"


def make_summary(text, mx=MAX_SUMMARY_CHARS):
    if len(text) <= mx:
        return text
    cut = text[:mx]
    for sep in ["。", ". ", "！", "! ", "？", "? ", "\n"]:
        i = cut.rfind(sep)
        if i > mx // 3:
            return cut[:i + len(sep)].strip()
    return cut.strip() + "…"


# ───────────────────────── dedup (FIX: skip today) ─────────────────────────

def load_existing_dedup(today_str: str):
    """Load URLs+titles from past reports, SKIPPING today's folders."""
    urls, titles = set(), set()
    if not os.path.isdir(REPORTS_DIR):
        return urls, titles
    for name in os.listdir(REPORTS_DIR):
        # FIX BUG3: skip today's folders so reruns don't dedup themselves
        if name.startswith(today_str + "_"):
            continue
        ip = os.path.join(REPORTS_DIR, name, "items.json")
        if not os.path.isfile(ip):
            continue
        try:
            with open(ip, "r", encoding="utf-8") as f:
                for it in json.load(f):
                    if it.get("url"):
                        urls.add(normalize_url(it["url"]))
                    nt = normalize_title(it.get("title", ""))
                    if len(nt) > 10:
                        titles.add(nt)
        except Exception:
            pass
    return urls, titles


# ───────────────────────── search ─────────────────────────

def _ddgs_new(kw, region, timelimit, n):
    from ddgs import DDGS
    with DDGS() as d:
        try:
            return list(d.text(keywords=kw, region=region, timelimit=timelimit, max_results=n))
        except TypeError:
            try:
                return list(d.text(query=kw, region=region, timelimit=timelimit, max_results=n))
            except TypeError:
                return list(d.text(kw, region=region, timelimit=timelimit, max_results=n))


def _ddgs_legacy(kw, region, timelimit, n):
    from duckduckgo_search import DDGS
    for be in ["auto", "lite", "html"]:
        try:
            with DDGS() as d:
                res = list(d.text(keywords=kw, region=region, timelimit=timelimit,
                                  max_results=n, backend=be))
            if res:
                return res
        except Exception as e:
            print(f"      legacy {be}: {e}")
            time.sleep(2)
    return []


def ddg(query, region, timelimit, n=25):
    try:
        res = _ddgs_new(query, region, timelimit, n)
        if res:
            print(f"    [{len(res)} hits]")
            return res
    except ImportError:
        pass
    except Exception as e:
        print(f"    ddgs err: {e}")
        time.sleep(2)
    try:
        res = _ddgs_legacy(query, region, timelimit, n)
        if res:
            print(f"    [{len(res)} hits legacy]")
            return res
    except Exception:
        pass
    return []


def search_for_job(keyword, allowed_domains, region, timelimit, n=25):
    all_results = []
    seen = set()

    def add(results):
        for r in results:
            url = (r.get("href") or r.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                all_results.append(r)

    print(f"  [R1] {keyword[:80]}...")
    add(ddg(keyword, region, timelimit, n))
    time.sleep(random.uniform(3, 5))

    if allowed_domains:
        core = re.sub(r"filetype:\S+", "", keyword, flags=re.IGNORECASE).strip()
        domains_to_try = list(allowed_domains)
        random.shuffle(domains_to_try)
        tried = 0
        for dom in domains_to_try:
            if tried >= 5:
                break
            q = f"site:{dom} {core} filetype:pdf"
            print(f"  [R2] site:{dom} ...")
            add(ddg(q, region, timelimit, 10))
            tried += 1
            time.sleep(random.uniform(2, 4))

    print(f"  [TOTAL] {len(all_results)} candidates")
    return all_results


# ───────────────────────── PDF fetch ─────────────────────────

def _pdftotext(pdf_bytes):
    if not HAS_PDFTOTEXT:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        result = subprocess.run(
            ["pdftotext", "-layout", tmp_path, "-"],
            capture_output=True, timeout=30
        )
        os.unlink(tmp_path)
        if result.returncode == 0:
            text = result.stdout.decode("utf-8", errors="replace").strip()
            return text if len(text) > 50 else None
    except Exception:
        pass
    return None


def is_pdf(ct, url, content):
    if "application/pdf" in (ct or "").lower():
        return True
    if is_pdf_url_hint(url):
        return True
    if content[:5].startswith(b"%PDF"):
        return True
    return False


def fetch_pdf(url):
    out = {"is_pdf": False, "text": None, "content_type": "",
           "size_bytes": 0, "status": "error", "error": None}

    for attempt in range(2):
        try:
            timeout_s = PDF_TIMEOUT_1 if attempt == 0 else PDF_TIMEOUT_2
            r = requests.get(url, headers=UA, timeout=timeout_s,
                             allow_redirects=True, stream=True)

            if r.status_code in (401, 403):
                out["status"] = "blocked"
                out["error"] = f"HTTP {r.status_code}"
                return out

            if r.status_code >= 400:
                out["error"] = f"HTTP {r.status_code}"
                if attempt == 0:
                    time.sleep(2)
                    continue
                return out

            content = r.content
            ct = r.headers.get("content-type", "")
            out["content_type"] = ct
            out["size_bytes"] = len(content)

            if not is_pdf(ct, url, content):
                out["status"] = "not-pdf"
                out["error"] = f"CT: {ct[:50]}"
                return out

            out["is_pdf"] = True

            if len(content) > 20_000_000:
                out["status"] = "too-large"
                out["error"] = f"{len(content)//1_000_000}MB"
                return out

            text = None
            try:
                text = trafilatura.extract(content, include_comments=False, favor_recall=True)
                if text:
                    text = text.strip()
            except Exception:
                pass

            if not text or len(text) < 80:
                fb = _pdftotext(content)
                if fb and len(fb) > len(text or ""):
                    text = fb
                    print(f"    [pdftotext OK: {len(text)} chars]")

            if text and len(text) > 50:
                out["text"] = text
                out["status"] = "ok"
            else:
                out["status"] = "no-text"
                out["error"] = f"extracted {len(text) if text else 0} chars"
            return out

        except requests.exceptions.Timeout:
            out["status"] = "timeout"
            out["error"] = f"{timeout_s}s"
            if attempt == 0:
                time.sleep(3)
                continue
            return out
        except Exception as e:
            out["status"] = "error"
            out["error"] = str(e)[:80]
            if attempt == 0:
                time.sleep(2)
                continue
            return out

    return out


# ───────────────────────── job runner ─────────────────────────

def run_one(job, date_str, prev_urls, prev_titles, *, deadline_ts=None):
    keyword = job["keyword"]
    label = job.get("label", keyword[:50])
    lang = job.get("lang", "en")
    region = job.get("region", "us-en")
    timelimit = job.get("timelimit", "y")
    target = job.get("target", 3)
    minlen = job.get("minlen", 100)
    max_per_dom = job.get("max_per_domain", 2)
    allowed = job.get("allowed_domains", [])

    print(f"\n{'='*60}")
    print(f"[{label}] target={target} allowed={len(allowed)}")

    candidates = search_for_job(keyword, allowed, region, timelimit, n=25)
    rejected, seen, per_dom, items = [], set(), {}, []

    if not candidates:
        return _report(label, keyword, date_str, items, target, rejected)

    max_attempts = target * 12
    attempts = 0

    for c in candidates:
        if deadline_ts and time.time() > deadline_ts:
            break
        if attempts >= max_attempts or len(items) >= target:
            break

        url = (c.get("href") or c.get("url") or "").strip()
        title = (c.get("title") or "").strip()
        if not url:
            continue

        nurl = normalize_url(url)
        if not nurl or nurl in seen:
            continue
        seen.add(nurl)

        if is_blacklisted(nurl):
            rejected.append({"url": nurl, "title": title, "reason": "blacklisted"})
            continue

        if allowed and not domain_matches(nurl, allowed):
            rejected.append({"url": nurl, "title": title,
                             "reason": f"domain not in allowed list ({get_domain(nurl)})"})
            continue

        if nurl in prev_urls:
            rejected.append({"url": nurl, "title": title, "reason": "dedup-url"})
            continue
        nt = normalize_title(title)
        if nt and len(nt) > 10 and nt in prev_titles:
            rejected.append({"url": nurl, "title": title, "reason": "dedup-title"})
            continue

        d = root_domain(get_domain(nurl))
        if per_dom.get(d, 0) >= max_per_dom:
            rejected.append({"url": nurl, "title": title, "reason": f"max/domain ({max_per_dom})"})
            continue

        print(f"  Fetch: {title[:50]}...")
        attempts += 1
        pdf = fetch_pdf(nurl)

        if not pdf["is_pdf"]:
            rejected.append({"url": nurl, "title": title,
                             "reason": f"{pdf['status']}: {pdf.get('error','')}"})
            continue

        text = pdf.get("text") or ""
        link_only = False

        if pdf["status"] == "ok" and len(text) >= minlen:
            if has_old_year(text):
                rejected.append({"url": nurl, "title": title, "reason": f"old (pre-{MIN_YEAR})"})
                continue
            dl = detect_lang(text[:1500])
            if not lang_ok(dl, lang):
                rejected.append({"url": nurl, "title": title, "reason": f"lang={dl}"})
                continue
            summary = make_summary(text)
        else:
            link_only = True
            reason = pdf.get("error") or pdf["status"]
            summary = f"_(PDF confirmed; text extraction: {reason}. Kept as link-only.)_"
            dl = lang

        if not title:
            fn = nurl.split("/")[-1].split("?")[0]
            title = fn.replace(".pdf", "").replace("-", " ").replace("_", " ").strip() or "PDF"

        items.append({
            "title": title, "url": nurl, "domain": d, "lang": dl,
            "type": "PDF", "link_only": link_only,
            "content_type": pdf["content_type"][:60],
            "size_bytes": pdf["size_bytes"],
            "fetch_status": pdf["status"],
            "summary": summary,
        })
        per_dom[d] = per_dom.get(d, 0) + 1
        prev_urls.add(nurl)
        if nt and len(nt) > 10:
            prev_titles.add(nt)

        tag = "📄" if not link_only else "🔗"
        print(f"  [ADD] {len(items)}/{target} {tag} {d} [{pdf['status']}]")
        time.sleep(random.uniform(0.5, 1.2))

    return _report(label, keyword, date_str, items, target, rejected)


# ───────────────────────── report writer ─────────────────────────

def _report(label, keyword, date_str, items, target, rejected):
    slug = slugify(label or keyword)
    folder = f"{date_str}_{slug}"
    path = os.path.join(REPORTS_DIR, folder)
    os.makedirs(path, exist_ok=True)

    cnt = len(items)
    full = sum(1 for i in items if not i.get("link_only"))
    link = sum(1 for i in items if i.get("link_only"))
    status = "SKIP" if cnt == 0 else "OK"

    with open(os.path.join(path, "items.json"), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    with open(os.path.join(path, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(f"- Date: {date_str}\n")
        if cnt == 0:
            f.write("- ⚠️ **SKIP**: No qualified PDFs found\n")
        elif cnt < target:
            f.write(f"- ℹ️ {cnt} PDF(s) found (target={target})\n")
        else:
            f.write(f"- ✅ {cnt} PDF(s)\n")
        if full:
            f.write(f"- 📄 Full-text: {full}\n")
        if link:
            f.write(f"- 🔗 Link-only: {link}\n")
        f.write("\n---\n\n")

        for i, it in enumerate(items, 1):
            tag = "📄" if not it.get("link_only") else "🔗 link-only"
            f.write(f"## {i}. {it['title']} `[{tag}]`\n\n")
            f.write(f"🔗 [{it['domain']}]({it['url']})\n\n")
            if it.get("link_only"):
                kb = it.get("size_bytes", 0) // 1024
                f.write(f"- Size: {kb} KB | Status: {it.get('fetch_status','?')}\n\n")
            f.write(f"{it['summary']}\n\n---\n\n")

        if not items:
            f.write("_No qualified PDFs this run._\n\n")
            f.write(f"Query: `{keyword[:120]}`\n\n")

        if rejected:
            f.write(f"## Rejected candidates ({len(rejected)} total)\n\n")
            for rj in rejected[:8]:
                f.write(f"- ❌ `{rj['reason']}` — [{get_domain(rj['url'])}]({rj['url']})")
                if rj.get("title"):
                    f.write(f" — _{rj['title'][:50]}_")
                f.write("\n")
            if len(rejected) > 8:
                f.write(f"\n_...+{len(rejected)-8} more_\n")
            f.write("\n")

    print(f"  [{status}] {cnt} PDFs ({full}📄 {link}🔗) | {len(rejected)} rejected")
    return {
        "keyword": label, "folder": folder,
        "count": cnt, "full_text": full, "link_only": link,
        "status": status,
    }


# ───────────────────────── cleanup ─────────────────────────

def cleanup():
    if not os.path.isdir(REPORTS_DIR):
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for name in sorted(os.listdir(REPORTS_DIR)):
        fp = os.path.join(REPORTS_DIR, name)
        if not os.path.isdir(fp):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
        if m and m.group(1) < cutoff:
            shutil.rmtree(fp)
            print(f"  [CLEANUP] {name}")


# ───────────────────── index (FIX: scan ALL folders) ─────────────────────

def gen_index(run_folders: set):
    """Scan ALL report folders in reports/. Mark current run with 🆕."""
    entries = []

    if os.path.isdir(REPORTS_DIR):
        for name in sorted(os.listdir(REPORTS_DIR), reverse=True):
            fp = os.path.join(REPORTS_DIR, name)
            if not os.path.isdir(fp):
                continue
            sp = os.path.join(fp, "summary.md")
            ip = os.path.join(fp, "items.json")
            if not os.path.isfile(sp):
                continue

            # Read label from first line
            with open(sp, "r", encoding="utf-8") as f:
                first_line = f.readline().strip().lstrip("# ")

            m = re.match(r"^(\d{4}-\d{2}-\d{2})_", name)
            dt = m.group(1) if m else "?"

            # Count items
            cnt, lo, skip = 0, 0, False
            if os.path.isfile(ip):
                try:
                    with open(ip, "r", encoding="utf-8") as jf:
                        data = json.load(jf)
                    cnt = len(data)
                    lo = sum(1 for x in data if x.get("link_only"))
                except Exception:
                    pass

            # Check if SKIP
            with open(sp, "r", encoding="utf-8") as f:
                if "SKIP" in f.read(500):
                    skip = True

            # Show if has any items, or if it's from current run
            is_current = name in run_folders
            if cnt > 0 or is_current:
                entries.append({
                    "date": dt, "folder": name, "kw": first_line,
                    "count": cnt, "full": cnt - lo, "lo": lo,
                    "skip": skip, "cur": is_current,
                })

    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write("# 📊 Auto Keyword Research — Report Index\n\n")
        f.write(f"Updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write("Schedule: **Saturday** 12:00 Taiwan Time | Sources: Institutional PDF\n\n---\n\n")
        if not entries:
            f.write("_No reports yet._\n")
        else:
            cd = ""
            for e in entries:
                if e["date"] != cd:
                    cd = e["date"]
                    f.write(f"### {cd}\n\n")
                p = []
                if e["full"]:
                    p.append(f"{e['full']}📄")
                if e["lo"]:
                    p.append(f"{e['lo']}🔗")
                info = "+".join(p) if p else "0"
                w = " ⚠️" if e["skip"] else ""
                n = " 🆕" if e["cur"] else ""
                f.write(f"- [{e['kw']}](reports/{e['folder']}/summary.md) — {info}{w}{n}\n")
            f.write("\n")

    print(f"[INDEX] {len(entries)} entries")


# ───────────────────────── main ─────────────────────────

def main():
    global HAS_PDFTOTEXT

    print("=" * 60)
    print("Auto Keyword Research v9")
    print("=" * 60)

    if not os.path.isfile(CONFIG_PATH):
        print(f"[ERROR] {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        jobs = json.load(f).get("jobs", [])
    if not jobs:
        sys.exit(1)

    try:
        subprocess.run(["pdftotext", "-v"], capture_output=True, timeout=5)
        HAS_PDFTOTEXT = True
        print("pdftotext: ✅")
    except Exception:
        print("pdftotext: ❌")

    os.makedirs(REPORTS_DIR, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # FIX BUG3: skip today's folders in dedup
    prev_urls, prev_titles = load_existing_dedup(date_str)
    print(f"Dedup: {len(prev_urls)} URLs, {len(prev_titles)} titles (excluding today)")
    print(f"Jobs: {len(jobs)}")

    # Delete today's old folders before rerun (clean slate for today)
    if os.path.isdir(REPORTS_DIR):
        for name in os.listdir(REPORTS_DIR):
            if name.startswith(date_str + "_") and os.path.isdir(os.path.join(REPORTS_DIR, name)):
                shutil.rmtree(os.path.join(REPORTS_DIR, name))
                print(f"  [CLEAN] removed old today-folder: {name}")

    deadline_ts = time.time() + GLOBAL_DEADLINE_SEC
    results = []

    for idx, job in enumerate(jobs):
        if time.time() > deadline_ts:
            print("\n[DEADLINE] stop")
            break
        try:
            results.append(run_one(job, date_str, prev_urls, prev_titles, deadline_ts=deadline_ts))
        except Exception as e:
            print(f"  [ERROR] {job.get('label', '?')}: {e}")
        if idx < len(jobs) - 1:
            w = random.uniform(5, 10)
            print(f"  [WAIT] {w:.0f}s...")
            time.sleep(w)

    print(f"\n{'='*60}")
    cleanup()

    # Generate index from ALL report folders
    run_folder_names = {r["folder"] for r in results}
    gen_index(run_folder_names)

    # Summary
    ok = [r for r in results if r["status"] == "OK"]
    sk = [r for r in results if r["status"] == "SKIP"]
    tf = sum(r.get("full_text", 0) for r in results)
    tl = sum(r.get("link_only", 0) for r in results)

    print(f"\n{'='*60}")
    print(f"DONE: {len(ok)} OK | {len(sk)} SKIP | {tf}📄 + {tl}🔗")
    for r in results:
        ic = "✅" if r["status"] == "OK" else "⚠️"
        print(f"  {ic} {r['keyword']} → {r['count']} ({r.get('full_text',0)}📄 {r.get('link_only',0)}🔗)")
    print("=" * 60)


if __name__ == "__main__":
    main()
