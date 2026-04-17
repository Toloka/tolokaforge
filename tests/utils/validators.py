"""Output validation utilities for test suite"""

import json
from pathlib import Path

import yaml


class ValidationError(Exception):
    """Raised when validation fails"""

    pass


def validate_trajectory(trajectory_path: Path) -> dict:
    """Validate trajectory file structure and return parsed content

    Args:
        trajectory_path: Path to trajectory file (json or yaml)

    Returns:
        Parsed trajectory dictionary

    Raises:
        ValidationError: If trajectory is invalid or missing
    """
    # Try YAML first, then JSON for backward compatibility
    yaml_path = trajectory_path.with_suffix(".yaml")
    json_path = trajectory_path.with_suffix(".json")

    trajectory = None
    trial_dir = trajectory_path.parent

    if yaml_path.exists():
        try:
            with open(yaml_path) as f:
                trajectory = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValidationError(f"Invalid YAML in trajectory: {e}")

        # For split YAML format, merge additional files for backward compatibility
        env_path = trial_dir / "env.yaml"
        if env_path.exists():
            with open(env_path) as f:
                trajectory["final_env_state"] = yaml.safe_load(f)

        metrics_path = trial_dir / "metrics.yaml"
        if metrics_path.exists():
            with open(metrics_path) as f:
                trajectory["metrics"] = yaml.safe_load(f)

        grade_path = trial_dir / "grade.yaml"
        if grade_path.exists():
            with open(grade_path) as f:
                trajectory["grade"] = yaml.safe_load(f)

        # tool_log not in split format, provide empty list
        trajectory.setdefault("tool_log", [])

    elif json_path.exists():
        try:
            with open(json_path) as f:
                trajectory = json.load(f)
        except json.JSONDecodeError as e:
            raise ValidationError(f"Invalid JSON in trajectory: {e}")
    else:
        raise ValidationError(
            f"Trajectory file not found: {trajectory_path} (tried .yaml and .json)"
        )

    # Validate required fields (tool_calls is optional - may be empty list or missing)
    required_fields = ["messages", "task_id"]
    missing = [f for f in required_fields if f not in trajectory]
    if missing:
        raise ValidationError(f"Missing required fields in trajectory: {missing}")

    return trajectory


def validate_metrics(
    output_dir: Path, expected_score_range: tuple[float, float] | None = None
) -> dict:
    """Validate aggregate metrics and return parsed content

    Args:
        output_dir: Path to output directory
        expected_score_range: Optional (min, max) tuple for score validation

    Returns:
        Parsed aggregate metrics dictionary

    Raises:
        ValidationError: If metrics are invalid or score out of range
    """
    aggregate_path = output_dir / "aggregate.json"

    if not aggregate_path.exists():
        raise ValidationError(f"Aggregate metrics not found: {aggregate_path}")

    try:
        with open(aggregate_path) as f:
            metrics = json.load(f)
    except json.JSONDecodeError as e:
        raise ValidationError(f"Invalid JSON in aggregate metrics: {e}")

    # Validate score if range provided
    if expected_score_range:
        min_score, max_score = expected_score_range

        # Try different possible score field names
        actual_score = metrics.get("overall_score") or metrics.get("avg_score_micro")

        if actual_score is None:
            # List available keys for debugging
            available = list(metrics.keys())
            raise ValidationError(
                f"No score field found in aggregate metrics. Available keys: {available}"
            )

        if not (min_score <= actual_score <= max_score):
            raise ValidationError(
                f"Score {actual_score} outside expected range [{min_score}, {max_score}]"
            )

    return metrics


def validate_tool_usage(
    trajectory: dict,
    required_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    min_tool_calls: int | None = None,
    max_tool_calls: int | None = None,
) -> bool:
    """Validate tool usage in trajectory

    Args:
        trajectory: Parsed trajectory dictionary
        required_tools: List of tools that must be used
        disallowed_tools: List of tools that must not be used
        min_tool_calls: Minimum number of tool calls expected
        max_tool_calls: Maximum number of tool calls expected

    Returns:
        True if validation passes

    Raises:
        ValidationError: If validation fails
    """
    # Try both tool_calls and tool_log (different trajectory formats)
    tool_calls = trajectory.get("tool_calls") or trajectory.get("tool_log", [])
    tools_used = {call.get("tool") for call in tool_calls if call.get("tool")}

    # Check required tools
    if required_tools:
        missing = set(required_tools) - tools_used
        if missing:
            raise ValidationError(f"Required tools not used: {missing}. Used: {tools_used}")

    # Check disallowed tools
    if disallowed_tools:
        forbidden = set(disallowed_tools) & tools_used
        if forbidden:
            raise ValidationError(f"Disallowed tools were used: {forbidden}")

    # Check tool call count
    num_calls = len(tool_calls)
    if min_tool_calls is not None and num_calls < min_tool_calls:
        raise ValidationError(f"Too few tool calls: {num_calls} < {min_tool_calls}")

    if max_tool_calls is not None and num_calls > max_tool_calls:
        raise ValidationError(f"Too many tool calls: {num_calls} > {max_tool_calls}")

    return True


def validate_output_files(
    output_dir: Path, expected_files: list[str], unexpected_files: list[str] | None = None
) -> bool:
    """Validate presence/absence of output files

    Args:
        output_dir: Path to output directory
        expected_files: List of files that must exist (relative to output_dir)
        unexpected_files: List of files that must not exist

    Returns:
        True if validation passes

    Raises:
        ValidationError: If validation fails
    """
    # Check expected files
    for file_path in expected_files:
        full_path = output_dir / file_path
        if not full_path.exists():
            raise ValidationError(f"Expected file not found: {file_path}")

    # Check unexpected files
    if unexpected_files:
        for file_path in unexpected_files:
            full_path = output_dir / file_path
            if full_path.exists():
                raise ValidationError(f"Unexpected file found: {file_path}")

    return True


def validate_grading_result(
    output_dir: Path,
    task_id: str,
    trial_num: int = 0,
    min_score: float = 0.0,
    max_score: float = 1.0,
) -> dict:
    """Validate complete grading result for a trial

    Args:
        output_dir: Path to output directory
        task_id: Task ID
        trial_num: Trial number (default: 0)
        min_score: Minimum expected score
        max_score: Maximum expected score

    Returns:
        Dict with trajectory and metrics

    Raises:
        ValidationError: If validation fails
    """
    trial_dir = output_dir / "trials" / task_id / str(trial_num)

    if not trial_dir.exists():
        raise ValidationError(f"Trial directory not found: {trial_dir}")

    # Validate trajectory (will try .yaml first, then .json)
    trajectory_path = trial_dir / "trajectory"  # No extension - validator will try both
    trajectory = validate_trajectory(trajectory_path)

    # Validate metrics
    metrics = validate_metrics(output_dir, expected_score_range=(min_score, max_score))

    return {"trajectory": trajectory, "metrics": metrics}
