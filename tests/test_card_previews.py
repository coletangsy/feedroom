import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from feedroom.briefing import select_draft
from feedroom.cli import main
from feedroom.writer import complete, write_draft
from test_writer import HEADLINES, collected, daily_stories, editorial


class CardPreviewTests(TestCase):
    def test_five_edited_items_share_one_order_between_text_cards_and_pngs(self):
        stories = daily_stories(5)
        order = [3, 0, 4, 2, 1]
        reply = {"items": [
            {"event_id": f"E{index + 1}", "headline": HEADLINES[index],
             "summary": f"研究介紹{HEADLINES[index]}嘅做法。"}
            for index in order
        ]}
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "cards"
            images = tuple(output_dir / f"{index + 1:02}.png" for index in range(5))
            with (patch("feedroom.cli.collect_from_args", return_value=collected(*stories)),
                  patch("feedroom.writer.complete", return_value=reply),
                  patch("feedroom.cli.render_cards", return_value=images) as render,
                  patch("feedroom.cli.open_journal") as journal,
                  patch("builtins.print") as output):
                self.assertEqual(main(["preview", "--editorial", "--cards-dir", str(output_dir)]), 0)
        journal.assert_not_called()
        response = json.loads(output.call_args.args[0])
        draft = response["draft"]
        headlines = [HEADLINES[index] for index in order]
        self.assertEqual(draft["text"].splitlines(), ["2026-11-20", *[f"• {title}" for title in headlines]])
        self.assertEqual([card["headline"] for card in draft["cards"]], headlines)
        self.assertEqual([card["source_url"] for card in draft["cards"]], [stories[index]["url"] for index in order])
        self.assertEqual([card["summary"] for card in draft["cards"]], [item["summary"] for item in reply["items"]])
        self.assertEqual(response["card_images"], list(map(str, images)))
        rendered_cards, rendered_dir = render.call_args.args
        self.assertEqual([card.headline for card in rendered_cards], headlines)
        self.assertEqual(rendered_dir, output_dir)

    def test_source_preview_limits_excerpts_and_preserves_missing_article_date(self):
        story = daily_stories(1)[0]
        story.update(source="TLDR AI", summary_or_snippet="第一句。第二句。第三句。")
        draft = select_draft(collected(story), set(), include_cards=True)
        self.assertEqual(draft.cards[0].summary, "第一句。第二句。")
        self.assertIsNone(draft.cards[0].source_date)
        self.assertEqual(draft.cards[0].source_url, story["url"])
        self.assertNotIn("第一句", draft.text)

    def test_card_summary_validation_retries_cross_event_figures_and_extra_sentences(self):
        stories = daily_stories(2)
        stories[0]["summary_or_snippet"] = "The first model achieves 72% accuracy."
        stories[1]["summary_or_snippet"] = "The second model achieves 95% accuracy."
        valid_item = {**editorial(HEADLINES[0])["items"][0], "summary": "模型準確率有 72%。"}
        for wrong in ("準確率有 95%。", "第一句。第二句。第三句。"):
            with (self.subTest(summary=wrong),
                  patch("feedroom.writer.complete", side_effect=[
                      {"items": [{**valid_item, "summary": wrong}]}, {"items": [valid_item]},
                  ]) as model):
                draft = write_draft(collected(*stories), set(), include_cards=True)
                self.assertEqual(model.call_count, 2)
                self.assertEqual(draft.cards[0].summary, valid_item["summary"])
                self.assertNotIn("72%", draft.text)

    def test_editorial_card_schema_accepts_null_without_losing_the_card(self):
        item = {**editorial(HEADLINES[0])["items"][0], "summary": None}
        reply = {"items": [item]}
        body = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        with (patch.dict("os.environ", {"OPENROUTER_API_KEY": "fixture", "OPENROUTER_MODEL": "fixture/model"}),
              patch("feedroom.writer.build_opener") as opener):
            opener.return_value.open.return_value = io.BytesIO(body)
            self.assertEqual(complete({"events": []}, None, include_cards=True), reply)
        request = json.loads(opener.return_value.open.call_args.args[0].data)
        schema = request["response_format"]["json_schema"]["schema"]
        item_schema = schema["properties"]["items"]["items"]
        self.assertIn("summary", item_schema["required"])
        self.assertEqual(item_schema["properties"]["summary"]["type"], ["string", "null"])

        story = daily_stories(1)[0]
        with patch("feedroom.writer.complete", return_value=reply):
            draft = write_draft(collected(story), set(), include_cards=True)
        self.assertEqual(len(draft.cards), 1)
        self.assertIsNone(draft.cards[0].summary)
        self.assertEqual(draft.cards[0].source_date, "2026-11-19")

    def test_existing_output_stops_before_reading_sources_or_calling_editor(self):
        with (TemporaryDirectory() as output,
              patch("feedroom.cli.collect_from_args") as collect,
              patch("feedroom.cli.write_draft") as write,
              patch("builtins.print")):
            self.assertEqual(main(["preview", "--editorial", "--cards-dir", output]), 1)
        collect.assert_not_called()
        write.assert_not_called()

    def test_empty_selection_does_not_call_renderer(self):
        with (patch("feedroom.cli.collect_from_args", return_value=collected()),
              patch("feedroom.cli.render_cards") as render,
              patch("builtins.print") as output):
            self.assertEqual(main(["preview", "--cards-dir", "unused"]), 0)
        render.assert_not_called()
        self.assertEqual(json.loads(output.call_args.args[0])["card_images"], [])
