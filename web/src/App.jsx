import { useState } from "react";

const STEPS = ["Resume", "Skills", "Repositories"];

function chips(items) {
  return (items || []).filter(Boolean);
}

export default function App() {
  const [file, setFile] = useState(null);
  const [dragOver, setDragOver] = useState(false);
  const [busy, setBusy] = useState(false);
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
    setStatus("Reading the resume and matching reviewed repositories…");
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
      setResult(payload);
      setStatus(
        payload.used_fallback
          ? "Ranked from skill overlap because Gemini ranking was unavailable."
          : "Matched against the reviewed repository list."
      );
    } catch {
      setStatus("The request did not finish. Check that the API is running.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main>
      <p className="kicker">Find FOSS fast</p>
      <h1>Repositories you can actually contribute to.</h1>
      <p className="lede">
        Upload a PDF resume. Gemini reads the skills you listed and the ones your
        experience shows, then matches them to a reviewed set of open-source
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
        <button type="submit" disabled={busy}>
          {busy ? "Matching…" : "Find repositories"}
        </button>
        <p className="status" role="status">
          {status}
        </p>
      </form>

      {result ? <Results payload={result} /> : null}
    </main>
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
  return (
    <>
      <section className="profile">
        <h2>Skills from the resume</h2>
        <p>{profile.summary}</p>
        <p className="meta">Seniority: {profile.seniority || "unspecified"}</p>
        <SkillGroup title="Listed in the skills section" items={listed} />
        <SkillGroup title="Shown in the rest of the resume" items={inferred} />
        <SkillGroup title="Languages and frameworks" items={chips([...(profile.languages || []), ...(profile.frameworks || [])])} />
      </section>
      {payload.issues_note ? <p className="note">{payload.issues_note}</p> : null}
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
                {(repo.languages || []).join(", ") || "Mixed"} · {repo.difficulty}
              </p>
              <IssueList issues={repo.issues} />
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

function IssueList({ issues }) {
  if (!issues?.length) {
    return (
      <p className="meta">
        No open starter issue came back from GitHub. The contributing guide is the next place to look.
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
