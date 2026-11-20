"""Load and check experiments.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PROTECTED_ENVS = {"prod", "production"}
TEMPLATE_ID = re.compile(r"^EXT[a-zA-Z0-9]{2,}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Experiment:
    name: str
    template_id: str
    url: str
    expect_status: int
    recovery_timeout_seconds: int
    runbook_step: str


@dataclass(frozen=True)
class Config:
    environment: str
    experiments: list[Experiment]


def load(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        env = str(raw["environment"]).strip().lower()
        experiments = [
            Experiment(
                name=e["name"],
                template_id=e["template_id"],
                url=e["steady_state"]["url"],
                expect_status=int(e["steady_state"].get("expect_status", 200)),
                recovery_timeout_seconds=int(e["recovery_timeout_seconds"]),
                runbook_step=e["runbook_step"],
            )
            for e in raw["experiments"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: missing or malformed field: {exc}") from exc

    if env in PROTECTED_ENVS:
        raise ConfigError(f"environment {env!r} is protected; chaos runs are not allowed there")
    if not experiments:
        raise ConfigError("no experiments listed")
    names = [e.name for e in experiments]
    if len(set(names)) != len(names):
        raise ConfigError("experiment names must be unique")
    for e in experiments:
        if not TEMPLATE_ID.match(e.template_id):
            raise ConfigError(f"{e.name}: template_id {e.template_id!r} doesn't look like EXT...")
        if not e.url.startswith("https://"):
            raise ConfigError(f"{e.name}: steady_state url must be https")
        if not 30 <= e.recovery_timeout_seconds <= 3600:
            raise ConfigError(f"{e.name}: recovery_timeout_seconds must be 30-3600")
        if not e.runbook_step.strip():
            raise ConfigError(f"{e.name}: runbook_step is required")
    return Config(env, experiments)
