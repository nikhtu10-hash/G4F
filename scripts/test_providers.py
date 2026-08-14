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
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

import g4f
from g4f import Provider

TEST_PROMPT = "How are you?"
PER_REQUEST_TIMEOUT = int(os.environ.get("G4F_TEST_TIMEOUT", "25"))
MAX_CONCURRENCY = int(os.environ.get("G4F_TEST_CONCURRENCY", "8"))
MAX_MODELS_PER_PROVIDER = int(os.environ.get("G4F_MAX_MODELS_PER_PROVIDER", "6"))
OUTPUT_DIR = "results"
RESULTS_JSON = os.path.join(OUTPUT_DIR, "results.json")

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


async def test_one(name, cls, model, semaphore) -> ProviderModelResult:
    needs_auth = bool(getattr(cls, "needs_auth", False))
    browser_required = looks_browser_required(cls)
    working_flag = getattr(cls, "working", True)

    result = ProviderModelResult(
        provider=name, model=model,
        needs_auth=needs_auth, browser_required=browser_required,
        status="skipped",
    )

    if not working_flag:
        result.error = "marked not working by g4f"
        return result

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
    return result


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    providers = discover_providers()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    tasks = []
    for name, cls in providers:
        for model in get_provider_models(cls):
            tasks.append(test_one(name, cls, model, semaphore))

    print(f"Testing {len(tasks)} provider/model pairs across {len(providers)} providers...")

    results = []
    for i, coro in enumerate(asyncio.as_completed(tasks), 1):
        try:
            r = await coro
        except Exception as exc:  # noqa: BLE001
            r = ProviderModelResult(
                provider="unknown", model="unknown", needs_auth=False,
                browser_required=False, status="error",
                error=f"harness error: {exc}",
            )
        results.append(r)
        print(f"[{i}/{len(tasks)}] {r.provider}/{r.model}: {r.status}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "g4f_version": getattr(g4f, "__version__", "unknown"),
        "total_pairs": len(results),
        "results": [asdict(r) for r in results],
    }
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {RESULTS_JSON}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        raise
