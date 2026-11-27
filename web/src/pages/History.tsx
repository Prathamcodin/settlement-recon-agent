import { History as HistoryIcon, Play } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError, type RunSummary } from "../api";
import { Callout, PageLoading } from "../components";
import { dateTime, sourceLabel, timeAgo } from "../format";

export default function History() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<{ status: number; message: string } | null>(null);

  useEffect(() => {
    api
      .runs(50)
      .then((r) => setRuns(r.runs))
      .catch((e) => setError(e instanceof ApiError ? { status: e.status, message: e.message } : { status: 0, message: String(e) }));
  }, []);

  return (
    <div className="container fade-in">
      <div className="page-head">
        <div className="grow">
          <span className="eyebrow">Audit store</span>
          <h1 style={{ marginTop: 8 }}>Run history</h1>
          <p className="muted" style={{ marginTop: 6 }}>
            Every run is saved with its per-record decisions. Open one to see its full audit trail.
          </p>
        </div>
        <Link to="/run" className="btn btn-ghost">
          <Play size={15} /> New run
        </Link>
      </div>

      {error && error.status === 503 && (
        <Callout>The audit store is not configured on this server, so runs are not being saved.</Callout>
      )}
      {error && error.status !== 503 && <Callout kind="error">{error.message}</Callout>}
      {!error && !runs && <PageLoading label="Loading runs" />}

      {runs && (
        <div className="card">
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Batch</th>
                  <th style={{ textAlign: "right" }}>Records</th>
                  <th style={{ textAlign: "right" }}>Matched</th>
                  <th style={{ textAlign: "right" }}>Escalated</th>
                  <th style={{ textAlign: "right" }}>Guard rejected</th>
                  <th style={{ textAlign: "right" }}>Needs review</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr
                    key={r.run_id}
                    onClick={() => navigate(`/runs/${r.run_id}`)}
                    tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && navigate(`/runs/${r.run_id}`)}
                  >
                    <td>
                      <div style={{ fontWeight: 600 }}>{timeAgo(r.created_at)}</div>
                      <div className="tiny faint">{dateTime(r.created_at)}</div>
                    </td>
                    <td>{sourceLabel(r.source)}</td>
                    <td className="num" style={{ textAlign: "right" }}>{r.counts.total_records}</td>
                    <td className="num" style={{ textAlign: "right" }}>{r.counts.matcher_matched}</td>
                    <td className="num" style={{ textAlign: "right" }}>{r.counts.matcher_escalated}</td>
                    <td className="num" style={{ textAlign: "right", color: r.counts.guard_rejected ? "var(--red)" : undefined }}>
                      {r.counts.guard_rejected}
                    </td>
                    <td className="num" style={{ textAlign: "right" }}>{r.counts.needs_review ?? 0}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {runs.length === 0 && (
              <div className="empty stack gap-12" style={{ alignItems: "center" }}>
                <HistoryIcon size={22} />
                No runs yet.
                <Link to="/run" className="btn btn-primary btn-sm">
                  Start the first one
                </Link>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
