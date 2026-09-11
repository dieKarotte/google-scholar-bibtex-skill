# google-scholar-bibtex-skill

Batch-fetch **literal** Google Scholar `Cite -> BibTeX` exports for a list of paper
titles, the same bytes a human would copy from the cite dialog — built for reference
lists of hundreds of entries, in environments where Google Scholar aggressively
rate-limits and CAPTCHA-walls scrapers.

Packaged as an agent skill (Claude Code / Qoder CLI / any SKILL.md-compatible agent)
and as a standalone CLI.

## Why not plain HTTP scraping?

Verified behavior of `scholar.google.com` against non-browser clients:

- the BibTeX export endpoint `/scholar.bib` 302-redirects plain `requests`/`curl`
  (and in-page `fetch()`/cors requests) to a CAPTCHA/sorry page;
- only **navigation-style requests from a real browser** (what a manual
  `Cite -> BibTeX` click sends) are served reliably;
- block state is **per mirror domain** (`.com` can be CAPTCHA-walled while `.com.pk`
  serves results from the same IP) and **per IP reputation** (datacenter/VPN ranges
  get flat 403s with no CAPTCHA; residential IPs get solvable CAPTCHAs).

This crawler therefore drives a persistent real Chrome via Playwright: JS execution,
accumulated cookies, human-like pacing, mirror-domain failover, and navigation-style
BibTeX capture.

## How it works

1. Parse a pipe-delimited list: `NNNN | citation_key | exact title` (optional embedded
   BibTeX blocks are used only as identity cross-checks).
2. Per entry, search with a strategy ladder (raw title → quoted phrase → main title
   before `:` → title + author/year boost) and pick the best normalized fuzzy title
   match among the top-N results (thresholds 0.97 exact / 0.85 fuzzy).
3. Open the cite dialog, click **BibTeX** in the same tab, capture the raw response
   bytes, validate (exactly one `@type{key,` entry, `}`-terminated, single trailing
   newline) and write `<citation_key>.bib` unchanged.
4. Anti-block: 5–20 s random pacing, persistent Chrome profile (cookie trust),
   mirror failover (`.com` → `.com.pr` → `.com.pk`), CAPTCHA polling with manual
   solve, backoff ladder, circuit breaker, per-entry checkpoints.
5. Resume-safe: entries with a valid `.bib` on disk are always skipped; re-run the
   same command after switching proxy exit IP to retry failures (round-based workflow).

## Install

```bash
pip install -r requirements.txt
# Chrome must be installed; the crawler falls back to bundled Chromium if absent
```

### Install as a skill

```bash
# Claude Code
git clone https://github.com/dieKarotte/google-scholar-bibtex-skill ~/.claude/skills/google-scholar-bibtex
# Qoder CLI / other agents: clone into the agent's skills directory, e.g.
git clone https://github.com/dieKarotte/google-scholar-bibtex-skill ~/.qoder/skills/google-scholar-bibtex
```

Then ask your agent: *"fetch Scholar BibTeX for every title in my list"* — or use the
CLI directly.

## Usage

```bash
# dry parse (no network)
python3 scripts/gs_bibtex_crawler.py --input examples/input_list.txt --parse-only

# run (visible Chrome window opens; Ctrl-C safe; re-run to resume)
python3 scripts/gs_bibtex_crawler.py --input examples/input_list.txt --outdir ./output

# live progress
tail -f output/crawler.log
```

Input format:

```
0001 | vaswani2017attention | Attention Is All You Need
0002 | he2016deep | Deep Residual Learning for Image Recognition
```

### Outputs (`--outdir`)

| File | Content |
|---|---|
| `<citation_key>.bib` | literal Scholar export bytes |
| `results.csv` / `results.jsonl` | status, similarity, strategy, Scholar search URL (provenance), review flags |
| `pending_keys.txt` | keys still lacking a valid .bib (next-round list) |
| `review_queue.csv` | fuzzy matches / year conflicts / blocked — for human review |
| `google_scholar_bibtex_filled.txt` | input format + fetched bib per entry (new file; input untouched) |
| `crawler.log`, `checkpoint.json`, `debug/` | plain log, progress, block screenshots |

Terminal log colors: green = bib obtained (full text shown) / success, yellow =
retry / mirror switch, red = blocked / error.

### Round-based workflow for big queues

run → read `pending_keys.txt` → switch proxy exit IP → run again → repeat until
pending = 0. Solve CAPTCHAs manually in the Chrome window when they appear.

## Key flags

`--input` (required) · `--outdir` · `--keys`/`--ordinals`/`--limit` · `--manifest`
(author/year hints CSV) · `--force` · `--retry-notfound` · `--headless` (avoid) ·
`--min-delay/--max-delay` · `--top-n` · `--exact-threshold/--fuzzy-threshold` ·
`--domains` (mirror order) · `--proxy/--no-proxy` · `--cookies` ·
`--max-block-retries/--block-timeout/--consecutive-block-abort` · `--parse-only` ·
`--build-filled-only` · full list: `--help`

## Ethics & terms of service

Google Scholar's terms discourage automated scraping. This tool targets personal /
research citation management at human-like pacing against your own reference lists.
Keep delays at or above the defaults, solve CAPTCHAs manually when asked, prefer
residential exit IPs, and stop when blocked instead of escalating. The authors assume
no responsibility for misuse or for Google account/IP consequences.

## License

MIT — see [LICENSE](LICENSE).
