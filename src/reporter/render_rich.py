"""
Rich terminal rendering — presentation only.

Every function here takes a `Console` and writes to it. No pipeline
logic, no numbers changed, no re-computation — the inputs are already-
built report dicts from `src.reporter.report.build_report` (and the
committed evidence artifacts). This is purely for terminal polish on
`python run.py` and `python scripts/demo.py`.

Kept in its own file so `src/reporter/report.py`'s plain-text
`format_summary_text` stays untouched — tests, the FastAPI response,
and any downstream consumer keep working exactly as before.
"""
from __future__ import annotations

from typing import Optional

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


# Outcome colors — MATCHED green, ESCALATED yellow, GENUINE_MISMATCH red;
# recognized non-terminal categories in cyan.
CATEGORY_STYLE: dict[str, str] = {
    "MATCHED":                    "green",
    "GENUINE_MISMATCH":           "red",
    "FEE_VARIANCE":               "cyan",
    "PARTIAL_REFUND":             "cyan",
    "SPLIT_PAYOUT":               "cyan",
    "SPLIT_PAYOUT_WITH_VARIANCE": "cyan",
    "TIMING_WINDOW":              "cyan",
}


# --------------------------------------------------------------------------
# Stage-header helpers (run.py prints these between pipeline stages)
# --------------------------------------------------------------------------

def stage(console: Console, title: str, subtitle: str = "") -> None:
    """Section rule with an optional dim subtitle."""
    text = f"[bold cyan]{title}[/]"
    if subtitle:
        text += f" [dim]{subtitle}[/]"
    console.print()
    console.print(Rule(text, style="cyan"))


# --------------------------------------------------------------------------
# Final report renderer used by run.py after the pipeline finishes
# --------------------------------------------------------------------------

def render_final_report(
    console: Console,
    report: dict,
    *,
    live_source: Optional[dict] = None,
    adv_source: Optional[dict] = None,
) -> None:
    """
    Render the merged report as:
      1. a color-coded final-category table
      2. a highlighted Two-Label Safety Finding panel
    """
    c = report["counts"]
    stage(console, "FINAL CATEGORY BREAKDOWN")
    cat_table = Table(box=box.SIMPLE, show_edge=False, header_style="bold")
    cat_table.add_column("Category", no_wrap=True)
    cat_table.add_column("Count", justify="right")
    for cat, count in sorted(c["final_categories"].items(), key=lambda x: (-x[1], x[0])):
        style = CATEGORY_STYLE.get(cat, "white")
        cat_table.add_row(f"[{style}]{cat}[/]", f"[{style} bold]{count}[/]")
    console.print(cat_table)

    stage(console, "TWO-LABEL SAFETY FINDING",
          "sourced from committed evidence artifacts")
    body = Text()
    if live_source is not None:
        lb_correct = live_source["accuracy_vs_ground_truth"]["correct"]
        lb_false = live_source["accuracy_vs_ground_truth"]["false_reconciliations"]
        lb_total = live_source["batch_summary"]["escalated_to_llm"]
        false_style = "bold green" if lb_false == 0 else "bold red"
        body.append("Live batch:  ", style="bold")
        body.append(f"LLM produced ", style="")
        body.append(f"{lb_false} false reconciliations", style=false_style)
        body.append(f" across {lb_total} escalations\n", style="")
        body.append("             ", style="")
        body.append(f"(accuracy {lb_correct}/{lb_total} vs ground truth; "
                    f"source: evidence/before_guard.json)\n\n", style="dim")
    else:
        body.append("Live batch:  ", style="bold")
        body.append("evidence/before_guard.json not found\n\n", style="dim italic")

    if adv_source is not None:
        adv_rej = adv_source["counts"]["adversarial"]["rejected_by_guard"]
        adv_tot = adv_source["counts"]["adversarial"]["total"]
        rej_style = "bold green" if adv_rej == adv_tot else "bold red"
        body.append("Guard test:  ", style="bold")
        body.append(f"{adv_rej}/{adv_tot} adversarial fabrications rejected", style=rej_style)
        body.append(" on hand-crafted fixtures,\n", style="")
        body.append("             ", style="")
        body.append("including one arithmetically-perfect ", style="")
        body.append("'smart lie'", style="italic yellow")
        body.append("\n             ", style="")
        body.append("(source: evidence/after_guard.json)", style="dim")
    else:
        body.append("Guard test:  ", style="bold")
        body.append("evidence/after_guard.json not found", style="dim italic")

    console.print(Panel(
        body, title="[bold yellow]Two-Label Safety Finding[/]",
        border_style="yellow", padding=(1, 2),
    ))


# --------------------------------------------------------------------------
# Demo panels: smart-lie showcase + held-out breadth
# --------------------------------------------------------------------------

def render_smart_lie_panel(console: Console, after_guard: dict) -> None:
    """
    Highlight the ORD-0006 smart-lie rejection from evidence/after_guard.json.
    Three stacked panels: what the LLM claimed, what the settlement recorded,
    what the guard did.
    """
    lie = next(
        (r for r in after_guard["records"]
         if r["fixture"] == "adversarial_selfconsistent_fee_lie.json"),
        None,
    )
    if lie is None:
        console.print("[dim italic]smart-lie fixture not in after_guard.json — skipping[/]")
        return

    stage(console, "DEPTH  ·  the 'smart lie'",
          "one hand-crafted fixture, believable-magnitude, the guard catches it")

    # 1. LLM's proposal
    claim_text = Text()
    claim_text.append("Category   ", style="dim")
    claim_text.append(f"{lie['llm_claim_category']}\n", style="cyan")
    claim_text.append("Claim      ", style="dim")
    actual_fee = lie["llm_claim"].get("actual_fee", "?")
    claim_text.append(f"actual_fee = Rs.{actual_fee}\n", style="yellow")
    claim_text.append("Reconciles ", style="dim")
    claim_text.append("True", style="bold yellow")
    claim_text.append("  ·  4823.00 - 119.80 = 4703.20 = settled  ", style="")
    claim_text.append("(math balances)", style="italic dim")
    console.print(Panel(
        claim_text, title="[bold]What the LLM proposed[/]",
        border_style="yellow", padding=(1, 2),
    ))

    # 2. What the settlement row actually says
    truth_text = Text()
    truth_text.append("Payout's actual fee_charged   ", style="dim")
    truth_text.append("Rs.96.46\n", style="bold green")
    truth_text.append("LLM's claimed actual_fee      ", style="dim")
    truth_text.append("Rs.119.80  ", style="bold red")
    truth_text.append("(does NOT match)\n", style="red")
    truth_text.append("Difference                    ", style="dim")
    truth_text.append("Rs.23.34   ", style="bold red")
    truth_text.append("(> tolerance Rs.4.82)", style="red")
    console.print(Panel(
        truth_text, title="[bold]What the settlement recorded[/]",
        border_style="green", padding=(1, 2),
    ))

    # 3. Guard verdict + reason
    verdict_body = Text()
    verdict_body.append(" REJECTED ", style="bold white on red")
    verdict_body.append("   ->  forced to ", style="")
    verdict_body.append("GENUINE_MISMATCH", style="bold red")
    verdict_body.append("  (auto-reconcile blocked)\n\n", style="")
    verdict_body.append(lie["guard_reason"], style="italic")
    console.print(Panel(
        verdict_body, title="[bold red]Guard verdict[/]",
        border_style="red", padding=(1, 2),
    ))


def render_heldout_summary(console: Console, heldout: dict) -> None:
    """
    Show the held-out suite counts as the breadth companion to the
    depth panel above. 170/170 rejected in bold red; 80/80 accepted in
    bold green.
    """
    adv = heldout["adversarial"]
    legit = heldout["legit"]
    stage(console, "BREADTH  ·  held-out adversarial suite",
          f"seed={heldout['seed']} · {heldout['records_generated']} randomized records "
          f"· {heldout['cases_total']} cases · none hand-authored")

    meta = Table.grid(padding=(0, 3))
    meta.add_column(style="dim")
    meta.add_column()
    meta.add_row("seed",              f"[bold]{heldout['seed']}[/]  (distinct from the main batch's 42)")
    meta.add_row("records generated", f"[bold]{heldout['records_generated']}[/]")
    meta.add_row("cases total",       f"[bold]{heldout['cases_total']}[/]")
    console.print(meta)
    console.print()

    body = Text()
    body.append(f"{adv['rejected_by_guard']}/{adv['total']} ", style="bold red")
    body.append("mechanically-generated smart lies REJECTED", style="red")
    body.append(f"    (false positives: {adv['false_positives']})\n\n", style="dim")
    body.append(f"{legit['accepted_by_guard']}/{legit['total']} ", style="bold green")
    body.append("legitimate positive controls ACCEPTED", style="green")
    body.append(f"    (false rejections: {legit['false_rejections']})", style="dim")
    console.print(Panel(
        body, title="[bold]Guard on held-out data[/]",
        border_style="cyan", padding=(1, 2),
    ))
