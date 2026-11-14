import { useEffect, useState } from "react";

const STEPS = ["Upload Resume", "View Skills", "Find Repositories"];

function chips(items) {
  return (items || []).filter(Boolean);
}

export default function App() {
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [settling, setSettling] = useState(false);
  const [status, setStatus] = useState("");
  const [result, setResult] = useState(null);

  function takeFile(next) {
    if (!next) return;
    const name = next.name || "";
    if (!name.toLowerCase().endsWith(".pdf")) {
      setStatus("Upload a PDF resume.");
      return;
    }
    setFile(next);
    setStatus("");
    setResult(null);
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (!file) {
      setStatus("Choose a PDF resume first.");
      return;
    }
    setBusy(true);
    setSettling(false);
    setStatus("");
    setResult(null);
    try {
      const body = new FormData();
      body.append("resume", file);
      const response = await fetch("/api/match", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) {
        setStatus(typeof payload.detail === "string" ? payload.detail : "Match failed.");
        return;
      }
      setSettling(true);
      await new Promise((resolve) => setTimeout(resolve, 450));
      setResult(payload);
      setStatus(
        payload.used_fallback
          ? "Ranked from skill overlap because Gemini ranking was unavailable."
          : "Here are some repositories that might be a good fit for you."
      );
    } catch {
      setStatus("The request did not finish. Check that the API is running.");
    } finally {
      setBusy(false);
      setSettling(false);
    }
  }

  return (
    <main>
      <header className="hero">
        <h1>
          <img className="logo" src="/favicon.svg" alt="" width="32" height="32" />
          FOSSFinder
        </h1>
        <p className="subtitle">Find FOSS Fast</p>
        <p className="support">Projects you can actually contribute to.</p>
      </header>
      <p className="lede">
        Upload your resume (PDF). We'll read the skills you've listed and the ones your
        experience shows, then match them to a set of open-source
        GitHub repositories.
      </p>
      <ol className="steps">
        {STEPS.map((label, index) => (
          <li key={label} className={stepClass(index, busy, result)}>
            <span>{index + 1}</span>
            {label}
          </li>
        ))}
      </ol>

      <form onSubmit={onSubmit}>
        <label
          className={dragOver ? "drop over" : "drop"}
          onDragOver={(event) => {
            event.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragOver(false);
            takeFile(event.dataTransfer.files?.[0]);
          }}
        >
          <span>PDF resume</span>
          <strong>{file ? file.name : "Drop a file here, or browse"}</strong>
          <input
            type="file"
            accept="application/pdf,.pdf"
            onChange={(event) => takeFile(event.target.files?.[0])}
          />
        </label>
        <p className="privacy">Resumes and personal information are never stored, and promptly deleted.</p>
        <button type="submit" disabled={busy}>
          {busy ? "Matching…" : "Get Started"}
        </button>
        {busy ? <MatchProgress complete={settling} /> : null}
        <p className="status" role="status">
          {status}
        </p>
      </form>

      {result ? <Results payload={result} /> : null}
    </main>
  );
}

const MATCH_STAGES = [
  { label: "Reading your resume", at: 0 },
  { label: "Extracting your skills", at: 4 },
  { label: "Searching the catalog", at: 16 },
  { label: "Ranking matches", at: 24 },
  { label: "Finding starter issues", at: 40 },
];

function easedProgress(seconds) {
  return 90 * (1 - Math.exp(-seconds / 28));
}

function stageIndexFor(seconds) {
  let index = 0;
  for (let i = 0; i < MATCH_STAGES.length; i += 1) {
    if (seconds >= MATCH_STAGES[i].at) index = i;
  }
  return index;
}

function MatchProgress({ complete }) {
  const [ms, setMs] = useState(0);

  useEffect(() => {
    if (complete) return undefined;
    const started = performance.now();
    const id = setInterval(() => setMs(performance.now() - started), 200);
    return () => clearInterval(id);
  }, [complete]);

  const seconds = ms / 1000;
  const elapsed = Math.floor(seconds);
  const stageIndex = complete ? MATCH_STAGES.length - 1 : stageIndexFor(seconds);
  const progress = complete ? 100 : easedProgress(seconds);
  const note =
    elapsed >= 45
      ? "Gemini may be retrying with a backup model. This can take up to a minute."
      : elapsed >= 20
        ? "Gemini is busy, still working, this can take up to a minute"
        : "";

  return (
    <div className="match-progress">
      <div
        className="progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(progress)}
        aria-valuetext={`${MATCH_STAGES[stageIndex].label}, ${elapsed} seconds`}
      >
        <span style={{ width: `${progress}%` }} />
      </div>
      <p className="elapsed">{elapsed}s</p>
      <p className="sr-only" aria-live="polite">
        {complete ? "Match finished." : MATCH_STAGES[stageIndex].label}
        {note ? ` ${note}` : ""}
      </p>
      <ol className="match-stages">
        {MATCH_STAGES.map((stage, index) => {
          const state = complete || index < stageIndex ? "done" : index === stageIndex ? "current" : "";
          return (
            <li key={stage.label} className={state}>
              <span aria-hidden="true" />
              {stage.label}
            </li>
          );
        })}
      </ol>
      {note ? <p className="match-note">{note}</p> : null}
    </div>
  );
}

function stepClass(index, busy, result) {
  if (!busy && !result) return index === 0 ? "current" : "";
  if (busy) return index < 2 ? "done" : "current";
  return "done";
}

function Results({ payload }) {
  const profile = payload.profile || {};
  const listed = chips(profile.skills_listed);
  const inferred = chips(profile.skills_inferred);
  const models = modelLine(payload.model_used);
  return (
    <>
      <section className="profile">
        <h2>Skills from the resume</h2>
        <p>{profile.summary}</p>
        <p className="meta">Seniority: {profile.seniority || "unspecified"}</p>
        <SkillGroup title="Explicitly listed skills:" items={listed} />
        <SkillGroup title="From the rest of the resume:" items={inferred} />
        <SkillGroup title="Languages and frameworks:" items={chips([...(profile.languages || []), ...(profile.frameworks || [])])} />
      </section>
      {payload.issues_note ? <p className="note">{payload.issues_note}</p> : null}
      {models ? <p className="model-used">{models}</p> : null}
      <section className="results">
        {(payload.recommendations || []).map((repo) => (
          <article key={repo.full_name}>
            <div className="score">
              <strong>{repo.score}</strong>
              <span>confidence</span>
            </div>
            <div>
              <h2>
                <a href={repo.url} target="_blank" rel="noreferrer">
                  {repo.full_name}
                </a>
              </h2>
              <p>{repo.description}</p>
              <p className="why">{repo.why}</p>
              <p className="meta">
                {(repo.languages || []).join(", ") || "Mixed"} · {repo.difficulty} ·{" "}
                {workflowLabel(repo.contribution_workflow)}
              </p>
              <IssueList issues={repo.issues} />
              {repo.show_contributing ? <ContributingGuide url={repo.contributing_url} /> : null}
            </div>
          </article>
        ))}
      </section>
    </>
  );
}

function SkillGroup({ title, items }) {
  if (!items.length) return null;
  return (
    <div className="group">
      <h3>{title}</h3>
      <div className="chips">
        {items.map((item) => (
          <span className="chip" key={item}>
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}

function modelLine(modelUsed) {
  if (!modelUsed) return "";
  const profile = modelUsed.profile || "the Gemini model";
  if (modelUsed.ranking_source === "local-keywords") {
    return `Profile read with ${profile}. Ranked from skill overlap because Gemini ranking was unavailable.`;
  }
  const ranking = modelUsed.ranking || profile;
  if (ranking !== profile) {
    return `Profile read with ${profile}. Ranked with ${ranking}.`;
  }
  return `Read and ranked with ${ranking}.`;
}

function workflowLabel(workflow) {
  if (workflow === "mailing-list") return "Mailing list, not GitHub pull requests";
  if (workflow === "external") return "Contributions happen outside GitHub";
  return "GitHub pull requests";
}

function ContributingGuide({ url }) {
  return (
    <div className="guide">
      <h3>Contribution guide</h3>
      {url ? (
        <a href={url} target="_blank" rel="noreferrer">
          Open the contribution guide
        </a>
      ) : (
        <p className="meta">No contribution guide was found for this repository.</p>
      )}
    </div>
  );
}

function IssueList({ issues }) {
  if (!issues?.length) {
    return (
      <p className="meta">
        No "good first issue" came back from GitHub. Try looking in the contribution guide instead.
      </p>
    );
  }
  return (
    <ul className="issues">
      {issues.map((issue) => (
        <li key={issue.url}>
          <a href={issue.url} target="_blank" rel="noreferrer">
            {issue.title}
          </a>
          {issue.labels?.length ? <span>{issue.labels.slice(0, 2).join(" · ")}</span> : null}
        </li>
      ))}
    </ul>
  );
}
