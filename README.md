# Feedroom

Feedroom 蒐集 AI 與資料科學動態，供手動預覽或每日發佈到 Threads。每日工作由本機 Docker 主機排程或 Cloudflare Cron Trigger 啟動。

## Maintainer Documentation

- [Repository guidance](AGENTS.md)
- [Architecture overview](ARCHITECTURE.md)
- [Changelog](CHANGELOG.md)

## 設計

`feedroom collect` 蒐集、正規化、評分同去重，並輸出 JSON。`latest` 收錄最近 36 小時的新聞、文章同 releases。`reading` 從 Hugging Face Daily Papers 回看 90 日，只收錄至少 100 個 upvote 的論文，並連到原文摘要。收集會排除參考時間之後發佈的內容。 [Hugging Face Daily Papers API](https://huggingface.co/docs/huggingface_hub/main/en/package_reference/hf_api#huggingface_hub.HfApi.list_daily_papers)

撰稿前先合併標題相近的同一事件，再嘗試補讀候選的文章正文。補讀限於已配置的公開來源網域，遵守 `robots.txt`，遇到封鎖或正文太短就沿用摘要。OpenRouter 從證據中挑最多一件事，以香港粵語寫背景、事實同對讀者的影響。程式核對事件 ID、數字、格式同長度，再加上原文連結。

官方研究與新聞來源：

- [OpenAI RSS](https://openai.com/news/rss.xml)：共用官方綜合 feed，包含 Research 與 Signals 文章；不另抓 Signals 的互動資料頁或保證完整收錄其所有報告。
- [Anthropic News](https://www.anthropic.com/news) 與 [Anthropic Research](https://www.anthropic.com/research)：解析目前頁面上的文章，取得原文摘要及發布日期。
- [Google DeepMind Blog](https://deepmind.google/blog/rss.xml)：包含 DeepMind 研究與模型消息。
- [Google Research](https://research.google/blog/rss/) 與 [Google Innovation & AI](https://blog.google/innovation-and-ai/rss/)：補充研究、科學及應用文章，並保留 Google AI RSS。
- [Apple Machine Learning Research](https://machinelearning.apple.com/)：補充 Apple 的機器學習研究文章。
- [Meta Engineering AI Research](https://engineering.fb.com/category/ai-research/feed/)：採用 Meta 官方工程 AI 研究分類，涵蓋訓練、推薦系統與基礎設施；這不是 ai.meta.com 的完整鏡像。
- [Microsoft Research](https://www.microsoft.com/en-us/research/feed/)：補充企業研究及實作內容。
- [Netflix TechBlog](https://netflixtechblog.com/feed) 與 [Spotify Engineering](https://engineering.atspotify.com/feed/)：補充推薦系統、個人化、資料平台及 AI 工程實務。
- [Uber Engineering](https://www.uber.com/us/en/blog/engineering/)：解析官方工程頁內嵌的文章資料，最多讀取當頁 20 篇。

另有 Hugging Face RSS、AWS Machine Learning Blog、Databricks、PyTorch、NVIDIA Blog、TLDR AI（會展開為原文連結）、MIT Technology Review AI、The Verge AI、TechCrunch AI、Hacker News Algolia 與指定 GitHub releases。

企業工程文章仍須符合 AI、資料科學、資料工程、推薦系統或實驗等主題。重疊來源依原文網址或相同標題去重。實際涵蓋範圍受來源提供的文章數限制。單一來源或論文週次出錯會記錄在 `collection_errors`，其餘來源繼續收集。

## 安裝與預覽

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
feedroom collect > /tmp/ai-news-candidates.json
feedroom preview --candidates /tmp/ai-news-candidates.json
feedroom preview --editorial --candidates /tmp/ai-news-candidates.json
python3 -m unittest discover -s tests -v
node --test tests/cloudflare.test.mjs
```

套件支援 Python 3.11 或以上。`feedroom preview` 預設係免憑證的來源預覽，可即場收集或讀取既有 JSON。加 `--editorial` 可預覽正式帖文，會用 `OPENROUTER_API_KEY` 同 `OPENROUTER_MODEL` 呼叫 OpenRouter。模型需支援結構化輸出。兩種預覽都毋須 Threads token。`collect` 或 `preview` 可用 `--as-of` 指定參考時間，歷史可見範圍仍取決於來源網站。完整 Python 測試需安裝 `python -m pip install '.[r2]'`。 [OpenRouter 結構化輸出](https://openrouter.ai/docs/guides/features/structured-outputs)

原有 `python3 scripts/ai_news_collect.py` 指令仍可使用。它的 `--mark-seen` 只管理舊有手動摘要的 14 日紀錄。

## Threads 發佈

在 Meta 建立 Threads App，用 tester 帳戶做首次驗證，授權 `threads_basic` 同 `threads_content_publish`，並將該帳戶的數字 ID 填入 `THREADS_ACCOUNT_ID`。將 Threads User Access Token 存入 `THREADS_ACCESS_TOKEN`。長效 token 有效期為 60 日，由操作者在到期前用 Meta 的 `refresh_access_token` 刷新並更新部署 secret。 [Meta Threads 開始使用](https://developers.facebook.com/documentation/threads/get-started) · [發佈權限](https://developers.facebook.com/documentation/threads/posts)

`feedroom publish` 會先核對 token 帳戶，再用 OpenRouter 草擬並驗證最多一帖。有效草稿建立 Threads container 時會加上 Meta 的 AI 標籤，並在 container 就緒後提交發佈。無合適項目會跳過，模型或驗證出錯會回報失敗。同一帳戶的歷史紀錄保留原文及合併來源的 ID，已發佈的論文會先排除，再補足閱讀候選。若失去發佈回應，重跑會先查原 container，再分頁核對貼文。狀態仍不明時會暫停新發佈。 [Meta 發佈](https://developers.facebook.com/documentation/threads/posts) · [Meta 發佈狀態](https://developers.facebook.com/documentation/threads/troubleshooting)

### 本機 Docker

在目標電腦 clone repo，安裝 Docker 同 Compose v2，並確認排程帳戶可以啟動容器及連接外部來源。工作需要主機在排程時間開機，Docker 服務亦要已啟動。先從 `.env.example` 建立本機 `.env`，以個人憑證填入 OpenRouter 同 Threads 設定。

```sh
docker compose build
docker compose run --rm feedroom preview
docker compose run --rm feedroom preview --editorial
```

確認編輯預覽後，可用 `docker compose run --rm feedroom publish` 對已授權的目標帳戶試帖。試帖查核完成後，由主機排程每日呼叫 Compose，並將輸出接駁到排程紀錄。UTC 主機如要保留 08:00 HKT，cron 可用以下設定。使用其他主機時區時要換算時間，並將路徑改為實際 checkout 路徑。

```cron
0 0 * * * cd "/absolute/path/to/feedroom" && "/absolute/path/to/docker" compose --env-file .env run --rm feedroom publish
```

Compose 用 `threads-state` volume 保存 `/data/threads.sqlite3`。備份此 volume 後才做主機搬遷。若已計劃搬上 Cloudflare，可在首次正式發佈前設定下列四個 R2 變數，讓本機同 Cloudflare 沿用同一份紀錄。

### Cloudflare

`cloudflare.config.ts` 使用 Cloudflare Containers 同 Cron Triggers，設定為每日 08:00 HKT。部署需 Cloudflare Workers Paid、Docker 同 Node.js 24.22 或同系列更新版本。執行 `npm ci` 安裝本專案指定的 Cloudflare CLI，再用 `npx cf build` 檢查建置。`npx cf deploy` 的排程預設跑 `preview`。 [Cloudflare CLI](https://developers.cloudflare.com/cf/projects/) · [Cloudflare 排程容器](https://developers.cloudflare.com/containers/examples/cron/)

正式發佈前，準備私人 R2 bucket 同只限該 bucket 的 Object Read & Write 憑證。在 Cloudflare 控制台的 Worker Settings → Variables and Secrets 設定 `OPENROUTER_API_KEY`、`OPENROUTER_MODEL`、`THREADS_ACCOUNT_ID`、`THREADS_ACCESS_TOKEN`、`FEEDROOM_R2_ACCOUNT_ID`、`FEEDROOM_R2_BUCKET`、`FEEDROOM_R2_ACCESS_KEY_ID`、`FEEDROOM_R2_SECRET_ACCESS_KEY`，再用 `npx cf deploy --mode publish` 部署。發佈歷史寫入 `threads/<Threads account ID>.json`，跨容器休眠保留。 [Worker secrets](https://developers.cloudflare.com/workers/configuration/secrets/) · [R2 憑證](https://developers.cloudflare.com/r2/api/tokens/) · [Cloudflare 部署模式](https://developers.cloudflare.com/cf/projects/)

切換正式排程前，先停用原有 Codex heartbeat 或其他舊觸發器。搬遷已發帖的帳戶時，先處理未確認的發佈，並確認新環境讀到同一份歷史。若本機已有 SQLite 發佈紀錄，先備份和遷移歷史，避免以空白 R2 紀錄重開同一帳戶的排程。
