import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.translate_site import (
    Config,
    TranslationStats,
    output_path_for_url,
    preserve_surrounding_whitespace,
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


if __name__ == "__main__":
    unittest.main()
