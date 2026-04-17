# demo-recorder

Generate split-screen mobile demo videos from tolokaforge trajectories.

The left panel shows the phone viewport with tap/scroll indicators, while the
right panel displays the agent conversation with tool-call summaries.

## Prerequisites

- **Mock-web service** running on port 8080
- **JSON DB service** running on port 8000
- **ffmpeg** installed (for MP4 stitching)
- **Playwright Chromium** installed: `uv run playwright install --with-deps chromium`

## Installation

From the repository root:

```bash
uv sync
```

The tool is registered as a workspace member and will be installed automatically.

## Usage

### Record a single trajectory

```bash
demo-recorder record \
  --trajectory results/run/trials/task_name/0/trajectory.yaml \
  --output demos/task_name.mp4
```

Generate frames only (no MP4):

```bash
demo-recorder record \
  --trajectory results/run/trials/task_name/0/trajectory.yaml \
  --frames-only \
  --frames-dir demos/frames/task_name
```

### Batch-record all trajectories from a run

```bash
demo-recorder record-all \
  --run-dir results/mobile_run_20240101 \
  --output-dir demos \
  --workers 8
```

Auto-detect the latest run directory:

```bash
demo-recorder record-all --output-dir demos
```

## CLI Reference

```
demo-recorder record [OPTIONS]
  --trajectory PATH         Path to a trajectory.yaml file
  --frames-dir PATH         Directory to write individual PNG frames
  --output PATH             Output MP4 file path
  --mock-web-url TEXT       Base URL of the mock-web service
  --task-yaml PATH          Explicit path to task.yaml (auto-inferred when omitted)
  --frames-only             Capture PNG frames only and skip MP4 stitching
  --allow-default-start     Allow fallback start URL when task initial_app URL cannot be inferred

demo-recorder record-all [OPTIONS]
  --run-dir PATH            Run directory containing trials/*/0/trajectory.yaml
  --output-dir PATH         Destination for rendered MP4 files (default: demos)
  --workers INT             Number of parallel render workers (default: 8)
  --prefix TEXT             Run prefix for auto-detection (default: mobile_run_)
  --frames-root PATH        Root directory for per-task frame folders
  --keep-frames             Keep per-task frame folders under --frames-root
  --frames-only             Capture frames only; skip MP4 generation
```
