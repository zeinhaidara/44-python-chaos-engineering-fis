"""python -m chaos --config experiments.yaml --only stop-one-api-task [--execute]"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from chaos.config import load
from chaos.report import to_markdown
from chaos.runner import STOP_FAILED, Runner, http_check

EXIT_OK, EXIT_FAILED, EXIT_STOP_FAILED = 0, 1, 3


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run AWS FIS experiments and verify recovery.")
    p.add_argument("--config", type=Path, default=Path("experiments.yaml"))
    p.add_argument("--only", action="append", help="experiment name to run (repeatable)")
    p.add_argument(
        "--max-experiments",
        type=int,
        default=1,
        help="refuse to run more than this many in one invocation",
    )
    p.add_argument("--execute", action="store_true", help="really inject (default: dry run)")
    p.add_argument("--region", help="AWS region for FIS")
    p.add_argument("--report", type=Path, default=Path("chaos-report.md"))
    args = p.parse_args(argv)
    if args.max_experiments < 1:
        p.error("--max-experiments must be >= 1")
    return args


def main(argv=None, fis=None, check=http_check, clock=None, sleep=None) -> int:
    args = parse_args(argv)
    clock = clock or (lambda: datetime.now(UTC))
    try:
        config = load(args.config)
        chosen = config.experiments
        if args.only:
            unknown = set(args.only) - {e.name for e in chosen}
            if unknown:
                raise ValueError(f"unknown experiment(s): {sorted(unknown)}")
            chosen = [e for e in chosen if e.name in args.only]
        if fis is None:
            import boto3

            fis = boto3.client("fis", region_name=args.region)
        runner = Runner(
            fis, config, check, clock, sleep or time.sleep, args.execute, args.max_experiments
        )
        results = runner.run(chosen)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED

    args.report.write_text(to_markdown(results, config.environment, args.execute), encoding="utf-8")
    for r in results:
        print(f"{r.experiment.name}: {r.status}")
    print(f"report written to {args.report}")

    stuck = [r for r in results if r.status == STOP_FAILED]
    for r in stuck:
        print(
            f"STOP FAILED: experiment {r.experiment_id} ({r.experiment.name}) may still be "
            "injecting faults. Stop it in the FIS console now "
            f"(or: aws fis stop-experiment --id {r.experiment_id}).",
            file=sys.stderr,
        )
    if stuck:
        return EXIT_STOP_FAILED
    ok = all(r.status in ("PASS", "DRY_RUN") for r in results)
    return EXIT_OK if ok else EXIT_FAILED
