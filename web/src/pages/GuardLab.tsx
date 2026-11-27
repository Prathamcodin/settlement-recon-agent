import { Bot, CheckCircle2, Equal, Minus, ShieldAlert, ShieldCheck, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError, type GuardCase, type GuardCasesResponse } from "../api";
import { CategoryBadge, Callout, Checks, ClaimBox, PageLoading, VerdictPill } from "../components";
import { caseTitle, money } from "../format";

const SMART_LIE = "adversarial_selfconsistent_fee_lie";

function Ring({ value, total, tone }: { value: number; total: number; tone: "green" | "red" }) {
  const r = 26;
  const c = 2 * Math.PI * r;
  const frac = total ? value / total : 0;
  return (
    <svg className="score-ring" viewBox="0 0 64 64" aria-hidden="true">
      <circle cx="32" cy="32" r={r} fill="none" stroke="var(--surface-3)" strokeWidth="7" />
      <circle
        cx="32"
        cy="32"
        r={r}
        fill="none"
        stroke={tone === "green" ? "var(--bar-green)" : "var(--bar-red)"}
        strokeWidth="7"
        strokeLinecap="round"
        strokeDasharray={`${c * frac} ${c}`}
        transform="rotate(-90 32 32)"
      />
    </svg>
  );
}

/** Cents from a decimal string, for display arithmetic only. */
const cents = (s: string) => Math.round(Number(s) * 100);
const fromCents = (n: number) => (n / 100).toFixed(2);

function SmartLie({ c }: { c: GuardCase }) {
  const order = c.context.order;
  const payout = c.context.in_window_payouts[0];
  const claimedFee = String(c.llm_response.claim.actual_fee ?? "");
  if (!order || !payout || !claimedFee) return null;
  const aiNet = fromCents(cents(order.gross_amount) - cents(claimedFee));
  const gap = fromCents(Math.abs(cents(claimedFee) - cents(payout.fee_charged)));
  const failed = c.guard_checks.filter((ch) => !ch.passed);

  return (
    <div className="card feature">
      <div className="feature-side">
        <div className="row gap-8">
          <Bot size={18} className="muted" />
          <span className="card-title">What the AI claimed</span>
          <span className="spacer" />
          <CategoryBadge category={c.llm_response.category} />
        </div>
        <p className="quote">“{c.llm_response.reasoning}”</p>
        <div className="math">
          <div className="math-line">
            <span>{money(order.gross_amount)}</span>
            <Minus size={14} className="op" />
            <span className="hl tone-amber">{money(claimedFee)}</span>
            <Equal size={14} className="op" />
            <span className="hl tone-green">{money(aiNet)}</span>
          </div>
          <div className="small muted" style={{ fontFamily: "var(--font)" }}>
            gross − claimed fee = {money(payout.settled_amount)} settled. <b>Its own maths balances perfectly.</b>
          </div>
        </div>
      </div>
      <div className="feature-side">
        <div className="row gap-8">
          <ShieldAlert size={18} style={{ color: "var(--red)" }} />
          <span className="card-title">What the guard checked</span>
          <span className="spacer" />
          <VerdictPill verdict={c.guard_verdict} />
        </div>
        <div className="math">
          <div className="math-line">
            <span className="small" style={{ fontFamily: "var(--font)" }}>Settlement row's actual fee</span>
            <span className="hl tone-green">{money(payout.fee_charged)}</span>
          </div>
          <div className="math-line">
            <span className="small" style={{ fontFamily: "var(--font)" }}>AI's claimed fee</span>
            <span className="hl tone-red">{money(claimedFee)}</span>
            <span className="small muted" style={{ fontFamily: "var(--font)" }}>
              off by {money(gap)}
            </span>
          </div>
        </div>
        <p className="small muted">
          The guard checks claims against the settlement's own numbers, never against numbers the model produced.
        </p>
        <Checks checks={failed} />
      </div>
    </div>
  );
}

function CaseCard({ c }: { c: GuardCase }) {
  const lie = c.kind === "adversarial";
  return (
    <div className="card case">
      <div className="case-body">
        <div className="row gap-8 wrap">
          <span className={`badge ${lie ? "tone-red" : "tone-green"}`}>{lie ? "Planted lie" : "Honest claim"}</span>
          <span className="key small">{c.context.key}</span>
        </div>
        <h3 style={{ fontSize: 16 }}>{caseTitle(c.name)}</h3>
        <div className="row gap-8 wrap small">
          <span className="muted">AI said</span>
          <CategoryBadge category={c.llm_response.category} />
          <span className="muted">· auto-reconcile {c.llm_response.reconciles ? "yes" : "no"}</span>
        </div>
        <ClaimBox claim={c.llm_response.claim} />
        <details className="more">
          <summary>Why this case exists</summary>
          <p className="small muted">{c.description}</p>
        </details>
        <details className="more" open={lie}>
          <summary>Guard checks ({c.guard_checks.length})</summary>
          <Checks checks={c.guard_checks} />
        </details>
      </div>
      <div className="case-foot">
        <VerdictPill verdict={c.guard_verdict} />
        <span className="spacer" />
        {c.guard_correct ? (
          <span className="row gap-6 small" style={{ color: "var(--green)" }}>
            <CheckCircle2 size={15} /> Expected {c.expected_guard.toLowerCase()}
          </span>
        ) : (
          <span className="row gap-6 small" style={{ color: "var(--red)" }}>
            <XCircle size={15} /> Expected {c.expected_guard.toLowerCase()}
          </span>
        )}
      </div>
    </div>
  );
}

export default function GuardLab() {
  const [data, setData] = useState<GuardCasesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .guardCases()
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, []);

  if (error)
    return (
      <div className="container" style={{ paddingTop: 40 }}>
        <Callout kind="error">{error}</Callout>
      </div>
    );
  if (!data) return <PageLoading label="Running the guard on every case" />;

  const s = data.summary;
  const smart = data.cases.find((c) => c.name === SMART_LIE);
  const lies = data.cases.filter((c) => c.kind === "adversarial" && c.name !== SMART_LIE);
  const honest = data.cases.filter((c) => c.kind === "positive");

  return (
    <div className="container fade-in">
      <div className="page-head">
        <div className="grow">
          <span className="eyebrow">Guard Lab</span>
          <h1 style={{ marginTop: 8 }}>Planted lies, judged live</h1>
          <p className="muted" style={{ marginTop: 6, maxWidth: 720 }}>
            These are hand-written AI responses built to fool the guard, plus honest ones it must not block. Each one
            was just run through the real guard code on the server. None of them are real model output.
          </p>
        </div>
      </div>

      <div className="scoreboard">
        <div className="card score">
          <Ring value={s.adversarial_rejected} total={s.adversarial_total} tone="red" />
          <div>
            <div className="score-big num">
              {s.adversarial_rejected} / {s.adversarial_total}
            </div>
            <div className="muted small">fabricated claims rejected</div>
          </div>
        </div>
        <div className="card score">
          <Ring value={s.positive_accepted} total={s.positive_total} tone="green" />
          <div>
            <div className="score-big num">
              {s.positive_accepted} / {s.positive_total}
            </div>
            <div className="muted small">honest claims accepted</div>
          </div>
        </div>
      </div>

      {smart && (
        <section className="section" style={{ paddingBottom: 8 }}>
          <div className="stack gap-6" style={{ marginBottom: 16 }}>
            <span className="eyebrow">
              <ShieldCheck size={14} /> The smart lie
            </span>
            <h2 className="section-title">A claim whose maths is perfect, and still wrong</h2>
          </div>
          <SmartLie c={smart} />
        </section>
      )}

      <section className="section" style={{ paddingBottom: 8 }}>
        <h2 className="section-title" style={{ marginBottom: 16 }}>
          Other planted lies
        </h2>
        <div className="cases">
          {lies.map((c) => (
            <CaseCard key={c.name} c={c} />
          ))}
        </div>
      </section>

      <section className="section">
        <h2 className="section-title" style={{ marginBottom: 16 }}>
          Honest claims it must let through
        </h2>
        <div className="cases">
          {honest.map((c) => (
            <CaseCard key={c.name} c={c} />
          ))}
        </div>
      </section>
    </div>
  );
}
