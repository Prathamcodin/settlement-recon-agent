import { Dice5, FileCheck2, FileSpreadsheet, Play, Shuffle, Upload } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type Report } from "../api";
import { Callout, Spinner } from "../components";
import { openReport } from "../navigation";

type Busy = null | "sample" | "generate" | "upload";

function FileDrop({ label, hint, file, onFile }: {
  label: string;
  hint: string;
  file: File | null;
  onFile: (f: File) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const drop = (e: DragEvent) => {
    e.preventDefault();
    setOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) onFile(f);
  };
  return (
    <label
      className={`drop${over ? " over" : ""}${file ? " filled" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={drop}
    >
      <input
        ref={input}
        type="file"
        accept=".csv,text/csv"
        onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
      />
      {file ? <FileCheck2 size={20} style={{ color: "var(--green)" }} /> : <FileSpreadsheet size={20} className="faint" />}
      <div className="grow">
        <div className="drop-label">{label}</div>
        <div className="drop-name">{file ? file.name : hint}</div>
      </div>
    </label>
  );
}

export default function Run() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [seed, setSeed] = useState("7");
  const [orders, setOrders] = useState<File | null>(null);
  const [settlements, setSettlements] = useState<File | null>(null);

  const go = async (kind: Exclude<Busy, null>, call: () => Promise<Report>) => {
    setBusy(kind);
    setError(null);
    try {
      openReport(navigate, await call());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(null);
    }
  };

  const seedNum = Number(seed);
  const seedValid = seed.trim() !== "" && Number.isInteger(seedNum) && seedNum >= 0 && seedNum <= 2147483647;

  return (
    <div className="container fade-in">
      <div className="page-head">
        <div className="grow">
          <span className="eyebrow">New run</span>
          <h1 style={{ marginTop: 8 }}>Choose a batch to reconcile</h1>
          <p className="muted" style={{ marginTop: 6, maxWidth: 680 }}>
            Every option runs the real matcher and guard. Runs are saved to the audit store, so you can come back to
            them from History.
          </p>
        </div>
      </div>

      {error && (
        <div style={{ marginBottom: 16 }}>
          <Callout kind="error">{error}</Callout>
        </div>
      )}

      <div className="options">
        <div className="card option">
          <span className="option-icon">
            <Play size={20} />
          </span>
          <h3>Reference batch</h3>
          <p>
            The 63-record batch the AI recordings were captured on. Shows the full pipeline: 51 auto-matched, 12
            classified by the model, every claim checked by the guard.
          </p>
          <button className="btn btn-primary" disabled={busy !== null} onClick={() => go("sample", api.runSample)}>
            {busy === "sample" ? <Spinner /> : <Play size={16} />} Run reference batch
          </button>
        </div>

        <div className="card option">
          <span className="option-icon">
            <Dice5 size={20} />
          </span>
          <h3>Generate a batch</h3>
          <p>
            A fresh batch from the seeded generator: clean orders mixed with fee variances, refunds, splits, late
            settlements and orphans. Seed 42 reproduces the reference batch.
          </p>
          <div className="row gap-8">
            <input
              className="input mono"
              inputMode="numeric"
              value={seed}
              onChange={(e) => setSeed(e.target.value.replace(/[^\d]/g, ""))}
              aria-label="Seed"
              placeholder="Seed"
            />
            <button
              className="btn btn-ghost"
              type="button"
              aria-label="Random seed"
              title="Random seed"
              onClick={() => setSeed(String(Math.floor(Math.random() * 100000)))}
            >
              <Shuffle size={16} />
            </button>
          </div>
          <button
            className="btn btn-primary"
            disabled={busy !== null || !seedValid}
            onClick={() => go("generate", () => api.runGenerated(seedNum))}
          >
            {busy === "generate" ? <Spinner /> : <Dice5 size={16} />} Generate and run
          </button>
        </div>

        <div className="card option">
          <span className="option-icon">
            <Upload size={20} />
          </span>
          <h3>Upload CSVs</h3>
          <p>
            Your orders ledger and the aggregator's settlement file. Same columns as the samples:{" "}
            <a href={api.sampleUrl("orders")}>orders.csv</a> · <a href={api.sampleUrl("settlements")}>settlements.csv</a>
          </p>
          <FileDrop label="Orders ledger" hint="Drop or choose a CSV" file={orders} onFile={setOrders} />
          <FileDrop label="Settlement file" hint="Drop or choose a CSV" file={settlements} onFile={setSettlements} />
          <button
            className="btn btn-primary"
            disabled={busy !== null || !orders || !settlements}
            onClick={() => orders && settlements && go("upload", () => api.runUpload(orders, settlements))}
          >
            {busy === "upload" ? <Spinner /> : <Upload size={16} />} Upload and run
          </button>
        </div>
      </div>

      <div style={{ marginTop: 20 }}>
        <Callout kind="info">
          <b>Why some records say “Needs review”.</b> The public demo makes no live AI calls (no API key to leak, no
          bill). A recorded AI answer is reused only when it was captured on exactly the same numbers, so on new data
          the matcher and guard run for real and the leftovers are parked for a person instead of guessed.
        </Callout>
      </div>
    </div>
  );
}
