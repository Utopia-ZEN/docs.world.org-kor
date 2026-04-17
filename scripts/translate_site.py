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
from typing import Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

DEFAULT_BASE_URL = "https://docs.world.org"
DEFAULT_SITEMAP_URL = f"{DEFAULT_BASE_URL}/sitemap.xml"
DEFAULT_OUTPUT_DIR = "output"
DEFAULT_CACHE_PATH = ".translation-cache.json"
DEFAULT_MODEL = "gpt-4.1-mini"

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


@dataclass
class TranslationStats:
    urls_total: int = 0
    urls_ok: int = 0
    urls_failed: int = 0
    segments_total: int = 0
    segments_cached: int = 0
    segments_translated: int = 0


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
    )


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
    if IDENTIFIER_ONLY_RE.fullmatch(stripped):
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
                if attempt == max_retries:
                    res.raise_for_status()
                time.sleep(backoff)
                backoff *= 2
                continue
            res.raise_for_status()
            return res
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


def fetch_text(session: requests.Session, url: str, timeout: int) -> str:
    res = request_with_retry(session, "GET", url, timeout=timeout, max_retries=3)
    return res.text


def parse_sitemap(session: requests.Session, sitemap_url: str, timeout: int) -> List[str]:
    seen: Set[str] = set()

    def _parse(url: str) -> List[str]:
        if url in seen:
            return []
        seen.add(url)

        xml = fetch_text(session, url, timeout=timeout)
        soup = BeautifulSoup(xml, "xml")

        nested = [loc.text.strip() for loc in soup.select("sitemap > loc") if loc.text]
        if nested:
            urls: List[str] = []
            for nested_url in nested:
                urls.extend(_parse(nested_url))
            return urls

        return [loc.text.strip() for loc in soup.select("url > loc") if loc.text]

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

    def translate_value(original: str) -> str:
        normalized = normalize_text(original)
        stats.segments_total += 1
        key = digest(cfg.openai_model, normalized)
        if key in cache:
            stats.segments_cached += 1
            return preserve_surrounding_whitespace(original, cache[key])

        translated = translate_segment(session, cfg, normalized)
        cache[key] = translated
        stats.segments_translated += 1
        time.sleep(cfg.translate_sleep)
        return preserve_surrounding_whitespace(original, translated)

    for text_node in list(main.find_all(string=True)):
        parent = text_node.parent
        if not isinstance(parent, Tag) or should_skip_node(parent):
            continue

        original = str(text_node)
        if not should_translate_text(original):
            continue
        text_node.replace_with(NavigableString(translate_value(original)))

    for el in main.find_all(True):
        if should_skip_node(el):
            continue
        for attr in TRANS_ATTRS:
            if attr in el.attrs:
                raw_value = str(el.attrs[attr])
                if should_translate_text(raw_value):
                    el.attrs[attr] = translate_value(raw_value)

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


def keep_same_domain(urls: Iterable[str], base_url: str) -> List[str]:
    base_host = urlparse(base_url).netloc
    return [url for url in urls if urlparse(url).netloc == base_host]


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
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cache = load_cache(cfg.cache_path)
    stats = TranslationStats()
    session = requests.Session()

    urls = parse_sitemap(session, cfg.sitemap_url, timeout=cfg.request_timeout)
    urls = keep_same_domain(urls, cfg.base_url)
    urls = urls[: cfg.max_urls]
    stats.urls_total = len(urls)

    processed: List[str] = []

    for url in urls:
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
            print(f"[OK] {url} -> {out}")
            time.sleep(cfg.per_page_sleep)
        except Exception as exc:  # noqa: BLE001
            stats.urls_failed += 1
            print(f"[ERR] {url}: {exc}")

    build_index(cfg.output_dir, processed)
    save_cache(cfg.cache_path, cache)

    summary = {
        "base_url": cfg.base_url,
        "sitemap_url": cfg.sitemap_url,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats.__dict__,
        "processed_urls": processed,
    }

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
