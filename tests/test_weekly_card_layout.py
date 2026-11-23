import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch

from feedroom.briefing import Card
from feedroom.card_render import _render_card, render_cards


PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None


@skipUnless(PLAYWRIGHT_AVAILABLE, "Playwright is required for card layout tests")
class WeeklyCardLayoutTests(TestCase):
    def test_paired_weekly_cards_keep_headline_font_and_position(self):
        headline = "研究團隊發布新版工具評估跨語言模型喺長文本推理同實務情境嘅表現"
        short_summary = "團隊推出新版工具，協助研究員比較模型表現及實際應用啦。"
        long_summary = (
            "研究團隊詳細說明新版工具嘅設計、評估流程、跨語言測試結果，同埋喺長文本推理、資料分析及真實業務情境中觀察到嘅限制。"
            "研究亦比較唔同模型喺速度、準確度、成本同穩定性方面嘅差異，並提出後續驗證方向，方便研究員按需要選擇合適方法。"
            "測試資料涵蓋多種語言同任務，團隊會持續更新基準，方便日後比較結果。"
        )
        self.assertEqual(len(headline), 31)
        self.assertEqual(len(short_summary), 27)
        self.assertGreaterEqual(len(long_summary), 140)
        self.assertLessEqual(len(long_summary), 150)

        cards = (
            Card(headline, short_summary, "研究團隊", "2026-11-20", "https://example.com/story"),
            Card(headline, long_summary, "研究團隊", "2026-11-20", "https://example.com/story"),
        )

        metrics = []

        def capture_headline(page, markup, destination, **kwargs):
            _render_card(page, markup, destination, **kwargs)
            metrics.append(page.locator(".headline").evaluate(
                """element => {
                    const box = element.getBoundingClientRect();
                    return {fontSize: getComputedStyle(element).fontSize, x: box.x, y: box.y};
                }"""
            ))

        with (TemporaryDirectory() as temporary_directory,
              patch("feedroom.card_render._render_card", side_effect=capture_headline)):
            paths = render_cards(cards, Path(temporary_directory) / "cards", weekly=True)
            self.assertEqual([path.name for path in paths], ["01.png", "02.png"])

        self.assertEqual(metrics[0]["fontSize"], metrics[1]["fontSize"], metrics)
        self.assertAlmostEqual(metrics[0]["x"], metrics[1]["x"], places=3, msg=metrics)
        self.assertAlmostEqual(metrics[0]["y"], metrics[1]["y"], places=3, msg=metrics)
