import { AlertTriangle, Check, Info, Loader2, ShieldAlert, ShieldCheck, X } from "lucide-react";
import type { ReactNode } from "react";
import type { GuardCheck } from "./api";
import { categoryMeta, humanize } from "./format";

export function CategoryBadge({ category }: { category: string | null | undefined }) {
  const meta = categoryMeta(category);
  if (!category) return <span className="faint">—</span>;
  return (
    <span className={`badge tone-${meta.tone}`} title={meta.blurb}>
      <span className="dot" />
      {meta.label}
    </span>
  );
}

export function VerdictPill({ verdict }: { verdict: string | null | undefined }) {
  if (!verdict) return <span className="faint">—</span>;
  const ok = verdict === "ACCEPTED" || verdict === "ACCEPT";
  return (
    <span className={`pill ${ok ? "tone-green" : "tone-red"}`}>
      {ok ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />}
      {ok ? "Accepted" : "Rejected"}
    </span>
  );
}

export function Checks({ checks }: { checks: GuardCheck[] }) {
  if (!checks.length) return <p className="faint small">No checks recorded.</p>;
  return (
    <div className="checks">
      {checks.map((c, i) => (
        <div key={`${c.name}-${i}`} className={`check ${c.passed ? "" : "fail"}`}>
          <span className={`check-icon ${c.passed ? "ok" : "bad"}`}>
            {c.passed ? <Check size={13} strokeWidth={3} /> : <X size={13} strokeWidth={3} />}
          </span>
          <div className="grow">
            <div className="check-name">{c.name}</div>
            <div className="check-detail">{c.detail}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function Callout({ kind = "plain", children }: { kind?: "plain" | "error" | "info"; children: ReactNode }) {
  const Icon = kind === "error" ? AlertTriangle : Info;
  return (
    <div className={`callout ${kind === "plain" ? "" : kind}`} role={kind === "error" ? "alert" : undefined}>
      <Icon size={17} />
      <div>{children}</div>
    </div>
  );
}

export function Spinner({ size = 16 }: { size?: number }) {
  return <Loader2 size={size} className="spin" aria-label="Loading" />;
}

export function Kv({ items }: { items: [string, ReactNode][] }) {
  return (
    <div className="kv">
      {items.map(([k, v]) => (
        <div key={k}>
          <div className="kv-k">{k}</div>
          <div className="kv-v">{v}</div>
        </div>
      ))}
    </div>
  );
}

export function ClaimBox({ claim }: { claim: Record<string, unknown> | null | undefined }) {
  if (!claim || Object.keys(claim).length === 0) return <p className="faint small">No numeric claim.</p>;
  return (
    <Kv
      items={Object.entries(claim).map(([k, v]) => [
        humanize(k),
        Array.isArray(v) ? v.join(", ") : typeof v === "object" ? JSON.stringify(v) : String(v),
      ])}
    />
  );
}

export function PageLoading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="center-page">
      <div className="row gap-8 muted">
        <Spinner size={18} /> {label}
      </div>
    </div>
  );
}
