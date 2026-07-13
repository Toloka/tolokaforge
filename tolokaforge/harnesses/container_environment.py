"""Harbor-compatible container I/O backed by TolokaForge Docker primitives."""

from __future__ import annotations

from pathlib import Path

from tolokaforge.docker.container import Container, ExecResult


class AgentContainerEnvironment:
    """Expose the small exec/upload/download surface used by harness adapters.

    Container lifecycle and credentials remain owned by the caller. This class
    deliberately delegates to :class:`Container` instead of using the Docker
    SDK directly so resource policy, errors, and test doubles stay consistent.
    """

    def __init__(self, container: Container) -> None:
        self.container = container

    def exec(self, command: str | list[str]) -> ExecResult:
        """Execute a command in the running agent container."""

        return self.container.exec(command)

    def upload(self, source: str | Path, destination: str) -> None:
        """Upload one local file to an absolute container path."""

        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"Upload source is not a file: {source_path}")
        self.container.write_file(destination, source_path.read_bytes())

    def download(self, source: str, destination: str | Path) -> Path:
        """Download one container file to a local path and return that path."""

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(self.container.read_file(source))
        return destination_path
