"""Fixtures for working with test projects

Test projects are complete project snapshots in tests/data/projects/ with:
- Full task definitions
- MCP server implementations
- Initial data files
- Example output/trajectories (optional)

This allows testing with real project structures instead of mocks.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

# Base path to test projects
TEST_PROJECTS_DIR = Path(__file__).parent.parent / "data" / "projects"


@pytest.fixture
def test_projects_dir() -> Path:
    """Get path to test projects directory"""
    return TEST_PROJECTS_DIR


@pytest.fixture
def food_delivery_2_project() -> Path:
    """Get path to food_delivery_2 test project"""
    return TEST_PROJECTS_DIR / "food_delivery_2"


def load_project_task(project_name: str, task_id: str) -> dict[str, Any]:
    """Load task configuration from project

    Args:
        project_name: Name of project (e.g. "food_delivery_2")
        task_id: Task ID (e.g. "order_six_items_golden")

    Returns:
        Task configuration dict
    """
    task_path = TEST_PROJECTS_DIR / project_name / "tasks" / task_id / "task.yaml"
    with open(task_path) as f:
        return yaml.safe_load(f)


def load_project_grading(project_name: str, task_id: str) -> dict[str, Any]:
    """Load grading configuration from project

    Args:
        project_name: Name of project
        task_id: Task ID

    Returns:
        Grading configuration dict
    """
    grading_path = TEST_PROJECTS_DIR / project_name / "tasks" / task_id / "grading.yaml"
    with open(grading_path) as f:
        return yaml.safe_load(f)


def _is_lfs_pointer(path: Path) -> bool:
    """Check if a file is a Git LFS pointer (not actual content)."""
    try:
        # LFS pointers are small files (<200 bytes) starting with "version https://git-lfs"
        if path.stat().st_size > 512:
            return False
        content = path.read_text(encoding="utf-8", errors="replace")
        return content.startswith("version https://git-lfs")
    except OSError:
        return False


def load_project_trajectory(
    project_name: str, task_id: str, trial_index: int = 0
) -> dict[str, Any]:
    """Load trajectory from project output

    Args:
        project_name: Name of project
        task_id: Task ID
        trial_index: Trial number (default 0)

    Returns:
        Trajectory dict with messages, final_env_state, metrics, etc.

    Raises:
        pytest.skip: If trajectory data is a Git LFS pointer (not pulled)
    """
    # Try new structure first (with trials/ subdirectory)
    trial_dir_new = (
        TEST_PROJECTS_DIR / project_name / "output" / "trials" / task_id / str(trial_index)
    )
    # Fall back to old structure (without trials/ subdirectory)
    trial_dir_old = TEST_PROJECTS_DIR / project_name / "output" / task_id / str(trial_index)

    # Try YAML first (new split format), then JSON (old single file format)
    for trial_dir in [trial_dir_new, trial_dir_old]:
        yaml_path = trial_dir / "trajectory.yaml"
        json_path = trial_dir / "trajectory.json"

        # New split format (YAML) - merge files for backward compatibility
        if yaml_path.exists():
            if _is_lfs_pointer(yaml_path):
                pytest.skip(
                    f"Trajectory data is LFS pointer — run 'git lfs pull' first: {yaml_path}"
                )
            with open(yaml_path) as f:
                traj = yaml.safe_load(f)

            # Load and merge additional files
            env_path = trial_dir / "env.yaml"
            if env_path.exists():
                with open(env_path) as f:
                    traj["final_env_state"] = yaml.safe_load(f)

            metrics_path = trial_dir / "metrics.yaml"
            if metrics_path.exists():
                with open(metrics_path) as f:
                    traj["metrics"] = yaml.safe_load(f)

            grade_path = trial_dir / "grade.yaml"
            if grade_path.exists():
                with open(grade_path) as f:
                    traj["grade"] = yaml.safe_load(f)

            # tool_log not in split format, provide empty list
            traj.setdefault("tool_log", [])

            return traj

        # Old single file format (JSON)
        elif json_path.exists():
            if _is_lfs_pointer(json_path):
                pytest.skip(
                    f"Trajectory data is LFS pointer — run 'git lfs pull' first: {json_path}"
                )
            with open(json_path) as f:
                return json.load(f)

    raise FileNotFoundError(f"No trajectory file found for {project_name}/{task_id}/{trial_index}")


def load_project_initial_state(project_name: str) -> dict[str, Any]:
    """Load initial database state for project

    Args:
        project_name: Name of project

    Returns:
        Initial state dict
    """
    # Try combined state first
    combined_path = TEST_PROJECTS_DIR / project_name / "data" / "combined_initial_state.json"
    if combined_path.exists():
        with open(combined_path) as f:
            return json.load(f)

    # Fall back to loading individual JSON files
    data_dir = TEST_PROJECTS_DIR / project_name / "data"
    state = {}

    for json_file in data_dir.glob("*.json"):
        key = json_file.stem  # filename without .json
        with open(json_file) as f:
            state[key] = json.load(f)

    return state


def load_project_mcp_server(project_name: str):
    """Dynamically import MCP server module from project

    Args:
        project_name: Name of project

    Returns:
        MCP server module
    """
    import importlib.util

    server_path = TEST_PROJECTS_DIR / project_name / "mcp_server.py"

    spec = importlib.util.spec_from_file_location(f"test_project_{project_name}_mcp", server_path)
    if not spec or not spec.loader:
        raise ImportError(f"Could not load MCP server from {server_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


@pytest.fixture
def food_delivery_2_task_051fa6cb() -> dict[str, Any]:
    """Load task order_six_items_golden configuration"""
    return load_project_task("food_delivery_2", "order_six_items_golden")


@pytest.fixture
def food_delivery_2_grading_051fa6cb() -> dict[str, Any]:
    """Load grading config for task order_six_items_golden"""
    return load_project_grading("food_delivery_2", "order_six_items_golden")


@pytest.fixture
def food_delivery_2_trajectory_051fa6cb() -> dict[str, Any]:
    """Load trajectory for task order_six_items_golden (trial 051fa6cb).

    Skips automatically if trajectory data is a Git LFS pointer.
    """
    # Trial data is stored under the trial UUID, not the task name
    trial_uuid = "051fa6cb-a29e-4a0d-9ccf-e0f95802eee5"
    traj_dir = TEST_PROJECTS_DIR / "food_delivery_2" / "output" / "trials" / trial_uuid / "0"
    traj_path = traj_dir / "trajectory.yaml"
    if not traj_path.exists():
        pytest.skip("Trajectory data not available (missing file)")
    if _is_lfs_pointer(traj_path):
        pytest.skip("Trajectory data is LFS pointer — run 'git lfs pull' first")
    return load_project_trajectory("food_delivery_2", trial_uuid, 0)


@pytest.fixture
def food_delivery_2_initial_state() -> dict[str, Any]:
    """Load food_delivery_2 initial database state"""
    return load_project_initial_state("food_delivery_2")


@pytest.fixture
def food_delivery_2_mcp_server():
    """Load food_delivery_2 MCP server module"""
    return load_project_mcp_server("food_delivery_2")


# TlkMcpCore helper functions


def load_tlk_mcp_core_initial_state(project_name: str) -> dict[str, Any]:
    """Load initial database state for TlkMcpCore project

    TlkMcpCore projects store initial data in domain/internal_data.json

    Args:
        project_name: Name of project

    Returns:
        Initial state dict
    """
    internal_data_path = TEST_PROJECTS_DIR / project_name / "domain" / "internal_data.json"
    if internal_data_path.exists():
        with open(internal_data_path) as f:
            return json.load(f)

    return {}


def load_tlk_mcp_core_testcase(project_name: str, testcase_id: str) -> dict[str, Any]:
    """Load testcase from TlkMcpCore project

    Args:
        project_name: Name of project
        testcase_id: Testcase ID (e.g. "TC-001")

    Returns:
        Testcase dict
    """
    testcases_dir = TEST_PROJECTS_DIR / project_name / "domain" / "testcases"

    # Try exact filename match first
    for pattern in [f"{testcase_id}.json", f"testcase_{testcase_id.lower()}.json"]:
        testcase_path = testcases_dir / pattern
        if testcase_path.exists():
            with open(testcase_path) as f:
                return json.load(f)

    # Search by testcase_id field
    for testcase_file in testcases_dir.glob("*.json"):
        with open(testcase_file) as f:
            data = json.load(f)
            if data.get("testcase_id") == testcase_id:
                return data

    raise FileNotFoundError(f"Testcase {testcase_id} not found in {testcases_dir}")


def list_tlk_mcp_core_testcases(project_name: str) -> list[str]:
    """List all testcase IDs in a TlkMcpCore project

    Args:
        project_name: Name of project

    Returns:
        List of testcase IDs
    """
    testcases_dir = TEST_PROJECTS_DIR / project_name / "domain" / "testcases"
    testcase_ids = []

    if not testcases_dir.exists():
        return []

    for testcase_file in testcases_dir.glob("*.json"):
        with open(testcase_file) as f:
            data = json.load(f)
            testcase_id = data.get("testcase_id")
            if testcase_id:
                testcase_ids.append(testcase_id)

    return sorted(testcase_ids)


# Generic project fixtures that can be parameterized


@pytest.fixture
def project_task(request):
    """Parameterized fixture to load any project task

    Usage:
        @pytest.mark.parametrize("project_task", [
            ("food_delivery_2", "order_six_items_golden")
        ], indirect=True)
        def test_something(project_task):
            assert project_task["task_id"]
    """
    project_name, task_id = request.param
    return load_project_task(project_name, task_id)


@pytest.fixture
def project_trajectory(request):
    """Parameterized fixture to load any project trajectory

    Usage:
        @pytest.mark.parametrize("project_trajectory", [
            ("food_delivery_2", "order_six_items_golden", 0)
        ], indirect=True)
        def test_something(project_trajectory):
            assert "final_env_state" in project_trajectory
    """
    project_name, task_id, trial_index = request.param
    return load_project_trajectory(project_name, task_id, trial_index)


# Utility functions for test projects


def list_project_tasks(project_name: str) -> list[str]:
    """List all task IDs in a project

    Args:
        project_name: Name of project

    Returns:
        List of task IDs
    """
    tasks_dir = TEST_PROJECTS_DIR / project_name / "tasks"
    if not tasks_dir.exists():
        return []
    task_ids = []

    for task_dir in tasks_dir.iterdir():
        if task_dir.is_dir() and (task_dir / "task.yaml").exists():
            task_ids.append(task_dir.name)

    return sorted(task_ids)


def list_test_projects() -> list[str]:
    """List all available test projects

    Returns:
        List of project names
    """
    if not TEST_PROJECTS_DIR.exists():
        return []

    projects = []
    for project_dir in TEST_PROJECTS_DIR.iterdir():
        if project_dir.is_dir():
            projects.append(project_dir.name)

    return sorted(projects)


def get_project_output_trials(project_name: str, task_id: str) -> list[int]:
    """Get list of trial indices for a task

    Args:
        project_name: Name of project
        task_id: Task ID

    Returns:
        List of trial indices (e.g. [0, 1, 2])
    """
    output_dir = TEST_PROJECTS_DIR / project_name / "output" / task_id

    if not output_dir.exists():
        return []

    trials = []
    for trial_dir in output_dir.iterdir():
        if trial_dir.is_dir() and trial_dir.name.isdigit():
            trials.append(int(trial_dir.name))

    return sorted(trials)
