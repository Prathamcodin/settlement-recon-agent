import type { NavigateFunction } from "react-router-dom";
import type { Report } from "./api";

/** Open a fresh report: by its saved run id when the audit store kept it, else from memory. */
export function openReport(navigate: NavigateFunction, report: Report) {
  if (report.run_id) navigate(`/runs/${report.run_id}`, { state: { report } });
  else navigate("/result", { state: { report } });
}
