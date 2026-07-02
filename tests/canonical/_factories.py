"""Shared model factories for canonical contract tests.

Each contract-test file builds :class:`TaskDescription` / :class:`EnvEndpoints`
with site-specific literals (task_id, name, description). These factories
centralise HOW the model is constructed so a new required field on the
underlying Pydantic model lands in one place; each test file still passes
its own site-specific literals as keyword overrides.
"""

from __future__ import annotations

from tolokaforge.core.trial import EnvEndpoints, EnvironmentManifest
from tolokaforge.runner.models import TaskDescription


def make_task_description(
    *,
    task_id: str = "task",
    name: str = "task",
    category: str = "test",
    description: str = "contract-test task",
    adapter_type: str = "native",
    system_prompt: str = "",
    environment_manifest: EnvironmentManifest | None = None,
) -> TaskDescription:
    return TaskDescription(
        task_id=task_id,
        name=name,
        category=category,
        description=description,
        adapter_type=adapter_type,
        system_prompt=system_prompt,
        environment_manifest=environment_manifest,
    )


def make_env_endpoints(
    *,
    db_url: str = "http://db.local:8000",
    rag_url: str | None = None,
    runner_url: str = "http://runner.local:50051",
) -> EnvEndpoints:
    return EnvEndpoints(db_url=db_url, rag_url=rag_url, runner_url=runner_url)
