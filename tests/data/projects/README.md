# Test Projects

Complete project snapshots for functional testing of tolokaforge.

## Overview

Test projects are full project structures copied from production/development environments to enable:
- Testing with **real MCP servers** (no mocks needed)
- Testing with **real task configurations**
- Testing with **real execution data** (trajectories)
- **Reproducible bug investigation** using actual scenarios

## Structure

```
tests/data/projects/
├── food_delivery_2/                    # Complete food delivery project
│   ├── tasks/                          # Task definitions
│   │   ├── order_six_items_golden/     # Golden-set grading task
│   │   │   ├── task.yaml
│   │   │   └── grading.yaml
│   │   └── order_modify_with_checks/   # Custom-checks grading task
│   │       ├── task.yaml
│   │       ├── grading.yaml
│   │       └── checks.py
│   ├── data/                           # Initial database files
│   │   ├── combined_initial_state.json
│   │   ├── users.json
│   │   ├── restaurants.json
│   │   └── [other data files]
│   ├── mcp_server.py                   # Real MCP server implementation
│   ├── tools_helpers.py                # Tool utilities
│   ├── check_helpers.py                # Custom check helpers
│   ├── wiki.md                         # System prompt
│   ├── tau_tools/                      # Tool implementations
│   └── output/                         # Test execution results
│       └── trials/
│           └── 051fa6cb-.../           # Golden trial for hash grading tests
│               └── 0/
│                   ├── trajectory.yaml
│                   ├── env.yaml
│                   ├── grade.yaml
│                   ├── metrics.yaml
│                   ├── logs.yaml
│                   └── task.yaml
├── tau_retail_mini/                    # Minimal Tau-style project
│   ├── tasks_test.py
│   ├── env.py
│   ├── types_local.py
│   ├── tools/
│   ├── data/
│   └── output/                         # Tau adapter test output
│       └── trials/
│           ├── 0f3b1ff7/
│           ├── test_001/
│           └── test_002/
└── README.md
```

## Usage

### Using in Tests

```python
from tests.utils.project_fixtures import (
    food_delivery_2_project,
    food_delivery_2_initial_state,
    food_delivery_2_mcp_server,
    load_project_trajectory,
)

def test_something(food_delivery_2_initial_state, food_delivery_2_mcp_server):
    """Test using real project data"""
    # Use real initial state
    state = copy.deepcopy(food_delivery_2_initial_state)
    
    # Use real MCP tools
    TOOLS = food_delivery_2_mcp_server.TOOLS
    result = TOOLS["create_order"].invoke(data=state, ...)
    
    # Assertions...
```

## Available Projects

### food_delivery_2
- **Domain**: Food delivery platform
- **Tasks**: 2 tasks (order_six_items_golden, order_modify_with_checks)
- **Tools**: 15+ MCP tools
- **Example Data**: Real trajectory from trial 051fa6cb (hash bug reproduction)
- **Use Case**: Testing golden set hash grading and custom checks

### tau_retail_mini
- **Domain**: Minimal Tau-style retail project
- **Tasks**: 3 test tasks
- **Tools**: None (minimal project for adapter testing)
- **Structure**: Tau-bench format (tasks_test.py, env.py, wiki.md)
- **Use Case**: Testing adapter implementation
- **Files**:
  - `env.py`: Environment marker file
  - `types_local.py`: Local Task/Action types (standalone)
  - `tasks_test.py`: 3 test tasks for adapter testing
  - `tools/`: Tool stubs

## Adding a New Test Project

### Step 1: Copy Project Structure

```bash
# Copy from production/development environment
cp -r path/to/project/tasks tests/data/projects/PROJECT_NAME/
cp path/to/project/mcp_server.py tests/data/projects/PROJECT_NAME/
cp -r path/to/project/data tests/data/projects/PROJECT_NAME/
cp path/to/project/wiki.md tests/data/projects/PROJECT_NAME/
```

### Step 2: Copy Example Output (Optional)

```bash
# Copy execution results for testing
mkdir -p tests/data/projects/PROJECT_NAME/output
cp -r output/PROJECT_test/trials/TASK_ID tests/data/projects/PROJECT_NAME/output/
```

### Step 3: Create Fixtures

```python
# In tests/utils/project_fixtures.py
@pytest.fixture
def project_name_project() -> Path:
    """Get path to project_name test project"""
    return TEST_PROJECTS_DIR / "project_name"

@pytest.fixture
def project_name_initial_state() -> Dict[str, Any]:
    """Load project_name initial state"""
    return load_project_initial_state("project_name")
```

### Step 4: Write Tests

```python
# In tests/canonical/test_my_feature.py
def test_something(project_name_initial_state, project_name_mcp_server):
    """Test using project"""
    state = copy.deepcopy(project_name_initial_state)
    TOOLS = project_name_mcp_server.TOOLS
    # ... test implementation
```

## Benefits Over Mocks

| Aspect | Mocks | Real Projects |
|--------|-------|---------------|
| **Maintenance** | Must keep mocks in sync | No maintenance needed |
| **Accuracy** | Risk of divergence | Always matches reality |
| **Testing** | Only tests against mocks | Tests against real code |
| **Debugging** | Must debug both mock and real | Debug only real code |
| **Setup** | Create mocks for each tool | Just copy project |
| **Updates** | Update mocks when tools change | Auto-updated with project |

## Guidelines

### DO:
- ✅ Copy complete projects (all files needed)
- ✅ Include example output/trajectories for bug reproduction
- ✅ Keep projects as-is (don't modify)
- ✅ Document project purpose in this README

### DON'T:
- ❌ Modify project files (defeats purpose)
- ❌ Add production secrets/keys
- ❌ Include large binary files
- ❌ Mix test and production data
- ❌ Commit generated output to git (optional data)

## Project Maintenance

### Updating a Project

```bash
# Re-copy from source
cp -r source/path tests/data/projects/PROJECT_NAME/
```

### Cleaning Up

```bash
# Remove generated output (can be regenerated)
rm -rf tests/data/projects/*/output/

# Remove old projects no longer needed
rm -rf tests/data/projects/deprecated_project/
```

## Integration with CI/CD

Test projects can be:
1. **Committed to repo** (recommended for bug reproduction)
2. **Downloaded in CI** (for large projects)
3. **Generated on-demand** (for dynamic testing)

For committed projects:
- Include `.gitignore` to exclude large generated files
- Include README explaining project purpose
- Include at least one example trajectory for bug reproduction

## References

- **Fixtures**: [`tests/utils/project_fixtures.py`](../../utils/project_fixtures.py)
- **Test Suite**: [`tests/canonical/`](../../canonical/)
- **Main Test README**: [`tests/README.md`](../../README.md)
