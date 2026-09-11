---
name: google-scholar-bibtex
description: Batch-fetch literal Google Scholar Cite->BibTeX exports from a pipe-delimited title list (ordinal | citation_key | title) using a persistent real-Chrome Playwright crawler with CAPTCHA handling, mirror-domain failover, fuzzy title verification, and resumable checkpoints. Use when the user needs BibTeX for many paper titles at once, gets CAPTCHA-walled or HTTP 429/403 on scholar.google.com, or wants to build/resume a citation queue.
argument-hint: [input-list.txt] [output-dir]
---

# Google Scholar BibTeX Batch Crawler

Fetches the **literal** Google Scholar `Cite -> BibTeX` export bytes for every entry
of a title list — the same text a human would copy from the cite dialog.

Crawler script: `scripts/gs_bibtex_crawler.py` (Playwright sync API, single file).

## When to use

- Bulk BibTeX provenance for a citation queue (tens to hundreds of titles).
- Plain HTTP scrapers fail: Scholar CAPTCHA-walls the `/scholar.bib` export endpoint
  for non-browser clients, and rate-limits aggressively. This skill drives a **real
  persistent Chrome** (JS + cookies + human-like pacing + navigation-style requests).

Never fabricate BibTeX for titles Scholar does not index — the crawler honestly
reports `not_found` and queues such entries for review or later rounds.

## Requirements

- Python 3.10+, `playwright>=1.60` (`pip install -r requirements.txt`)
- Google Chrome installed (the crawler prefers `channel="chrome"`; falls back to
  bundled Chromium automatically)
- Network reachability to scholar.google.com (directly or via `--proxy`)

## Quickstart

```bash
# 1) dry parse (no network): verify entry counts
python3 scripts/gs_bibtex_crawler.py --input examples/input_list.txt --parse-only

# 2) run (a visible Chrome window opens — this is intentional; resume-safe)
python3 scripts/gs_bibtex_crawler.py --input examples/input_list.txt --outdir ./output

# 3) watch progress in a second terminal
tail -f output/crawler.log
```

Input format (one entry per block; optional embedded BibTeX blocks are used only as
cross-check hints and never modified):

```
0001 | vaswani2017attention | Attention Is All You Need
0002 | he2016deep | Deep Residual Learning for Image Recognition
```

Entries that already have a valid `<key>.bib` in `--outdir` are always skipped, so
re-running the same command is the resume mechanism. `--force` re-fetches.

## Key flags

| Flag | Purpose |
|---|---|
| `--input PATH` | pipe-delimited title list (required) |
| `--outdir PATH` | output dir (default `./output`) |
| `--keys k1,k2` / `--ordinals 1-10` / `--limit N` | subset selection |
| `--manifest PATH` | optional CSV with `citation_key,candidate_author,candidate_year` for author/year hints and identity cross-checks |
| `--force` | re-fetch even when a valid .bib exists on disk |
| `--retry-notfound` | also retry `not_found` entries on resume |
| `--headless` | hide the browser window (manual CAPTCHA solving then impossible — avoid) |
| `--min-delay 5 --max-delay 20` | uniform random seconds between entries |
| `--top-n 5` | search results scanned per strategy |
| `--exact-threshold 0.97 --fuzzy-threshold 0.85` | normalized title-match cutoffs |
| `--max-block-retries 3 --block-timeout 600 --consecutive-block-abort 3` | block-handling budget |
| `--domains a,b,c` | mirror failover order (default `scholar.google.com,.com.pr,.com.pk`); block state is per-domain |
| `--proxy URL` / `--no-proxy` | proxy override (auto-detects `HTTPS_PROXY` env, then probes `127.0.0.1:7890`) |
| `--cookies PATH` | inject Playwright storage_state JSON / Netscape cookies.txt (rarely needed) |
| `--parse-only` / `--build-filled-only` | dry parse / rebuild merged filled.txt |

## Workflow per entry

Search strategies until a match ≥ exact threshold: `raw` de-braced title → `quoted`
exact phrase → `maintitle` (before first `:`) → `boost` (+author surname/year from
manifest or key). Top-N hits compared by normalized fuzzy title similarity
(+containment bonus, year tie-breaker). BibTeX is captured as **raw response bytes**
from a real same-tab `Cite -> BibTeX` navigation (the only request style Google serves
reliably), validated (one `@type{key,` entry, `}`-terminated, single trailing newline
appended only if missing) and written unchanged.

## CAPTCHA / block playbook

1. **Interactive CAPTCHA** (`/sorry/` page or reCAPTCHA iframe): the script polls up to
   `--block-timeout` seconds — **solve it in the visible Chrome window**; success is
   detected automatically and the run continues.
2. **Pure rate-limit (429/403, no CAPTCHA)**: backoff ladder 60→180→600→1800 s with a
   real search probe after each step; the homepage loading fine is NOT an unblock signal.
3. **Mirror failover**: block state is per-domain — when one mirror is blocked the
   crawler probes the next in `--domains` and switches automatically.
4. `--consecutive-block-abort` (3) consecutive blocked entries ends the run — the exit
   IP is burned. Switch proxy exit node (prefer residential IPs; datacenter/VPN ranges
   get flat 403s with no CAPTCHA) or wait for cooldown, then re-run the same command.
5. Block episodes are screenshotted to `output/debug/block-*.png`.

## Outputs (under `--outdir`)

- `<citation_key>.bib` — literal export bytes
- `results.csv` / `results.jsonl` — status (`exact_match`/`fuzzy_match`/`not_found`/
  `blocked`/`error`), similarity, strategy, Scholar search URL (provenance), review flags
- `pending_keys.txt` — keys still lacking a valid .bib (round-based retry list)
- `review_queue.csv` — entries needing human eyes (fuzzy matches, year conflicts, …)
- `google_scholar_bibtex_filled.txt` — input format with fetched bib embedded (new file;
  the input list is never modified)
- `checkpoint.json`, `crawler.log` (plain), `debug/`

Terminal log is color-coded: green = bib obtained (full text printed) / success,
yellow = retries/mirror switches, red = blocked/errors.

## Round-based operation for large queues

Run → collect `pending_keys.txt` → switch proxy exit IP → run again (skips done
entries) → repeat until pending is 0. Each round is independent and resume-safe.

## Ethics & terms

Google Scholar's terms discourage automated scraping. This tool is intended for
personal/research citation management at human-like pacing (default 5–20 s between
entries), against your own reference lists. Keep delays at or above defaults, solve
CAPTCHAs manually when asked, and stop when blocked rather than escalating.

## Troubleshooting

- **Profile lock**: close other Chrome windows using the profile dir, or `--reset-profile`
  (loses cookie trust → more CAPTCHAs).
- **`FetchError: no results container …`**: Scholar UI changed — inspect
  `output/debug/*.html` and update `HITS_JS` / dialog selectors (`#gs_cit a`).
- **Quoted search returns "did not match any articles"**: legitimate — some titles are
  not indexed; falls through strategies and reports `not_found`.
- **Flat 403 with no CAPTCHA**: datacenter/VPN exit IP range blocked by Google — use a
  residential exit IP or wait; switching nodes inside the same provider won't help.
