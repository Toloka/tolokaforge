"""Unit tests for the terminal-bench adapter's ``SkillDelivery`` seam."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
from tolokaforge_adapter_terminal_bench.compose_synthesis import ImageLayerSkillDelivery
from tolokaforge_adapter_terminal_bench.harness import SkillsBundle

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]

_FIXTURE_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"


@dataclass(frozen=True)
class _RecordingDelivery:
    """A second runtime's answer: record the bundle, put it nowhere."""

    calls: list[SkillsBundle] = field(default_factory=list)

    def deliver(self, bundle: SkillsBundle) -> None:
        self.calls.append(bundle)


@dataclass(frozen=True)
class _HomeAgentResolver:
    """A second runtime's answer: the CLI runs as a non-root user."""

    def resolve(self, path: str) -> str:
        return path.replace("${HOME}", "/home/agent")


def _staging_dir(adapter: TerminalBenchAdapter) -> Path:
    return adapter.docker_stack_requirements().image_builds[-1].compose_file.parent


def _harness_dockerfile(staging_dir: Path) -> str:
    return (staging_dir / "_harness" / "harness.Dockerfile").read_text()


def _adapter(
    tmp_path: Path, task_id: str, harness: str, *, params: dict | None = None, **seams
) -> TerminalBenchAdapter:
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(_FIXTURE_DIR),
            "task_ids": [task_id],
            "staging_root": str(tmp_path / "staging"),
            "agent_harness": harness,
            "agent_model": "m",
            **(params or {}),
        },
        **seams,
    )


class TestTheDeliverySeamIsLoadBearing:
    """An embedder driving the adapter from Python decides how the bundle
    travels, and the shipped image-layer ``COPY`` stops happening."""

    def test_the_injected_delivery_receives_one_resolved_bundle(self, tmp_path):
        delivery = _RecordingDelivery()
        adapter = _adapter(tmp_path, "echo-hello-skills", "claude-code", skill_delivery=delivery)
        adapter.to_task_description("echo-hello-skills")

        assert len(delivery.calls) == 1
        bundle = delivery.calls[0]
        assert bundle.task_dir == adapter.get_task_dir("echo-hello-skills")
        assert bundle.source_rel == "skills/"
        assert bundle.target == "/root/.claude/skills/"
        assert bundle.staging_dir.is_dir()

    def test_nothing_else_copies_the_bundle_into_the_image(self, tmp_path):
        """The proof that the seam is not a wrapper around unconditional code:
        a delivery that copies nothing leaves a Dockerfile that copies
        nothing."""
        delivery = _RecordingDelivery()
        adapter = _adapter(tmp_path, "echo-hello-skills", "claude-code", skill_delivery=delivery)
        adapter.to_task_description("echo-hello-skills")

        staging_dir = delivery.calls[0].staging_dir
        assert "COPY skills" not in (staging_dir / "_harness" / "harness.Dockerfile").read_text()
        assert "!skills" not in (staging_dir / ".dockerignore").read_text()

    def test_a_harness_that_reads_no_skills_never_calls_deliver(self, tmp_path):
        """No target, no bundle, no delivery — the warning is the whole event."""
        delivery = _RecordingDelivery()
        adapter = _adapter(tmp_path, "echo-hello-skills", "codex", skill_delivery=delivery)
        with pytest.warns(UserWarning, match="declares no skills_dir_target"):
            adapter.to_task_description("echo-hello-skills")

        assert delivery.calls == []


class TestImageLayerSkillDelivery:
    """The shipped answer for this adapter: one more layer on the harness
    image."""

    def test_a_deferred_target_is_refused(self, tmp_path):
        """Docker expands a ``COPY`` destination from the image's own ``ENV``,
        which is neither the resolver's answer nor the container shell's — so
        the bundle would land where the CLI does not look while the trial still
        recorded skills."""
        bundle = SkillsBundle(
            task_dir=tmp_path,
            source_rel="skills/",
            target="${SOMETHING}/skills/",
            staging_dir=tmp_path,
        )
        with pytest.raises(ValueError, match=r"\$\{SOMETHING\}"):
            ImageLayerSkillDelivery().deliver(bundle)


class TestTheTwoSeamsCompose:
    """A registry entry names the target symbolically, the resolver answers it,
    and the shipped delivery copies to the answer."""

    @pytest.fixture
    def overlay(self, tmp_path) -> Path:
        path = tmp_path / "harness_presets.yaml"
        path.write_text(
            "harnesses:\n"
            "  my-cli:\n"
            "    install_source: '@acme/my-cli'\n"
            "    version: '1.0.0'\n"
            "    argv_prefix: [my-cli]\n"
            "    argv_suffix: ['--run']\n"
            '    skills_dir_target: "${HOME}/.claude/skills/"\n'
        )
        return path

    def test_a_symbolic_target_reaches_the_dockerfile_resolved(self, overlay, tmp_path):
        adapter = _adapter(
            tmp_path,
            "echo-hello-skills",
            "my-cli",
            params={"harness_presets_file": str(overlay)},
        )
        assert "COPY skills/. /root/.claude/skills/" in _harness_dockerfile(_staging_dir(adapter))

    def test_two_resolvers_over_one_staging_root_keep_their_own_build_contexts(
        self, overlay, tmp_path
    ):
        """The staging directory carries the generated build context, so whoever
        answered the paths is part of its identity: sharing one would leave the
        first adapter pointing at a Dockerfile that copies skills where its CLI
        never looks, while its artifact still recorded a bundle hash."""
        params = {"harness_presets_file": str(overlay)}
        shipped = _adapter(tmp_path, "echo-hello-skills", "my-cli", params=params)
        injected = _adapter(
            tmp_path,
            "echo-hello-skills",
            "my-cli",
            params=params,
            path_resolver=_HomeAgentResolver(),
        )

        assert _staging_dir(shipped) != _staging_dir(injected)
        assert "COPY skills/. /root/.claude/skills/" in _harness_dockerfile(_staging_dir(shipped))
        assert "COPY skills/. /home/agent/.claude/skills/" in _harness_dockerfile(
            _staging_dir(injected)
        )

    def test_two_deliveries_over_one_staging_root_keep_their_own_build_contexts(self, tmp_path):
        """Same target, same resolver, different delivery: one Dockerfile copies
        the bundle and the other does not, so the delivery is part of the staging
        directory's identity too."""
        shipped = _adapter(tmp_path, "echo-hello-skills", "claude-code")
        injected = _adapter(
            tmp_path, "echo-hello-skills", "claude-code", skill_delivery=_RecordingDelivery()
        )

        assert _staging_dir(shipped) != _staging_dir(injected)
        assert "COPY skills/." in _harness_dockerfile(_staging_dir(shipped))
        assert "COPY skills/." not in _harness_dockerfile(_staging_dir(injected))
