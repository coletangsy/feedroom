"""Render structured digest cards as 1080 by 1350 PNG files."""

from __future__ import annotations

import base64
from html import escape
import os
from pathlib import Path
import re
import shutil
from string import Template
from tempfile import mkdtemp
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .briefing import Card


CARD_WIDTH = 1080
CARD_HEIGHT = 1350
_DATE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})")


def _asset_path(name: str) -> Path:
    return Path(__file__).with_name("card_assets") / name


def _display_date(value: str | None) -> str:
    if not value:
        return ""
    match = _DATE.match(value)
    return match.group(1) if match else value.strip()


def _metadata(card: Card) -> str:
    date = _display_date(card.source_date)
    source = escape(card.source_name.strip(), quote=False)
    source_markup = f'<span class="metadata-source"><span class="metadata-label">來源</span> <strong>{source}</strong></span>'
    if date:
        date_markup = f'<time class="metadata-date" datetime="{escape(date)}">{escape(date, quote=False)}</time>'
        return f'{date_markup}<span class="separator">·</span>{source_markup}'
    return source_markup


def _template(card: Card) -> str:
    template_path = _asset_path("card.html")
    style_path = _asset_path("cards.css")
    logo_path = _asset_path("untitledagent-ivory-1200.png")
    try:
        template = template_path.read_text(encoding="utf-8")
        styles = style_path.read_text(encoding="utf-8")
        logo = base64.b64encode(logo_path.read_bytes()).decode("ascii")
    except OSError as error:
        raise OSError(f"Feedroom card assets are unavailable: {error}") from error

    summary = card.summary.strip() if card.summary else ""
    summary_markup = f'<p class="summary">{escape(summary, quote=False)}</p>' if summary else ""
    return Template(template).substitute(
        styles=styles,
        metadata=_metadata(card),
        logo_data_uri=f"data:image/png;base64,{logo}",
        headline=escape(card.headline.strip(), quote=False),
        summary_markup=summary_markup,
    )


def _fit_card(page) -> None:
    """Reduce text sizes until the fixed card has no overflow."""

    result = page.evaluate(
        """
        () => {
          const card = document.querySelector('.card');
          const story = document.querySelector('.story');
          const headline = document.querySelector('.headline');
          const summary = document.querySelector('.summary');
          const segmenter = new Intl.Segmenter('zh-HK', { granularity: 'word' });
          headline.replaceChildren(...Array.from(segmenter.segment(headline.textContent), ({ segment }) => {
            if (!/^\\p{Script=Han}{2,}$/u.test(segment)) return document.createTextNode(segment);
            const word = document.createElement('span');
            word.className = 'headline-word';
            word.textContent = segment;
            return word;
          }));
          const metadata = card.querySelector('.metadata');
          const sizes = { headline: 97, summary: summary ? 50 : 0, metadata: 38 };
          const cardBox = card.getBoundingClientRect();
          const elements = [card.querySelector('.brand'), story, headline, summary,
            card.querySelector('.brand-mark'), metadata].filter(Boolean);
          const contained = () => elements.every((element) => {
            const box = element.getBoundingClientRect();
            return box.top >= cardBox.top - 1 && box.bottom <= cardBox.bottom + 1 &&
              box.left >= cardBox.left - 1 && box.right <= cardBox.right + 1;
          });
          const fits = () => story.scrollHeight <= story.clientHeight + 1 && contained();
          while (!fits()) {
            if (!contained() && sizes.metadata > 26) {
              sizes.metadata -= 2;
              metadata.style.fontSize = `${sizes.metadata}px`;
            } else if (sizes.headline > 57) {
              sizes.headline -= 4;
              headline.style.fontSize = `${sizes.headline}px`;
            } else if (summary && sizes.summary > 32) {
              sizes.summary -= 2;
              summary.style.fontSize = `${sizes.summary}px`;
            } else {
              return false;
            }
          }
          return true;
        }
        """
    )
    if not result:
        raise ValueError("Card text does not fit within 1080 × 1350 after font-size adjustment.")


def _render_card(page, markup: str, destination: Path) -> None:
    page.set_content(markup, wait_until="load")
    page.evaluate("document.fonts.ready")
    _fit_card(page)
    page.locator(".card").screenshot(path=str(destination), type="png")
    if destination.stat().st_size == 0:
        raise OSError(f"Renderer created an empty image: {destination}")


def render_cards(cards: tuple[Card, ...], output_dir: Path) -> tuple[Path, ...]:
    """Render one image per card in order and atomically finalize the batch.

    ``output_dir`` must not exist. Existing files and batches are never replaced.
    The Playwright import is intentionally delayed until cards are requested.
    """

    cards = tuple(cards)
    if not cards:
        raise ValueError("Render at least one card.")
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"Card output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    temp_dir = Path(mkdtemp(prefix=f".{output_dir.name or 'cards'}.tmp-", dir=output_dir.parent))
    claimed_output_dir = False
    try:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise ValueError("Install feedroom[cards] to render cards with Playwright.") from error

        browser_path = os.environ.get("FEEDROOM_BROWSER_PATH", "").strip() or None
        with sync_playwright() as playwright:
            try:
                launch_args = {"headless": True}
                if browser_path:
                    launch_args["executable_path"] = browser_path
                browser = playwright.chromium.launch(**launch_args)
            except Exception as error:
                raise ValueError(
                    "Unable to launch Chromium. Install feedroom[cards] browsers or set "
                    "FEEDROOM_BROWSER_PATH to a trusted Chromium executable."
                ) from error

            try:
                paths: list[Path] = []
                index = 0
                page = browser.new_page(viewport={"width": CARD_WIDTH, "height": CARD_HEIGHT}, device_scale_factor=1)
                page.route("**/*", lambda route: route.abort())
                for index, card in enumerate(cards, 1):
                    destination = temp_dir / f"{index:02d}.png"
                    _render_card(page, _template(card), destination)
                    paths.append(destination)
            except PlaywrightError as error:
                card_label = f" {index:02d}" if index else ""
                raise ValueError(f"Unable to render card{card_label}: {error}") from error
            finally:
                browser.close()

        try:
            output_dir.mkdir()
        except FileExistsError as error:
            raise FileExistsError(f"Card output path appeared while rendering: {output_dir}") from error
        claimed_output_dir = True
        os.replace(temp_dir, output_dir)
        temp_dir = None
        return tuple(output_dir / path.name for path in paths)
    except Exception:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if claimed_output_dir and output_dir.is_dir() and not any(output_dir.iterdir()):
            output_dir.rmdir()
        raise
