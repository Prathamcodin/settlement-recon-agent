import {
  Bot, CheckCircle2, Clock, Fingerprint, GitMerge, Hash, Layers, Play, RotateCcw, Search, ShieldAlert,
  ShieldCheck, UserRoundSearch, X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link, useLocation, useParams, useSearchParams } from "react-router-dom";
import {
  api, ApiError, type Counts, type DecisionDetail, type DecisionRow, type Report, type RunSummary,
} from "../api";
import { CategoryBadge, Callout, Checks, ClaimBox, Kv, PageLoading, VerdictPill } from "../components";
import { CATEGORY_ORDER, categoryMeta, dateTime, humanize, money, pct, sourceLabel, timeAgo } from "../format";

interface Loaded {
  counts: Counts;
  categoryCounts: Record<string, number>;
  rows: DecisionRow[];
  details: Map<string, DecisionDetail> | null; // in-memory report has every detail
  runId?: string;
  source?: string;
  createdAt?: string;
  fingerprint?: string | null;
  previousRuns?: number;
}

function fromReport(report: Report): Loaded {
  return {
    counts: report.counts,
    categoryCounts: report.counts.final_categories,
    rows: report.records,
    details: new Map(report.records.map((r) => [r.key, r])),
    runId: report.run_id,
    source: report.source,
    createdAt: report.generated_at,
    fingerprint: report.input_fingerprint,
    previousRuns: report.previous_runs_with_same_input,
  };
}

async function fromStore(runId: string): Promise<Loaded> {
  const [run, rows]: [RunSummary, DecisionRow[]] = await Promise.all([api.run(runId), api.decisions(runId)]);
  return {
    counts: run.counts,
    categoryCounts: run.category_counts ?? run.counts.final_categories,
    rows,
    details: null,
    runId: run.run_id,
    source: run.source,
    createdAt: run.created_at,
    fingerprint: run.input_fingerprint,
  };
}

export default function Results() {
  const { runId } = useParams();
  const location = useLocation();
  const stateReport = (location.state as { report?: Report } | null)?.report;
  const [data, setData] = useState<Loaded | null>(() =>
    stateReport && (!runId || stateReport.run_id === runId) ? fromReport(stateReport) : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("ALL");
  const [query, setQuery] = useState("");
  // The open record lives in the URL (?record=ORD-0006), so a link can point at one audit trail.
  const [searchParams, setSearchParams] = useSearchParams();
  const selected = searchParams.get("record");
  const setSelected = (key: string | null) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (key) next.set("record", key);
        else next.delete("record");
        return next;
      },
      { replace: true, preventScrollReset: true, state: location.state },
    );

  useEffect(() => {
    if (stateReport && (!runId || stateReport.run_id === runId)) {
      setData(fromReport(stateReport));
      return;
    }
    if (!runId) return;
    setData(null);
    setError(null);
    fromStore(runId)
      .then(setData)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [runId, stateReport]);

  const rows = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return data.rows.filter((r) => {
      if (filter === "EXCEPTIONS" && r.final_category === "MATCHED") return false;
      if (filter !== "ALL" && filter !== "EXCEPTIONS" && r.final_category !== filter) return false;
      if (!q) return true;
      return (
        r.key.toLowerCase().includes(q) ||
        r.matcher_strategy.toLowerCase().includes(q) ||
        (r.llm_category ?? "").toLowerCase().includes(q)
      );
    });
  }, [data, filter, query]);

  if (!runId && !stateReport) {
    return (
      <div className="container center-page">
        <div className="stack gap-12" style={{ alignItems: "center" }}>
          <p className="muted">This run was not saved, so it cannot be reopened.</p>
          <Link to="/run" className="btn btn-primary">
            Start a new run
          </Link>
        </div>
      </div>
    );
  }
  if (error) {
    return (
      <div className="container" style={{ paddingTop: 40 }}>
        <Callout kind="error">{error}</Callout>
      </div>
    );
  }
  if (!data) return <PageLoading label="Loading run" />;

  const c = data.counts;
  const needsReview = c.needs_review ?? data.categoryCounts.NEEDS_REVIEW ?? 0;
  const exceptions = c.total_records - (data.categoryCounts.MATCHED ?? 0);

  return (
    <div className="container fade-in">
      <div className="page-head">
        <div className="grow">
          <span className="eyebrow">Run results</span>
          <h1 style={{ marginTop: 8 }}>{data.source ? sourceLabel(data.source) : "Reconciliation run"}</h1>
          <div className="meta">
            {data.createdAt && (
              <span title={data.createdAt}>
                <Clock size={14} /> {dateTime(data.createdAt)} · {timeAgo(data.createdAt)}
              </span>
            )}
            {data.runId && (
              <span title={data.runId}>
                <Hash size={14} /> <span className="mono">{data.runId.slice(0, 10)}</span>
              </span>
            )}
            {data.fingerprint && (
              <span title={`Input fingerprint ${data.fingerprint}`}>
                <Fingerprint size={14} /> <span className="mono">{data.fingerprint.slice(0, 10)}</span>
              </span>
            )}
            {data.previousRuns !== undefined && data.previousRuns > 0 && (
              <span>
                <RotateCcw size={14} /> Same input reconciled {data.previousRuns} time{data.previousRuns > 1 ? "s" : ""} before
              </span>
            )}
          </div>
        </div>
        <Link to="/run" className="btn btn-ghost">
          <Play size={15} /> New run
        </Link>
      </div>

      <div className="kpis">
        <Kpi icon={Layers} label="Records" value={c.total_records} sub="reconciliation units" />
        <Kpi
          icon={GitMerge}
          label="Auto-matched"
          value={c.matcher_matched}
          sub={`${pct(c.matcher_matched, c.total_records)} by exact maths`}
        />
        <Kpi icon={Bot} label="Escalated" value={c.matcher_escalated} sub="sent past the matcher" />
        <Kpi
          icon={c.guard_rejected ? ShieldAlert : ShieldCheck}
          label="Guard verdicts"
          value={`${c.guard_accepted} / ${c.guard_rejected}`}
          sub="accepted / rejected"
        />
        <Kpi icon={UserRoundSearch} label="Needs review" value={needsReview} sub="parked for a person" />
      </div>

      <div className="flow">
        <div className="card card-pad">
          <div className="row" style={{ marginBottom: 14 }}>
            <div className="card-title">Final categories</div>
            <span className="spacer" />
            <span className="small muted">
              {exceptions} exception{exceptions === 1 ? "" : "s"} of {c.total_records}
            </span>
          </div>
          <CategoryBar counts={data.categoryCounts} total={c.total_records} active={filter} onPick={setFilter} />
        </div>
        <div className="card card-pad">
          <div className="card-title" style={{ marginBottom: 16 }}>
            Where each record was decided
          </div>
          <div className="funnel">
            <FunnelRow label="All records" n={c.total_records} total={c.total_records} />
            <FunnelRow label="Matcher closed" n={c.matcher_matched} total={c.total_records} color="var(--bar-green)" />
            <FunnelRow label="AI classified" n={c.matcher_escalated - needsReview} total={c.total_records} color="var(--bar-violet)" />
            <FunnelRow label="Guard accepted" n={c.guard_accepted} total={c.total_records} color="var(--bar-teal)" />
            <FunnelRow label="Guard rejected" n={c.guard_rejected} total={c.total_records} color="var(--bar-red)" />
            <FunnelRow label="Parked for review" n={needsReview} total={c.total_records} color="var(--bar-slate)" />
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="toolbar">
          <div className="chips">
            <Chip active={filter === "ALL"} onClick={() => setFilter("ALL")} label="All" n={c.total_records} />
            <Chip active={filter === "EXCEPTIONS"} onClick={() => setFilter("EXCEPTIONS")} label="Exceptions" n={exceptions} />
            {CATEGORY_ORDER.filter((k) => k !== "MATCHED" && data.categoryCounts[k]).map((k) => (
              <Chip
                key={k}
                active={filter === k}
                onClick={() => setFilter(k)}
                label={categoryMeta(k).label}
                n={data.categoryCounts[k]}
              />
            ))}
          </div>
          <span className="spacer" />
          <div className="search">
            <Search size={15} />
            <input
              className="input"
              placeholder="Search record or category"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search records"
            />
          </div>
        </div>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Record</th>
                <th>Matcher</th>
                <th>AI said</th>
                <th>Guard</th>
                <th>Final category</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.key}
                  className={selected === r.key ? "selected" : ""}
                  onClick={() => setSelected(r.key)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && setSelected(r.key)}
                >
                  <td className="key">{r.key}</td>
                  <td>
                    <span className="row gap-6">
                      {r.matcher_status === "MATCHED" ? (
                        <CheckCircle2 size={15} style={{ color: "var(--green)" }} />
                      ) : (
                        <Bot size={15} className="faint" />
                      )}
                      <span className="small">{humanize(r.matcher_strategy)}</span>
                    </span>
                  </td>
                  <td>{r.llm_category ? <CategoryBadge category={r.llm_category} /> : <span className="faint">—</span>}</td>
                  <td>
                    <VerdictPill verdict={r.guard_verdict} />
                  </td>
                  <td>
                    <CategoryBadge category={r.final_category} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <div className="empty">No records match this filter.</div>}
        </div>
      </div>

      {selected && (
        <DecisionDrawer
          runKey={selected}
          runId={data.runId}
          preloaded={data.details?.get(selected) ?? null}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function Kpi({ icon: Icon, label, value, sub }: { icon: typeof Layers; label: string; value: number | string; sub: string }) {
  return (
    <div className="card kpi">
      <div className="kpi-label">
        <Icon size={15} /> {label}
      </div>
      <div className="kpi-value num">{value}</div>
      <div className="kpi-sub">{sub}</div>
    </div>
  );
}

function Chip({ active, onClick, label, n }: { active: boolean; onClick: () => void; label: string; n: number }) {
  return (
    <button className={`chip${active ? " active" : ""}`} onClick={onClick} aria-pressed={active}>
      {label} <span className="n">{n}</span>
    </button>
  );
}

function FunnelRow({ label, n, total, color }: { label: string; n: number; total: number; color?: string }) {
  return (
    <div className="funnel-row">
      <span className="muted">{label}</span>
      <div className="funnel-track">
        <div className="funnel-fill" style={{ width: `${total ? (n / total) * 100 : 0}%`, background: color }} />
      </div>
      <span className="funnel-n">{n}</span>
    </div>
  );
}

function CategoryBar({ counts, total, active, onPick }: {
  counts: Record<string, number>;
  total: number;
  active: string;
  onPick: (k: string) => void;
}) {
  const keys = [
    ...CATEGORY_ORDER.filter((k) => counts[k]),
    ...Object.keys(counts).filter((k) => !CATEGORY_ORDER.includes(k)),
  ];
  return (
    <>
      <div className="stackbar" role="img" aria-label="Share of records per final category">
        {keys.map((k) => (
          <div
            key={k}
            className={`bar-${categoryMeta(k).tone}`}
            style={{ width: `${(counts[k] / total) * 100}%`, opacity: active === "ALL" || active === k || (active === "EXCEPTIONS" && k !== "MATCHED") ? 1 : 0.3 }}
            title={`${categoryMeta(k).label}: ${counts[k]}`}
          />
        ))}
      </div>
      <div className="legend">
        {keys.map((k) => (
          <button
            key={k}
            className={`legend-item${active === k ? " active" : ""}`}
            onClick={() => onPick(active === k ? "ALL" : k)}
            title={categoryMeta(k).blurb}
          >
            <span className={`legend-swatch bar-${categoryMeta(k).tone}`} />
            {categoryMeta(k).label}
            <span className="legend-count">
              {counts[k]} <span className="faint small">· {pct(counts[k], total)}</span>
            </span>
          </button>
        ))}
      </div>
    </>
  );
}

function DecisionDrawer({ runKey, runId, preloaded, onClose }: {
  runKey: string;
  runId?: string;
  preloaded: DecisionDetail | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<DecisionDetail | null>(preloaded);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
    if (preloaded) {
      setDetail(preloaded);
      return;
    }
    if (!runId) return;
    setDetail(null);
    api
      .decision(runId, runKey)
      .then(setDetail)
      .catch((e) => setError(e instanceof ApiError ? e.message : String(e)));
  }, [runKey, runId, preloaded]);

  const onKey = useCallback((e: KeyboardEvent) => e.key === "Escape" && onClose(), [onClose]);
  useEffect(() => {
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onKey]);

  return createPortal(
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`Audit trail for ${runKey}`}>
        <div className="drawer-head">
          <span className="key" style={{ fontSize: 16 }}>
            {runKey}
          </span>
          {detail && <CategoryBadge category={detail.final_category} />}
          <span className="spacer" />
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={17} />
          </button>
        </div>
        <div className="drawer-body">
          {error && <Callout kind="error">{error}</Callout>}
          {!detail && !error && (
            <div className="stack gap-12">
              <div className="skeleton" style={{ height: 70 }} />
              <div className="skeleton" style={{ height: 120 }} />
              <div className="skeleton" style={{ height: 160 }} />
            </div>
          )}
          {detail && <AuditTrail d={detail} />}
        </div>
      </aside>
    </>,
    document.body,
  );
}

function AuditTrail({ d }: { d: DecisionDetail }) {
  const matched = d.matcher_status === "MATCHED";
  const parked = d.final_category === "NEEDS_REVIEW";
  return (
    <>
      <p className="muted small">{categoryMeta(d.final_category).blurb}</p>

      <section className="stack gap-8">
        <div className="card-title">Raw data</div>
        {d.order ? (
          <Kv
            items={[
              ["Gross", money(d.order.gross_amount)],
              ["Expected fee", money(d.order.expected_fee)],
              ["Net expected", money(d.order.net_expected)],
              ["Ledger status", humanize(d.order.status)],
              ["Created", dateTime(d.order.created_at)],
            ]}
          />
        ) : (
          <p className="small faint">No ledger order for this key: the settlement row is an orphan.</p>
        )}
        {d.payouts.length > 0 ? (
          <div className="card" style={{ boxShadow: "none", overflow: "hidden" }}>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Payout</th>
                    <th>Settled</th>
                    <th>Fee</th>
                    <th>Settled at</th>
                    <th>Window</th>
                  </tr>
                </thead>
                <tbody>
                  {d.payouts.map((p) => (
                    <tr key={p.payout_id} style={{ cursor: "default" }}>
                      <td className="key">{p.payout_id}</td>
                      <td className="num">{money(p.settled_amount)}</td>
                      <td className="num">{money(p.fee_charged)}</td>
                      <td className="small">{dateTime(p.settled_at)}</td>
                      <td>
                        <span className={`pill ${p.in_window ? "tone-green" : "tone-amber"}`}>
                          {p.in_window ? "In" : "Late"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <p className="small faint">No settlement row found for this order.</p>
        )}
      </section>

      <Step n={1} title="Deterministic matcher" badge={
        <span className={`pill ${matched ? "tone-green" : "tone-amber"}`}>{matched ? "Matched" : "Escalated"}</span>
      }>
        <span className="small">
          Strategy: <b>{humanize(d.matcher_strategy)}</b>
        </span>
        {d.matcher_reason && <p>{d.matcher_reason}</p>}
      </Step>

      <Step n={2} title="LLM classifier" badge={d.llm_category ? <CategoryBadge category={d.llm_category} /> : undefined}>
        {matched && <p>Skipped. The matcher proved this record with exact maths, so the model never saw it.</p>}
        {parked && (
          <p>
            Not classified. No recorded AI answer exists for these exact numbers and the public demo makes no live AI
            calls, so nothing was guessed.
          </p>
        )}
        {!matched && !parked && (
          <>
            <span className="small">
              Asked to auto-reconcile: <b>{d.llm_reconciles ? "Yes" : "No"}</b>
            </span>
            {d.llm_reasoning && <p className="quote">“{d.llm_reasoning}”</p>}
            <ClaimBox claim={d.llm_claim} />
          </>
        )}
      </Step>

      <Step n={3} title="Validation guard" badge={<VerdictPill verdict={d.guard_verdict} />}>
        {matched && <p>Nothing to verify: no AI claim was made.</p>}
        {!matched && d.guard_reason && <p>{d.guard_reason}</p>}
        {!matched && !parked && <Checks checks={d.guard_checks} />}
      </Step>

      <Step n={4} title="Final category" badge={<CategoryBadge category={d.final_category} />}>
        <p>{categoryMeta(d.final_category).blurb}</p>
      </Step>
    </>
  );
}

function Step({ n, title, badge, children }: { n: number; title: string; badge?: ReactNode; children: ReactNode }) {
  return (
    <section className="step">
      <span className="step-num">{n}</span>
      <div>
        <div className="step-title">
          {title} {badge}
        </div>
        <div className="step-body">{children}</div>
      </div>
    </section>
  );
}
