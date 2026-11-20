"""Markdown report for one chaos run."""

from __future__ import annotations

from chaos.runner import Result


def to_markdown(results: list[Result], environment: str, execute: bool) -> str:
    lines = [
        "# Chaos run report",
        "",
        f"- Environment: {environment}",
        f"- Mode: {'execute' if execute else 'dry run'}",
        "",
    ]
    for r in results:
        if r.status == "STOP_FAILED":
            lines += [
                f"> **STOP FAILED: experiment {r.experiment_id} ({r.experiment.name}) could not "
                "be stopped and may still be injecting faults. Stop it in the FIS console now.**",
                "",
            ]
    lines += [
        "| Experiment | FIS template | Result | Time to recover | Runbook step verified |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ttr = f"{r.time_to_recover.total_seconds():.0f}s" if r.time_to_recover else "-"
        verified = r.experiment.runbook_step if r.status == "PASS" else "no"
        lines.append(
            f"| {r.experiment.name} | {r.experiment.template_id} | **{r.status}** | "
            f"{ttr} | {verified} |"
        )
    for r in results:
        lines += ["", f"## {r.experiment.name}", ""]
        lines += [f"- {when.isoformat()} {text}" for when, text in r.log]
    return "\n".join(lines) + "\n"
