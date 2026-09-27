"""Behaviour lock for the terminal decisions that need no terminal.

Everything here runs without tmux, without Docker, and without a subprocess:
capture-range arithmetic, observation-header selection, status-line parsing
from recorded ``display-message`` stdout, keystroke-to-argv construction, and
the oversized-keystroke paste staging. The live terminal is exercised
separately in ``test_tmux_terminal_local.py``.

``test_previous_buffer_at_a_non_zero_offset_is_not_sliced_from_its_own_rfind``
is the regression lock for the upstream harbor bug this module was vendored to
fix; the rest of the file is the contract of what replaced it.
"""

from __future__ import annotations

import base64
import shlex

import pytest

from tolokaforge.tools.tmux_terminal.pure import (
    CURRENT_SCREEN_HEADER,
    EVICTED_MARKER,
    NEW_OUTPUT_HEADER,
    CaptureKind,
    PaneWatermark,
    PasteKey,
    SendKeysBatch,
    format_status_line,
    parse_pane_probe,
    plan_capture,
    plan_key_delivery,
    plan_paste,
    render_observation,
    trim_trailing_blank_lines,
)
from tolokaforge.tools.tmux_terminal.session import (
    ExecResult,
    TmuxTerminalSession,
    TmuxUnavailableError,
)

pytestmark = pytest.mark.unit

PANE_HEIGHT = 40
HISTORY_LIMIT = 20_000

SEND_PREFIX = ["tmux", "-L", "tf-trial", "send-keys", "-t", "trial", "--"]
TMUX_PREFIX = ["tmux", "-L", "tf-trial"]


# --------------------------------------------------------------------------
# Capture-range arithmetic
# --------------------------------------------------------------------------


def test_first_observation_has_no_watermark_and_shows_the_screen():
    plan = plan_capture(None, PaneWatermark(0, 3), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.FIRST
    assert plan.start_line == 0
    assert plan.captures_new_output is False


def test_no_growth_is_idle_not_an_empty_capture():
    previous = PaneWatermark(history_size=12, cursor_line=7)

    plan = plan_capture(previous, PaneWatermark(12, 7), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.IDLE
    assert plan.new_lines == 0
    assert plan.captures_new_output is False


def test_growth_inside_the_visible_pane_starts_at_the_old_cursor_row():
    """Five new lines below row 7 start the capture at row 7 — the row the
    cursor sat on last time, which may have been written only partially."""
    previous = PaneWatermark(history_size=0, cursor_line=7)

    plan = plan_capture(previous, PaneWatermark(0, 12), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.GROWTH
    assert plan.new_lines == 5
    assert plan.start_line == 7


def test_growth_that_scrolled_reaches_into_scrollback_with_a_negative_start():
    previous = PaneWatermark(history_size=10, cursor_line=39)
    # 100 more lines scrolled past: history 110, cursor pinned at the last row.
    plan = plan_capture(previous, PaneWatermark(110, 39), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.GROWTH
    assert plan.new_lines == 100
    assert plan.start_line == 39 - 100 == -61


def test_growth_start_never_addresses_a_line_older_than_retained_history():
    previous = PaneWatermark(history_size=0, cursor_line=0)

    plan = plan_capture(previous, PaneWatermark(5, 39), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.start_line == -5, "must not ask tmux for a line above the scrollback"


def test_saturated_scrollback_reports_eviction_rather_than_degrading_quietly():
    previous = PaneWatermark(history_size=HISTORY_LIMIT, cursor_line=39)

    plan = plan_capture(previous, PaneWatermark(HISTORY_LIMIT, 39), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.EVICTED
    assert plan.evicted is True
    assert plan.start_line == -HISTORY_LIMIT, "capture everything still retained"
    assert plan.new_lines == HISTORY_LIMIT + PANE_HEIGHT


def test_shrinking_scrollback_restarts_tracking_instead_of_going_negative():
    previous = PaneWatermark(history_size=900, cursor_line=20)

    plan = plan_capture(previous, PaneWatermark(0, 0), PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.RESET
    assert plan.start_line == 0


@pytest.mark.parametrize(("pane_height", "history_limit"), [(0, 100), (-1, 100), (40, 0)])
def test_non_positive_geometry_fails_loud(pane_height, history_limit):
    with pytest.raises(ValueError):
        plan_capture(None, PaneWatermark(0, 0), pane_height, history_limit)


def _harbor_find_new_content(previous_buffer: str, current_buffer: str) -> str | None:
    """The upstream harbor 0.23.0 ``_find_new_content``, verbatim.

    Reproduced here so the bug it contains is locked, not re-derived.
    """
    pb = "" if previous_buffer is None else previous_buffer.strip()
    if pb in current_buffer:
        idx = current_buffer.index(pb)
        if "\n" in pb:
            idx = pb.rfind("\n")  # overwrites an index into current_buffer
        return current_buffer[idx:]
    return None


def test_previous_buffer_at_a_non_zero_offset_is_not_sliced_from_its_own_rfind():
    """Regression lock: upstream mixed up two coordinate systems.

    ``idx`` is computed as an offset into ``current_buffer`` and then thrown
    away for ``pb.rfind("\\n")`` — an offset into the *previous* buffer. The
    two agree only while the previous buffer is a prefix of the current one.
    As soon as it appears at a non-zero offset (a pane that scrolled, a
    repeated prompt line), the slice starts in the wrong place and replays old
    output as if it were new.

    The watermark arithmetic that replaced it never looks at content at all,
    so it cannot be misled this way.
    """
    preamble = ["preamble-1", "preamble-2", "preamble-3"]
    seen = ["seen-a", "seen-b", "seen-c"]
    fresh = ["fresh-1", "fresh-2"]
    current_buffer = "\n".join(preamble + seen + fresh)
    previous_buffer = "\n".join(seen)

    harbor = _harbor_find_new_content(previous_buffer, current_buffer)
    assert harbor is not None
    assert current_buffer.index(previous_buffer) > 0, "the previous buffer is not a prefix"
    assert "preamble" in harbor, (
        "upstream sliced from an offset inside the previous buffer, landing in "
        "content the agent had already seen"
    )
    assert harbor.count("seen-a") == 1 and "fresh-1" in harbor

    # The same pane expressed as watermarks: nothing scrolled, and the cursor
    # advanced from the row below the last seen line to the row below the last
    # fresh one.
    seen_rows = len(preamble) + len(seen)
    previous = PaneWatermark(history_size=0, cursor_line=seen_rows)
    current = PaneWatermark(history_size=0, cursor_line=seen_rows + len(fresh))

    plan = plan_capture(previous, current, PANE_HEIGHT, HISTORY_LIMIT)

    assert plan.kind is CaptureKind.GROWTH
    assert plan.new_lines == len(fresh)
    assert plan.start_line == seen_rows, "start at the row the cursor last occupied"
    captured = current_buffer.split("\n")[plan.start_line :]
    assert captured == fresh, (
        "the capture reaches back exactly as far as the cursor moved, whatever "
        "the pane happens to contain"
    )
    assert "preamble" not in "\n".join(captured)


# --------------------------------------------------------------------------
# Observation rendering
# --------------------------------------------------------------------------


def test_new_output_selects_the_new_output_header():
    rendered = render_observation(
        new_output="build finished", screen="ignored", status_line="[terminal] x"
    )

    assert rendered.startswith(f"{NEW_OUTPUT_HEADER}\nbuild finished")
    assert CURRENT_SCREEN_HEADER not in rendered
    assert rendered.endswith("[terminal] x")


@pytest.mark.parametrize("new_output", ["", "   ", "\n\n"])
def test_absent_new_output_falls_back_to_the_screen_header(new_output):
    rendered = render_observation(
        new_output=new_output, screen="$ still building", status_line="[terminal] x"
    )

    assert rendered.startswith(f"{CURRENT_SCREEN_HEADER}\n$ still building")
    assert NEW_OUTPUT_HEADER not in rendered


def test_eviction_marker_precedes_the_output_it_qualifies():
    rendered = render_observation(
        new_output="tail of the log", screen="", status_line="[terminal] x", evicted=True
    )

    assert rendered.index(EVICTED_MARKER) < rendered.index(NEW_OUTPUT_HEADER)


def test_status_line_is_always_last_so_it_survives_a_long_screen():
    rendered = render_observation(
        new_output="x" * 5000, screen="", status_line="[terminal] tail", evicted=True
    )

    assert rendered.splitlines()[-1] == "[terminal] tail"


def test_trailing_blank_pane_rows_are_dropped_but_interior_ones_are_kept():
    assert trim_trailing_blank_lines("a\n\nb\n\n   \n\n") == "a\n\nb"
    assert trim_trailing_blank_lines("\n \n") == ""


# --------------------------------------------------------------------------
# Status line
# --------------------------------------------------------------------------


def test_probe_at_a_prompt_reports_idle_and_the_last_exit_code():
    status = parse_pane_probe("bash|0|12|5\n0\n")

    assert status is not None
    assert status.foreground == "bash"
    assert status.idle is True
    assert status.alternate_screen is False
    assert status.watermark == PaneWatermark(history_size=12, cursor_line=5)
    assert status.last_exit == 0
    assert format_status_line(status, 1.25) == (
        "[terminal] foreground=bash idle=yes last_exit=0 elapsed=1.2s"
    )


def test_probe_with_a_foreground_command_is_not_idle():
    status = parse_pane_probe("sleep|0|12|5\n130\n")

    assert status is not None
    assert status.idle is False
    assert status.last_exit == 130
    assert "foreground=sleep idle=no last_exit=130" in format_status_line(status, 9.0)


def test_alternate_screen_vetoes_idle_even_when_the_command_names_a_shell():
    status = parse_pane_probe("bash|1|8|0\n0\n")

    assert status is not None
    assert status.alternate_screen is True
    assert status.idle is False
    assert format_status_line(status, 0.5).endswith("alternate_screen=yes")


def test_elapsed_alone_keeps_two_identical_screens_distinguishable():
    """A poll on a long build returns the same pixels; a stuck detector hashing
    that into one signature is what forces the trial's score to zero."""
    status = parse_pane_probe("make|0|400|39\n")

    assert format_status_line(status, 12.0) != format_status_line(status, 30.0)


def test_missing_exit_code_line_degrades_to_a_question_mark():
    status = parse_pane_probe("vim|1|8|0\n")

    assert status is not None
    assert status.last_exit is None
    assert "last_exit=?" in format_status_line(status, 0.0)


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "\n",
        "no server running on /tmp/tmux-0/tf-trial\n",
        "can't find session: trial\n",
        "bash|0|12\n",
        "bash|0|twelve|5\n",
        "bash|0|-1|5\n",
        "|0|12|5\n",
    ],
    ids=[
        "empty",
        "blank-line",
        "no-server",
        "no-session",
        "too-few-fields",
        "non-numeric-history",
        "negative-history",
        "no-command",
    ],
)
def test_an_unusable_probe_is_none_rather_than_an_exception(stdout):
    assert parse_pane_probe(stdout) is None


def test_a_failed_probe_renders_every_field_unknown():
    assert format_status_line(None, 3.0) == (
        "[terminal] foreground=? idle=? last_exit=? elapsed=3.0s"
    )


def test_a_command_name_containing_a_pipe_cannot_shift_the_numeric_fields():
    status = parse_pane_probe("weird|name|0|12|5\n0\n")

    assert status is not None
    assert status.foreground == "weird|name"
    assert status.watermark == PaneWatermark(history_size=12, cursor_line=5)


def test_elapsed_never_renders_negative():
    assert "elapsed=0.0s" in format_status_line(None, -4.0)


# --------------------------------------------------------------------------
# Keystroke delivery
# --------------------------------------------------------------------------


def test_keys_are_argv_elements_after_the_end_of_options_marker():
    (step,) = plan_key_delivery(SEND_PREFIX, ["echo hi; rm -rf /", "Enter"])

    assert isinstance(step, SendKeysBatch)
    assert step.argv == (*SEND_PREFIX, "echo hi; rm -rf /", "Enter")
    assert step.argv[len(SEND_PREFIX) - 1] == "--", "`--` keeps a key like `-n` from parsing"


def test_a_key_of_pure_punctuation_survives_as_one_argument():
    (step,) = plan_key_delivery(SEND_PREFIX, ["'\"$(whoami)`\\"])

    assert isinstance(step, SendKeysBatch)
    assert step.argv[-1] == "'\"$(whoami)`\\"


def test_keys_are_batched_so_no_single_command_exceeds_the_tmux_limit():
    keys = ["y" * 200] * 60  # ~12 KB of quoted keys

    steps = plan_key_delivery(SEND_PREFIX, keys, max_command_bytes=2_000)

    assert len(steps) > 1
    for step in steps:
        assert isinstance(step, SendKeysBatch)
        assert len(shlex.join(step.argv).encode()) <= 2_000
    delivered = [key for step in steps for key in step.argv[len(SEND_PREFIX) :]]
    assert delivered == keys, "batching must not reorder or drop a keystroke"


def test_quoting_is_what_sizes_a_batch_not_raw_length():
    """A key of single quotes escapes to four bytes per character, so the
    conservative bound has to be the quoted length."""
    nasty = "'" * 300

    steps = plan_key_delivery(SEND_PREFIX, [nasty, nasty], max_command_bytes=2_000)

    assert len(steps) == 2, "two keys of 300 quotes cannot share a 2 KB command"


def test_an_oversized_key_is_pasted_and_keeps_its_place_in_the_order():
    huge = "Z" * 40_000

    steps = plan_key_delivery(SEND_PREFIX, ["cat <<'X'", "Enter", huge, "Enter"])

    assert [type(step) for step in steps] == [SendKeysBatch, PasteKey, SendKeysBatch]
    assert steps[1] == PasteKey(key=huge)
    assert steps[0].argv[len(SEND_PREFIX) :] == ("cat <<'X'", "Enter")
    assert steps[2].argv[len(SEND_PREFIX) :] == ("Enter",)


def test_a_prefix_that_leaves_no_room_for_keys_fails_loud():
    with pytest.raises(ValueError, match="leaves no room"):
        plan_key_delivery(SEND_PREFIX, ["x"], max_command_bytes=10)


def test_paste_stages_base64_and_pastes_it_into_the_named_session():
    key = "line one\nline two\n"

    plan = plan_paste(TMUX_PREFIX, "trial", key, "deadbeef")

    assert plan.path == "/tmp/.tolokaforge-tmux-paste-deadbeef"
    (stage,) = plan.stage_commands
    payload = stage.split("printf %s ")[1].split(" | ")[0]
    assert base64.b64decode(shlex.split(payload)[0]).decode() == key
    assert stage.endswith(f"> {plan.path}")
    assert "load-buffer -b tolokaforge-paste-deadbeef" in plan.paste_command
    assert "paste-buffer -d -b tolokaforge-paste-deadbeef -t trial" in plan.paste_command
    assert plan.cleanup_command == f"rm -f {plan.path}"


def test_paste_chunks_append_after_the_first_so_nothing_is_overwritten():
    plan = plan_paste(TMUX_PREFIX, "trial", "A" * 120, "cafe", chunk_len=16)

    assert len(plan.stage_commands) > 1
    assert plan.stage_commands[0].endswith(f"> {plan.path}")
    assert all(cmd.endswith(f">> {plan.path}") for cmd in plan.stage_commands[1:])
    joined = "".join(
        shlex.split(cmd.split("printf %s ")[1].split(" | ")[0])[0] for cmd in plan.stage_commands
    )
    assert base64.b64decode(joined).decode() == "A" * 120


def test_pasting_an_empty_key_still_produces_a_single_staging_step():
    plan = plan_paste(TMUX_PREFIX, "trial", "", "feed")

    assert len(plan.stage_commands) == 1


# --------------------------------------------------------------------------
# tmux verification — fails loud, never installs
# --------------------------------------------------------------------------


class _StubBackend:
    """A backend that answers every command with one canned result."""

    def __init__(self, result: ExecResult, location: str) -> None:
        self._result = result
        self._location = location
        self.calls: list[list[str]] = []

    @property
    def location(self) -> str:
        return self._location

    def exec(self, argv, timeout_s=30.0) -> ExecResult:
        self.calls.append(list(argv))
        return self._result

    def exec_shell(self, script, timeout_s=30.0) -> ExecResult:
        self.calls.append(["sh", "-c", script])
        return self._result


def test_a_missing_tmux_raises_naming_the_image_and_never_tries_to_install_it():
    backend = _StubBackend(
        ExecResult(returncode=127, stdout="", stderr="tmux: command not found"),
        location="container 'trial-1' (image 'tolokaforge-task:9')",
    )
    session = TmuxTerminalSession(backend, session_name="trial")

    with pytest.raises(TmuxUnavailableError) as excinfo:
        session.open()

    message = str(excinfo.value)
    assert "tolokaforge-task:9" in message
    assert "networking disabled" in message
    assert backend.calls == [["tmux", "-V"]], (
        "a missing tmux must stop at the check — upstream tried apt/dnf/apk and a "
        "source build, then continued regardless"
    )


def test_a_present_tmux_reports_its_version():
    backend = _StubBackend(
        ExecResult(returncode=0, stdout="tmux 3.4\n", stderr=""), location="this host"
    )

    assert TmuxTerminalSession(backend).verify_tmux() == "tmux 3.4"
