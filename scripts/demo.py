"""
Screen-recordable demo: the smart-lie rejection (depth) and the held-out
adversarial summary (breadth), in sequence, with polished rich output.

Runs entirely from committed evidence artifacts — no API key, no network,
no pipeline execution. Meant for the pitch video: one command, ~1 second,
prints two visually distinct sections plus a closing takeaway.

  python scripts/demo.py

Optional: `--html <path>` writes an HTML rendering of the demo output
(useful for embedding in a webpage or attaching to a submission), without
changing the terminal output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make src.* imports work when this file is run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from src.reporter.render_rich import render_heldout_summary, render_smart_lie_panel


def _intro(console: Console) -> None:
    body = Text()
    body.append("Settlement Reconciliation Agent", style="bold cyan")
    body.append("  ·  Guard Demonstration\n", style="")
    body.append("Razorpay Buildathon · Track 04 (AI Finance Controller)", style="dim")
    console.print(Panel(body, padding=(1, 2), border_style="cyan"))


def _takeaway(console: Console) -> None:
    body = Text()
    body.append("DEPTH   ", style="bold red")
    body.append("catches a subtle, believable-magnitude lie ", style="")
    body.append("(ORD-0006, Rs.119.80 vs Rs.96.46)\n", style="italic dim")
    body.append("BREADTH ", style="bold cyan")
    body.append("holds across 170 mechanically-generated fabrications ", style="")
    body.append("(seed=1337, none hand-authored)\n", style="italic dim")
    body.append("\nThe guard is the invariant. The LLM is bounded by it, not trusted.",
                style="bold")
    console.print(Panel(
        body, title="[bold cyan]Takeaway[/]", padding=(1, 2), border_style="cyan",
    ))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, default=None,
                        help="Optionally save an HTML rendering to this path.")
    parser.add_argument("--width", type=int, default=110,
                        help="Console width (default 110; good for screen recording).")
    args = parser.parse_args()

    record = args.html is not None
    console = Console(record=record, width=args.width)

    _intro(console)

    after_guard_path = Path("evidence/after_guard.json")
    if not after_guard_path.exists():
        console.print(f"[bold red]missing[/] {after_guard_path}. "
                      f"Run: python scripts/capture_after_guard.py")
        sys.exit(1)
    render_smart_lie_panel(
        console, json.loads(after_guard_path.read_text(encoding="utf-8")),
    )

    heldout_path = Path("evidence/heldout_guard.json")
    if not heldout_path.exists():
        console.print(f"[bold red]missing[/] {heldout_path}. "
                      f"Run: python scripts/capture_heldout_guard.py")
        sys.exit(1)
    render_heldout_summary(
        console, json.loads(heldout_path.read_text(encoding="utf-8")),
    )

    _takeaway(console)

    if args.html is not None:
        html = console.export_html(inline_styles=True)
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(html, encoding="utf-8")
        console.print(f"\n[dim]HTML rendering written to {args.html}[/]")


if __name__ == "__main__":
    main()
