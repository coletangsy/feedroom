# Feedroom Architecture

## Purpose and boundary

Feedroom is an installable Python package for one daily Hong Kong-dated Threads digest of sourced AI and data-science headlines. A normal digest aims for five or six one-line headline bullets, with six as the hard maximum. The workflow may produce fewer bullets, `no_story`, or `collection_incomplete` when distinct supported events, usable evidence, or the Threads length limit do not allow a complete draft. The collector and source helpers provide inputs. The CLI builds previews or runs an explicit publication, while the deployment scheduler owns the daily trigger.

## Data flow

```text
RSS / official pages / lists / release feeds
                 ↓
feedroom collect + source helpers
                 ↓
candidate JSON (latest / reading + collection_errors)
                 ↓
event groups → source titles or bounded article evidence and OpenRouter headlines → validation
                 ↓
dated post: Hong Kong date + up to six bullets + ordered source URLs and IDs
                 ├─ preview JSON and optional ordered PNG cards (no journal or Threads access)
                 └─ explicit Threads publish → account journal in SQLite or R2
```

`feedroom/collector.py` owns the time windows, normalization, topic and depth signals, deduplication, and per-source errors. The source modules hold Anthropic and Uber extraction. The legacy `scripts/ai_news_collect.py` entry point still supports the 14-day seen file for manual briefings.

The `latest` lane uses a 36-hour window. The `reading` lane looks back 90 days for Hugging Face Daily Papers with at least 100 upvotes and for the most recent DAIR.AI Papers issue whose coverage ended before the reference date. DAIR.AI cards supply titles and curator snippets. The collector uses canonical arXiv URLs when the DAIR.AI paper slug contains an arXiv ID and the DAIR.AI paper page otherwise. For DAIR.AI, candidate `published_at` holds the issue coverage end date. Editorial evidence labels it `coverage_end_date` and cards omit the paper date. The shared shortlist has up to eight papers and makes room for the highest-ranked eligible DAIR.AI item after the highest-ranked Hugging Face item. Shortlist extraction honors the configured source hosts, robots rules, per-host pacing, and a two-MB page cap. It keeps usable snippets when full text is unavailable. Collection logs TLDR issue, Hugging Face paper-week, and DAIR.AI source failures.

The writer loads `feedroom/prompts/threads.v1.md` from the installed package and receives up to twelve event groups with bounded source excerpts and ephemeral IDs. It returns up to six event IDs with one Hong Kong Cantonese headline each. Each headline keeps the opening main clause and is limited to 40 characters. The validator checks each headline against its own event, rejects duplicate selections, rejects unsupported figures or unsafe formatting, and enforces the Threads length limit for the whole post. A failed validation gets one revision attempt. Non-editorial previews select source titles directly and apply the same grouping, deduplication, and the total post length limit. Previews use the collection date, while publishing uses the run date. The post text contains the Hong Kong date and headline bullets. The draft keeps the selected source URLs in headline order, the lead candidate and title IDs, and the related candidate and title IDs used for cross-day deduplication.

## Card previews

`preview --cards-dir` derives one card per selected item from the same ordered selection that formats the dated title list. In editorial mode, the writer requests one or two short Cantonese sentences for each selected event, grounded in its primary source, and allows a null summary when evidence is too thin. It validates the summary format and figures against that source. Source-only previews take up to two complete sentences from the selected source snippet. Newsletter edition and aggregator dates are omitted when the linked article's publication date is unknown.

The preview JSON includes the source name, supported date and original URL for each card. The selected D template renders local PNGs through the optional Playwright dependency. The `cards` Docker target installs Chromium and CJK fonts for previews and explicit `publish --with-cards` runs. The scheduled job uses the default runtime and publishes the dated text digest.

Card publishing uses a private R2 bucket with separate credentials for media. Each key includes the Hong Kong run date, headline position and SHA-256 content hash under `threads/YYYY-MM-DD/`. Meta fetches each PNG through a one-hour signed GET URL. Feedroom saves card content, acknowledged image child IDs and the chosen parent container in the shared publishing journal. Signed URLs are only used during media creation. For multiple cards, retries resume from the saved children, and the publisher waits for those images before creating the carousel parent. If a child creation response is lost, that unpublished child can expire without being attached to the carousel. Only an explicit card run can submit a prepared card post. The existing conditional journal writes guard single-post publication and recovery for both modes.

## Delivery boundary

- Source preview runs without credentials. Editorial preview requires the OpenRouter key and model, while posting additionally requires the Threads credentials. Editorial preview produces a draft without Threads credentials or journal access.
- `feedroom publish` verifies the Threads account and conditionally claims the Hong Kong run date. The journal retains the lead candidate and title IDs, related IDs, and ordered source URLs for each selected digest, so later runs can exclude already-used stories. The publishing run uses those IDs to exclude published papers before keeping up to eight reading candidates.
- The job creates a text container with `topic_tag=AI Threads` and omits both the link attachment and the AI self-disclosure field. It saves the container ID with an empty link attachment in the journal and waits for the container to become ready. It records `publishing` with a conditional write before submitting the post. A later run checks the existing container and searches pages of account posts for a unique match. An unresolved result holds further publishing. Duplicate container creation under contention is safe because only the recorded container may be published.
- After a publish or recovery succeeds, the publisher saves the confirmed post ID and marks the record published. It then reads the Threads media `timestamp` and conditionally fills `published_at` for the same ID. If the metadata request fails, the timestamp stays unknown and the confirmed publication remains recorded.
- Local Docker Compose is the primary runtime. It passes `.env` values to the container and keeps the SQLite journal at `/data/threads.sqlite3` in the named `threads-state` volume. The host scheduler runs `feedroom publish`. An operator can run either preview or publish explicitly. The optional Cloudflare scheduled Worker starts the same image and publishes with a private R2 journal backed by conditional object writes. Both deployments start in preview mode by default.
- Recovery matches the exact post text and also checks the attachment URL for older containers that recorded a lead `source_url` as their attachment. Existing entries can be reused as-is. A missing or ambiguous Threads response remains unresolved until a later run can identify the recorded container and unique post.

## Weekly editorial preview

`weekly-preview` computes the coverage window from Hong Kong Monday 00:00 through the provided cutoff. It reads the selected account's SQLite journal in read-only mode. Daily story IDs and URLs qualify when the confirmed Threads publication time falls within the window, including stories whose original article predates the week. Records with an unknown publication time cannot establish daily coverage. Live collection matches journal keys against publisher feeds, releases, and Hugging Face paper listings. When a journal link is missing from those listings, the preview can recover a daily story from the original page on a configured publisher host. This fallback requires article text and leaves the original publication date unknown. Supplemental sources must have an original publication date within the window. This filter applies before ranking and capping papers. Live and replayed date-only records use the Hong Kong calendar, while timestamps must meet the precise cutoff. Hacker News submissions and TLDR or DAIR.AI issue dates cannot establish original publication.

The preview groups related reports and sends up to twelve events to the packaged `prompts/weekly.v1.md` writer. The supplement shortlist gives different publishers priority before considering more stories from the same publisher. The eight-article read budget prioritizes supplemental primary sources, then daily primary sources, then fallbacks for stories without usable evidence. The reader keeps up to 5,000 characters per excerpt. If a configured publisher redirects the canonical URL to the same article with a trailing slash, the reader checks robots rules for the new URL before requesting it.

The validator allows up to three stories, with at most one supplement. The writer receives evidence only from the primary source linked in the caption and cards. The validator checks generated numbers against that source's title, snippet and excerpt. Publication metadata is used for the coverage window. The writer receives the remaining caption budget and link lengths, and the validator keeps the highest-ranked stories whose captions and original links fit within 500 characters. Card pairs share one headline size and layout and can fit their body text individually. Preview work uses the existing OpenRouter transport and is isolated from Threads delivery and scheduling.
