export interface CategoryMeta {
  label: string;
  tone: "green" | "amber" | "violet" | "sky" | "teal" | "red" | "slate";
  blurb: string;
}

export const CATEGORIES: Record<string, CategoryMeta> = {
  MATCHED: { label: "Matched", tone: "green", blurb: "Closed by the deterministic matcher. No AI involved." },
  FEE_VARIANCE: { label: "Fee variance", tone: "amber", blurb: "Aggregator charged a different fee than expected." },
  PARTIAL_REFUND: { label: "Partial refund", tone: "violet", blurb: "Part of the order was refunded before settlement." },
  TIMING_WINDOW: { label: "Timing window", tone: "sky", blurb: "Money is expected but has not settled inside the window yet." },
  SPLIT_PAYOUT: { label: "Split payout", tone: "teal", blurb: "One order settled across several payouts." },
  GENUINE_MISMATCH: { label: "Genuine mismatch", tone: "red", blurb: "Does not reconcile. Needs a person." },
  NEEDS_REVIEW: {
    label: "Needs review",
    tone: "slate",
    blurb: "No AI classification exists for this exact data, so nothing was guessed.",
  },
};

export const CATEGORY_ORDER = [
  "MATCHED",
  "FEE_VARIANCE",
  "PARTIAL_REFUND",
  "SPLIT_PAYOUT",
  "TIMING_WINDOW",
  "GENUINE_MISMATCH",
  "NEEDS_REVIEW",
];

export function categoryMeta(cat: string | null | undefined): CategoryMeta {
  if (!cat) return { label: "—", tone: "slate", blurb: "" };
  return CATEGORIES[cat] ?? { label: humanize(cat), tone: "slate", blurb: "" };
}

export function humanize(snake: string): string {
  const s = snake.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const inr = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** Display only: amounts arrive as exact decimal strings and are never used for arithmetic here. */
export function money(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? `₹${inr.format(n)}` : String(value);
}

export function pct(part: number, whole: number): string {
  if (!whole) return "0%";
  const p = (part / whole) * 100;
  return `${p >= 10 || p === 0 ? p.toFixed(0) : p.toFixed(1)}%`;
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function sourceLabel(source: string): string {
  if (source === "sample") return "Reference batch";
  if (source === "upload") return "Uploaded CSVs";
  const m = /^generated:seed=(\d+)$/.exec(source);
  if (m) return `Generated · seed ${m[1]}`;
  return humanize(source);
}

export function caseTitle(name: string): string {
  return humanize(name.replace(/^(adversarial|positive)_/, ""));
}
