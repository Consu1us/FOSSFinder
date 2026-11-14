# FOSSFinder

Upload a PDF resume. Gemini reads the skills listed on it and the skills shown in the rest of the resume. The API matches those skills to a reviewed list of FOSS GitHub repositories stored in MongoDB Atlas, then asks GitHub for a few starter issues in each match.

## Setup

1. Create a [MongoDB Atlas](https://www.mongodb.com/atlas) cluster and a database user.
2. Create a [Gemini API key](https://aistudio.google.com/apikey).
3. Create a GitHub token with public repository read access. Issue lookup works without one, at a much lower rate limit.
4. Install Node.js 20 or newer for the React app.
5. Copy the environment file and fill it in:

```powershell
copy .env.example .env
```

`MONGODB_URI` is the Atlas connection string. The reviewed repositories live in the `repos` collection. The database name defaults to `fossfinder` (`MONGODB_DB`).

The reviewed list is `data/catalog.json`. Each match writes that list into Atlas before ranking.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m unittest tests.test_match_logic
uvicorn app.main:app --reload
```

In a second terminal:

```powershell
cd web
npm install
npm run dev
```

Open http://127.0.0.1:5173. The Vite dev server proxies `/api` to the Python API on port 8000.

`npm run build` writes `web/dist`. When that folder exists, the API serves the built React app at http://127.0.0.1:8000.

## API

- `POST /api/match` with a multipart field named `resume` (PDF)
- `GET /api/repos` lists the reviewed repositories in Atlas
- `GET /api/health` checks the API key, Atlas, and GitHub token
