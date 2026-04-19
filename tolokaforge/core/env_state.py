"""Environment state management for tasks"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Union

from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import InitialStateConfig

# Extensions treated as binary (read/write as bytes, not text)
_BINARY_EXTENSIONS = frozenset(
    {
        ".xlsx",
        ".xls",
        ".docx",
        ".doc",
        ".pptx",
        ".ppt",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".zip",
        ".gz",
        ".tar",
        ".parquet",
        ".pickle",
        ".pkl",
    }
)


class EnvironmentState:
    """Manages environment state for a single trial"""

    def __init__(self, task_dir: Path, initial_state_config: InitialStateConfig):
        """
        Initialize environment state

        Args:
            task_dir: Directory containing the task files
            initial_state_config: Initial state configuration from task.yaml
        """
        self.task_dir = task_dir
        self.config = initial_state_config
        self.initial_db_state: dict[str, Any] = {}  # Store original for reset
        self.db_state: dict[str, Any] = {}
        self.filesystem_state: dict[str, Union[str, bytes]] = {}  # Maps dest path -> content
        self.initial_filesystem_state: dict[str, Union[str, bytes]] = {}  # Store original for reset
        self.logger = get_logger("env_state")

        # Service URLs — empty by default, set explicitly by executor/orchestrator
        # when the service is actually needed. Never default to Docker-internal URLs
        # to avoid leaking implementation details into task results.
        self.json_db_url: str = ""
        self.rag_service_url: str = ""
        self.mock_web_url: str = ""

        # File system paths (for tools)
        self.agent_visible_dir: Path = Path("/work")  # Agent's working directory
        self.hidden_dir: Path = task_dir / ".hidden"  # Hidden files (for grading)

        self.rag_corpus_dir: Path | None = None

    def hydrate(self) -> None:
        """Load initial state from configuration"""
        # Load JSON database
        if self.config.json_db:
            db_path = self.task_dir / self.config.json_db
            if db_path.exists():
                with open(db_path) as f:
                    self.initial_db_state = json.load(f)
                    # Create a deep copy for working state
                    self.db_state = deepcopy(self.initial_db_state)
            else:
                self.logger.warning("JSON DB file not found", path=str(db_path))
                self.initial_db_state = {}
                self.db_state = {}

        # Ensure db_state has consistent structure (device + surroundings mirrors user_db)
        self._normalize_db_state()

        # Apply per-task device state overrides (for τ² telecom tasks)
        if self.config.device_overrides and "device" in self.db_state:
            for key, value in self.config.device_overrides.items():
                self.db_state["device"][key] = value
            # Also update initial state so reset() works correctly
            for key, value in self.config.device_overrides.items():
                self.initial_db_state.setdefault("device", {})[key] = value

        # Keep normalized view after overrides
        self._normalize_db_state()

        # Load filesystem state
        if self.config.filesystem and self.config.filesystem.get("copy"):
            for file_spec in self.config.filesystem["copy"]:
                src_path = self.task_dir / file_spec["from"]
                dest_path = file_spec["to"]
                if src_path.exists():
                    if src_path.suffix.lower() in _BINARY_EXTENSIONS:
                        content: Union[str, bytes] = src_path.read_bytes()
                    else:
                        content = src_path.read_text(encoding="utf-8")
                    self.filesystem_state[dest_path] = content
                    self.initial_filesystem_state[dest_path] = content
                else:
                    self.logger.warning("Filesystem file not found", path=str(src_path))

        # Load mock web state (override URL if specified in config)
        if self.config.mock_web and self.config.mock_web.get("base_url"):
            self.mock_web_url = self.config.mock_web["base_url"]

        # Load RAG state (store corpus directory for reference)
        if self.config.rag and self.config.rag.get("corpus_dir"):
            corpus_dir = self.task_dir / self.config.rag["corpus_dir"]
            if corpus_dir.exists():
                self.rag_corpus_dir = corpus_dir
            else:
                self.logger.warning("RAG corpus directory not found", path=str(corpus_dir))

    def get_db(self) -> dict[str, Any]:
        """Get current database state (mutable reference)"""
        # Provide a normalized copy so downstream consumers always see device/surroundings at top level
        state = deepcopy(self.db_state)
        if "user_db" in state:
            user_db = state["user_db"] or {}
            if "device" not in state and "device" in user_db:
                state["device"] = deepcopy(user_db["device"])
            if "surroundings" not in state and "surroundings" in user_db:
                state["surroundings"] = deepcopy(user_db["surroundings"])
        return state

    def get_final_state(self) -> dict[str, Any]:
        """Get final environment state for grading

        Returns state in τ²-compatible format:
        - agent: Agent-side state (DB records: customers, lines, plans, devices, bills)
        - user: User-side state (current device state)
        - db: Legacy format (full db_state)
        - filesystem: File system state
        """
        db_copy = deepcopy(self.db_state)

        user_device = {}
        user_surroundings = {}

        if "user_db" in db_copy:
            user_db = db_copy.pop("user_db") or {}
            user_device = deepcopy(user_db.get("device", {}))
            user_surroundings = deepcopy(user_db.get("surroundings", {}))
        else:
            user_device = deepcopy(db_copy.pop("device", {}))
            user_surroundings = (
                deepcopy(db_copy.pop("surroundings", {})) if "surroundings" in db_copy else {}
            )

        # Everything else is agent state (DB records)
        agent_state = db_copy

        # Build τ²-compatible state structure
        state = {
            "agent": agent_state,  # DB records: customers, lines, plans, devices, bills
            "user": {"device": user_device},  # Current device state
            "db": deepcopy(self.db_state),  # Legacy format for backward compatibility
            "filesystem": deepcopy(self.filesystem_state),
        }

        if user_surroundings:
            state["user"]["surroundings"] = user_surroundings

        # Add mock web URL if configured (actual state would come from service)
        if self.mock_web_url:
            state["mock_web_url"] = self.mock_web_url

        # Add RAG corpus info if configured
        if self.rag_corpus_dir:
            state["rag_corpus_dir"] = str(self.rag_corpus_dir)

        return state

    def reset(self) -> None:
        """Reset environment to initial state (for new trial)"""
        # Reset database to pristine initial state
        self.db_state = deepcopy(self.initial_db_state)

        # Ensure normalized view is restored on reset
        self._normalize_db_state()

        # Reset filesystem state
        self.filesystem_state = deepcopy(self.initial_filesystem_state)

        # Note: Mock web and RAG services would need to be reset via their APIs
        # For now we just track their configuration
        # In Docker mode, services would be reset via HTTP endpoints

    def _logical_fs_to_relative(self, logical_path: str) -> Path:
        """Map logical task filesystem path to a relative path under agent_visible_dir."""
        normalized = logical_path.strip()
        if normalized.startswith("/env/fs/agent-visible/"):
            normalized = normalized[len("/env/fs/agent-visible/") :]
        elif normalized == "/env/fs/agent-visible":
            normalized = ""
        elif normalized.startswith("/work/"):
            normalized = normalized[len("/work/") :]
        elif normalized == "/work":
            normalized = ""
        elif normalized.startswith("/"):
            normalized = normalized.lstrip("/")
        return Path(normalized)

    def materialize_filesystem_to_disk(self) -> None:
        """Materialize logical filesystem state into agent_visible_dir.

        Used by the Runner service to prepare the filesystem for tool execution.
        """
        self.agent_visible_dir.mkdir(parents=True, exist_ok=True)
        for logical_path, content in self.filesystem_state.items():
            rel_path = self._logical_fs_to_relative(logical_path)
            out_path = (self.agent_visible_dir / rel_path).resolve()
            if not str(out_path).startswith(str(self.agent_visible_dir.resolve())):
                self.logger.warning(
                    "Skipping filesystem entry outside agent directory", path=logical_path
                )
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                out_path.write_bytes(content)
            else:
                out_path.write_text(content, encoding="utf-8")

    def sync_filesystem_from_disk(self) -> None:
        """Sync agent-visible disk files back into logical filesystem state for grading."""
        if not self.agent_visible_dir.exists():
            return
        prefix = "/env/fs/agent-visible"
        for file_path in self.agent_visible_dir.rglob("*"):
            if not file_path.is_file():
                continue
            try:
                rel = file_path.relative_to(self.agent_visible_dir).as_posix()
            except Exception:
                continue
            logical_key = f"{prefix}/{rel}" if rel else prefix
            if file_path.suffix.lower() in _BINARY_EXTENSIONS:
                self.filesystem_state[logical_key] = file_path.read_bytes()
            else:
                try:
                    self.filesystem_state[logical_key] = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    self.filesystem_state[logical_key] = file_path.read_bytes()

    def _normalize_db_state(self) -> None:
        """Ensure helper views like device/surroundings exist alongside user_db."""
        if not self.db_state:
            return

        user_db = self.db_state.get("user_db")
        if isinstance(user_db, dict):
            device = user_db.get("device")
            if device is not None:
                self.db_state["device"] = deepcopy(device)
            surroundings = user_db.get("surroundings")
            if surroundings is not None:
                self.db_state["surroundings"] = deepcopy(surroundings)
