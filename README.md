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

`MONGODB_URI` is the Atlas connection string. `MONGODB_DB` defaults to `fossfinder`. Reviewed repositories live in the `repos` collection. Starter-issue responses are cached in `starter_issue_cache` for about an hour, so a repeated match for the same repository and seniority does not call GitHub again. `GEMINI_API_KEY` is required. `GEMINI_MODEL` is optional and defaults to `gemini-3.8-flash`. `GITHUB_TOKEN` is optional; set it before demoing several matches.

The reviewed list is `data/catalog.json`. When `MONGODB_URI` is set, startup loads that file into Atlas with one bulk write. If Atlas is unreachable, the error is logged, the API stays up, and `GET /api/health` reports the failure. Match and repo requests read the catalog already stored in Atlas. If that seed failed and Atlas has no reviewed repositories, those routes return 503. After Atlas is reachable again, restart the API so the catalog seed runs.

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

## API

- `POST /api/match` with a multipart field named `resume` (PDF). `issues_note` is set when starter issues could not be loaded, including a rate limit with no `GITHUB_TOKEN`.
- `GET /api/repos` lists the reviewed repositories in Atlas.
- `GET /api/health` reports missing settings, whether Atlas answered a ping, whether the startup catalog seed succeeded, how many catalog rows that seed wrote, and whether `GITHUB_TOKEN` is set.
