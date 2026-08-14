#!/usr/bin/env python3
"""
g4f Provider & Model Health Checker
------------------------------------
Enumerates every provider exposed by the installed `g4f` package and, for
each one, tests every capability it advertises:

  - chat / text completion
  - image generation (every model = every "style", since g4f exposes style
    variants as separate model names, e.g. flux / flux-realism / flux-3d)
  - audio / text-to-speech (every voice it lists, capped per model)
  - video generation
  - audio transcription (speech-to-text)

It records whether each provider/model(/voice) pair worked, whether it
needs auth, and whether it needs real browser automation vs. a real logged
-in browser session (HAR file / cookies) that can't be produced in CI.

Designed to run unattended in CI (GitHub Actions). g4f's internals -
especially its unified `g4f.client` audio/video surface - change often
between releases, so this script is defensive: one bad provider (or one
capability g4f doesn't expose the way we expect) can never crash the whole
run, and results are saved incrementally so a run that hits the wall-clock
timeout still produces a usable report instead of nothing.

Env vars (all optional):
  G4F_TEST_TIMEOUT             chat request timeout, seconds (default 25)
  G4F_IMAGE_TIMEOUT            image generation timeout, seconds (default 60)
  G4F_AUDIO_TIMEOUT            TTS generation timeout, seconds (default 45)
  G4F_VIDEO_TIMEOUT            video generation timeout, seconds (default 180)
  G4F_TRANSCRIPTION_TIMEOUT    transcription timeout, seconds (default 45)
  G4F_TEST_CONCURRENCY         max concurrent requests, chat/image/audio/
                                transcription (default 8)
  G4F_VIDEO_CONCURRENCY        max concurrent video requests (default 2,
                                video generation is slow/expensive - keep low)
  G4F_MAX_MODELS_PER_PROVIDER  cap on models tested per provider, per
                                capability (default 6)
  G4F_MAX_VOICES_PER_MODEL     cap on voices tested per TTS model (default 8)
  G4F_SAVE_EVERY                write results.json every N completed tests,
                                so a run that gets killed still leaves a
                                usable partial report (default 15)
"""

import asyncio
import inspect
import io
import json
import math
import os
import struct
import sys
import time
import traceback
import wave
import warnings
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

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
TEST_IMAGE_PROMPT = "a small red circle on a white background"

CHAT_TIMEOUT = int(os.environ.get("G4F_TEST_TIMEOUT", "25"))
IMAGE_TIMEOUT = int(os.environ.get("G4F_IMAGE_TIMEOUT", "60"))
AUDIO_TIMEOUT = int(os.environ.get("G4F_AUDIO_TIMEOUT", "45"))
VIDEO_TIMEOUT = int(os.environ.get("G4F_VIDEO_TIMEOUT", "180"))
TRANSCRIPTION_TIMEOUT = int(os.environ.get("G4F_TRANSCRIPTION_TIMEOUT", "45"))

MAX_CONCURRENCY = int(os.environ.get("G4F_TEST_CONCURRENCY", "8"))
MAX_VIDEO_CONCURRENCY = int(os.environ.get("G4F_VIDEO_CONCURRENCY", "2"))
MAX_MODELS_PER_PROVIDER = int(os.environ.get("G4F_MAX_MODELS_PER_PROVIDER", "6"))
MAX_VOICES_PER_MODEL = int(os.environ.get("G4F_MAX_VOICES_PER_MODEL", "8"))
SAVE_EVERY = int(os.environ.get("G4F_SAVE_EVERY", "15"))

TIMEOUTS = {
    "chat": CHAT_TIMEOUT,
    "image": IMAGE_TIMEOUT,
    "audio": AUDIO_TIMEOUT,
    "video": VIDEO_TIMEOUT,
    "transcription": TRANSCRIPTION_TIMEOUT,
}

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


# ---------------------------------------------------------------------------
# Browser-requirement classification
# ---------------------------------------------------------------------------
# g4f providers that need "a browser" actually fall into two very different
# buckets, and lumping them together (as a single "browser_required" flag)
# was the original script's biggest blind spot:
#
#   1. Nodriver / webdriver / playwright automation: g4f drives a real,
#      throwaway Chromium instance itself. This CAN be tested in CI as long
#      as a browser binary is actually installed (see the workflow's
#      "Install Chromium" step) - so we attempt these normally.
#
#   2. HAR file / cookies / browser_cookie3: these require cookies from a
#      *real, already logged-in* human browser session that was exported
#      ahead of time. There is no way to produce that from scratch in an
#      unattended CI job, so attempting them just burns the whole timeout
#      waiting for a login that will never happen. We detect these and skip
#      them immediately with a clear, honest reason instead of reporting a
#      misleading "error".
NODRIVER_KEYWORDS = [
    "nodriver", "webdriver", "selenium", "playwright", "open_browser",
]
SESSION_REQUIRED_KEYWORDS = [
    "get_cookies", "har_file", ".har", "get_har_files", "cookies_dir",
    "browser_cookie3",
]

SKIP_CLASS_NAMES = {
    "BaseProvider", "AsyncProvider", "AsyncGeneratorProvider",
    "AbstractProvider", "RetryProvider", "ProviderUtils",
    "AsyncAuthedProvider", "BaseRetryProvider",
}


def _source_of(cls) -> str:
    try:
        return inspect.getsource(cls).lower()
    except (OSError, TypeError):
        return ""


def uses_nodriver_automation(cls) -> bool:
    for attr in ("use_nodriver", "needs_browser", "nodriver", "use_webdriver"):
        if getattr(cls, attr, False):
            return True
    src = _source_of(cls)
    return any(k in src for k in NODRIVER_KEYWORDS)


def needs_manual_session(cls) -> bool:
    src = _source_of(cls)
    return any(k in src for k in SESSION_REQUIRED_KEYWORDS)


@dataclass
class TestResult:
    provider: str
    kind: str  # "chat" | "image" | "audio" | "video" | "transcription"
    model: str
    voice: Optional[str]
    needs_auth: bool
    browser_automation: bool     # nodriver/webdriver - we DO attempt these
    needs_manual_session: bool   # HAR/cookies - we skip these, can't automate
    status: str  # "working" | "error" | "timeout" | "skipped"
    response_time_ms: Optional[float] = None
    error: Optional[str] = None
    response_snippet: Optional[str] = None


STATUS_ICON = {
    "working": "\u2705",   # check
    "error": "\u274c",     # x
    "timeout": "\u23f1",   # stopwatch
    "skipped": "\u23ed",   # skip
}


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


def _as_list(value) -> list:
    """Best-effort coercion of a models/voices attribute into a plain list.
    Handles: None, callables returning a list, dicts (returns keys),
    plain iterables, and single scalar values."""
    if value is None:
        return []
    if callable(value):
        try:
            value = value()
        except Exception:
            return []
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return [v for v in value]
    except TypeError:
        return [value]


def discover_capabilities(cls) -> dict:
    """Best-effort discovery of every capability a provider class advertises.

    g4f provider classes commonly (but not always - versions differ) expose:
      models / default_model                       -> chat
      image_models / default_image_model            -> image generation
      audio_models / default_audio_model             -> text-to-speech
                                                         (dict of model -> voices,
                                                          or a plain list)
      video_models / default_video_model             -> video generation
      transcription_models                            -> speech-to-text

    Anything not present on this g4f version is simply an empty list, so we
    never crash on a version mismatch - we just don't test that capability
    for that provider, which is exactly what "if it exists" means.
    """
    chat_models = _as_list(getattr(cls, "models", None))
    if not chat_models:
        default = getattr(cls, "default_model", None)
        if default:
            chat_models = [default]

    image_models = _as_list(getattr(cls, "image_models", None))
    if not image_models:
        default_image = getattr(cls, "default_image_model", None)
        if default_image:
            image_models = [default_image]

    video_models = _as_list(getattr(cls, "video_models", None))
    if not video_models:
        default_video = getattr(cls, "default_video_model", None)
        if default_video:
            video_models = [default_video]

    audio_raw = getattr(cls, "audio_models", None)
    audio_models: list = []
    voices_by_model: dict = {}
    if isinstance(audio_raw, dict):
        for model_name, voices in audio_raw.items():
            audio_models.append(str(model_name))
            voices_by_model[str(model_name)] = [str(v) for v in _as_list(voices)]
    else:
        audio_models = [str(m) for m in _as_list(audio_raw)]
    if not audio_models:
        default_audio = getattr(cls, "default_audio_model", None)
        if default_audio:
            audio_models = [str(default_audio)]

    transcription_models = _as_list(getattr(cls, "transcription_models", None))

    nothing_found = not (
        chat_models or image_models or audio_models or video_models or transcription_models
    )
    if nothing_found:
        # Same last-resort fallback as the original script: still try a bare
        # chat call, since plenty of providers only expose a working
        # `default_model` and nothing else we can introspect.
        chat_models = ["default"]

    def _dedup_cap(models):
        out, seen = [], set()
        for m in models:
            m = str(m)
            if m and m not in seen:
                seen.add(m)
                out.append(m)
        return out[:MAX_MODELS_PER_PROVIDER]

    return {
        "chat": _dedup_cap(chat_models),
        "image": _dedup_cap(image_models),
        "audio": _dedup_cap(audio_models),
        "video": _dedup_cap(video_models),
        "transcription": _dedup_cap(transcription_models),
        "voices_by_model": voices_by_model,
    }


# ---------------------------------------------------------------------------
# Unified client detection
# ---------------------------------------------------------------------------
# Image/audio/video generation go through g4f's OpenAI-style unified client
# rather than g4f.ChatCompletion. The exact method names on that client have
# shifted between g4f releases (client.audio.speech vs client.media.generate,
# etc.), so we try the known shapes in order and fall through cleanly if a
# given g4f version doesn't expose one. If NONE of them exist, image/audio/
# video/transcription testing is disabled entirely for the run (with a clear
# warning) rather than pretending it happened.
_AsyncClient = None
try:
    from g4f.client import AsyncClient as _AsyncClient  # noqa: N816
except Exception:
    _AsyncClient = None

CLIENT = _AsyncClient() if _AsyncClient else None


async def _try_calls(calls):
    """Try a list of zero-arg async callables in order. TypeError (wrong/
    missing kwarg for this g4f version) moves on to the next candidate.
    Any other exception is a real provider error and is raised immediately
    so it's reported accurately instead of masked."""
    last_type_error = None
    tried_any = False
    for call in calls:
        tried_any = True
        try:
            return await call()
        except TypeError as exc:
            last_type_error = exc
            continue
    if not tried_any:
        raise RuntimeError("no compatible API found on the installed g4f client")
    raise last_type_error


async def generate_image(cls, model: str):
    if CLIENT is None:
        raise RuntimeError("g4f.client.AsyncClient not available in this g4f version")
    resp = await CLIENT.images.generate(
        model=model, prompt=TEST_IMAGE_PROMPT, provider=cls, response_format="url",
    )
    item = resp.data[0]
    return getattr(item, "url", None) or (getattr(item, "b64_json", "") or "")[:80]


async def generate_speech(cls, model: str, voice: Optional[str]):
    if CLIENT is None:
        raise RuntimeError("g4f.client.AsyncClient not available in this g4f version")
    calls = []
    audio_ns = getattr(CLIENT, "audio", None)
    if audio_ns is not None and hasattr(audio_ns, "speech"):
        calls.append(lambda: audio_ns.speech.create(
            model=model, input=TEST_PROMPT, voice=voice, provider=cls,
        ))
    media_ns = getattr(CLIENT, "media", None)
    if media_ns is not None and hasattr(media_ns, "generate"):
        calls.append(lambda: media_ns.generate(
            model=model, prompt=TEST_PROMPT, voice=voice, provider=cls, media_type="audio",
        ))
    resp = await _try_calls(calls)
    return f"audio response received ({type(resp).__name__})"


async def generate_video(cls, model: str):
    if CLIENT is None:
        raise RuntimeError("g4f.client.AsyncClient not available in this g4f version")
    calls = []
    videos_ns = getattr(CLIENT, "videos", None)
    if videos_ns is not None and hasattr(videos_ns, "generate"):
        calls.append(lambda: videos_ns.generate(model=model, prompt=TEST_PROMPT, provider=cls))
    media_ns = getattr(CLIENT, "media", None)
    if media_ns is not None and hasattr(media_ns, "generate"):
        calls.append(lambda: media_ns.generate(
            model=model, prompt=TEST_PROMPT, provider=cls, media_type="video",
        ))
    resp = await _try_calls(calls)
    return f"video response received ({type(resp).__name__})"


def _make_test_wav_bytes(duration_s: float = 1.0, freq: float = 440.0, rate: int = 16000) -> bytes:
    """A short synthetic tone (not real speech) used purely as a connectivity
    / does-it-error check for transcription endpoints - this is a health
    check, not an accuracy test."""
    n_samples = int(duration_s * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n_samples):
            val = int(3000 * math.sin(2 * math.pi * freq * (i / rate)))
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


async def transcribe_audio(cls, model: str):
    if CLIENT is None:
        raise RuntimeError("g4f.client.AsyncClient not available in this g4f version")
    audio_ns = getattr(CLIENT, "audio", None)
    if audio_ns is None or not hasattr(audio_ns, "transcriptions"):
        raise RuntimeError("no transcription API found on the installed g4f client")
    wav_bytes = _make_test_wav_bytes()
    audio_file = io.BytesIO(wav_bytes)
    audio_file.name = "healthcheck.wav"
    resp = await audio_ns.transcriptions.create(model=model, file=audio_file, provider=cls)
    text = getattr(resp, "text", None)
    return text if text else str(resp)[:200]


async def chat_once(cls, model: str):
    coro = g4f.ChatCompletion.create_async(
        model=model if model != "default" else "",
        messages=[{"role": "user", "content": TEST_PROMPT}],
        provider=cls,
    )
    response = await coro
    return str(response).strip()


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

async def run_test(item, semaphore, counters) -> TestResult:
    name, cls, kind, model, voice, needs_auth, browser_automation, manual_session = item

    label = f"{name} / {kind} / {model}" + (f" (voice={voice})" if voice else "")
    tags = []
    if needs_auth:
        tags.append("needs auth")
    if browser_automation:
        tags.append("browser automation")
    if manual_session:
        tags.append("needs real login session")
    tag_str = f" [{', '.join(tags)}]" if tags else ""

    result = TestResult(
        provider=name, kind=kind, model=model, voice=voice,
        needs_auth=needs_auth, browser_automation=browser_automation,
        needs_manual_session=manual_session, status="skipped",
    )

    working_flag = getattr(cls, "working", True)
    if not working_flag:
        result.error = "marked not working by g4f"
        counters["done"] += 1
        log(f"{STATUS_ICON['skipped']} SKIP  [{counters['done']}/{counters['total']}] {label}{tag_str} - g4f marks this provider as currently not working")
        return result

    if manual_session:
        # No way to produce a real logged-in cookie/HAR session unattended -
        # attempting this would just burn the timeout for a guaranteed failure.
        result.error = "requires a real logged-in browser session (HAR file / cookies) - cannot be automated in CI"
        counters["done"] += 1
        log(f"{STATUS_ICON['skipped']} SKIP  [{counters['done']}/{counters['total']}] {label}{tag_str} - {result.error}")
        return result

    log(f"\u23f3 START [{counters['started'] + 1}/{counters['total']}] Testing {label}{tag_str} ...")
    counters["started"] += 1

    timeout = TIMEOUTS[kind]
    async with semaphore:
        start = time.monotonic()
        try:
            if kind == "chat":
                text = await asyncio.wait_for(chat_once(cls, model), timeout=timeout)
                snippet = text
            elif kind == "image":
                snippet = await asyncio.wait_for(generate_image(cls, model), timeout=timeout)
            elif kind == "audio":
                snippet = await asyncio.wait_for(generate_speech(cls, model, voice), timeout=timeout)
            elif kind == "video":
                snippet = await asyncio.wait_for(generate_video(cls, model), timeout=timeout)
            elif kind == "transcription":
                snippet = await asyncio.wait_for(transcribe_audio(cls, model), timeout=timeout)
            else:
                raise RuntimeError(f"unknown capability kind: {kind}")

            elapsed = (time.monotonic() - start) * 1000
            result.response_time_ms = round(elapsed, 1)
            if not snippet:
                result.status = "error"
                result.error = "empty response"
            else:
                result.status = "working"
                result.response_snippet = str(snippet)[:200]
        except asyncio.TimeoutError:
            result.status = "timeout"
            result.error = f"no response within {timeout}s"
            result.response_time_ms = timeout * 1000.0
        except Exception as exc:  # noqa: BLE001 - intentionally broad in a health check
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            result.response_time_ms = round((time.monotonic() - start) * 1000, 1)

    counters["done"] += 1
    icon = STATUS_ICON.get(result.status, "?")
    detail = f"{result.response_time_ms}ms" if result.response_time_ms is not None else ""
    if result.status != "working" and result.error:
        detail = f"{detail} - {result.error}" if detail else result.error
    log(f"{icon} DONE  [{counters['done']}/{counters['total']}] {label}: {result.status.upper()} ({detail})")
    return result


def save_results(results, partial: bool) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "g4f_version": getattr(g4f, "__version__", "unknown"),
        "total_pairs": len(results),
        "partial": partial,
        "results": [asdict(r) for r in results],
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp_path = RESULTS_JSON + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, RESULTS_JSON)  # atomic - never leaves a half-written file


async def main():
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(quiet_exception_handler)

    log("=== g4f Provider & Model Health Check starting ===")
    log(
        f"Config: chat_timeout={CHAT_TIMEOUT}s image_timeout={IMAGE_TIMEOUT}s "
        f"audio_timeout={AUDIO_TIMEOUT}s video_timeout={VIDEO_TIMEOUT}s "
        f"transcription_timeout={TRANSCRIPTION_TIMEOUT}s concurrency={MAX_CONCURRENCY} "
        f"video_concurrency={MAX_VIDEO_CONCURRENCY} max_models_per_provider={MAX_MODELS_PER_PROVIDER} "
        f"max_voices_per_model={MAX_VOICES_PER_MODEL}"
    )
    log(f"g4f version detected: {getattr(g4f, '__version__', 'unknown')}")
    if CLIENT is None:
        log(
            "WARNING: could not import g4f.client.AsyncClient - image, audio, video, "
            "and transcription testing are disabled for this run; only chat will be tested. "
            "This usually means the installed g4f version exposes a different client API - "
            "check the version above and update generate_image/generate_speech/generate_video/"
            "transcribe_audio in this script to match."
        )

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    group_start("Step 1/3: Discovering providers and capabilities")
    log("Getting ready: scanning g4f.Provider for usable provider classes...")
    providers = discover_providers()
    log(f"Found {len(providers)} provider classes. Resolving their capabilities...")

    plan = []  # list of tuples matching run_test's `item` signature
    for name, cls in providers:
        caps = discover_capabilities(cls)
        needs_auth = bool(getattr(cls, "needs_auth", False))
        browser_automation = uses_nodriver_automation(cls)
        manual_session = needs_manual_session(cls)

        summary_bits = []
        for kind in ("chat", "image", "video", "transcription"):
            if caps[kind]:
                summary_bits.append(f"{kind}={len(caps[kind])}")
                for model in caps[kind]:
                    plan.append((name, cls, kind, model, None, needs_auth, browser_automation, manual_session))

        if caps["audio"]:
            voice_count = 0
            for model in caps["audio"]:
                voices = caps["voices_by_model"].get(model) or [None]
                voices = voices[:MAX_VOICES_PER_MODEL] if voices != [None] else voices
                for voice in voices:
                    plan.append((name, cls, "audio", model, voice, needs_auth, browser_automation, manual_session))
                    voice_count += 1
            summary_bits.append(f"audio={len(caps['audio'])}({voice_count} voice combos)")

        log(f"  - {name}: {', '.join(summary_bits) if summary_bits else 'nothing testable found'}")

    total = len(plan)
    log(f"Discovery complete: {total} provider/capability/model(/voice) combinations to test.")
    group_end()

    group_start(f"Step 2/3: Testing {total} combinations (concurrency={MAX_CONCURRENCY}, video_concurrency={MAX_VIDEO_CONCURRENCY})")
    main_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    video_semaphore = asyncio.Semaphore(MAX_VIDEO_CONCURRENCY)
    counters = {"started": 0, "done": 0, "total": total}

    def semaphore_for(item):
        return video_semaphore if item[2] == "video" else main_semaphore

    tasks = [run_test(item, semaphore_for(item), counters) for item in plan]

    results = []
    try:
        for coro in asyncio.as_completed(tasks):
            try:
                r = await coro
            except Exception as exc:  # noqa: BLE001
                counters["done"] += 1
                r = TestResult(
                    provider="unknown", kind="unknown", model="unknown", voice=None,
                    needs_auth=False, browser_automation=False, needs_manual_session=False,
                    status="error", error=f"harness error: {exc}",
                )
                log(f"\u274c DONE  [{counters['done']}/{counters['total']}] harness error: {exc}")
            results.append(r)
            if len(results) % SAVE_EVERY == 0:
                save_results(results, partial=True)
    finally:
        # Always write whatever we have, even if the run is interrupted
        # (CI timeout, cancellation, unexpected crash) - a partial report
        # beats no report at all.
        save_results(results, partial=(len(results) < total))

    log("All combinations finished." if len(results) >= total else "Run ended early - partial results saved.")
    group_end()

    group_start("Step 3/3: Summary")
    working = [r for r in results if r.status == "working"]
    errored = [r for r in results if r.status in ("error", "timeout")]
    skipped = [r for r in results if r.status == "skipped"]

    log("Summary by capability:")
    for kind in ("chat", "image", "audio", "video", "transcription"):
        kind_results = [r for r in results if r.kind == kind]
        if not kind_results:
            continue
        kind_working = [r for r in kind_results if r.status == "working"]
        log(f"  {kind:<14}: {len(kind_working)}/{len(kind_results)} working")

    log("Overall:")
    log(f"  Total tested        : {len(results)}")
    log(f"  Working             : {len(working)}")
    log(f"  Failed / timed out  : {len(errored)}")
    log(f"  Skipped             : {len(skipped)}")
    group_end()

    # Give any lingering aiohttp sessions a moment to close cleanly before the
    # event loop shuts down - reduces (but per the exception handler above,
    # doesn't need to fully eliminate) "Unclosed client session" noise.
    await asyncio.sleep(0.25)

    log("=== Done ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
