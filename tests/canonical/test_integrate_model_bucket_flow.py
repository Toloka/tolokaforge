"""Shape lock for the ``Finalize - verify + commit + report`` step of
``.github/workflows/integrate-model.yml``.

The finalize step stages the models-wheel + engine surfaces produced by the
resolve/finalize agent, classifies the staged tree as Bucket A (models-wheel
only) or Bucket B (any engine-side path touched) via ``automation
classify-paths``, and — post-ADR-0030 — commits Bucket A only. This test locks
the properties a future refactor could silently regress:

1. The ``git add`` whitelist references the current models-wheel layout
   (current) and drops the pre-cutover paths.
2. The classification step runs between ``git add`` and ``git commit`` so
   ``git diff --cached`` sees the staged tree that is about to be committed.
3. All three Slack ``MSG`` bodies on the non-data-scope branch carry a
   bucket-tagging shell expression (merged / auto-merge-attempted-failed /
   auto-merge-off - not just the "merged" one).
4. The finalize ``git add`` no longer swallows missing-path errors via
   ``2>/dev/null || true`` — every whitelist entry exists in the tree, so a
   missing path signals a workflow bug, not a legitimate skip.
5. Bucket B is refused before anything is committed, and the refusal reaches a
   person (needs-human Slack + dedup marker) rather than only the run log.
6. An ``integrate:`` commit on ``main`` auto-releases the models wheel at
   ``minor``, and the release workflow it calls validates the increment.

Runs under the ``canonical`` marker alongside
``test_python_version_single_source.py``; the same YAML-parse +
assert-on-step-shape idiom applies.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "integrate-model.yml"
_STEP_NAME_PREFIX = "Finalize - verify + commit + report"

_EXPECTED_STAGED_PATHS = frozenset(
    {
        "tolokaforge/core/llm",
        "tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml",
        "tolokaforge_models/src/tolokaforge_models/data/pricing.json",
        "tolokaforge_models/src/tolokaforge_models/certificates/registry.py",
        "tolokaforge_models/src/tolokaforge_models/policies",
        "tolokaforge_models/src/tolokaforge_models/__init__.py",
        "tolokaforge_models/pyproject.toml",
        "tolokaforge_models/tests",
    }
)

_PRE_CUTOVER_PATHS_FORBIDDEN = (
    "tolokaforge/core/data/model_presets.yaml",
    "tolokaforge/core/data/pricing.json",
    "tolokaforge/testing/certify/_registry.py",
)

_MSG_ASSIGN_RE = re.compile(r'^\s*MSG="\$\(printf .*\)"\s*$', re.MULTILINE)
_BUCKET_SHELL_EXPR_RE = re.compile(r"\$\{?BUCKET(?:_[A-Z_]+)?\}?")


def _load_finalize_step() -> dict[str, object]:
    doc = yaml.safe_load(_WORKFLOW_PATH.read_text())
    jobs = doc["jobs"]
    for job in jobs.values():
        for step in job.get("steps", []):
            name = step.get("name", "")
            if isinstance(name, str) and name.startswith(_STEP_NAME_PREFIX):
                return step
    rel = _WORKFLOW_PATH.relative_to(_REPO_ROOT)
    raise AssertionError(f"finalize step '{_STEP_NAME_PREFIX}' not found in {rel}")


def _finalize_step_body() -> str:
    step = _load_finalize_step()
    run = step.get("run")
    assert isinstance(run, str), "finalize step must have a `run:` shell body"
    return run


def _extract_git_add_paths(body: str) -> set[str]:
    match = re.search(
        r"^\s*git add(?:\s+\\\n|\s+)(.*?)\n\s*if git diff --cached",
        body,
        re.DOTALL | re.MULTILINE,
    )
    msg = "expected a `git add ... \\n if git diff --cached` block in the finalize step"
    assert match is not None, msg
    tokens = re.split(r"[\s\\]+", match.group(1))
    return {token for token in tokens if token and not token.startswith("#")}


def test_finalize_git_add_targets_models_wheel_layout() -> None:
    body = _finalize_step_body()
    staged = _extract_git_add_paths(body)
    msg = (
        f"finalize git-add whitelist drift: expected {sorted(_EXPECTED_STAGED_PATHS)}, "
        f"got {sorted(staged)}"
    )
    assert staged == _EXPECTED_STAGED_PATHS, msg


def test_finalize_drops_pre_cutover_paths() -> None:
    body = _finalize_step_body()
    for path in _PRE_CUTOVER_PATHS_FORBIDDEN:
        msg = f"pre-cutover path '{path}' still referenced in the finalize step"
        assert path not in body, msg


def test_finalize_classifies_between_git_add_and_git_commit() -> None:
    body = _finalize_step_body()
    add_match = re.search(r"^\s*git add\b", body, re.MULTILINE)
    classify_match = re.search(r"uv run automation classify-paths --paths-from-cached", body)
    commit_match = re.search(r"^\s*git commit\b", body, re.MULTILINE)
    assert add_match is not None, "`git add` not found in finalize step"
    classify_msg = (
        "expected `uv run automation classify-paths --paths-from-cached` in finalize step"
    )
    assert classify_match is not None, classify_msg
    assert commit_match is not None, "`git commit` not found in finalize step"
    order_msg = (
        "classification must run AFTER `git add` and BEFORE `git commit` "
        f"(add@{add_match.start()}, classify@{classify_match.start()}, "
        f"commit@{commit_match.start()})"
    )
    assert add_match.start() < classify_match.start() < commit_match.start(), order_msg


def test_finalize_classification_reads_paths_from_cached_exactly_once() -> None:
    body = _finalize_step_body()
    hits = list(re.finditer(r"uv run automation classify-paths --paths-from-cached", body))
    msg = (
        "finalize step must invoke `classify-paths --paths-from-cached` exactly once; "
        f"found {len(hits)}"
    )
    assert len(hits) == 1, msg


def test_finalize_fails_loud_on_unknown_bucket() -> None:
    body = _finalize_step_body()
    guard_msg = "finalize step must fail loud when classifier returns a bucket other than A or B"
    assert re.search(r'BUCKET"?\s*!=\s*"A".*BUCKET"?\s*!=\s*"B"', body, re.DOTALL), guard_msg
    error_msg = "finalize step must emit `::error::` naming `classify` on bucket parse failure"
    assert re.search(r'::error::[^"\n]*classify', body), error_msg


def test_commit_subject_carries_bucket_suffix() -> None:
    body = _finalize_step_body()
    a_msg = "finalize commit-subject template for Bucket A missing"
    assert re.search(r"Bucket A: preset \+ cert", body), a_msg
    # No Bucket B counterpart: post-split the finalize step refuses that tree
    # instead of committing it under a different subject. See
    # `test_finalize_refuses_to_commit_bucket_b`.
    integrate_subjects = re.findall(r"integrate: \$\{TF_NAME\}[^\"]*", body)
    template_msg = "expected at least one `integrate: ${TF_NAME}` commit-subject template"
    assert integrate_subjects, template_msg
    for subject in integrate_subjects:
        walker_msg = (
            f"commit subject '{subject}' — the replay walker's `_MODEL_RE` requires "
            "a whitespace-terminated slug immediately after `integrate: <slug>`"
        )
        assert re.match(r"integrate: \$\{TF_NAME\}\s", subject), walker_msg


def test_all_three_slack_messages_carry_bucket_tag() -> None:
    body = _finalize_step_body()
    non_data_scope_branch = body.split('if [ "$DATA_SCOPE" = "yes" ]')[-1]
    msg_assignments = _MSG_ASSIGN_RE.findall(non_data_scope_branch)
    count_msg = (
        "expected exactly three `MSG=$(printf ...)` assignments on the non-data-scope "
        f"Slack branch; found {len(msg_assignments)}"
    )
    assert len(msg_assignments) == 3, count_msg
    for i, assignment in enumerate(msg_assignments, start=1):
        expr_msg = (
            f"MSG assignment #{i} on the non-data-scope branch lacks a bucket-tagging "
            f"shell expression: {assignment!r}"
        )
        assert _BUCKET_SHELL_EXPR_RE.search(assignment), expr_msg


def test_finalize_step_has_no_silent_swallow() -> None:
    body = _finalize_step_body()
    swallow = re.search(r"2>/dev/null\s*\|\|\s*true", body)
    if swallow is not None:
        excerpt = body[max(0, swallow.start() - 40) : swallow.end() + 20]
        raise AssertionError(
            f"finalize step contains a swallow pattern at offset {swallow.start()}: {excerpt!r}"
        )


def test_finalize_preserves_existing_verification_gates() -> None:
    body = _finalize_step_body()
    required_gates = (
        (r"automation reconcile-cert", "cert-reconcile invocation"),
        (r"git stash push --keep-index --include-untracked", "verify-time stash push"),
        (r"git stash pop", "verify-time stash pop"),
        (r"automation ensure-pricing --name .* --check", "auto-merge price gate"),
        (r"case \"\$\{HEAD_REF:-\}\" in\s+test/\*\)", "disposable test-branch guard"),
    )
    for pattern, label in required_gates:
        msg = f"finalize step no longer contains the {label} - the retarget must not drop it"
        assert re.search(pattern, body), msg


# ---------------------------------------------------------------------------
# The observe cleanliness gate treats infra contamination as a rate.
#
# `any(non-zero)` discarded an entire observe run for one transient error in
# 200 trials — a full-cost run thrown away and a clean candidate sent to a
# human. The gate's job is to keep the agent away from transport-caused
# failures, which arrive in bulk; a single hiccup cannot bias what it sees.
# ---------------------------------------------------------------------------


def _strip_comments(body: str) -> str:
    """Shell body minus comment lines — the rationale mentions the construct it
    replaced, and a substring check must not trip on the explanation."""
    return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))


def _gate_step_body() -> str:
    doc = yaml.safe_load(_WORKFLOW_PATH.read_text())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            name = step.get("name", "")
            if isinstance(name, str) and name.startswith("Cleanliness gate"):
                run = step.get("run")
                assert isinstance(run, str)
                return run
    raise AssertionError("no `Cleanliness gate` step found")


def test_the_gate_scores_a_rate_not_a_boolean() -> None:
    body = _strip_comments(_gate_step_body())
    assert "any(" not in body, (
        "a boolean over the infra counters fails the whole observe on one "
        "transient error; the gate must weigh them against the trial count"
    )
    assert "trials" in body, "the rate needs the denominator"
    assert "r>0.05" in body or "r > 0.05" in body, "expected the 5%-of-trials threshold"


def test_a_suite_that_never_ran_is_still_absolute() -> None:
    # Not a rate question: no capability data at all means nothing to analyse,
    # however quiet the wire pack was.
    body = _gate_step_body()
    assert 'not f.get("capability_ran")' in body


def test_the_verdict_and_its_numbers_are_surfaced() -> None:
    body = _gate_step_body()
    assert "observe gate:" in body, "the decision must be greppable in the run log"
    assert "$GATE" in body, "the needs-human Slack must name the counts, not just say dirty"


def test_the_gate_sums_every_transport_counter_the_producer_emits() -> None:
    """The consumed counter list must match the producer's transport set.

    `api_timeout` was missing from the sum, so a wire run where every trial
    died on a provider timeout scored zero noise and chained to resolve — the
    exact case the producer's own comment says those counters exist to catch.
    Nothing locked the list, which is why it survived a rewrite of that very
    expression.
    """
    body = _strip_comments(_gate_step_body())
    producer = (_REPO_ROOT / "tools/automation/src/automation/observe.py").read_text()
    infra_block = producer[producer.index('"infra": {') : producer.index('"infra": {') + 600]

    transport = {"rate_limit", "status_error", "api_error", "api_timeout"}
    for counter in transport:
        assert f'"{counter}"' in infra_block, f"{counter} is no longer produced; revisit the gate"
        assert f'"{counter}"' in body, (
            f"the gate ignores {counter!r}, so a run contaminated only that way "
            f"scores zero noise and reaches the agent as clean data"
        )

    # Model-behaviour counters must stay out: a model that fails to finish is
    # the finding, not contamination.
    for counter in ("max_turns", "stuck"):
        assert f'"{counter}"' not in body, (
            f"{counter!r} measures the model, not the transport; counting it "
            f"would discard exactly the runs worth analysing"
        )


def test_the_verdict_carries_a_reason_token() -> None:
    # `dirty … 0 200 0.0` on its own reads as a contradiction; the reason says
    # whether the rate was exceeded or the capability suite never ran.
    body = _strip_comments(_gate_step_body())
    assert "capability-suite-did-not-run" in body
    assert "infra-noise" in body


# Post-split policy: this pipeline commits Bucket A only.
#
# Before the split the classifier was a taxonomy and both buckets flowed through
# the same commit + push path, differing only in the commit subject. Per-model
# policy code now belongs in the models wheel, so an engine-side write means the
# candidate needs a base hook / slot / capability category that is a human
# decision on the engine's release axis. These lock the refusal in shape.
# ---------------------------------------------------------------------------

_RELEASE_TRIGGER_PATH = _REPO_ROOT / ".github" / "workflows" / "release-models-on-integrate.yml"
_RELEASE_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "release-models.yml"


def test_finalize_refuses_to_commit_bucket_b() -> None:
    body = _finalize_step_body()
    # `_finalize_step_body` returns the YAML block scalar with its block
    # indentation already stripped, so the closing `fi` sits at column 0.
    match = re.search(r'if \[ "\$BUCKET" = "B" \]; then\n(.*?)\n *fi\n', body, re.DOTALL)
    assert match is not None, (
        "finalize must refuse Bucket B explicitly — expected an "
        '`if [ "$BUCKET" = "B" ]; then ... fi` guard'
    )
    guard = match.group(1)
    assert "exit 1" in guard, "the Bucket B guard must exit non-zero, not merely warn"
    assert "needs_human" in guard, (
        "the Bucket B guard must send a needs-human Slack reply so the refusal "
        "reaches a person instead of only the run log"
    )
    assert (
        "slack_terminal_sent" in guard
    ), "the Bucket B guard must set the dedup marker so the catch-all handler does not double-ping"


def test_bucket_b_guard_precedes_the_commit() -> None:
    body = _finalize_step_body()
    guard = body.index('if [ "$BUCKET" = "B" ]')
    commit = body.index("git commit -m")
    assert guard < commit, "the Bucket B refusal must run before anything is committed"


def test_commit_subject_no_longer_branches_on_bucket() -> None:
    body = _finalize_step_body()
    subject_block = body[body.index("COMMIT_SUBJECT=") : body.index("git commit -m")]
    assert "Bucket B" not in subject_block, (
        "Bucket B cannot reach the commit, so the subject must not offer it as an "
        "alternative — a dead branch here reads as if the old flow still exists"
    )


# ---------------------------------------------------------------------------
# Auto-release: an `integrate:` commit on main publishes the models wheel.
# ---------------------------------------------------------------------------


def test_release_trigger_fires_only_on_integrate_commits_touching_the_models_wheel() -> None:
    doc = yaml.safe_load(_RELEASE_TRIGGER_PATH.read_text())
    # `on` is the YAML 1.1 boolean True, not the string "on".
    push = doc[True]["push"]
    assert push["branches"] == ["main"], "auto-release must fire on main only"
    assert push["paths"] == [
        "tolokaforge_models/**"
    ], "a push that does not touch the models wheel has nothing to release"
    job = doc["jobs"]["release"]
    assert "startsWith(github.event.head_commit.message, 'integrate: ')" in job["if"], (
        "only an integration commit auto-releases; a hand edit to the models "
        "wheel stays on the manual workflow_dispatch"
    )


def test_release_trigger_requests_a_minor_bump() -> None:
    doc = yaml.safe_load(_RELEASE_TRIGGER_PATH.read_text())
    job = doc["jobs"]["release"]
    assert job["uses"] == "./.github/workflows/release-models.yml"
    assert job["with"]["bump"] == "minor", (
        "`integrate:` is not a Conventional Commit type so `auto` derives "
        "nothing; minor also keeps the patch axis free for fixing a model that "
        "shipped wrong"
    )
    assert job["with"]["dry_run"] is False


def test_release_workflow_is_callable_and_validates_the_increment() -> None:
    doc = yaml.safe_load(_RELEASE_WORKFLOW_PATH.read_text())
    triggers = doc[True]
    assert "workflow_call" in triggers, (
        "release-models.yml must be callable so the auto-release reuses it "
        "rather than duplicating the bump + tag body"
    )
    assert "workflow_dispatch" in triggers, "the manual release path must survive"
    body = _RELEASE_WORKFLOW_PATH.read_text()
    assert "unknown bump increment" in body, (
        "workflow_call passes `bump` as a free string (choice is dispatch-only), "
        "so an unrecognised value must fail loud instead of silently meaning auto"
    )


# ---------------------------------------------------------------------------
# The auto-merge path has to dispatch its own release.
#
# `gh pr merge` pushes with GITHUB_TOKEN, and a GITHUB_TOKEN push does not
# trigger `push`-event workflows (the Actions recursion guard this workflow
# already documents for the label path). So on the auto-merge path the push
# trigger never fires and the release must be dispatched explicitly.
# ---------------------------------------------------------------------------


def _report_step_body() -> str:
    doc = yaml.safe_load(_WORKFLOW_PATH.read_text())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str) and "gh pr merge" in run:
                return run
    raise AssertionError("no step invoking `gh pr merge` found")


def test_auto_merge_dispatches_the_models_release() -> None:
    body = _report_step_body()
    assert "gh workflow run release-models.yml" in body, (
        "a GITHUB_TOKEN squash-push cannot trigger the push-event release "
        "workflow, so the auto-merge path must dispatch the release itself"
    )
    assert "-f bump=minor" in body, "the dispatched release must request the minor increment"
    guard = re.search(r'if \[ "\$MERGED" = "yes" \]; then(.*?)\n *fi\n', body, re.DOTALL)
    assert guard is not None, "the dispatch must be gated on the merge having succeeded"
    assert "gh workflow run" in guard.group(1), (
        "the dispatch belongs inside the MERGED guard — dispatching when we did not "
        "merge would release a model that is not on main, and a human merge is "
        "already covered by the push trigger"
    )


def test_workflow_can_dispatch_workflows() -> None:
    doc = yaml.safe_load(_WORKFLOW_PATH.read_text())
    assert doc["permissions"].get("actions") == "write", (
        "`gh workflow run` needs `actions: write`; without it the release "
        "dispatch fails at the end of an otherwise successful integration"
    )


def test_generic_needs_human_defers_to_a_specific_handler() -> None:
    doc = yaml.safe_load(_WORKFLOW_PATH.read_text())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            name = step.get("name", "")
            run = step.get("run")
            if not isinstance(name, str) or not name.startswith("Flag needs-human"):
                continue
            if isinstance(run, str):
                assert "slack_terminal_sent" in run, (
                    "the generic needs-human notifier must skip when a specific "
                    "handler already spoke, or a precise message (e.g. the Bucket B "
                    "refusal) is followed by a generic one that is wrong about why "
                    "the run stopped"
                )
                assert run.index("slack_terminal_sent") < run.index(
                    "gh pr comment"
                ), "the dedup check must precede the PR comment, not only the Slack call"
                return
    raise AssertionError("no `Flag needs-human` step found")


def test_release_trigger_does_not_share_the_callee_concurrency_group() -> None:
    doc = yaml.safe_load(_RELEASE_TRIGGER_PATH.read_text())
    callee = yaml.safe_load(_RELEASE_WORKFLOW_PATH.read_text())
    caller_group = (doc.get("concurrency") or {}).get("group")
    callee_group = (callee.get("concurrency") or {}).get("group")
    assert callee_group == "release-models", "the callee is the one that serializes the lane"
    assert caller_group != callee_group, (
        "a caller sharing its callee's concurrency group contends with the job it "
        "is waiting on; the callee's group already serializes every entry point"
    )
