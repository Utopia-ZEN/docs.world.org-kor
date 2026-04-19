#!/usr/bin/env python3
"""Daily translator for docs.world.org -> Korean static mirror.
Design goals
- Translate human-readable documentation text into Korean.
- Preserve code blocks, inline code identifiers, URLs, and command tokens.
- Keep runs idempotent and cost-efficient through translation caching.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag
DEFAULT_BASE_URL = "https://docs.world.org"
DEFAULT_SITEMAP_URL = f"{DEFAULT_BASE_URL}/sitemap.xml"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_CACHE_PATH = ".translation-cache.json"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_STATE_PATH = ".state/source-fingerprint.json"
DEFAULT_PROGRESS_STATE_PATH = ".state/translated-pages.json"
SUMMARY_VERSION = 1
SKIP_TAGS = {"script", "style", "code", "pre", "kbd", "samp", "noscript"}
SKIP_CONTAINERS = {
    "header",
    "footer",
    "nav",
    "aside",
}
TRANS_ATTRS = {"title", "aria-label", "alt", "placeholder"}
WHITESPACE_RE = re.compile(r"\s+")
KOREAN_RE = re.compile(r"[가-힣]")
IDENTIFIER_ONLY_RE = re.compile(r"^[\w./:#\-]{2,}$")
LEADING_WS_RE = re.compile(r"^\s*")
TRAILING_WS_RE = re.compile(r"\s*$")
@dataclass
class Config:
    base_url: str
    sitemap_url: str
    output_dir: Path
    cache_path: Path
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    max_urls: int
    request_timeout: int
    per_page_sleep: float
    translate_sleep: float
    openai_max_retries: int
    state_path: Path
    progress_state_path: Path = Path(DEFAULT_PROGRESS_STATE_PATH)
    max_pages_per_run: int = 30
    max_segments_per_run: int = 400
    max_runtime_seconds: int = 900
    max_batch_items: int = 20
    priority_prefixes: List[str] = None  # type: ignore[assignment]
@dataclass
class TranslationStats:
    urls_total: int = 0
    urls_ok: int = 0
    urls_failed: int = 0
    segments_total: int = 0
    segments_cached: int = 0
    segments_translated: int = 0
    api_calls_total: int = 0


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def build_summary(
    *,
    cfg: Config,
    stats: TranslationStats,
    processed: List[str],
    errors: List[Dict[str, object]],
    skipped: bool,
    started: float,
    rate_limit_count: int,
    abort_reason: str,
    skip_reason: str = "",
) -> Dict[str, object]:
    return {
        "summary_version": SUMMARY_VERSION,
        "base_url": cfg.base_url,
        "sitemap_url": cfg.sitemap_url,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats.__dict__,
        "processed_urls": processed,
        "errors": errors,
        "skipped": skipped,
        "skip_reason": skip_reason if skipped else "",
        "rate_limit_count": rate_limit_count,
        "abort_reason": abort_reason,
        "elapsed_seconds": round(time.time() - started, 2),
        "cache_hit_ratio": (
            round(stats.segments_cached / stats.segments_total, 4)
            if stats.segments_total
            else 0.0
        ),
    }
@dataclass
class SitemapEntry:
    url: str
    lastmod: str = ""
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate docs.world.org pages into Korean")
    parser.add_argument("--base-url", default=os.getenv("SOURCE_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--sitemap-url", default=os.getenv("SOURCE_SITEMAP_URL", DEFAULT_SITEMAP_URL))
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--cache-path",
        default=os.getenv("TRANSLATION_CACHE_PATH", DEFAULT_CACHE_PATH),
    )
    parser.add_argument("--openai-model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-urls", type=int, default=int(os.getenv("MAX_URLS", "500")))
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=int(os.getenv("REQUEST_TIMEOUT", "30")),
    )
    parser.add_argument(
        "--per-page-sleep",
        type=float,
        default=float(os.getenv("SLEEP_BETWEEN_REQUESTS", "0.1")),
    )
    parser.add_argument(
        "--translate-sleep",
        type=float,
        default=float(os.getenv("SLEEP_BETWEEN_TRANSLATIONS", "0.03")),
    )
    parser.add_argument(
        "--openai-max-retries",
        type=int,
        default=int(os.getenv("OPENAI_MAX_RETRIES", "4")),
    )
    parser.add_argument(
        "--openai-base-url",
        default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    parser.add_argument(
        "--state-path",
        default=os.getenv("STATE_PATH", DEFAULT_STATE_PATH),
    )
    parser.add_argument(
        "--progress-state-path",
        default=os.getenv("PROGRESS_STATE_PATH", DEFAULT_PROGRESS_STATE_PATH),
    )
    parser.add_argument(
        "--max-pages-per-run",
        type=int,
        default=int(os.getenv("MAX_PAGES_PER_RUN", "30")),
    )
    parser.add_argument(
        "--max-segments-per-run",
        type=int,
        default=int(os.getenv("MAX_SEGMENTS_PER_RUN", "400")),
    )
    parser.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=int(os.getenv("MAX_RUNTIME_SECONDS", "900")),
    )
    parser.add_argument(
        "--max-batch-items",
        type=int,
        default=int(os.getenv("MAX_BATCH_ITEMS", "20")),
    )
    parser.add_argument(
        "--priority-prefixes",
        default=os.getenv("PRIORITY_PREFIXES", "/agents/,/mini-apps/,/api-reference/"),
    )
    return parser.parse_args()
def build_config(args: argparse.Namespace) -> Config:
    return Config(
        base_url=args.base_url.rstrip("/"),
        sitemap_url=args.sitemap_url,
        output_dir=Path(args.output_dir),
        cache_path=Path(args.cache_path),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=args.openai_base_url.rstrip("/"),
        openai_model=args.openai_model,
        max_urls=args.max_urls,
        request_timeout=args.request_timeout,
        per_page_sleep=args.per_page_sleep,
        translate_sleep=args.translate_sleep,
        openai_max_retries=args.openai_max_retries,
        state_path=Path(args.state_path),
        progress_state_path=Path(args.progress_state_path),
        max_pages_per_run=args.max_pages_per_run,
        max_segments_per_run=args.max_segments_per_run,
        max_runtime_seconds=args.max_runtime_seconds,
        max_batch_items=args.max_batch_items,
        priority_prefixes=[p.strip() for p in str(args.priority_prefixes).split(",") if p.strip()],
    )


def load_progress_state(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"pages": {}, "deferred": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"pages": {}, "deferred": []}
    pages = raw.get("pages", {})
    deferred = raw.get("deferred", [])
    if not isinstance(pages, dict):
        pages = {}
    if not isinstance(deferred, list):
        deferred = []
    return {"pages": pages, "deferred": deferred}


def save_progress_state(path: Path, state: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def sort_by_priority(urls: List[str], prefixes: List[str]) -> List[str]:
    def rank(u: str) -> tuple[int, str]:
        path = urlparse(u).path or "/"
        for idx, pref in enumerate(prefixes):
            if path.startswith(pref):
                return (idx, path)
        return (len(prefixes), path)

    return sorted(urls, key=rank)
def load_cache(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
    except json.JSONDecodeError:
        pass
    return {}
def save_cache(path: Path, cache: Dict[str, str]) -> None:
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()
def should_translate_text(text: str) -> bool:
    if not text or text.isspace():
        return False
    stripped = normalize_text(text)
    if len(stripped) < 2:
        return False
    if KOREAN_RE.search(stripped):
        return False
    if IDENTIFIER_ONLY_RE.fullmatch(stripped) and any(ch in stripped for ch in "/._:#-"):
        return False
    return True
def preserve_surrounding_whitespace(original: str, translated: str) -> str:
    leading = LEADING_WS_RE.search(original)
    trailing = TRAILING_WS_RE.search(original)
    leading_ws = leading.group(0) if leading else ""
    trailing_ws = trailing.group(0) if trailing else ""
    core = translated.strip()
    return f"{leading_ws}{core}{trailing_ws}"
def digest(model: str, text: str) -> str:
    payload = f"{model}\n{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: int,
    max_retries: int,
    **kwargs,
) -> requests.Response:
    backoff = 1.0
    for attempt in range(max_retries + 1):
        try:
            res = session.request(method, url, timeout=timeout, **kwargs)
            if res.status_code in {429, 500, 502, 503, 504}:
                retry_after = res.headers.get("Retry-After")
                if attempt == max_retries:
                    if res.status_code == 429:
                        parsed_retry_after = None
                        if retry_after:
                            try:
                                parsed_retry_after = float(retry_after)
                            except ValueError:
                                parsed_retry_after = None
                        raise RateLimitError(
                            f"429 Too Many Requests for {url}",
                            retry_after=parsed_retry_after,
                        )
                    res.raise_for_status()
                if retry_after:
                    try:
                        time.sleep(max(float(retry_after), backoff))
                    except ValueError:
                        time.sleep(backoff)
                else:
                    time.sleep(backoff)
                backoff *= 2
                continue
            res.raise_for_status()
            return res
        except RateLimitError:
            raise
        except requests.RequestException:
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError("unreachable retry state")
def translate_segment(session: requests.Session, cfg: Config, text: str) -> str:
    if not cfg.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for translation")
    system_prompt = (
        "Translate input into natural Korean for technical docs. "
        "IMPORTANT: preserve code blocks, inline backticks, identifiers, URLs, "
        "file paths, shell commands, and version strings exactly. "
        "Do not add explanation. Return only translated text."
    )
    res = request_with_retry(
        session,
        "POST",
        f"{cfg.openai_base_url}/chat/completions",
        timeout=cfg.request_timeout,
        max_retries=cfg.openai_max_retries,
        headers={
            "Authorization": f"Bearer {cfg.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.openai_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
        },
    )
    content = res.json()["choices"][0]["message"]["content"]
    return str(content).strip()


def _parse_json_array(content: str) -> List[object]:
    raw = content.strip()
    if raw.startswith("```"):
        # common model response shape: ```json ... ```
        raw = raw.strip("`")
        raw = raw.replace("json\n", "", 1).strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise RuntimeError("expected JSON array")
    return parsed


def translate_segments_batch(session: requests.Session, cfg: Config, texts: List[str]) -> List[str]:
    if not texts:
        return []
    if not cfg.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required for translation")

    system_prompt = (
        "Translate each input string into natural Korean for technical docs. "
        "Preserve code blocks, inline backticks, identifiers, URLs, file paths, shell commands, and version strings exactly. "
        "Return ONLY a JSON array of translated strings in the same order and same length."
    )
    user_payload = json.dumps(texts, ensure_ascii=False)
    res = request_with_retry(
        session,
        "POST",
        f"{cfg.openai_base_url}/chat/completions",
        timeout=cfg.request_timeout,
        max_retries=cfg.openai_max_retries,
        headers={
            "Authorization": f"Bearer {cfg.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.openai_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
        },
    )
    content = str(res.json()["choices"][0]["message"]["content"]).strip()
    parsed = _parse_json_array(content)
    if len(parsed) != len(texts):
        raise RuntimeError("batch translation response shape mismatch")
    return [str(x).strip() for x in parsed]
def fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    res = request_with_retry(session, "GET", url, timeout=timeout, max_retries=3)
    return res.text
def parse_sitemap(session: requests.Session, sitemap_url: str, timeout: int) -> List[SitemapEntry]:
    seen: Set[str] = set()
    def _parse(url: str) -> List[SitemapEntry]:
        if url in seen:
            return []
        seen.add(url)
        xml = fetch_text(session, url, timeout=timeout)
        soup = BeautifulSoup(xml, "xml")
        nested = [loc.text.strip() for loc in soup.select("sitemap > loc") if loc.text]
        if nested:
            entries: List[SitemapEntry] = []
            for nested_url in nested:
                entries.extend(_parse(nested_url))
            return entries
        entries: List[SitemapEntry] = []
        for url_node in soup.select("url"):
            loc_node = url_node.select_one("loc")
            if not loc_node or not loc_node.text:
                continue
            lastmod_node = url_node.select_one("lastmod")
            entries.append(
                SitemapEntry(
                    url=loc_node.text.strip(),
                    lastmod=lastmod_node.text.strip() if lastmod_node and lastmod_node.text else "",
                )
            )
        return entries
    return _parse(sitemap_url)
def pick_main_content(soup: BeautifulSoup) -> Tag:
    for selector in ["main", "article", "div.theme-doc-markdown", "body"]:
        node = soup.select_one(selector)
        if isinstance(node, Tag):
            return node
    return soup
def should_skip_node(parent: Tag) -> bool:
    if parent.name in SKIP_TAGS:
        return True
    for anc in parent.parents:
        if isinstance(anc, Tag) and anc.name in SKIP_CONTAINERS:
            return True
    return False
def rewrite_internal_links_to_relative(soup: BeautifulSoup, base_url: str) -> None:
    base_host = urlparse(base_url).netloc
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue
        parsed = urlparse(href)
        # Absolute link to same host -> path-only local link.
        if parsed.scheme in {"http", "https"} and parsed.netloc == base_host:
            local = parsed.path or "/"
            if parsed.fragment:
                local += f"#{parsed.fragment}"
            anchor["href"] = local
def translate_html(
    html: str,
    *,
    session: requests.Session,
    cfg: Config,
    cache: Dict[str, str],
    stats: TranslationStats,
) -> str:
    soup = BeautifulSoup(html, "lxml")
    main = pick_main_content(soup)

    ops: List[Dict[str, object]] = []
    for text_node in list(main.find_all(string=True)):
        parent = text_node.parent
        if not isinstance(parent, Tag) or should_skip_node(parent):
            continue
        original = str(text_node)
        if not should_translate_text(original):
            continue
        ops.append({"kind": "text", "node": text_node, "original": original, "normalized": normalize_text(original)})

    for el in main.find_all(True):
        if should_skip_node(el):
            continue
        for attr in TRANS_ATTRS:
            if attr in el.attrs:
                raw_value = str(el.attrs[attr])
                if should_translate_text(raw_value):
                    ops.append({"kind": "attr", "el": el, "attr": attr, "original": raw_value, "normalized": normalize_text(raw_value)})

    pending: List[Dict[str, object]] = []
    for op in ops:
        normalized = str(op["normalized"])
        stats.segments_total += 1
        key = digest(cfg.openai_model, normalized)
        op["cache_key"] = key
        if key in cache:
            stats.segments_cached += 1
            op["translated"] = cache[key]
        else:
            pending.append(op)

    # Batch translate uncached segments for fewer API calls.
    for i in range(0, len(pending), max(cfg.max_batch_items, 1)):
        chunk = pending[i : i + max(cfg.max_batch_items, 1)]
        texts = [str(op["normalized"]) for op in chunk]
        try:
            translated = translate_segments_batch(session, cfg, texts)
            stats.api_calls_total += 1
        except Exception:
            # Fallback to single-segment translation for robustness.
            translated = []
            for t in texts:
                translated.append(translate_segment(session, cfg, t))
                stats.api_calls_total += 1

        for op, out in zip(chunk, translated):
            op["translated"] = out
            cache[str(op["cache_key"])] = out
            stats.segments_translated += 1
        time.sleep(cfg.translate_sleep)

    for op in ops:
        translated = preserve_surrounding_whitespace(str(op["original"]), str(op["translated"]))
        if op["kind"] == "text":
            node = op["node"]
            node.replace_with(NavigableString(translated))
        else:
            el = op["el"]
            attr = str(op["attr"])
            el.attrs[attr] = translated
    if isinstance(soup.html, Tag):
        soup.html.attrs["lang"] = "ko"
    rewrite_internal_links_to_relative(soup, cfg.base_url)
    return str(soup)
def output_path_for_url(url: str, output_dir: Path) -> Path:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        path = "index"
    if path.endswith(".html"):
        rel = Path(path)
    else:
        rel = Path(path) / "index.html"
    return output_dir / rel
def compute_source_fingerprint(entries: List[SitemapEntry]) -> str:
    payload = "\n".join(sorted(f"{e.url}|{e.lastmod}" for e in entries))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
def has_source_changed(entries: List[SitemapEntry], state_path: Path) -> bool:
    current = compute_source_fingerprint(entries)
    if not state_path.exists():
        return True
    try:
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return True
    return previous.get("fingerprint") != current
def save_source_fingerprint(entries: List[SitemapEntry], state_path: Path) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fingerprint": compute_source_fingerprint(entries),
        "saved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entry_count": len(entries),
    }
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
def build_index(output_dir: Path, processed_urls: List[str]) -> None:
    links = "\n".join(
        f"<li><a href='{urlparse(url).path or '/'}'>{url}</a></li>" for url in processed_urls
    )
    html = f"""<!doctype html>
<html lang=\"ko\">
  <head>
    <meta charset=\"utf-8\" />
    <title>docs.world.org 한국어 미러</title>
  </head>
  <body>
    <h1>docs.world.org 한국어 미러</h1>
    <p>자동 생성 시각(UTC): {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}</p>
    <ul>{links}</ul>
  </body>
</html>
"""
    (output_dir / "index.html").write_text(html, encoding="utf-8")
def run(cfg: Config) -> Dict[str, object]:
    started = time.time()
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cfg.cache_path)
    progress = load_progress_state(cfg.progress_state_path)
    pages_state = progress.get("pages", {})
    deferred_state = progress.get("deferred", [])
    if not isinstance(pages_state, dict):
        pages_state = {}
    if not isinstance(deferred_state, list):
        deferred_state = []
    stats = TranslationStats()
    session = requests.Session()
    processed: List[str] = []
    errors: List[Dict[str, object]] = []
    rate_limit_errors = 0
    abort_reason = ""
    try:
        entries = parse_sitemap(session, cfg.sitemap_url, timeout=cfg.request_timeout)
        entries = [e for e in entries if urlparse(e.url).netloc == urlparse(cfg.base_url).netloc]
    except Exception as exc:  # noqa: BLE001
        # If sitemap lookup fails, still generate a minimal output by trying base URL.
        err = {
            "url": cfg.sitemap_url,
            "error_type": type(exc).__name__,
            "status_code": None,
            "message": str(exc),
        }
        errors.append(err)
        print(f"[SITEMAP_ERR] {cfg.sitemap_url}: {exc}")
        entries = [SitemapEntry(url=cfg.base_url, lastmod="")]
    entries = entries[: cfg.max_urls]
    entry_map = {e.url: e for e in entries}
    changed_urls = [
        e.url for e in entries if str(pages_state.get(e.url, "")) != str(e.lastmod or "")
    ]
    deferred_urls = [u for u in deferred_state if u in entry_map]
    pending_urls = list(dict.fromkeys(deferred_urls + changed_urls))
    pending_urls = sort_by_priority(pending_urls, cfg.priority_prefixes or [])
    if cfg.max_pages_per_run > 0:
        pending_urls = pending_urls[: cfg.max_pages_per_run]

    stats.urls_total = len(pending_urls)
    if not has_source_changed(entries, cfg.state_path):
        summary = build_summary(
            cfg=cfg,
            stats=stats,
            processed=processed,
            errors=errors,
            skipped=True,
            skip_reason="no_source_changes",
            started=started,
            rate_limit_count=rate_limit_errors,
            abort_reason=abort_reason,
        )
        summary["pending_count"] = len(pending_urls)
        summary["translated_count"] = 0
        summary["deferred_count"] = len(deferred_state)
        (cfg.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        build_index(cfg.output_dir, processed)
        print("[PASS] No source changes detected. Skipping translation.")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary
    new_deferred: List[str] = []
    for idx, url in enumerate(pending_urls):
        if (time.time() - started) >= cfg.max_runtime_seconds:
            abort_reason = "runtime_budget_exceeded"
            new_deferred.extend(pending_urls[idx:])
            break
        if stats.segments_total >= cfg.max_segments_per_run:
            abort_reason = "segment_budget_exceeded"
            new_deferred.extend(pending_urls[idx:])
            break
        try:
            page = fetch_text(session, url, timeout=cfg.request_timeout)
            translated_html = translate_html(
                page,
                session=session,
                cfg=cfg,
                cache=cache,
                stats=stats,
            )
            out = output_path_for_url(url, cfg.output_dir)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(translated_html, encoding="utf-8")
            processed.append(url)
            stats.urls_ok += 1
            entry = entry_map.get(url)
            if entry:
                pages_state[url] = entry.lastmod or ""
            print(f"[OK] {url} -> {out}")
            time.sleep(cfg.per_page_sleep)
        except RateLimitError as exc:
            stats.urls_failed += 1
            rate_limit_errors += 1
            errors.append(
                {
                    "url": url,
                    "error_type": "RateLimitError",
                    "status_code": 429,
                    "message": str(exc),
                    "retry_after": exc.retry_after,
                }
            )
            print(f"[ERR] {url}: {exc}")
            sleep_seconds = exc.retry_after if exc.retry_after is not None else min(60, 10 * rate_limit_errors)
            time.sleep(sleep_seconds)
            if rate_limit_errors >= 3:
                abort_reason = "too_many_rate_limits"
                errors.append(
                    {
                        "url": url,
                        "error_type": "Abort",
                        "status_code": None,
                        "message": "Too many consecutive 429 errors. Stopping this run.",
                    }
                )
                print("[ABORT] Too many consecutive 429 errors. Stopping this run.")
                new_deferred.extend(pending_urls[idx:])
                break
        except Exception as exc:  # noqa: BLE001
            stats.urls_failed += 1
            errors.append(
                {
                    "url": url,
                    "error_type": type(exc).__name__,
                    "status_code": None,
                    "message": str(exc),
                }
            )
            print(f"[ERR] {url}: {exc}")
            new_deferred.append(url)

    valid_urls = set(entry_map.keys())
    deferred_merged = [u for u in dict.fromkeys(new_deferred) if u in valid_urls]
    save_progress_state(
        cfg.progress_state_path,
        {
            "pages": pages_state,
            "deferred": deferred_merged,
            "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    build_index(cfg.output_dir, processed)
    save_cache(cfg.cache_path, cache)
    # If the run effectively failed due to rate limiting, mark as skipped so
    # workflow deploy is skipped and previous published site remains intact.
    if rate_limit_errors > 0 and stats.urls_ok == 0:
        summary = build_summary(
            cfg=cfg,
            stats=stats,
            processed=processed,
            errors=errors,
            skipped=True,
            skip_reason="rate_limited",
            started=started,
            rate_limit_count=rate_limit_errors,
            abort_reason=abort_reason or "rate_limited_zero_success",
        )
        summary["pending_count"] = len(pending_urls)
        summary["translated_count"] = stats.urls_ok
        summary["deferred_count"] = len(deferred_merged)
        (cfg.output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("[PASS] Skipping deploy because translation run was rate-limited.")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary

    save_source_fingerprint(entries, cfg.state_path)
    summary = build_summary(
        cfg=cfg,
        stats=stats,
        processed=processed,
        errors=errors,
        skipped=False,
        started=started,
        rate_limit_count=rate_limit_errors,
        abort_reason=abort_reason,
    )
    summary["pending_count"] = len(pending_urls)
    summary["translated_count"] = stats.urls_ok
    summary["deferred_count"] = len(deferred_merged)
    (cfg.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary
def main() -> None:
    args = parse_args()
    cfg = build_config(args)
    run(cfg)
if __name__ == "__main__":
    main()
