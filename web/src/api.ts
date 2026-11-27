/** The dev server talks to a local uvicorn; a production build calls /api on the
 *  same origin (Vercel routes it to the Python function). VITE_API_URL overrides both. */
export const API_URL: string =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ??
  (import.meta.env.DEV ? "http://localhost:8000" : "/api");

export type Category =
  | "MATCHED"
  | "FEE_VARIANCE"
  | "PARTIAL_REFUND"
  | "TIMING_WINDOW"
  | "SPLIT_PAYOUT"
  | "GENUINE_MISMATCH"
  | "NEEDS_REVIEW"
  | string;

export interface GuardCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface OrderRow {
  order_id: string;
  gross_amount: string;
  expected_fee: string;
  net_expected: string;
  status: string;
  created_at: string;
}

export interface PayoutRow {
  payout_id: string;
  order_ref: string | null;
  settled_amount: string;
  fee_charged: string;
  settled_at: string;
  batch_id: string;
  in_window: boolean;
}

/** Slim row, as the decisions list returns it. */
export interface DecisionRow {
  key: string;
  matcher_status: string;
  matcher_strategy: string;
  llm_category: string | null;
  guard_verdict: "ACCEPTED" | "REJECTED" | null;
  guard_reason: string | null;
  final_category: Category;
}

/** Full audit trail for one record. */
export interface DecisionDetail extends DecisionRow {
  matcher_matched_payout_ids: string[];
  matcher_reason: string;
  llm_reconciles: boolean | null;
  llm_reasoning: string | null;
  llm_claim: Record<string, unknown> | null;
  guard_checks: GuardCheck[];
  order: OrderRow | null;
  payouts: PayoutRow[];
}

export interface Counts {
  total_records: number;
  matcher_matched: number;
  matcher_escalated: number;
  llm_says_reconciles: number;
  llm_says_exception: number;
  guard_accepted: number;
  guard_rejected: number;
  needs_review?: number;
  final_categories: Record<string, number>;
}

export interface Report {
  generated_at: string;
  counts: Counts;
  records: DecisionDetail[];
  run_id?: string;
  input_fingerprint?: string;
  previous_runs_with_same_input?: number;
  seed?: number;
  source?: string;
}

export interface RunSummary {
  run_id: string;
  source: string;
  created_at: string;
  input_fingerprint: string | null;
  counts: Counts;
  category_counts?: Record<string, number>;
}

export interface GuardCase {
  name: string;
  kind: "adversarial" | "positive";
  description: string;
  context: {
    key: string;
    matcher_strategy?: string;
    order: OrderRow | null;
    in_window_payouts: Omit<PayoutRow, "in_window">[];
    out_of_window_payouts: Omit<PayoutRow, "in_window">[];
  };
  llm_response: {
    category: string;
    reconciles: boolean;
    reasoning: string;
    claim: Record<string, unknown>;
  };
  expected_guard: "ACCEPT" | "REJECT";
  guard_verdict: "ACCEPT" | "REJECT";
  guard_correct: boolean;
  final_category: string;
  guard_reason: string;
  guard_checks: GuardCheck[];
}

export interface GuardCasesResponse {
  summary: {
    adversarial_total: number;
    adversarial_rejected: number;
    positive_total: number;
    positive_accepted: number;
  };
  cases: GuardCase[];
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, init);
  } catch {
    throw new ApiError(0, "Could not reach the API. It may be waking up; try again in a moment.");
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  info: () => request<{ audit_store: string; limits: { max_upload_bytes: number; max_rows: number } }>("/"),
  runSample: () => request<Report>("/reconcile/sample"),
  runGenerated: (seed: number) =>
    request<Report>(`/reconcile/generate?seed=${encodeURIComponent(seed)}`, { method: "POST" }),
  runUpload: (orders: File, settlements: File) => {
    const form = new FormData();
    form.append("orders", orders);
    form.append("settlements", settlements);
    return request<Report>("/reconcile", { method: "POST", body: form });
  },
  guardCases: () => request<GuardCasesResponse>("/guard/cases"),
  runs: (limit = 50) => request<{ runs: RunSummary[] }>(`/runs?limit=${limit}`),
  run: (runId: string) => request<RunSummary>(`/runs/${encodeURIComponent(runId)}`),
  decisions: async (runId: string): Promise<DecisionRow[]> => {
    const all: DecisionRow[] = [];
    const limit = 1000;
    for (let skip = 0; ; skip += limit) {
      const page = await request<{ decisions: DecisionRow[] }>(
        `/runs/${encodeURIComponent(runId)}/decisions?limit=${limit}&skip=${skip}`,
      );
      all.push(...page.decisions);
      if (page.decisions.length < limit) return all;
    }
  },
  decision: (runId: string, key: string) =>
    request<DecisionDetail>(`/runs/${encodeURIComponent(runId)}/decisions/${encodeURIComponent(key)}`),
  sampleUrl: (name: "orders" | "settlements") => `${API_URL}/sample/${name}.csv`,
};
