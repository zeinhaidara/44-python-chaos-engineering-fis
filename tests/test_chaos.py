from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from chaos.cli import main
from chaos.config import ConfigError, Experiment, load
from chaos.report import to_markdown
from chaos.runner import GuardError, Runner, StopFailed

ROOT = Path(__file__).parent.parent
CONFIG = load(ROOT / "experiments.yaml")
API = CONFIG.experiments[0]  # stop-one-api-task, timeout 300s


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeFIS:
    """get_experiment walks through `statuses`; the last one repeats."""

    def __init__(
        self,
        statuses=("initiating", "running", "completed"),
        env="staging",
        start_error=None,
        stop_error=None,
    ):
        self.statuses = list(statuses)
        self.env = env
        self.start_error, self.stop_error = start_error, stop_error
        self.started, self.stopped, self.tokens = [], [], []

    def get_experiment_template(self, id):
        tags = {"environment": self.env} if self.env else {}
        return {"experimentTemplate": {"id": id, "tags": tags}}

    def start_experiment(self, experimentTemplateId, clientToken, tags):
        if self.start_error:
            raise self.start_error
        self.started.append(experimentTemplateId)
        self.tokens.append(clientToken)
        return {"experiment": {"id": f"EXP{len(self.started):04d}"}}

    def get_experiment(self, id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"experiment": {"id": id, "state": {"status": status, "reason": ""}}}

    def stop_experiment(self, id):
        if self.stop_error:
            raise self.stop_error
        self.stopped.append(id)


class SteadyState:
    """Healthy except during [down_from, down_until) on the shared clock."""

    def __init__(self, clock, down_from=None, down_for=0, never=False):
        self.clock, self.never = clock, never
        self.down_from = down_from
        self.down_until = down_from + timedelta(seconds=down_for) if down_from else None

    def __call__(self, url, expect):
        now = self.clock()
        down = self.never or (
            self.down_from is not None and self.down_from <= now < self.down_until
        )
        return (not down), f"GET {url} -> {500 if down else 200} (want {expect})"


def runner(fis, clock, check, execute=True, max_experiments=1):
    return Runner(fis, CONFIG, check, clock, clock.sleep, execute, max_experiments, poll_seconds=15)


# --- config ---------------------------------------------------------------------------


def test_example_config_loads():
    assert [e.name for e in CONFIG.experiments] == ["stop-one-api-task", "reboot-cache-node"]


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("environment: staging", "environment: Production", "protected"),
        ("template_id: EXT1a2b3c4d5e6f7a", "template_id: abc", "EXT"),
        (
            "url: https://api.staging.example.com/healthz\n      expect_status: 200\n"
            "    recovery_timeout_seconds: 300",
            "url: http://api.staging.example.com/healthz\n      expect_status: 200\n"
            "    recovery_timeout_seconds: 300",
            "https",
        ),
        ("recovery_timeout_seconds: 300", "recovery_timeout_seconds: 5", "30-3600"),
        ("name: reboot-cache-node", "name: stop-one-api-task", "unique"),
        (
            'runbook_step: "RB-12 step 1: ECS replaces the stopped task; no action needed"',
            'runbook_step: "  "',
            "runbook_step",
        ),
    ],
)
def test_bad_config_rejected(tmp_path, old, new, match):
    text = (ROOT / "experiments.yaml").read_text()
    assert old in text
    p = tmp_path / "e.yaml"
    p.write_text(text.replace(old, new, 1))
    with pytest.raises(ConfigError, match=match):
        load(p)


# --- guards -----------------------------------------------------------------------------


def test_cap_refuses_before_anything_runs():
    clock, fis = Clock(), FakeFIS()
    with pytest.raises(GuardError, match="--max-experiments"):
        runner(fis, clock, SteadyState(clock)).run(CONFIG.experiments)
    assert fis.started == []


@pytest.mark.parametrize("env", ["prod", "", "dev"])
def test_template_environment_must_match(env):
    clock, fis = Clock(), FakeFIS(env=env)
    with pytest.raises(GuardError, match="tagged environment"):
        runner(fis, clock, SteadyState(clock)).run([API])
    assert fis.started == []


def test_unsteady_system_is_not_injected():
    clock, fis = Clock(), FakeFIS()
    [r] = runner(fis, clock, SteadyState(clock, never=True)).run([API])
    assert r.status == "NOT_RUN"
    assert fis.started == []


def test_dry_run_is_default_and_injects_nothing():
    clock, fis = Clock(), FakeFIS()
    [r] = runner(fis, clock, SteadyState(clock), execute=False).run([API])
    assert r.status == "DRY_RUN"
    assert fis.started == []


# --- execute ------------------------------------------------------------------------------


def test_recovery_within_timeout_passes():
    clock, fis = Clock(), FakeFIS()
    # Down from 1s after injection until 60s; the experiment completes at 30s.
    check = SteadyState(clock, down_from=clock.now + timedelta(seconds=1), down_for=59)
    [r] = runner(fis, clock, check).run([API])
    assert r.status == "PASS"
    assert r.time_to_recover == timedelta(seconds=60)
    assert fis.stopped == []  # finished on its own, nothing to stop
    assert len(fis.tokens[0]) == 36  # idempotency token sent


def test_no_recovery_within_timeout_fails():
    clock, fis = Clock(), FakeFIS()
    check = SteadyState(clock, down_from=clock.now + timedelta(seconds=1), down_for=10_000)
    [r] = runner(fis, clock, check).run([API])
    assert r.status == "FAIL"
    assert any("not recovered within 300s" in t for _, t in r.log)


def test_experiment_stuck_running_is_stopped_on_timeout():
    clock, fis = Clock(), FakeFIS(statuses=["running"])
    [r] = runner(fis, clock, SteadyState(clock)).run([API])
    assert r.status == "FAIL"
    assert fis.stopped == ["EXP0001"]
    assert any("timeout: experiment still running" in t for _, t in r.log)


def test_failed_experiment_is_a_fail():
    clock, fis = Clock(), FakeFIS(statuses=["running", "failed"])
    [r] = runner(fis, clock, SteadyState(clock)).run([API])
    assert r.status == "FAIL"
    assert fis.stopped == []  # already terminal


def test_error_while_running_still_stops_the_experiment():
    clock, fis = Clock(), FakeFIS()

    def boom(id):
        raise RuntimeError("ThrottlingException")

    fis.get_experiment = boom
    [r] = runner(fis, clock, SteadyState(clock)).run([API])
    assert r.status == "FAIL"
    assert fis.stopped == ["EXP0001"]
    assert any("ThrottlingException" in t for _, t in r.log)


def test_stop_failure_is_raised_not_swallowed():
    clock, fis = Clock(), FakeFIS(statuses=["running"], stop_error=RuntimeError("AccessDenied"))
    with pytest.raises(StopFailed, match="stop it in the FIS console"):
        runner(fis, clock, SteadyState(clock)).run([API])


def test_start_error_needs_no_stop():
    clock, fis = Clock(), FakeFIS(start_error=RuntimeError("ValidationException"))
    [r] = runner(fis, clock, SteadyState(clock)).run([API])
    assert r.status == "FAIL"
    assert fis.stopped == []


# --- report and CLI --------------------------------------------------------------------------


def test_report_marks_runbook_step_verified_only_on_pass():
    clock, fis = Clock(), FakeFIS()
    [r] = runner(
        fis, clock, SteadyState(clock, down_from=clock.now + timedelta(seconds=1), down_for=29)
    ).run([API])
    md = to_markdown([r], "staging", True)
    assert "| stop-one-api-task | EXT1a2b3c4d5e6f7a | **PASS** | 30s | RB-12 step 1" in md
    failed = runner(FakeFIS(statuses=["failed"]), clock, SteadyState(clock)).run([API])
    assert "| **FAIL** | - | no |" in to_markdown(failed, "staging", True)


def run_cli(tmp_path, fis, *args, check=None):
    clock = Clock()
    report = tmp_path / "r.md"
    code = main(
        ["--config", str(ROOT / "experiments.yaml"), "--report", str(report), *args],
        fis=fis,
        check=check or SteadyState(clock),
        clock=clock,
        sleep=clock.sleep,
    )
    return code, report


def test_cli_exit_codes(tmp_path):
    assert run_cli(tmp_path, FakeFIS(), "--only", "stop-one-api-task")[0] == 0
    code, report = run_cli(
        tmp_path, FakeFIS(statuses=["failed"]), "--only", "stop-one-api-task", "--execute"
    )
    assert code == 1
    assert "**FAIL**" in report.read_text(encoding="utf-8")
    # Both experiments with the default cap of 1: refused.
    assert run_cli(tmp_path, FakeFIS())[0] == 1
    assert run_cli(tmp_path, FakeFIS(), "--only", "nope")[0] == 1


def test_cli_cap_can_be_raised(tmp_path):
    fis = FakeFIS()
    code, _ = run_cli(tmp_path, fis, "--max-experiments", "2")
    assert code == 0  # dry run of both
    assert fis.started == []


def test_experiment_dataclass_is_frozen():
    with pytest.raises(AttributeError):
        API.name = "x"  # type: ignore[misc]
    assert isinstance(API, Experiment)
