"""Run FIS experiments one by one and check the system recovers within the timeout."""

from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chaos.config import Config, Experiment

TERMINAL = {"completed", "stopped", "failed", "cancelled"}
SteadyCheck = Callable[[str, int], tuple[bool, str]]


def http_check(url: str, expect_status: int) -> tuple[bool, str]:
    """Default steady-state check: one HTTPS GET, compare the status code."""
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except OSError as exc:
        return False, f"GET {url} failed: {exc}"
    return status == expect_status, f"GET {url} -> {status} (want {expect_status})"


class GuardError(RuntimeError):
    """Refused before anything was injected."""


class StopFailed(RuntimeError):
    """We could not stop a running experiment. A human must stop it."""


@dataclass
class Result:
    experiment: Experiment
    status: str = "PENDING"  # PASS, FAIL, NOT_RUN, DRY_RUN
    experiment_id: str | None = None
    started_at: datetime | None = None
    recovered_at: datetime | None = None
    log: list[tuple[datetime, str]] = field(default_factory=list)

    @property
    def time_to_recover(self) -> timedelta | None:
        if self.started_at and self.recovered_at:
            return self.recovered_at - self.started_at
        return None


class Runner:
    def __init__(
        self,
        fis,
        config: Config,
        check: SteadyCheck,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
        execute: bool = False,
        max_experiments: int = 1,
        poll_seconds: float = 15,
    ):
        self.fis, self.config, self.check = fis, config, check
        self.clock, self.sleep = clock, sleep
        self.execute, self.max_experiments, self.poll_seconds = (
            execute,
            max_experiments,
            poll_seconds,
        )

    def guard(self, experiments: list[Experiment]) -> None:
        if len(experiments) > self.max_experiments:
            raise GuardError(
                f"{len(experiments)} experiments selected but --max-experiments is "
                f"{self.max_experiments}; nothing was run"
            )
        for e in experiments:
            template = self.fis.get_experiment_template(id=e.template_id)["experimentTemplate"]
            env = template.get("tags", {}).get("environment", "").lower()
            if env != self.config.environment:
                raise GuardError(
                    f"{e.name}: template {e.template_id} is tagged environment={env or '(none)'}, "
                    f"config says {self.config.environment}; nothing was run"
                )

    def run(self, experiments: list[Experiment]) -> list[Result]:
        self.guard(experiments)
        return [self._one(e) for e in experiments]

    def _note(self, r: Result, text: str) -> None:
        r.log.append((self.clock(), text))

    def _one(self, e: Experiment) -> Result:
        r = Result(e)
        ok, detail = self.check(e.url, e.expect_status)
        self._note(r, f"steady state before: {detail}")
        if not ok:
            # Never inject into a system that is already unhealthy.
            r.status = "NOT_RUN"
            return r
        if not self.execute:
            r.status = "DRY_RUN"
            self._note(r, f"dry run: would start FIS template {e.template_id}")
            return r

        terminal = False
        try:
            resp = self.fis.start_experiment(
                experimentTemplateId=e.template_id,
                clientToken=str(uuid.uuid4()),
                tags={"started-by": "chaos-runner", "experiment": e.name},
            )
            r.experiment_id = resp["experiment"]["id"]
            r.started_at = self.clock()
            self._note(r, f"started experiment {r.experiment_id}")
            terminal = self._watch(r)
        except Exception as exc:
            r.status = "FAIL"
            self._note(r, f"error: {type(exc).__name__}: {exc}")
        finally:
            if r.experiment_id and not terminal:
                self._stop(r)
        return r

    def _deadline(self, r: Result) -> datetime:
        return r.started_at + timedelta(seconds=r.experiment.recovery_timeout_seconds)

    def _watch(self, r: Result) -> bool:
        """Returns True once the experiment reached a terminal state."""
        e = r.experiment
        while True:
            state = self.fis.get_experiment(id=r.experiment_id)["experiment"]["state"]
            status = state["status"]
            if status in TERMINAL:
                self._note(r, f"experiment {status}: {state.get('reason', '')}".rstrip(": "))
                break
            if self.clock() >= self._deadline(r):
                r.status = "FAIL"
                self._note(r, f"timeout: experiment still {status}")
                return False
            self.sleep(self.poll_seconds)

        if status != "completed":
            r.status = "FAIL"
            return True

        # Injection is over; now the system has to come back on its own.
        while True:
            ok, detail = self.check(e.url, e.expect_status)
            if ok:
                r.recovered_at = self.clock()
                r.status = "PASS"
                self._note(r, f"steady state after: {detail}")
                return True
            if self.clock() >= self._deadline(r):
                r.status = "FAIL"
                self._note(r, f"not recovered within {e.recovery_timeout_seconds}s: {detail}")
                return True
            self.sleep(self.poll_seconds)

    def _stop(self, r: Result) -> None:
        try:
            self.fis.stop_experiment(id=r.experiment_id)
        except Exception as exc:
            self._note(r, f"STOP FAILED: {type(exc).__name__}: {exc}")
            raise StopFailed(
                f"could not stop {r.experiment_id}; stop it in the FIS console now"
            ) from exc
        self._note(r, f"stopped experiment {r.experiment_id}")
