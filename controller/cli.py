"""
M5 - Typer CLI for Report Generation

Usage:
    python cli.py report              # heuristic-only (default)
    python cli.py report --llm        # include Gemini LLM analysis
    python cli.py report --db PATH    # custom DB path
    python cli.py report --out DIR    # custom output directory

Exposed via:  make report  →  python cli.py report
"""

import typer
from rich.console import Console
from rich.table import Table

from reporter import generate_report

app = typer.Typer(
    name="genesis-m5",
    help="🧪 Genesis Chaos Platform — M5 Reporting CLI",
)
console = Console()

@app.callback()
def callback():
    """Genesis Chaos Platform - M5 Reporting CLI"""
    pass

@app.command(name="report")
def report(
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Enable Gemini LLM analysis (requires GEMINI_API_KEY).",
    ),
    db: str = typer.Option(
        "sqlite:///./chaos_state.db",
        "--db",
        help="SQLAlchemy DB URL for the chaos_state database.",
    ),
    out: str = typer.Option(
        ".",
        "--out",
        help="Output directory for report.json and REPORT.md.",
    ),
) -> None:
    """
    Generate the resilience report from the SQLite experiment journal.

    Reads ExperimentIntent, ExperimentResult, and MetricsSnapshot tables
    directly - the FastAPI controller does NOT need to be running.
    """
    console.print("\n[bold cyan]Genesis - Generating Resilience Report[/bold cyan]\n")

    if llm:
        import os
        if not os.environ.get("GEMINI_API_KEY"):
            console.print(
                "[bold red]WARNING: GEMINI_API_KEY not set. "
                "Falling back to heuristic analysis.[/bold red]\n"
            )
            llm = False

    data = generate_report(db_url=db, use_llm=llm, output_dir=out)

    # ── Pretty-print summary ────────────────────────────────────────
    table = Table(title="Resilience Summary", show_lines=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold white")

    table.add_row("Total Experiments", str(data["total_experiments"]))
    table.add_row("Overall Score", f"{data['overall_score']}/10")
    table.add_row("Kill Switch Activations", str(data["kill_switch_count"]))
    table.add_row("Weakest Service", data["weakest_service"])
    console.print(table)

    # Weakness ranking
    if data["weaknesses"]:
        wtable = Table(title="Ranked Weaknesses (worst first)", show_lines=True)
        wtable.add_column("#", style="dim")
        wtable.add_column("Service", style="bold")
        wtable.add_column("Fault", style="yellow")
        wtable.add_column("Score", style="red")
        wtable.add_column("Criticality")
        wtable.add_column("Recommended Fix", style="bold green")

        for i, w in enumerate(data["weaknesses"], 1):
            wtable.add_row(
                str(i), w["service"], w["fault_type"],
                f"{w['score']}/10", w["criticality"],
                w.get("recommended_fix") or "N/A",
            )
        console.print(wtable)

    # Analysis and Recommended Fixes
    if data["experiments"]:
        atable = Table(title="Analysis & Recommended Fixes", show_lines=True)
        atable.add_column("Experiment", style="bold cyan")
        atable.add_column("Recommended Fix", style="bold green")
        atable.add_column("Analysis", style="white")

        for exp in data["experiments"]:
            target_str = f"#{exp['intent_id']} {exp['target_service']}\n({exp['fault_type']})"
            fix = exp.get("recommended_fix") or "N/A"
            analysis_text = exp.get("llm_analysis") or exp.get("analysis") or "N/A"
            atable.add_row(target_str, fix, analysis_text)
        console.print(atable)

    console.print(f"\n[green]SUCCESS: report.json and REPORT.md written to {out}/[/green]\n")


if __name__ == "__main__":
    app()
