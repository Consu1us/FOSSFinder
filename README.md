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

The reviewed list is `data/catalog.json`. When `MONGODB_URI` is set, startup loads that file into Atlas with one bulk write. If Atlas is unreachable, the error is logged, the API stays up, and `GET /api/health` reports the failure. Match and repo requests read the catalog already stored in Atlas. If that seed failed and Atlas has no reviewed repositories, those routes return 503. After Atlas is reachable again, restart the API so the catalog seed runs

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

`fossfinder.work` currently resolves to Porkbun parking addresses `192.0.79.146` and `192.0.79.180`. The app is ready to be served from a Vultr Ubuntu server at `https://fossfinder.work`.

1. Create an Ubuntu 24.04 Vultr VPS and copy its public IPv4 address.
2. In Porkbun, open `fossfinder.work`, then DNS. Delete the parking A records for the apex and for `www`. Add one A record for the apex and one A record for `www`, both pointing at the Vultr IPv4 address. Leave the nameservers on Porkbun.
3. In MongoDB Atlas, allow that same IPv4 address under Network Access.
4. On the server, clone the `vultr` branch, copy `.env` into that directory, and set `PUBLIC_URL=https://fossfinder.work`.
5. From the repository directory, install the proxy and the service:

```bash
sudo bash deploy/vultr.sh
```

Set `CERTBOT_EMAIL` when you run the script if the DNS change has already propagated. Certbot then requests a certificate for `fossfinder.work` and `www.fossfinder.work` and turns on HTTPS. Nginx proxies both names to the API on `127.0.0.1:8000`.

## API

- `POST /api/match` with a multipart field named `resume` (PDF). The resume and the extracted profile are kept for that response only. MongoDB stores the time and the recommended repository names, not the resume. `model_used` names the Gemini model that read the profile and, when it differs, the model that ranked repositories. `issues_note` is set when starter issues could not be loaded, including a rate limit with no `GITHUB_TOKEN`.
- `GET /api/repos` lists the reviewed repositories in Atlas.
- `GET /api/health` reports missing settings, whether Atlas answered a ping, whether the startup catalog seed succeeded, how many catalog rows that seed wrote, the Gemini model, fallback model, and per-request timeout, and whether `GITHUB_TOKEN` is set.
