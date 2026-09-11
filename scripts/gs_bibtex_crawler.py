#!/usr/bin/env python3
"""Batch-fetch literal Google Scholar Cite->BibTeX exports for a pipe-delimited title list.

Input format (one entry per block):
    NNNN | citation_key | exact title to search
optionally followed by an embedded BibTeX block. Embedded blocks are used only as
identity cross-check hints; the input file is never modified.

Engine: Playwright persistent real-Chrome context. Plain HTTP clients get
CAPTCHA-walled on the /scholar.bib export endpoint; a real browser with JS,
accumulated cookies (GSP/NID) and human-like pacing passes.

Outputs (all under --outdir, never inside a paper archive):
    <citation_key>.bib                     literal export bytes, single trailing newline
    results.csv / results.jsonl            per-entry status report (jsonl includes hit dumps)
    google_scholar_bibtex_filled.txt       input format + freshly fetched bib per entry
    review_queue.csv                       entries needing human review
    checkpoint.json                        progress counters for resume
    crawler.log                            timestamped run log
    debug/*.html                           page dumps on unexpected layouts
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import random
import re
import shutil
import socket
import sys
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

REPO = Path(__file__).resolve().parent

ENTRY_RE = re.compile(r"^(\d{4})\s*\|\s*(\S+)\s*\|\s*(.+?)\s*$")
ENTRY_START_RE = re.compile(r"^@[A-Za-z]+\{[^,\n]+,", re.MULTILINE)
BIB_TITLE_RE = re.compile(r"title\s*=\s*\{(.+?)\}", re.DOTALL)
KEY_HINT_RE = re.compile(r"^([a-z]+?)(\d{4})")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\{([^{}]*)\}")
TITLE_MARKER_RE = re.compile(r"^\[[^\]]{1,15}\]\s*")

BLOCK_MARKERS = ("unusual traffic", "not a robot", "systems have detected")
BACKOFF_LADDER = (60, 180, 600, 1800)
GREEN, YELLOW, RED, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[0m"

CSV_COLUMNS = [
    "citation_key", "ordinal", "status", "matched_title", "similarity", "strategy",
    "scholar_search_url", "bib_path", "identity_source_candidate", "review_reason",
    "embedded_present", "embedded_sim", "error", "timestamp_utc",
]

HITS_JS = """
() => {
  const rows = document.querySelectorAll('div.gs_r.gs_or');
  const out = [];
  rows.forEach((row, i) => {
    const h3 = row.querySelector('h3.gs_rt');
    if (!h3) return;
    const a = h3.querySelector('a[href]');
    const meta = row.querySelector('.gs_a');
    const cit = row.querySelector('a.gs_or_cit, button.gs_or_cit');
    let title = (h3.innerText || h3.textContent || '').trim();
    title = title.replace(/^\\[[^\\]]{1,15}\\]\\s*/, '');
    out.push({
      row_index: i,
      title: title,
      href: a ? a.href : null,
      cid: row.getAttribute('data-cid') || (a ? a.id : null),
      meta: meta ? (meta.innerText || '').trim() : '',
      has_cite: !!cit
    });
  });
  return out;
}
"""

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


class BlockError(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass
class Entry:
    ordinal: int
    citation_key: str
    title: str
    embedded_bib: str | None = None


@dataclass
class EntryResult:
    citation_key: str
    ordinal: int
    status: str
    matched_title: str = ""
    similarity: float = 0.0
    strategy: str = ""
    scholar_search_url: str = ""
    bib_path: str = ""
    identity_source_candidate: str = ""
    review_reason: str = ""
    embedded_present: bool = False
    embedded_sim: float = -1.0
    error: str = ""
    timestamp_utc: str = ""
    hits: list = field(default_factory=list)


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

def debrace_title(t: str) -> str:
    prev = None
    while prev != t:
        prev = t
        t = LATEX_CMD_RE.sub(r"\1", t)
    t = t.replace("{", "").replace("}", "")
    t = t.replace("\\&", "&").replace("\\%", "%").replace("~", " ")
    return re.sub(r"\s+", " ", t).strip()


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


# --------------------------------------------------------------------------
# input parsing
# --------------------------------------------------------------------------

def parse_input_list(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    cur: Entry | None = None
    bib_lines: list[str] = []
    in_bib = False

    def finish_bib():
        nonlocal bib_lines, in_bib
        if cur is not None and bib_lines:
            cur.embedded_bib = "\n".join(bib_lines).strip()
        bib_lines, in_bib = [], False

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            m = ENTRY_RE.match(line)
            if m:
                finish_bib()
                cur = Entry(int(m.group(1)), m.group(2), m.group(3))
                entries.append(cur)
                continue
            if cur is None:
                continue
            s = line.strip()
            if not in_bib:
                if s.startswith("@"):
                    in_bib = True
                    bib_lines = [line]
                    if s.endswith("}") and line.count("{") == line.count("}"):
                        finish_bib()
            else:
                if not s:
                    continue
                bib_lines.append(line)
                if s == "}":
                    finish_bib()
    finish_bib()
    return entries


def load_manifest_hints(path: Path) -> dict[str, dict]:
    hints: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("citation_key") or "").strip()
            if not key:
                continue
            surname = None
            author = (row.get("candidate_author") or "").strip()
            if author:
                first = author.split(" and ")[0].strip()
                if first:
                    surname = first.split()[-1]
            year = None
            y = (row.get("candidate_year") or "").strip()
            if y.isdigit():
                year = int(y)
            hints[key] = {"surname": surname, "year": year}
    return hints


def key_hints(key: str) -> dict:
    m = KEY_HINT_RE.match(key)
    if m:
        return {"surname": m.group(1), "year": int(m.group(2))}
    return {"surname": None, "year": None}


def build_strategies(entry: Entry, hint: dict) -> list[tuple[str, str]]:
    full = debrace_title(entry.title)
    main = full.split(":", 1)[0].strip()
    out = [("raw", full), ("quoted", f'"{full}"')]
    if main and normalize(main) != normalize(full) and len(normalize(main)) >= 12:
        out.append(("maintitle", main))
    surname = hint.get("surname")
    if surname:
        base = main if (main and len(main) < len(full) * 0.75) else full
        out.append(("boost", f"{base} {surname}"))
    seen, dedup = set(), []
    for name, q in out:
        k = q.strip()
        if k and k not in seen:
            seen.add(k)
            dedup.append((name, q))
    return dedup[:5]


# --------------------------------------------------------------------------
# bib validation (mirrors the archive importer contract)
# --------------------------------------------------------------------------

def validate_bib(data: bytes) -> tuple[bytes | None, str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not valid utf-8"
    if text.lstrip().startswith("<"):
        return None, "endpoint returned HTML, not BibTeX"
    n = len(ENTRY_START_RE.findall(text))
    if n != 1:
        return None, f"expected exactly one @entry, found {n}"
    if not text.lstrip().startswith("@"):
        return None, "does not start with @"
    if not text.rstrip().endswith("}"):
        return None, "does not end with }"
    if not text.endswith("\n"):
        text += "\n"
        data = text.encode("utf-8")
    return data, ""


def embedded_bib_title(bib: str) -> str:
    m = BIB_TITLE_RE.search(bib)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# browser plumbing
# --------------------------------------------------------------------------

def detect_proxy(args) -> str | None:
    if args.no_proxy:
        return None
    if args.proxy:
        return args.proxy
    for var in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(var)
        if v:
            return v
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", 7890))
        return "http://127.0.0.1:7890"
    except OSError:
        return None
    finally:
        s.close()


def load_cookies_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json" or text.lstrip().startswith("{"):
        state = json.loads(text)
        return state.get("cookies", state if isinstance(state, list) else [])
    cookies = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, cpath, secure, expiry, name, value = parts[:7]
        cookies.append({
            "name": name, "value": value, "domain": domain, "path": cpath,
            "expires": float(expiry) if expiry.isdigit() else -1,
            "secure": secure.upper() == "TRUE",
        })
    return cookies


def launch_context(p, cfg, logger):
    kwargs = dict(
        headless=cfg.headless,
        proxy={"server": cfg.proxy} if cfg.proxy else None,
        locale="en-US",
        viewport=None,
        ignore_default_args=["--enable-automation"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    try:
        context = p.chromium.launch_persistent_context(str(cfg.profile_dir), channel="chrome", **kwargs)
    except Exception as e:
        logger(f"channel=chrome launch failed ({e}); falling back to bundled chromium")
        context = p.chromium.launch_persistent_context(str(cfg.profile_dir), **kwargs)
    context.add_init_script(STEALTH_JS)
    if cfg.cookies:
        cookies = load_cookies_file(cfg.cookies)
        if cookies:
            context.add_cookies(cookies)
            logger(f"injected {len(cookies)} cookies from {cfg.cookies}")
    return context


def dump_debug(page, cfg, tag: str):
    try:
        d = cfg.outdir / "debug"
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (d / f"{tag}-{ts}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


BANNER_JS = """(txt) => {
    let el = document.getElementById('qoder_crawler_banner');
    if (!el) {
        el = document.createElement('div');
        el.id = 'qoder_crawler_banner';
        el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;'
            + 'background:#1a73e8;color:#fff;font:12px/1.5 monospace;padding:6px 12px;'
            + 'white-space:pre-wrap;pointer-events:none;';
        (document.body || document.documentElement).appendChild(el);
    }
    el.textContent = txt;
    try { document.title = '[crawler] ' + txt.split('\\n')[0]; } catch (e) {}
}"""


def set_status(page, text: str):
    try:
        page.evaluate(BANNER_JS, text)
    except Exception:
        pass


def is_blocked(page) -> bool:
    try:
        if "/sorry/" in (page.url or ""):
            return True
        body = page.inner_text("body", timeout=5000)[:2000].lower()
    except Exception:
        return False
    return any(m in body for m in BLOCK_MARKERS)


def page_has_captcha(page) -> bool:
    try:
        if "/sorry/" in (page.url or ""):
            return True
        return page.locator("iframe[src*='recaptcha']").count() > 0
    except Exception:
        return False


def probe_unblocked(page, cfg) -> bool:
    """True only when a real search request returns a normal results page."""
    try:
        resp = page.goto(f"https://{cfg.domain}/scholar?hl=en&q=acoustics",
                         wait_until="domcontentloaded", timeout=45000)
        if resp is not None and resp.status == 429:
            return False
        if "/sorry/" in (page.url or ""):
            return False
        try:
            page.wait_for_selector("div.gs_ri", timeout=8000)
            return True
        except PWTimeout:
            body = page.inner_text("body", timeout=5000).lower()
            return "did not match any articles" in body
    except Exception:
        return False


def handle_block(page, cfg, logger) -> bool:
    """Wait out a block: manual CAPTCHA solve (if interactive) + backoff,
    verified by a real search probe (the homepage is NOT a valid unblock signal)."""
    for attempt in range(cfg.max_block_retries):
        backoff = BACKOFF_LADDER[min(attempt, len(BACKOFF_LADDER) - 1)]
        for d in getattr(cfg, "domains_list", []):
            if d == cfg.domain:
                continue
            cfg.domain = d
            if probe_unblocked(page, cfg):
                logger(f"  switched to mirror {d} (previous domain blocked)", YELLOW)
                return True
        set_status(page, f"BLOCKED (attempt {attempt + 1}/{cfg.max_block_retries})\n"
                         f"if a CAPTCHA is visible in THIS window, solve it manually — auto-resume;\n"
                         f"otherwise waiting out rate-limit backoff ({backoff}s ladder)")
        try:
            d = cfg.outdir / "debug"
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            page.screenshot(path=str(d / f"block-{ts}.png"))
        except Exception:
            pass
        if page_has_captcha(page):
            logger(f"  BLOCKED with interactive CAPTCHA. Polling up to {cfg.block_timeout}s — "
                   f"solve it in the Chrome window; success is detected automatically.", RED)
            deadline = time.time() + cfg.block_timeout
            while time.time() < deadline:
                time.sleep(15)
                try:
                    if "/sorry/" not in (page.url or "") and page.locator("div.gs_ri").count() > 0:
                        logger("  block cleared (results visible after manual solve)", GREEN)
                        return True
                except Exception:
                    pass
        else:
            logger(f"  BLOCKED (rate-limit page, no interactive CAPTCHA). "
                   f"Backing off {backoff}s, then probing the search endpoint.", RED)
        time.sleep(backoff)
        if probe_unblocked(page, cfg):
            logger("  block cleared (search probe OK)", GREEN)
            return True
        logger(f"  still blocked after probe (attempt {attempt + 1}/{cfg.max_block_retries})", YELLOW)
    return False


def do_search(page, query: str, cfg, state) -> tuple[list[dict], str]:
    url = f"https://{cfg.domain}/scholar?hl=en&q={urllib.parse.quote(query)}"
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if resp is not None and resp.status == 429:
        raise BlockError("HTTP 429 on search")
    if is_blocked(page):
        raise BlockError("sorry/CAPTCHA page after search")
    hits: list[dict] = []
    try:
        page.wait_for_selector("div.gs_ri", timeout=12000)
        hits = page.evaluate(HITS_JS)
    except PWTimeout:
        hits = []
    if not hits:
        try:
            body = page.inner_text("body", timeout=5000).lower()
        except Exception:
            body = ""
        if "did not match any articles" in body:
            return [], page.url
        if not state.get("saw_hits"):
            dump_debug(page, cfg, "noresults-firstentry")
            raise FetchError("no results container and no 'did not match' text — possible UI change (see debug dump)")
        dump_debug(page, cfg, "noresults")
    else:
        state["saw_hits"] = True
    return hits, page.url


def pick_match(hits: list[dict], entry_norm: str, main_norm: str, cfg, hint: dict):
    best = None
    hint_year = hint.get("year")
    for h in hits[: cfg.top_n]:
        if not h.get("has_cite"):
            continue
        tnorm = normalize(h.get("title", ""))
        if not tnorm:
            continue
        sim = similarity(entry_norm, tnorm)
        if main_norm and len(main_norm) >= 20 and (
            tnorm.startswith(main_norm) or main_norm.startswith(tnorm)
        ):
            sim = max(sim, 0.95)
        years = YEAR_RE.findall(h.get("meta", ""))
        hit_year = int(years[-1]) if years else None
        year_warning = ""
        if hint_year and hit_year:
            if abs(hit_year - hint_year) <= 1:
                sim = min(1.0, sim + 0.01)
            else:
                year_warning = f"year_mismatch(hit={hit_year},expected={hint_year})"
        cand = (sim, h, year_warning)
        if best is None or cand[0] > best[0]:
            best = cand
    return best


def fetch_bibtex(context, page, hit: dict, cfg) -> tuple[bytes, str]:
    # Google only serves scholar.bib to navigation-style requests (what a manual
    # Cite->BibTeX click sends); cors/fetch requests get 302'd to a cross-origin
    # sorry page. So always fetch via real same-tab navigation.
    def _open_dialog():
        row = page.locator("div.gs_r.gs_or").nth(hit["row_index"])
        cit = row.locator("a.gs_or_cit, button.gs_or_cit").first
        cit.scroll_into_view_if_needed(timeout=5000)
        cit.click(timeout=8000)
        link = page.locator("#gs_cit a, #gs_citd a, .gs_cit_box a", has_text="BibTeX").first
        link.wait_for(state="attached", timeout=12000)
        return link

    href = None
    try:
        link = _open_dialog()
        href = link.get_attribute("href")
        with page.expect_response(
                lambda r: "scholar.bib" in r.url and r.status == 200, timeout=30000) as ri:
            link.click(timeout=8000)
        resp = ri.value
        data = resp.body()
        url = resp.url
        page.go_back(wait_until="domcontentloaded", timeout=30000)
        if data.lstrip()[:1] == b"<":
            raise BlockError(f"bib endpoint returned HTML on {cfg.domain}")
        if data.strip():
            return data, url
        raise BlockError(f"bib endpoint returned empty body on {cfg.domain}")
    except PWTimeout:
        try:
            if "/sorry/" in (page.url or ""):
                page.go_back(wait_until="domcontentloaded", timeout=15000)
                raise BlockError(f"bib navigation redirected to CAPTCHA on {cfg.domain}")
        except BlockError:
            raise
        except Exception:
            pass
    except BlockError:
        raise
    except Exception:
        pass
    finally:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    url = None
    if href:
        url = urllib.parse.urljoin(f"https://{cfg.domain}/", href)
    elif hit.get("cid"):
        url = (f"https://{cfg.domain}/scholar.bib?q=info:{hit['cid']}:scholar.google.com/"
               f"&output=cite&scirp=c&hl=en")
    if not url:
        dump_debug(page, cfg, f"nocite-{safe_filename(hit.get('title', 'x')[:30])}")
        raise FetchError("cite dialog unavailable and no document id to construct bib url")
    resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        if resp is None or resp.status != 200 or "/sorry/" in (page.url or ""):
            raise BlockError(f"bib navigation failed on {cfg.domain} "
                             f"(status={resp.status if resp else 'none'})")
        data = resp.body()
    finally:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
    if data.lstrip()[:1] == b"<":
        raise BlockError(f"bib endpoint returned HTML on {cfg.domain}")
    if not data.strip():
        raise BlockError(f"bib endpoint returned empty body on {cfg.domain}")
    return data, url


# --------------------------------------------------------------------------
# per-entry processing
# --------------------------------------------------------------------------

def _process_entry_once(page, context, entry: Entry, cfg, hint: dict, state, logger) -> EntryResult:
    set_status(page, f"{entry.ordinal:04d} {entry.citation_key}\nsearching Scholar (strategy ladder)…")
    entry_clean = debrace_title(entry.title)
    entry_norm = normalize(entry_clean)
    main_norm = normalize(entry_clean.split(":", 1)[0].strip())
    best = None  # (sim, hit, year_warning, strategy, search_url)
    last_url = ""
    for name, query in build_strategies(entry, hint):
        hits, url = do_search(page, query, cfg, state)
        last_url = url
        m = pick_match(hits, entry_norm, main_norm, cfg, hint)
        if m and (best is None or m[0] > best[0]):
            best = (m[0], m[1], m[2], name, url)
        if best and best[0] >= cfg.exact_threshold:
            break
        time.sleep(random.uniform(3.0, 8.0))

    base = dict(
        citation_key=entry.citation_key, ordinal=entry.ordinal,
        embedded_present=bool(entry.embedded_bib), timestamp_utc=utcnow(),
    )
    if best is None or best[0] < cfg.fuzzy_threshold:
        return EntryResult(
            status="not_found", review_reason="not_found",
            matched_title=best[1]["title"] if best else "",
            similarity=round(best[0], 4) if best else 0.0,
            strategy=best[3] if best else "",
            scholar_search_url=best[4] if best else last_url,
            identity_source_candidate=(best[1].get("href") or "") if best else "",
            hits=[best[1]] if best else [], **base,
        )

    sim, hit, year_warning, strategy, search_url = best
    set_status(page, f"{entry.citation_key}: matched sim={sim:.2f} via {strategy}\nfetching BibTeX…")
    time.sleep(random.uniform(5.0, 10.0))
    raw_data, _bib_url, bib_err = None, "", None
    for bib_try in range(2):
        try:
            raw_data, _bib_url = fetch_bibtex(context, page, hit, cfg)
            break
        except BlockError as be:
            bib_err = be
            if bib_try < 1:
                wait = random.uniform(3.0, 8.0)
                prev_domain = cfg.domain
                dl = getattr(cfg, "domains_list", [])
                if dl:
                    idx = dl.index(cfg.domain) if cfg.domain in dl else -1
                    cfg.domain = dl[(idx + 1) % len(dl)]
                logger(f"  bib endpoint challenged on {prev_domain} ({be}); switched to "
                       f"{cfg.domain}, retry in {wait:.0f}s", YELLOW)
                set_status(page, f"{entry.citation_key}: bib challenged on {prev_domain}\n"
                                 f"switched to {cfg.domain}, retrying in {wait:.0f}s…")
                time.sleep(wait)
                try:
                    if "/sorry/" in (page.url or ""):
                        page.go_back(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
    if raw_data is None:
        raise BlockError(f"bib endpoint challenged after retries: {bib_err}")
    data, verr = validate_bib(raw_data)
    if data is None:
        try:
            d = cfg.outdir / "debug"
            d.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            (d / f"invalidbib-{safe_filename(entry.citation_key)}-{ts}.bin").write_bytes(raw_data)
        except Exception:
            pass
        return EntryResult(status="error", error=f"invalid bib: {verr}",
                           matched_title=hit["title"], similarity=round(sim, 4),
                           strategy=strategy, scholar_search_url=search_url, **base)

    status = "exact_match" if sim >= cfg.exact_threshold else "fuzzy_match"
    reasons = []
    if status == "fuzzy_match":
        reasons.append("low_similarity")
    if year_warning:
        reasons.append(year_warning)

    emb_sim = -1.0
    if entry.embedded_bib:
        emb_title = embedded_bib_title(entry.embedded_bib)
        if emb_title:
            emb_sim = round(similarity(normalize(debrace_title(emb_title)),
                                       normalize(hit["title"])), 4)
            if emb_sim < 0.6:
                reasons.append(f"embedded_mismatch(sim={emb_sim})")

    bib_path = cfg.outdir / f"{safe_filename(entry.citation_key)}.bib"
    bib_path.write_bytes(data)
    logger(f"  bib obtained -> {bib_path.name}:\n"
           + data.decode("utf-8", "replace").rstrip(), GREEN)
    return EntryResult(
        status=status, matched_title=hit["title"], similarity=round(sim, 4),
        strategy=strategy, scholar_search_url=search_url, bib_path=str(bib_path),
        identity_source_candidate=hit.get("href") or "",
        review_reason=";".join(reasons), embedded_sim=emb_sim, hits=[hit], **base,
    )


def process_entry(page, context, entry: Entry, cfg, hint: dict, state, logger) -> EntryResult:
    for attempt in range(2):
        try:
            return _process_entry_once(page, context, entry, cfg, hint, state, logger)
        except BlockError as be:
            logger(f"  block during {entry.citation_key}: {be}", RED)
            if attempt == 1 or not handle_block(page, cfg, logger):
                return EntryResult(
                    citation_key=entry.citation_key, ordinal=entry.ordinal,
                    status="blocked", review_reason="blocked", error=str(be),
                    embedded_present=bool(entry.embedded_bib), timestamp_utc=utcnow(),
                )
        except FetchError as fe:
            return EntryResult(
                citation_key=entry.citation_key, ordinal=entry.ordinal,
                status="error", review_reason="error", error=str(fe),
                embedded_present=bool(entry.embedded_bib), timestamp_utc=utcnow(),
            )
        except PWTimeout as te:
            return EntryResult(
                citation_key=entry.citation_key, ordinal=entry.ordinal,
                status="error", review_reason="error", error=f"timeout: {te}"[:300],
                embedded_present=bool(entry.embedded_bib), timestamp_utc=utcnow(),
            )
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# result persistence
# --------------------------------------------------------------------------

def write_result(result: EntryResult, cfg, logger):
    d = asdict(result)
    color = (GREEN if result.status in ("exact_match", "fuzzy_match")
             else RED if result.status in ("blocked", "error") else YELLOW)
    csv_path = cfg.outdir / "results.csv"
    new_file = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                           lineterminator="\n")
        if new_file:
            w.writeheader()
        w.writerow(d)
    with open(cfg.outdir / "results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")
    logger(f"  -> {result.status}"
           + (f" sim={result.similarity:.3f} strategy={result.strategy}" if result.matched_title else "")
           + (f" reason={result.review_reason}" if result.review_reason else "")
           + (f" error={result.error[:120]}" if result.error else ""), color)


def latest_results(cfg) -> dict[str, dict]:
    path = cfg.outdir / "results.csv"
    out: dict[str, dict] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["citation_key"]] = row
    return out


def write_checkpoint(cfg, state, entries_total: int, done: int):
    state.update({"updated_utc": utcnow(), "entries_selected": entries_total, "done": done})
    (cfg.outdir / "checkpoint.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_review_queue(cfg):
    rows = [r for r in latest_results(cfg).values()
            if r.get("status") != "exact_match" or r.get("review_reason")]
    path = cfg.outdir / "review_queue.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x.get("ordinal", "0")):
            w.writerow(r)
    return len(rows)


def build_filled_txt(entries: list[Entry], cfg):
    lines = [
        "# Google Scholar BibTeX filled list (GENERATED FILE — original input untouched)",
        f"# Generated: {utcnow()} by gs_bibtex_crawler.py",
        f"# Source input: {cfg.input}",
        "# Each entry: ordinal | citation_key | title, followed by the freshly fetched",
        "# literal Scholar BibTeX when available, else the embedded block from the input.",
        "",
    ]
    filled = 0
    for e in entries:
        lines.append(f"{e.ordinal:04d} | {e.citation_key} | {e.title}")
        bib_path = cfg.outdir / f"{safe_filename(e.citation_key)}.bib"
        bib_text = None
        if bib_path.exists():
            bib_text = bib_path.read_text(encoding="utf-8").strip()
            filled += 1
        elif e.embedded_bib:
            bib_text = e.embedded_bib.strip()
        if bib_text:
            lines.append(bib_text)
        lines.append("")
    out = cfg.outdir / "google_scholar_bibtex_filled.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return filled, out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parse_ordinals_spec(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=Path, required=True,
                    help="pipe-delimited title list: 'NNNN | citation_key | title' per entry")
    ap.add_argument("--outdir", type=Path, default=Path("output"))
    ap.add_argument("--manifest", type=Path, default=None,
                    help="optional manifest.csv for author/year hints + identity cross-check")
    ap.add_argument("--keys", default=None, help="comma-separated citation keys to process")
    ap.add_argument("--ordinals", default=None, help="e.g. 0024-0030,0079")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only-missing", action="store_true",
                    help="also skip not_found rows from results.csv (disk-based skipping is always on)")
    ap.add_argument("--force", action="store_true",
                    help="re-fetch even when a valid .bib already exists on disk")
    ap.add_argument("--retry-notfound", action="store_true",
                    help="with --only-missing, also retry not_found entries")
    ap.add_argument("--skip-embedded", action="store_true",
                    help="skip entries that already embed a BibTeX block in the input")
    ap.add_argument("--headless", action="store_true", help="run without visible browser window")
    ap.add_argument("--proxy", default=None, help="e.g. http://127.0.0.1:7890")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--cookies", type=Path, default=None,
                    help="Playwright storage_state JSON or Netscape cookies.txt to inject")
    ap.add_argument("--user-data-dir", type=Path, default=None,
                    help="persistent Chrome profile dir (default: ~/.cache/gs-bibtex-crawler/chrome_profile)")
    ap.add_argument("--reset-profile", action="store_true")
    ap.add_argument("--min-delay", type=float, default=5.0)
    ap.add_argument("--max-delay", type=float, default=20.0)
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--exact-threshold", type=float, default=0.97)
    ap.add_argument("--fuzzy-threshold", type=float, default=0.85)
    ap.add_argument("--max-block-retries", type=int, default=3)
    ap.add_argument("--block-timeout", type=int, default=600,
                    help="seconds to poll for manual CAPTCHA solving per block episode")
    ap.add_argument("--consecutive-block-abort", type=int, default=3)
    ap.add_argument("--domain", default=None,
                    help="initial domain override; default: first working domain from --domains")
    ap.add_argument("--domains", default="scholar.google.com,scholar.google.com.pr,scholar.google.com.pk",
                    help="comma-separated mirror failover order (block state is per-domain)")
    ap.add_argument("--parse-only", action="store_true")
    ap.add_argument("--build-filled-only", action="store_true")
    return ap


def main() -> int:
    args = build_argparser().parse_args()
    args.domains_list = [x.strip() for x in args.domains.split(",") if x.strip()]
    if not args.domain:
        args.domain = args.domains_list[0]

    input_path: Path = args.input
    if not input_path.exists():
        print(f"ERROR: input not found: {input_path}", file=sys.stderr)
        return 1
    outdir: Path = args.outdir

    entries = parse_input_list(input_path)
    if not entries:
        print("ERROR: no entries parsed from input", file=sys.stderr)
        return 1
    keys = [e.citation_key for e in entries]
    dupes = {k for k in keys if keys.count(k) > 1}

    if args.parse_only:
        emb = sum(1 for e in entries if e.embedded_bib)
        braces = sum(1 for e in entries if "{" in e.title)
        colons = sum(1 for e in entries if ":" in e.title)
        print(f"entries parsed:        {len(entries)}")
        print(f"with embedded bibtex:  {emb}")
        print(f"titles with braces:    {braces}")
        print(f"titles with colon:     {colons}")
        print(f"duplicate keys:        {sorted(dupes) if dupes else 'none'}")
        print(f"ordinal range:         {entries[0].ordinal:04d}..{entries[-1].ordinal:04d}")
        for e in entries[:3]:
            print(f"  sample: {e.ordinal:04d} | {e.citation_key} | {e.title[:60]} "
                  f"| embedded={'yes' if e.embedded_bib else 'no'}")
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    log_file = open(outdir / "crawler.log", "a", encoding="utf-8")

    def logger(msg: str, color: str = ""):
        line = f"[{utcnow()}] {msg}"
        print(f"{color}{line}{RESET}" if color else line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    if args.build_filled_only:
        filled, out = build_filled_txt(entries, args)
        logger(f"filled.txt rebuilt: {filled}/{len(entries)} entries have bib -> {out}")
        return 0

    if args.user_data_dir is None:
        args.user_data_dir = Path.home() / ".cache" / "gs-bibtex-crawler" / "chrome_profile"
    args.user_data_dir.mkdir(parents=True, exist_ok=True)
    if args.reset_profile and args.user_data_dir.exists():
        shutil.rmtree(args.user_data_dir)
        logger("chrome profile reset")

    hints_db = {}
    if args.manifest and args.manifest.exists():
        hints_db = load_manifest_hints(args.manifest)
        logger(f"manifest hints loaded for {len(hints_db)} keys")
    elif args.manifest:
        logger(f"WARNING: manifest not found: {args.manifest}")

    selected = entries
    if args.keys:
        want = {k.strip() for k in args.keys.split(",") if k.strip()}
        selected = [e for e in selected if e.citation_key in want]
        missing = want - {e.citation_key for e in selected}
        if missing:
            logger(f"WARNING: keys not in input: {sorted(missing)}")
    if args.ordinals:
        ords = parse_ordinals_spec(args.ordinals)
        selected = [e for e in selected if e.ordinal in ords]
    if args.skip_embedded:
        selected = [e for e in selected if not e.embedded_bib]
    if not args.force:
        def _valid_bib_on_disk(e: Entry) -> bool:
            p = args.outdir / f"{safe_filename(e.citation_key)}.bib"
            if not p.exists():
                return False
            try:
                return validate_bib(p.read_bytes())[0] is not None
            except Exception:
                return False
        before = len(selected)
        selected = [e for e in selected if not _valid_bib_on_disk(e)]
        if before - len(selected):
            logger(f"skipped {before - len(selected)} keys with valid .bib already on disk")
    prior = latest_results(args) if args.only_missing else {}
    if args.only_missing:
        def completed(e: Entry) -> bool:
            r = prior.get(e.citation_key)
            if not r:
                return False
            st = r.get("status")
            if st in ("exact_match", "fuzzy_match"):
                bib = args.outdir / f"{safe_filename(e.citation_key)}.bib"
                if bib.exists():
                    data, err = validate_bib(bib.read_bytes())
                    return data is not None
                return False
            if st == "not_found":
                return not args.retry_notfound
            return False  # blocked/error -> retry
        before = len(selected)
        selected = [e for e in selected if not completed(e)]
        logger(f"--only-missing: {before - len(selected)} already done, {len(selected)} to fetch")
    if args.limit is not None:
        selected = selected[: args.limit]

    if not selected:
        logger("nothing to do")
        build_review_queue(args)
        build_filled_txt(entries, args)
        return 0

    proxy = detect_proxy(args)
    args.proxy = proxy
    args.profile_dir = args.user_data_dir
    logger(f"run start: {len(selected)} entries | domain={args.domain} | "
           f"proxy={proxy or 'NONE (direct)'} | headless={args.headless} | "
           f"delays={args.min_delay}-{args.max_delay}s")
    if not proxy:
        logger("WARNING: no proxy detected — Google is unreachable directly on this machine")

    state = {"started_utc": utcnow(), "saw_hits": False, "blocks": 0,
             "counts": {}, "input": str(input_path)}
    aborted = False
    consecutive_blocks = 0
    done = 0

    with sync_playwright() as p:
        context = launch_context(p, args, logger)
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(30000)
        try:
            page.goto(f"https://{args.domain}/", wait_until="domcontentloaded", timeout=60000)
            if "consent.google" in (page.url or ""):
                try:
                    page.locator("button", has_text=re.compile("I agree|Accept all|Agree")).first.click(timeout=6000)
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    logger("consent interstitial accepted")
                except Exception:
                    logger("WARNING: consent interstitial present but could not auto-accept")
            if is_blocked(page):
                logger("blocked already at warm-up")
                if not handle_block(page, args, logger):
                    logger("ABORT: blocked at warm-up and not cleared")
                    return 2
            chosen = None
            for d in args.domains_list:
                args.domain = d
                if probe_unblocked(page, args):
                    chosen = d
                    break
            if chosen is None:
                logger("WARNING: every domain blocked at warm-up")
                if not handle_block(page, args, logger):
                    for d in args.domains_list:
                        args.domain = d
                        if probe_unblocked(page, args):
                            chosen = d
                            break
            if chosen is None:
                logger("ABORT: search endpoint blocked on all mirrors — switch the proxy exit "
                       "node or wait for the IP cooldown, then re-run with --only-missing")
                return 3
            args.domain = chosen
            logger(f"warm-up OK: search endpoint reachable on {chosen}")
            time.sleep(random.uniform(3.0, 6.0))

            for i, entry in enumerate(selected):
                hint = dict(key_hints(entry.citation_key))
                if entry.citation_key in hints_db:
                    hint.update({k: v for k, v in hints_db[entry.citation_key].items() if v})
                if not (hint.get("surname") or "").strip():
                    hint["surname"] = key_hints(entry.citation_key)["surname"]
                logger(f"[{i + 1}/{len(selected)}] {entry.ordinal:04d} {entry.citation_key}: "
                       f"{entry.title[:70]}")
                result = process_entry(page, context, entry, args, hint, state, logger)
                write_result(result, args, logger)
                done += 1
                state["counts"][result.status] = state["counts"].get(result.status, 0) + 1
                if result.status == "blocked":
                    state["blocks"] += 1
                    consecutive_blocks += 1
                    if consecutive_blocks >= args.consecutive_block_abort:
                        logger(f"ABORT: {consecutive_blocks} consecutive blocked entries — "
                               f"IP is likely burned. Resume later (plain command skips done).", RED)
                        aborted = True
                        break
                else:
                    consecutive_blocks = 0
                write_checkpoint(args, state, len(selected), done)
                if i < len(selected) - 1:
                    d = random.uniform(args.min_delay, args.max_delay)
                    set_status(page, f"[{i + 1}/{len(selected)}] {entry.citation_key} -> {result.status}\n"
                                     f"idle {d:.0f}s (anti-block pacing — window looking frozen is NORMAL)")
                    logger(f"  idle {d:.0f}s before next entry")
                    time.sleep(d)
        except KeyboardInterrupt:
            logger("interrupted by user — progress saved; resume with --only-missing")
            aborted = True
        finally:
            try:
                context.close()
            except Exception:
                pass

    write_checkpoint(args, state, len(selected), done)
    n_review = build_review_queue(args)
    filled, filled_path = build_filled_txt(entries, args)
    pending = []
    for e in entries:
        bibp = args.outdir / f"{safe_filename(e.citation_key)}.bib"
        ok = False
        if bibp.exists():
            try:
                ok = validate_bib(bibp.read_bytes())[0] is not None
            except Exception:
                ok = False
        if not ok:
            pending.append(e.citation_key)
    (args.outdir / "pending_keys.txt").write_text(
        "\n".join(pending) + ("\n" if pending else ""), encoding="utf-8")
    logger(f"run end: done={done}/{len(selected)} counts={state['counts']} "
           f"review_queue={n_review} filled={filled}/{len(entries)} -> {filled_path}")
    logger(f"pending after this round: {len(pending)} keys -> pending_keys.txt")
    log_file.close()
    if aborted:
        return 2
    bad = state["counts"].get("blocked", 0) + state["counts"].get("error", 0)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
