# Technical Reference

Consolidated reference for Tolokaforge configuration schemas, APIs, and tools.

## Table of Contents

1. [Configuration Schemas](#configuration-schemas)
2. [Python API](#python-api)
3. [Built-in Tools](#built-in-tools)
4. [Environment Services](#environment-services)

---

## Configuration Schemas

### run.yaml

```yaml
models:
  agent:
    provider: "openai"              # openai, anthropic, google, openrouter, azure, bedrock, ollama
    name: "gpt-4o-mini"             # Model name (provider-specific)
    temperature: 0.0                # 0.0 = deterministic
    max_tokens: 4096
    seed: 42                        # For reproducibility (OpenAI, Anthropic)

  user:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.7                # Higher for natural variation

orchestrator:
  workers: 4                        # Parallel worker threads
  repeats: 5                        # Trials per task (for pass@k)
  max_budget_usd: 100.0             # Optional hard spend limit
  max_requests_per_second: 2.0      # Optional global request throttle
  max_attempt_retries: 1            # Retry transient infra failures
  queue_backend: "sqlite"           # or "postgres" for distributed workers
  queue_postgres_dsn: null          # required when queue_backend="postgres"
  max_turns: 50

  timeouts:
    turn_s: 60                      # Per-turn timeout
    episode_s: 1200                 # Total episode timeout

  stuck_heuristics:
    max_repeated_tool_calls: 5      # Identical tool call threshold
    max_idle_turns: 8               # Turns without tool calls

evaluation:
  task_packs:
    - "/abs/path/private-pack-core"
    - "/abs/path/private-pack-mobile"
  tasks_glob: "**/task.yaml"
  output_dir: "results/run_001"
  metrics: [pass@1, pass@4, pass@8]
```

### task.yaml

```yaml
task_id: "unique_task_identifier"
name: "Human-readable task name"
category: "terminal"                # terminal, web, telecom, airline, etc.
description: |
  Detailed task description.

initial_state:
  json_db: "initial_state.json"     # JSON database seed
  filesystem:
    copy:
      - from: "fixtures/file.json"
        to: "/env/fs/agent-visible/file.json"
  mock_web:
    base_url: "http://mock-web:8080"
  rag:
    corpus_dir: "rag/corpus"

system_prompt: "../wiki.md"         # Custom system prompt (optional)

tools:
  agent:
    enabled: ["bash", "read_file", "write_file", "db_query"]
    mcp_server: "../mcp_server.py"  # Custom MCP tools (optional)
  user:
    enabled: []                     # User-side tools (dual-control)

user_simulator:
  mode: "llm"                       # "llm" or "scripted"
  persona: "cooperative"
  backstory: |
    Context for LLM user simulator...
  scripted_flow:                    # For scripted mode
    - if_assistant_contains: "name"
      user: "My name is Alice."
    - default: "Please proceed."

policies:
  disallowed_actions:
    - "Do not reset entire account"
  guidance:
    - "Explain steps before executing"

metadata:
  complexity: "hard"                # optional analytics slices
  expected_failure_modes: ["tool_selection", "grader_contract"]
  tags: ["multi_app", "long_horizon"]

grading: "grading.yaml"
```

### grading.yaml

```yaml
combine:
  method: "weighted"
  weights:
    state_checks: 0.6
    transcript_rules: 0.1
    llm_judge: 0.3
  pass_threshold: 0.8

state_checks:
  jsonpaths:
    - path: "$.users[?(@.id=='123')].verified"
      equals: true
    - path: "$.orders[-1].status"
      equals: "completed"
  hash:
    enabled: true
    expected_state_hash: "abc123..."  # SHA256 of normalized final state
    weight: 0.5

transcript_rules:
  must_contain: ["confirmation number"]
  disallow_regex: ["(?i)password"]
  max_turns: 40
  tool_expectations:
    required_tools: ["db_update"]
    disallowed_tools: ["bash"]

llm_judge:
  model_ref: "openai/gpt-4o"
  rubric: |
    Grading criteria here...
  output_schema:
    type: object
    properties:
      score: { type: number, minimum: 0, maximum: 1 }
      reasons: { type: string }
    required: ["score"]
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GOOGLE_API_KEY` | Google API key |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `AZURE_API_KEY` / `AZURE_API_BASE` | Azure OpenAI |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | AWS Bedrock |
| `OLLAMA_API_BASE` | Ollama endpoint (default: localhost:11434) |

---

## Python API

### Core Classes

#### Orchestrator

```python
from tolokaforge.core.orchestrator import Orchestrator

orchestrator = Orchestrator(
    config: Dict[str, Any],     # Run configuration
    output_dir: str = "output",
    workers: int = 1,
)

results = orchestrator.run()                    # Execute evaluation
results = orchestrator.resume(run_id="...")     # Resume partial run
errors = orchestrator.validate_tasks("glob")    # Validate without running
```

#### TrialRunner

```python
from tolokaforge.core.runner import TrialRunner

runner = TrialRunner(
    task_spec: TaskSpec,
    agent_client: LLMClient,
    user_simulator: UserSimulator,
    tool_executor: ToolExecutor,
    max_turns: int = 50,
    timeouts: Dict[str, float] = None,
    stuck_heuristics: Dict[str, Any] = None,
)

result = runner.run()  # Returns trajectory, metrics, final_state, grade
```

#### LLMClient

```python
from tolokaforge.core.model_client import LLMClient

client = LLMClient(
    provider: str = "anthropic",
    model_name: str = "claude-3-5-sonnet",
    temperature: float = 0.0,
    max_tokens: int = 4096,
    seed: Optional[int] = None,
)

response = client.generate(
    system="System prompt",
    messages=[{"role": "user", "content": "..."}],
    tools=[...],          # OpenAI function calling format
    tool_choice="auto",   # "auto", "none", or specific tool name
)
# Returns: text, tool_calls, token_usage, cost_usd
```

### Tool API

#### Tool Base Class

```python
from tolokaforge.tools.registry import Tool, ToolResult, ToolPolicy, ToolCategory

class MyTool(Tool):
    def __init__(self):
        policy = ToolPolicy(
            timeout_s=30.0,
            rate_limit=100,           # Max calls per trial
            category=ToolCategory.COMPUTE,
            visibility=["agent"],     # "agent", "user", or both
        )
        super().__init__(name="my_tool", description="...", policy=policy)

    def get_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {...}
            }
        }

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, output="result", error=None)
```

#### ToolRegistry

```python
from tolokaforge.tools.registry import ToolRegistry, get_registry

registry = get_registry()              # Global registry
registry.register(MyTool())            # Register tool
tool = registry.get_tool("my_tool")    # Get by name
schemas = registry.get_schemas(["bash", "read_file"])  # Get OpenAI schemas
```

### Grading API

```python
from tolokaforge.core.grading.combine import GradingEngine

engine = GradingEngine(
    grading_config: GradingConfig,
    judge_model: ModelConfig | None = None,
    task_domain: str = "telecom",
    task_dir: Path | None = None,
)

grade = engine.grade_trajectory(trajectory: Trajectory, final_env_state: dict)
# Returns: Grade with binary_pass, score, components, reasons, state_diff
```

---

## Built-in Tools

### File System

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `read_file` | Read file contents | `file_path` (relative to /env/fs/agent-visible/) |
| `write_file` | Write file contents | `file_path`, `content` |
| `list_dir` | List directory | `dir_path` |

### Database

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `db_query` | Query JSON DB with JSONPath | `jsonpath` (e.g., `$.users[?(@.id=='123')]`) |
| `db_update` | Update JSON DB | `ops` array with `{op, path, value}` |

**JSONPath syntax**: `$.field`, `$.array[0]`, `$.array[-1]`, `$[?(@.field=='value')]`

**Update operations**: `replace`, `add`, `remove`

### Web

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `browser` | Playwright web automation | `action` string + `x`, `y`, `text` (actions: click_at, type_text_at, scroll_document, select, navigate, etc.) |
| `http_request` | HTTP requests to mock services | `method`, `url`, `headers`, `body` |

### RAG

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `search_kb` | Hybrid search (BM25 + semantic) | `query`, `top_k`, `alpha` (0=keyword, 1=semantic) |

### Utility

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `bash` | Execute shell commands (restricted) | `command` |
| `calculator` | Safe arithmetic evaluation | `expression` |

---

## Environment Services

### JSON DB API

Base URL: `http://json-db:8000`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/reset` | POST | Initialize state (body: JSON object) |
| `/query` | POST | JSONPath query (body: `{jsonpath: "..."}`) |
| `/update` | POST | JSON Patch operations (body: `{ops: [...]}`) |
| `/dump` | GET | Get full normalized state |
| `/health` | GET | Health check |

### RAG Service API

Base URL: `http://rag-service:8001`

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/search` | POST | Search documents (body: `{query, top_k}`) |
| `/index` | POST | Build index (body: `{corpus_dir}`) |
| `/health` | GET | Health check |

### Mock Web API

Base URL: `http://mock-web:8080`

Routes defined per-task in `mock_web/routes.yaml`:

```yaml
routes:
  - path: /api/booking
    method: POST
    response:
      status: 201
      body: {"booking_id": "BK123", "status": "confirmed"}
```

---

## MCP Custom Tools

Create custom tools via Model Context Protocol:

```python
# mcp_server.py
import json
from typing import Any, Dict

_data = {}

def load_data(path: str):
    global _data
    with open(path) as f:
        _data = json.load(f)

def get_data() -> Dict[str, Any]:
    return _data  # For state sync

def get_tool_schema(tool_name: str) -> Dict[str, Any]:
    schemas = {
        "my_tool": {
            "type": "function",
            "function": {
                "name": "my_tool",
                "description": "...",
                "parameters": {...}
            }
        }
    }
    return schemas.get(tool_name, {})

def invoke_tool(tool_name: str, **kwargs) -> str:
    if tool_name == "my_tool":
        return json.dumps({"result": "..."})
    return json.dumps({"error": f"Unknown tool: {tool_name}"})

load_data("data/initial_state.json")
```

**Usage in task.yaml:**
```yaml
tools:
  agent:
    enabled: ["my_tool"]
    mcp_server: "../mcp_server.py"
```

---

## See Also

- [GRADING.md](GRADING.md) - Detailed grading system (hash algorithm, pass@k)
- [ADAPTER_ARCHITECTURE.md](ADAPTER_ARCHITECTURE.md) - Adapter architecture for task loading
- [CUSTOM_CHECKS.md](CUSTOM_CHECKS.md) - Custom Python validation
- [SECURITY.md](SECURITY.md) - Security model
