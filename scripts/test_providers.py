#!/usr/bin/env python3
"""
g4f Provider & Model Health Checker
------------------------------------
Enumerates every provider and model exposed by the installed `g4f` package,
sends a simple test prompt ("How are you?") to each provider/model pair,
and records whether it worked, whether it needs auth, whether it needs a
browser/cookies/HAR file, and how fast it responded.

Designed to run unattended in CI (GitHub Actions). g4f's internals change
often between releases, so this script is defensive: one bad provider can
never crash the whole run, and it falls back gracefully if some internal
attribute/API has moved in a newer g4f version.

Env vars (all optional):
  G4F_TEST_TIMEOUT           per-request timeout in seconds (default 25)
  G4F_TEST_CONCURRENCY       max concurrent requests (default 8)
  G4F_MAX_MODELS_PER_PROVIDER  cap on models tested per provider (default 6)
"""

import asyncio
import inspect
import json
import os
import sys
import time
import traceback
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

# Silence known-harmless noise from optional g4f extras before anything else loads:
# - onnxruntime prints a device-discovery warning on virtualized CI hardware
# - pydub warns if ffmpeg isn't on PATH (we still install ffmpeg in the workflow as the real fix)
warnings.filterwarnings("ignore", message="Couldn't find ffmpeg or avconv")
try:
    import onnxruntime as _ort  # noqa: E402
    _ort.set_default_logger_severity(3)  # 3 = only errors and above
except Exception:
    pass

import g4f
from g4f import Provider

TEST_PROMPT = "How are you?"
PER_REQUEST_TIMEOUT = int(os.environ.get("G4F_TEST_TIMEOUT", "25"))
MAX_CONCURRENCY = int(os.environ.get("G4F_TEST_CONCURRENCY", "8"))
MAX_MODELS_PER_PROVIDER = int(os.environ.get("G4F_MAX_MODELS_PER_PROVIDER", "6"))
OUTPUT_DIR = "results"
RESULTS_JSON = os.path.join(OUTPUT_DIR, "results.json")


def log(msg: str) -> None:
    """Timestamped, immediately-flushed log line so GitHub Actions streams it live."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def group_start(title: str) -> None:
    """Opens a collapsible section in the GitHub Actions log viewer."""
    print(f"::group::{title}", flush=True)


def group_end() -> None:
    print("::endgroup::", flush=True)


def quiet_exception_handler(loop, context):
    """
    Several g4f providers reuse an aiohttp ClientSession without ever closing
    it, which makes asyncio print an "Unclosed client session" message when
    the event loop shuts down. It's cosmetic and doesn't affect test results,
    so we filter just that message out and let anything else through normally.
    """
    message = str(context.get("message", ""))
    if "Unclosed client session" in message or "Unclosed connector" in message:
        return
    loop.default_exception_handler(context)

# Heuristics used to flag providers that rely on a real browser session,
# cookies, or a HAR file rather than working over a plain HTTP request.
BROWSER_KEYWORDS = [
    "nodriver", "webdriver", "selenium", "get_cookies", "har_file",
    ".har", "playwright", "browser_cookie3", "open_browser",
    "get_har_files", "cookies_dir",
]

SKIP_CLASS_NAMES = {
    "BaseProvider", "AsyncProvider", "AsyncGeneratorProvider",
    "AbstractProvider", "RetryProvider", "ProviderUtils",
    "AsyncAuthedProvider", "BaseRetryProvider",
}


@dataclass
class ProviderModelResult:
    provider: str
    model: str
    needs_auth: bool
    browser_required: bool
    status: str  # "working" | "error" | "timeout" | "skipped"
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    response_snippet: Optional[str] = None


def discover_providers():
    """Return [(name, provider_class), ...] for every usable provider class."""
    found = []
    names = getattr(Provider, "__all__", None)
    if not names:
        names = [n for n in dir(Provider) if not n.startswith("_")]

    seen = set()
    for name in names:
        if name in SKIP_CLASS_NAMES or name in seen:
            continue
        try:
            cls = getattr(Provider, name)
        except AttributeError:
            continue
        if not inspect.isclass(cls):
            continue
        seen.add(name)
        found.append((name, cls))
    return found


def looks_browser_required(cls) -> bool:
    """Best-effort: does this provider rely on browser automation / cookies?"""
    for attr in ("use_nodriver", "needs_browser", "nodriver", "use_webdriver"):
        if getattr(cls, attr, False):
            return True
    try:
        src = inspect.getsource(cls).lower()
    except (OSError, TypeError):
        src = ""
    return any(k in src for k in BROWSER_KEYWORDS)


def get_provider_models(cls):
    """Best-effort list of model names this provider advertises."""
    models = []
    try:
        raw = getattr(cls, "models", None)
        if callable(raw):
            raw = raw()
        if raw:
            models = list(raw)
    except Exception:
        models = []

    if not models:
        default = getattr(cls, "default_model", None)
        if default:
            models = [default]

    if not models:
        models = ["default"]

    out, seen = [], set()
    for m in models:
        m = str(m)
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out[:MAX_MODELS_PER_PROVIDER]


STATUS_ICON = {
    "working": "\u2705",   # check
    "error": "\u274c",     # x
    "timeout": "\u23f1",   # stopwatch
    "skipped": "\u23ed",   # skip
}


async def test_one(name, cls, model, semaphore, counters) -> ProviderModelResult:
    needs_auth = bool(getattr(cls, "needs_auth", False))
    browser_required = looks_browser_required(cls)
    working_flag = getattr(cls, "working", True)

    tags = []
    if needs_auth:
        tags.append("needs auth")
    if browser_required:
        tags.append("needs browser/cookies")
    tag_str = f" [{', '.join(tags)}]" if tags else ""

    result = ProviderModelResult(
        provider=name, model=model,
        needs_auth=needs_auth, browser_required=browser_required,
        status="skipped",
    )

    if not working_flag:
        result.error = "marked not working by g4f"
        counters["done"] += 1
        log(
            f"{STATUS_ICON['skipped']} SKIP  [{counters['done']}/{counters['total']}] "
            f"{name} / {model}{tag_str} - g4f marks this provider as currently not working"
        )
        return result

    log(
        f"\u23f3 START [{counters['started'] + 1}/{counters['total']}] "
        f"Testing provider '{name}' with model '{model}'{tag_str} ..."
    )
    counters["started"] += 1

    async with semaphore:
        start = time.monotonic()
        try:
            coro = g4f.ChatCompletion.create_async(
                model=model if model != "default" else "",
                messages=[{"role": "user", "content": TEST_PROMPT}],
                provider=cls,
            )
            response = await asyncio.wait_for(coro, timeout=PER_REQUEST_TIMEOUT)
            elapsed = (time.monotonic() - start) * 1000
            text = str(response).strip()
            result.response_time_ms = round(elapsed, 1)
            if not text:
                result.status = "error"
                result.error = "empty response"
            else:
                result.status = "working"
                result.response_snippet = text[:200]
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.error = f"no response within {PER_REQUEST_TIMEOUT}s"
            result.response_time_ms = PER_REQUEST_TIMEOUT * 1000.0
        except Exception as exc:  # noqa: BLE001 - intentionally broad in a health check
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            result.response_time_ms = round((time.monotonic() - start) * 1000, 1)

    counters["done"] += 1
    icon = STATUS_ICON.get(result.status, "?")
    detail = f"{result.response_time_ms}ms" if result.response_time_ms is not None else ""
    if result.status != "working" and result.error:
        detail = f"{detail} - {result.error}" if detail else result.error
    log(
        f"{icon} DONE  [{counters['done']}/{counters['total']}] "
        f"{name} / {model}: {result.status.upper()} ({detail})"
    )
    return result


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(quiet_exception_handler)

    log("=== g4f Provider & Model Health Check starting ===")
    log(
        f"Config: timeout={PER_REQUEST_TIMEOUT}s, concurrency={MAX_CONCURRENCY}, "
        f"max_models_per_provider={MAX_MODELS_PER_PROVIDER}"
    )
    log(f"g4f version detected: {getattr(g4f, '__version__', 'unknown')}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    group_start("Step 1/3: Discovering providers and models")
    log("Getting ready: scanning g4f.Provider for usable provider classes...")
    providers = discover_providers()
    log(f"Found {len(providers)} provider classes. Resolving their model lists...")

    plan = []
    for name, cls in providers:
        models = get_provider_models(cls)
        plan.append((name, cls, models))
        log(f"  - {name}: {len(models)} model(s) queued -> {', '.join(models)}")

    total_pairs = sum(len(models) for _, _, models in plan)
    log(f"Discovery complete: {total_pairs} provider/model pairs to test.")
    group_end()

    group_start(f"Step 2/3: Testing {total_pairs} provider/model pairs (concurrency={MAX_CONCURRENCY})")
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    counters = {"started": 0, "done": 0, "total": total_pairs}

    tasks = [
        test_one(name, cls, model, semaphore, counters)
        for name, cls, models in plan
        for model in models
    ]

    results = []
    for coro in asyncio.as_completed(tasks):
        try:
            r = await coro
        except Exception as exc:  # noqa: BLE001
            counters["done"] += 1
            r = ProviderModelResult(
                provider="unknown", model="unknown", needs_auth=False,
                browser_required=False, status="error",
                error=f"harness error: {exc}",
            )
            log(f"\u274c DONE  [{counters['done']}/{counters['total']}] harness error: {exc}")
        results.append(r)

    log("All provider/model pairs finished.")
    group_end()

    group_start("Step 3/3: Writing results.json")
    working = [r for r in results if r.status == "working"]
    instant = [r for r in working if not r.needs_auth and not r.browser_required]
    errored = [r for r in results if r.status in ("error", "timeout")]
    skipped = [r for r in results if r.status == "skipped"]

    log("Summary:")
    log(f"  Total tested        : {len(results)}")
    log(f"  Working             : {len(working)}")
    log(f"  Working, no auth,")
    log(f"  no browser (instant): {len(instant)}")
    log(f"  Failed / timed out  : {len(errored)}")
    log(f"  Skipped (not working per g4f): {len(skipped)}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "g4f_version": getattr(g4f, "__version__", "unknown"),
        "total_pairs": len(results),
        "results": [asdict(r) for r in results],
    }
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log(f"Wrote {RESULTS_JSON}")
    group_end()

    # Give any lingering aiohttp sessions a moment to close cleanly before the
    # event loop shuts down — reduces (but per the exception handler above,
    # doesn't need to fully eliminate) "Unclosed client session" noise.
    await asyncio.sleep(0.25)

    log("=== Done ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
