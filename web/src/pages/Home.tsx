import { ArrowRight, Bot, Database, GitMerge, Play, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { Callout, Spinner } from "../components";
import { openReport } from "../navigation";

const STAGES = [
  {
    icon: GitMerge,
    title: "Deterministic matcher",
    text: "Matches every order to its settlement rows with exact decimal maths and a rounding tolerance. Closes what it can prove.",
    foot: "100,800 records in 1.9 s",
  },
  {
    icon: Bot,
    title: "LLM classifier",
    text: "Only the leftovers reach the model, which proposes a category and a numeric claim. It never decides on its own.",
    foot: "Sees 12 of 63 records",
  },
  {
    icon: ShieldCheck,
    title: "Validation guard",
    text: "Re-checks every claim against the raw ledger. A claim that balances its own maths but contradicts the settlement is rejected.",
    foot: "6 / 6 planted lies rejected",
  },
  {
    icon: Database,
    title: "Audit store",
    text: "Every run and every per-record decision is saved, so a reviewer can answer “why was ORD-0006 cleared?” later.",
    foot: "PostgreSQL or MongoDB",
  },
];

const STATS = [
  ["0", "false auto-matches on 100,800 records"],
  ["6 / 6", "fabricated AI claims rejected"],
  ["50,000", "generated claims in the guard stress test"],
  ["127", "automated tests on every push"],
];

export default function Home() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSample = async () => {
    setBusy(true);
    setError(null);
    try {
      openReport(navigate, await api.runSample());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <div className="container">
      <section className="hero fade-in">
        <span className="eyebrow">Settlement reconciliation</span>
        <h1 style={{ marginTop: 14 }}>
          Reconcile payouts with AI, <span className="grad">without letting it move money on a guess.</span>
        </h1>
        <p className="hero-sub">
          Deterministic code closes what it can. The model only looks at the leftovers, and no claim it makes is
          accepted until a validation guard re-checks it against the raw ledger.
        </p>
        <div className="hero-cta">
          <button className="btn btn-primary btn-lg" onClick={runSample} disabled={busy}>
            {busy ? <Spinner /> : <Play size={17} />} Run the reference batch
          </button>
          <Link to="/guard" className="btn btn-ghost btn-lg">
            See the guard catch a lie <ArrowRight size={17} />
          </Link>
        </div>
        {error && (
          <div style={{ marginTop: 18, maxWidth: 640 }}>
            <Callout kind="error">{error}</Callout>
          </div>
        )}

        <div className="stats">
          {STATS.map(([value, label]) => (
            <div className="card stat" key={label}>
              <div className="stat-value num">{value}</div>
              <div className="stat-label">{label}</div>
            </div>
          ))}
        </div>
      </section>

      <section className="section">
        <div className="stack gap-8" style={{ marginBottom: 22 }}>
          <span className="eyebrow">How it works</span>
          <h2 className="section-title">Four layers, one rule: nothing the model says is trusted unchecked</h2>
        </div>
        <div className="pipeline">
          {STAGES.map((s, i) => (
            <div className="stage" key={s.title}>
              <div className="card stage-card">
                <div className="row gap-8">
                  <span className="stage-num">{i + 1}</span>
                  <s.icon size={18} className="muted" />
                </div>
                <div className="stage-title">{s.title}</div>
                <p className="stage-text">{s.text}</p>
                <div className="stage-foot">{s.foot}</div>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="section">
        <div className="card card-pad row wrap gap-16">
          <div className="grow stack gap-6" style={{ minWidth: 260 }}>
            <h3 style={{ fontSize: 18 }}>Try it on your own data</h3>
            <p className="muted">
              Generate a fresh batch from any seed or upload two CSVs. The matcher and guard run live. The public demo
              makes no live AI calls, so records the model never saw are parked as <b>Needs review</b> instead of
              being guessed.
            </p>
          </div>
          <Link to="/run" className="btn btn-primary">
            Start a run <ArrowRight size={16} />
          </Link>
        </div>
      </section>
    </div>
  );
}
