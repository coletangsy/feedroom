# Changelog

## [Unreleased]

### Added

- Added the `feedroom` Python package and CLI for collection, preview, and daily Threads publishing with account verification and a durable post journal.
- Added popular papers as a separate reading lane, grouped related source coverage, and added a validated OpenRouter writer for Cantonese Threads posts.
- Added local Docker and Cloudflare scheduled job configuration and Python, container, and scheduler checks.
- Added merged same-repository branch cleanup with pre-delete branch checks, an explicit SHA lease, and 22 regression tests.

### Changed

- Formatted the daily Threads digest as a Hong Kong date followed by five or six headline bullets when the evidence and post length allow. The lead source is sent as a link attachment. The journal retains each headline's source URL and the deduplication IDs for related coverage.

### Fixed

- Excluded published papers before capping the reading shortlist for Threads.
- Added a container readiness check before publication and paginated post recovery after a lost response.
- Kept valid stories when a feed article URL is malformed or an individual TLDR issue fails.
- Recognized Anthropic listing cards with a date in the card header and title in a sibling link.

### Documentation

- Removed the repository-level briefing skills and routed Feedroom previews through the CLI.
- Moved daily trigger ownership to the deployment scheduler and documented credential setup, retries, and cutover.
- Added repository guidance links, a collector and delivery architecture overview, and a changelog on 2026-11-07.
- Updated the README, architecture, and contributor guidance for the daily digest, with Docker Compose and SQLite as the primary setup and Cloudflare with R2 as an option.

This file begins tracking from 2026-11-07. Earlier source-maintenance history remains in Git.
