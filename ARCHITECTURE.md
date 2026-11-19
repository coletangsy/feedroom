# Feedroom Architecture

## Purpose and boundary

Feedroom is an installable Python package for a daily AI and data-science briefing. The collector gathers and normalizes candidate stories. The CLI can preview the collection or select one sourced story for Threads. The deployment scheduler owns the daily trigger.

## Data flow

```text
RSS / official pages / lists / release feeds
                 ↓
feedroom collect + source helpers
                 ↓
candidate JSON (latest / reading + collection_errors)
                 ↓
event groups → bounded article evidence → OpenRouter writer → validation
                 ↓
preview JSON or explicit Threads publish
                 ↓
account journal in SQLite or R2
```

`feedroom/collector.py` owns the time windows, normalization, topic and depth signals, deduplication, and per-source errors. The source modules hold Anthropic and Uber extraction. The legacy `scripts/ai_news_collect.py` entry point still supports the 14-day seen file for manual briefings.

The `latest` lane uses a 36-hour window. The `reading` lane looks back 90 days and contains only Hugging Face Daily Papers with at least 100 upvotes. Shortlist extraction honors the configured source hosts, robots rules, per-host pacing, and a two-MB page cap. It keeps usable snippets when full text is unavailable. Collection logs partial TLDR issue or paper-week failures.

The writer loads `feedroom/prompts/threads.v1.md` from the installed package and receives a bounded list of events and excerpts with ephemeral IDs. It returns a headline, facts, and practical assessment in Hong Kong Cantonese. The validator checks the selection, known Simplified Chinese forms, figures, formatting, and Threads length before resolving the URL from the original candidate. A failed validation gets one revision attempt. The publishing journal also records alternate item and title IDs from the selected event.

## Delivery boundary

- Source preview runs without credentials. Editorial preview and posting require the OpenRouter key and model. Editorial preview produces a draft without Threads credentials or journal access.
- `feedroom publish` verifies the Threads account and conditionally claims the Hong Kong run date. The journal retains the candidate and title IDs for cross-day deduplication. The publishing run uses those IDs to exclude published papers before keeping up to eight reading candidates.
- The job saves the media container ID and waits for the container to become ready. It records `publishing` with a conditional write before submitting the post. A later run checks the existing container and searches pages of account posts for a unique match. An unresolved result holds further publishing. Duplicate container creation under contention is safe because only the recorded container may be published.
- Local Docker uses a named volume with SQLite. Cloudflare starts the same image from a scheduled Worker and keeps its journal on private R2 using conditional object writes. Preview is the default mode for both.
