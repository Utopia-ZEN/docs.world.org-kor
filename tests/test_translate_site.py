import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from bs4 import BeautifulSoup

from scripts.translate_site import (
    Config,
    TranslationStats,
    output_path_for_url,
    preserve_surrounding_whitespace,
    run,
    should_translate_text,
    translate_html,
)


class DummySession:
    pass


class TranslateSiteTests(unittest.TestCase):
    def test_should_translate_text(self):
        self.assertTrue(should_translate_text("Install World App now"))
        self.assertFalse(should_translate_text("   "))
        self.assertFalse(should_translate_text("/api/v1/users"))
        self.assertFalse(should_translate_text("이미 한글입니다"))

    def test_preserve_surrounding_whitespace(self):
        original = "  Hello world\n"
        translated = "안녕하세요 세계"
        self.assertEqual(
            preserve_surrounding_whitespace(original, translated),
            "  안녕하세요 세계\n",
        )

    def test_output_path_for_url(self):
        out = output_path_for_url("https://docs.world.org/developers", Path("output"))
        self.assertEqual(str(out), "output/developers/index.html")

    def test_translate_html_skips_code(self):
        html = """
        <html><body><main>
          <p>Hello docs</p>
          <pre><code>npm install world-id</code></pre>
        </main></body></html>
        """

        cfg = Config(
            base_url="https://docs.world.org",
            sitemap_url="https://docs.world.org/sitemap.xml",
            output_dir=Path("output"),
            cache_path=Path(".translation-cache.json"),
            openai_api_key="dummy",
            openai_base_url="https://api.openai.com/v1",
            openai_model="gpt-4.1-mini",
            max_urls=1,
            request_timeout=10,
            per_page_sleep=0,
            translate_sleep=0,
            openai_max_retries=1,
            state_path=Path(".state/source-fingerprint.json"),
        )

        cache = {}
        stats = TranslationStats()

        # monkey patch function locally by replacing module symbol
        import scripts.translate_site as mod

        original_fn = mod.translate_segment
        mod.translate_segment = lambda session, cfg, text: f"KO({text})"
        try:
            out = translate_html(
                html,
                session=DummySession(),
                cfg=cfg,
                cache=cache,
                stats=stats,
            )
        finally:
            mod.translate_segment = original_fn

        soup = BeautifulSoup(out, "lxml")
        self.assertIn("KO(Hello docs)", soup.get_text())
        self.assertIn("npm install world-id", soup.get_text())

    def test_run_writes_summary_when_sitemap_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(
                base_url="https://docs.world.org",
                sitemap_url="https://docs.world.org/sitemap.xml",
                output_dir=tmp,
                cache_path=tmp / ".translation-cache.json",
                openai_api_key="dummy",
                openai_base_url="https://api.openai.com/v1",
                openai_model="gpt-4.1-mini",
                max_urls=1,
                request_timeout=1,
                per_page_sleep=0,
                translate_sleep=0,
                openai_max_retries=0,
                state_path=tmp / ".state/source-fingerprint.json",
            )

            with patch("scripts.translate_site.parse_sitemap", side_effect=RuntimeError("boom")):
                with patch("scripts.translate_site.fetch_text", side_effect=RuntimeError("fetch fail")):
                    summary = run(cfg)

            self.assertTrue((tmp / "index.html").exists())
            self.assertTrue((tmp / "summary.json").exists())
            self.assertIn("errors", summary)
            self.assertGreaterEqual(len(summary["errors"]), 1)

    def test_run_skips_when_no_source_changes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state = tmp / ".state/source-fingerprint.json"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text('{"fingerprint":"dummy"}', encoding="utf-8")

            cfg = Config(
                base_url="https://docs.world.org",
                sitemap_url="https://docs.world.org/sitemap.xml",
                output_dir=tmp,
                cache_path=tmp / ".translation-cache.json",
                openai_api_key="dummy",
                openai_base_url="https://api.openai.com/v1",
                openai_model="gpt-4.1-mini",
                max_urls=1,
                request_timeout=1,
                per_page_sleep=0,
                translate_sleep=0,
                openai_max_retries=0,
                state_path=state,
            )

            import scripts.translate_site as mod
            entry = mod.SitemapEntry(url="https://docs.world.org/page", lastmod="2026-01-01")

            with patch("scripts.translate_site.parse_sitemap", return_value=[entry]):
                with patch("scripts.translate_site.has_source_changed", return_value=False):
                    summary = run(cfg)

            self.assertTrue(summary.get("skipped"))
            self.assertEqual(summary.get("skip_reason"), "no_source_changes")


if __name__ == "__main__":
    unittest.main()
