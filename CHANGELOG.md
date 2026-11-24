# Changelog

## [Unreleased]

### Added

- Added an editorial weekly preview that starts from published daily stories, allows one sourced supplement, and renders ordered weekly cards.
- Persisted confirmed Threads post IDs before fetching their publication times, so incomplete metadata responses leave the post recorded as published.
- Added DAIR.AI Papers of the Week to the daily digest's reading candidates, with cross-source arXiv deduplication.
- Added explicit Threads card publishing with a dedicated private R2 bucket, Hong Kong date folders and short-lived image URLs.
- Added an opt-in preview that renders one 1080 × 1350 news card per selected Threads headline, with a source-grounded summary and a fixture for Docker or local replays.

- Added the `feedroom` Python package and CLI for collection, preview, and daily Threads publishing with account verification and a durable post journal.
- Added popular papers as a separate reading lane, grouped related source coverage, and added a validated OpenRouter writer for Cantonese Threads posts.
- Added local Docker and Cloudflare scheduled job configuration and Python, container, and scheduler checks.
- Added merged same-repository branch cleanup with pre-delete branch checks, an explicit SHA lease, and 22 regression tests.

### Changed

- Labelled daily news cards with 「每日 AI 摘要」 and styled weekly previews with a pale sage background and a 「每週 AI 摘要」 label.
- Aligned headlines across paired weekly cards so readers can follow the same story across slides.
- Shortened Cantonese digest headlines to their opening main clause, with supporting details in the card summary.
- Limited remote branch cleanup to PRs merged into `main` and documented the local branch cleanup and Docker image rebuild on the scheduled host.
- Formatted the daily Threads digest as a Hong Kong date followed by five or six headline bullets when the evidence and post length allow. The journal retains each headline's source URL and the deduplication IDs for related coverage.
- Added the `AI Threads` topic tag to daily posts and removed the lead link preview and Feedroom's automatic AI self-disclosure.

### Fixed

- Kept daily stories whose Threads publication is in the weekly window even when their original articles predate it. Applied the same Hong Kong date rules to live and replayed candidates and filtered supplemental papers before ranking them.
- Grounded weekly headlines and card copy in the original source linked to each story.

- Removed Feedroom's 5,000-token limit on OpenRouter requests so model reasoning can run beyond that limit and still return a draft.
- Excluded published papers before capping the reading shortlist for Threads.
- Added a container readiness check before publication and paginated post recovery after a lost response.
- Kept valid stories when a feed article URL is malformed or an individual TLDR issue fails.
- Recognized Anthropic listing cards with a date in the card header and title in a sibling link.

### Removed

- Removed the archived MODHAUS reports from `reports/`.

### Documentation

- Removed the repository-level briefing skills and routed Feedroom previews through the CLI.
- Moved daily trigger ownership to the deployment scheduler and documented credential setup, retries, and cutover.
- Added repository guidance links, a collector and delivery architecture overview, and a changelog on 2026-11-07.
- Updated the README, architecture, and contributor guidance for the daily digest, with Docker Compose and SQLite as the primary setup and Cloudflare with R2 as an option.

This file begins tracking from 2026-11-07. Earlier source-maintenance history remains in Git.
