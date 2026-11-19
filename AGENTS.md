# Feedroom instructions

## Browser and computer use

- Browser and computer-use tools are authorized by default for in-scope research in this repository. Prefer Google Chrome for browser work unless the user specifies another browser.
- Open research tabs, search, navigate, and read public sources without asking for routine confirmation. Use browser interaction directly when needed; do not require a failed web-reader attempt first.
- Follow the tools' permission requirements for consequential actions. This research authorization does not expand the task to sending messages, purchases, or account/security changes.

## Daily Threads digest

Use `feedroom collect` to inspect candidate JSON, `feedroom preview` for a source-title draft, and `feedroom preview --editorial` for the Cantonese digest.

- Ground each headline in its event evidence. Keep the Hong Kong date on the first line and place up to six headlines below it.
- The local Docker host owns the daily trigger. See the [README](README.md) for Compose, publishing, and optional Cloudflare deployment.
- Keep runtime journals and credentials out of Git. Use fixtures and a mocked Threads client for automated checks.
- Run `python3 -m unittest discover -s tests -v` for Python changes and `node --test tests/cloudflare.test.mjs` for Cloudflare scheduler changes.

## Documentation

- [README](README.md) covers the daily digest format, setup, previews, and scheduling.
- [ARCHITECTURE.md](ARCHITECTURE.md) describes collection, editorial validation, publication, and recovery.
- [CHANGELOG.md](CHANGELOG.md) records delivered behavior and documentation changes.
- Update `ARCHITECTURE.md` when sources, candidate fields, selection limits, scheduling, delivery permissions, or seen-state handling changes. Update `CHANGELOG.md` for user-visible or workflow-significant changes.
