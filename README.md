# FOSSFinder

Upload a PDF resume. Gemini reads the skills listed on it and the skills shown in the rest of the resume. The API matches those skills to a reviewed list of FOSS GitHub repositories stored in MongoDB Atlas, then asks GitHub for a few starter issues in each match.

## Setup

1. Create a [MongoDB Atlas](https://www.mongodb.com/atlas) cluster and a database user.
2. Create a [Gemini API key](https://aistudio.google.com/apikey).
3. Create a [GitHub personal access token](https://github.com/settings/tokens) with public repository read access. Starter-issue lookup works without a token, but unauthenticated clients only get about 60 REST requests per hour (the search API is about 10 requests per minute), and one match can make up to 15 GitHub calls. When that quota is used up and `GITHUB_TOKEN` is empty, the match response explains how to add a token.
4. Install Node.js 20 or newer for the React app.
5. Copy the environment file and fill in the empty values.

PowerShell:
```powershell
copy .env.example .env
```

bash:

```bash
cp .env.example .env
```

`MONGODB_URI` is the Atlas connection string. `MONGODB_DB` defaults to `fossfinder`. Reviewed repositories live in the `repos` collection. Starter-issue responses are cached in `starter_issue_cache` for about an hour, so a repeated match for the same repository and seniority does not call GitHub again. `GEMINI_API_KEY` is required. `GEMINI_MODEL` is optional and defaults to `gemini-3.5-flash`. `GEMINI_FALLBACK_MODEL` is optional. When the primary model is busy (HTTP 503 or UNAVAILABLE, 429 or RESOURCE_EXHAUSTED, 500, or a timeout), that call is retried once with a short backoff and then sent to the backup model. `GEMINI_TIMEOUT_SECONDS` is the per-request deadline (default 30 seconds). Gemini rejects anything under 10 seconds, so lower values are raised to 10. A profile call often takes 10-20 seconds. The whole Gemini call, retry included, still stops after about 60 seconds. If the resume still cannot be read, the response asks you to wait and try again. If ranking still fails, repositories are ranked from skill overlap. `GITHUB_TOKEN` is optional; set it before demoing several matches.

The reviewed list is `data/catalog.json`. When `MONGODB_URI` is set, startup loads that file into Atlas with one bulk write. If Atlas is unreachable, the error is logged, the API stays up, and `GET /api/health` reports the failure. Match and repo requests read the catalog already stored in Atlas. If that seed failed and Atlas has no reviewed repositories, those routes return 503. After Atlas is reachable again, restart the API so the catalog seed runs.

## Atlas Vector Search

The match shortlist is a MongoDB Atlas Vector Search query. At startup the API embeds each catalog repository with Gemini (`GEMINI_EMBEDDING_MODEL`, default `gemini-embedding-001`, 768 dimensions) from its description, languages, skills, domains, and contribution notes. The vector is stored on the `repos` document. A content hash includes the model and dimensions, so a restart embeds a repository again only when that text changes. Embedding calls are batched. A failed batch is logged and skipped.

The API creates a vector search index named `repo_embeddings` (`ATLAS_VECTOR_INDEX`) when it is missing:

```json
{
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 768,
      "similarity": "cosine"
    },
    { "type": "filter", "path": "reviewed" },
    { "type": "filter", "path": "source" }
  ]
}
```

The same definition can be created in the Atlas UI: database `fossfinder`, collection `repos`, index name `repo_embeddings`, type Vector Search. Atlas Vector Search runs on M0 free clusters as well as Flex and dedicated tiers. It does not run on a standalone Community `mongod`. The index becomes queryable after Atlas finishes building it, which can take a minute or two. Until `GET /api/health` reports `vector_search.index_ready`, and whenever embeddings or the index fail, the shortlist falls back to keyword overlap. The response field `shortlist_method` is `hybrid` when vector scores are blended with that keyword score, and `keyword` on fallback.

A match embeds the extracted skills and summary only in memory to query the index. That vector is not written to MongoDB. The resume, its text, and the profile are still not stored.

Changing `GEMINI_EMBEDDING_MODEL` re-embeds the catalog. If you also change the 768-dimension size in code, delete the old Atlas index so it can be recreated with the new `numDimensions`.

## Run

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest discover -s tests
uvicorn app.main:app --reload
```

bash:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests
uvicorn app.main:app --reload
```

In a second terminal, PowerShell:

```powershell
cd web
npm install
npm run dev
```

bash:

```bash
cd web
npm install
npm run dev
```

Open http://127.0.0.1:5173. The Vite dev server proxies `/api` to the Python API on port 8000.

`npm run build` writes `web/dist`. When that folder exists, the API serves the built React app at http://127.0.0.1:8000. Until then, `/` tells you to run the Vite dev server or `npm run build`.

## Vultr and Porkbun

https://fossfinder.work is the live site. Porkbun DNS for `fossfinder.work` and `www.fossfinder.work` points at the Vultr server `45.76.63.79`. The checkout is `/var/www/fossfinder` on `main`. Nginx proxies both names to the API on `127.0.0.1:8000`, and the systemd service is `fossfinder`.

Publish a `main` commit from that server as root:

```bash
cd /var/www/fossfinder
git -c safe.directory=/var/www/fossfinder pull origin main
cd web && npm ci && npm run build
systemctl restart fossfinder
```

`safe.directory` is required because root does not own the checkout. Pull before the build. `npm run build` writes `web/dist`, which is the page the API serves. A build that follows a failed pull republishes the previous page.

A new Ubuntu server uses the same layout. Clone `main` into the directory you want, copy `.env` there, set `PUBLIC_URL=https://fossfinder.work`, and allow that server's IPv4 under Atlas Network Access. In Porkbun, point the apex and `www` A records at the server and leave the nameservers on Porkbun. From the repository directory:

```bash
sudo bash deploy/vultr.sh
```

The script installs the virtualenv, builds the React app, writes the nginx site, and installs a systemd unit whose paths match that directory. Set `CERTBOT_EMAIL` when DNS already points at the server. Certbot then requests a certificate for `fossfinder.work` and `www.fossfinder.work`.

## API

- `POST /api/match` with a multipart field named `resume` (PDF). The resume and the extracted profile are kept for that response only. MongoDB stores the time and the recommended repository names, not the resume. `model_used` names the Gemini model that read the profile and, when it differs, the model that ranked repositories. `issues_note` is set when starter issues could not be loaded, including a rate limit with no `GITHUB_TOKEN`.
- `GET /api/repos` lists the reviewed repositories in Atlas.
- `GET /api/health` reports missing settings, whether Atlas answered a ping, whether the startup catalog seed succeeded, how many catalog rows that seed wrote, vector search index readiness and how many repositories have embeddings, the Gemini model, fallback model, and per-request timeout, and whether `GITHUB_TOKEN` is set.

## FOR?

for hackathon @consu1us @kwakux1
