# Feedroom instructions

## Browser and computer use

- Browser and computer-use tools are authorized by default for in-scope research in this repository. Prefer Google Chrome for browser work unless the user specifies another browser.
- Open research tabs, search, navigate, and read public sources without asking for routine confirmation. Use browser interaction directly when needed; do not require a failed web-reader attempt first.
- Follow the tools' permission requirements for consequential actions. This research authorization does not expand the task to sending messages, purchases, or account/security changes.

## AI briefing

Use the [AI briefing skill](.agents/skills/ai-news-briefing/SKILL.md) for on-demand editorial previews and candidate quality reviews.

- Use collector JSON as the source of briefing facts. Previews only produce drafts.
- The deployment environment owns the daily trigger. Follow the [README](README.md) for the standalone CLI, scheduler, and Threads publishing controls.
- Keep runtime journals and credentials out of Git. Use fixtures and a mocked Threads client for automated checks.
- Run `python3 -m unittest discover -s tests -v` for Python changes and `node --test tests/cloudflare.test.mjs` for Cloudflare scheduler changes.

## Documentation

- [README](README.md) is the collector design and operator workflow entry point.
- [ARCHITECTURE.md](ARCHITECTURE.md) describes candidate collection, selection, delivery, and seen-state boundaries.
- [CHANGELOG.md](CHANGELOG.md) records delivered behavior and documentation changes.
- Update `ARCHITECTURE.md` when sources, candidate fields, selection limits, scheduling, delivery permissions, or seen-state handling changes. Update `CHANGELOG.md` for user-visible or workflow-significant changes.
