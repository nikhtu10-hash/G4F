# g4f Provider Health Monitor

Automatically tests every provider/model combination exposed by
[gpt4free (g4f)](https://github.com/xtekky/gpt4free), 3x a day, and publishes
a dark-mode filterable report plus a plain-text log — all committed back into
this repo.

## What it does

1. **`scripts/test_providers.py`** enumerates every provider class in `g4f.Provider`
   and every model each one advertises, then sends `"How are you?"` to each
   provider/model pair with a timeout (default 25s). Each result is tagged with:
   - `needs_auth` — read from the provider's own `needs_auth` flag
   - `browser_required` — heuristic: flags providers that use `nodriver`,
     `selenium`, `playwright`, cookie files, HAR files, etc.
   - `status` — `working`, `error`, `timeout`, or `skipped` (skipped = g4f
     itself marks the provider as not currently working)
   - response time in ms, and a short snippet of the response or error

   Raw results go to `results/results.json`.

2. **`scripts/generate_report.py`** turns that JSON into:
   - `results/results.txt` — plain-text log, with a "working instantly, no
     auth, no browser" section pulled to the top
   - `results/index.html` — a dark-mode, single-file HTML report with
     search, status filter, "hide requires-auth", "hide requires-browser",
     and an "instant-working only" toggle, sortable by any column
     (click a header, e.g. response time)

3. **`.github/workflows/test-providers.yml`** runs the above on a schedule
   (00:00 / 08:00 / 16:00 UTC — edit the cron lines for your own timezone)
   and commits the refreshed `results/` folder back to the repo. You can
   also trigger it manually from the Actions tab (`workflow_dispatch`).

## Viewing the report

After the workflow runs once, open `results/index.html` — or enable GitHub
Pages pointed at this repo/branch to browse it as a live site.

## Tuning

Environment variables read by `test_providers.py` (set them in the workflow
`env:` block):

| Variable | Default | Purpose |
|---|---|---|
| `G4F_TEST_TIMEOUT` | `25` | Seconds to wait per request before marking it a timeout |
| `G4F_TEST_CONCURRENCY` | `8` | Max simultaneous requests |
| `G4F_MAX_MODELS_PER_PROVIDER` | `6` | Cap on models tested per provider, to keep run time reasonable |

## Known limitations

- g4f changes frequently — provider counts, model lists, and internal
  attribute names shift between releases. The scripts are written
  defensively (broad `try/except`, attribute fallbacks) so a single
  provider breaking doesn't crash the whole run, but a `g4f[all]` upgrade
  can still change *which* providers show up as working.
- Browser/cookie-driven providers are detected heuristically by scanning
  each provider class's source for known keywords (`nodriver`, `selenium`,
  `.har`, etc.) plus a few known flag attributes. This catches the common
  cases but isn't guaranteed exhaustive — spot-check `browser_required`
  results if you're relying on that flag precisely.
- GitHub-hosted runners have no real browser profile or persistent cookies,
  so browser-driven providers will almost always fail here even when they'd
  work on your own machine — which is exactly why they're separated out
  from the "instant, no setup" list.
