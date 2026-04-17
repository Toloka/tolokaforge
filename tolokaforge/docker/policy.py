"""Resource Policy module for Docker Foundation Layer.

Provides declarative resource limits and security constraints applied at container creation.
Uses Pydantic BaseModel for validation and serialization.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Capability(str, Enum):
    """Linux capabilities that can be added or dropped from containers.

    This enum provides type-safe capability names for use with cap_drop and cap_add.
    Values are uppercase strings matching Linux capability names.
    """

    ALL = "ALL"
    NET_BIND_SERVICE = "NET_BIND_SERVICE"
    NET_RAW = "NET_RAW"
    NET_ADMIN = "NET_ADMIN"
    SYS_PTRACE = "SYS_PTRACE"
    CHOWN = "CHOWN"
    DAC_OVERRIDE = "DAC_OVERRIDE"
    FOWNER = "FOWNER"
    SETGID = "SETGID"
    SETUID = "SETUID"
    SYS_ADMIN = "SYS_ADMIN"
    MKNOD = "MKNOD"


class ResourcePolicy(BaseModel):
    """Resource limits and security constraints for a container.

    This model defines the resource constraints and security settings that can be
    applied when creating a Docker container. All fields are optional with sensible
    defaults that prioritize security.

    Attributes:
        cpu_limit: CPU cores limit (e.g., 0.5, 2.0). None means no limit.
        memory_limit: Memory limit string (e.g., "256m", "1g"). None means no limit.
        timeout_s: Kill container after this many seconds. None means no timeout.
        cap_drop: Linux capabilities to drop (e.g., [Capability.ALL]).
        cap_add: Linux capabilities to add (e.g., [Capability.NET_BIND_SERVICE]).
        no_new_privileges: Prevent privilege escalation via setuid/setgid.
        read_only_rootfs: Mount root filesystem as read-only.

    Example:
        >>> policy = ResourcePolicy(
        ...     memory_limit="512m",
        ...     cpu_limit=1.0,
        ...     cap_drop=[Capability.ALL],
        ...     cap_add=[Capability.NET_BIND_SERVICE],
        ... )
        >>> policy.memory_limit
        '512m'
    """

    # Resource limits
    cpu_limit: float | None = Field(
        default=None,
        gt=0.0,
        description="CPU cores limit (e.g., 0.5 for half a core, 2.0 for two cores)",
    )
    memory_limit: str | None = Field(
        default=None,
        description="Memory limit in Docker format (e.g., '256m', '1g', '512000000')",
    )
    timeout_s: float | None = Field(
        default=None,
        gt=0.0,
        description="Kill container after this many seconds",
    )

    # Security constraints
    cap_drop: list[Capability] = Field(
        default_factory=list,
        description="Linux capabilities to drop (e.g., [Capability.ALL])",
    )
    cap_add: list[Capability] = Field(
        default_factory=list,
        description="Linux capabilities to add (e.g., [Capability.NET_BIND_SERVICE])",
    )
    no_new_privileges: bool = Field(
        default=True,
        description="Prevent privilege escalation via setuid/setgid binaries",
    )

    # Filesystem
    read_only_rootfs: bool = Field(
        default=False,
        description="Mount root filesystem as read-only",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }

    @field_validator("memory_limit")
    @classmethod
    def validate_memory_limit(cls, v: str | None) -> str | None:
        """Validate memory limit format.

        Accepts Docker memory format: number with optional suffix (b, k, m, g).
        Examples: '256m', '1g', '512000000', '100k'
        """
        if v is None:
            return v

        v = v.strip().lower()
        if not v:
            return None

        # Check for valid suffixes
        valid_suffixes = ("b", "k", "m", "g")
        if v[-1] in valid_suffixes:
            number_part = v[:-1]
        else:
            number_part = v

        # Validate the numeric part
        try:
            value = float(number_part)
            if value < 0:
                raise ValueError("Memory limit must be non-negative")
        except ValueError as e:
            raise ValueError(
                f"Invalid memory limit format: {v!r}. "
                "Expected format: number with optional suffix (b, k, m, g)"
            ) from e

        return v

    @field_validator("cap_drop", "cap_add", mode="before")
    @classmethod
    def validate_capabilities(cls, v: list[str | Capability]) -> list[Capability]:
        """Validate and normalize capability names.

        Accepts both Capability enum values and strings (converted to enum).
        """
        result = []
        for cap in v:
            if isinstance(cap, Capability):
                result.append(cap)
            else:
                # Convert string to Capability enum
                cap_upper = cap.upper()
                try:
                    result.append(Capability(cap_upper))
                except ValueError:
                    raise ValueError(
                        f"Invalid capability: {cap!r}. "
                        f"Valid capabilities: {[c.value for c in Capability]}"
                    )
        return result

    def to_docker_host_config(self) -> dict:
        """Convert policy to Docker SDK host_config parameters.

        Returns a dictionary suitable for passing to docker-py's
        create_container() or run() methods as host_config kwargs.

        Returns:
            Dictionary with Docker host configuration parameters.

        Example:
            >>> policy = ResourcePolicy(memory_limit="256m", cpu_limit=0.5)
            >>> config = policy.to_docker_host_config()
            >>> 'mem_limit' in config
            True
        """
        config: dict = {}

        if self.cpu_limit is not None:
            # Docker uses nano_cpus (1e9 = 1 CPU)
            config["nano_cpus"] = int(self.cpu_limit * 1e9)

        if self.memory_limit is not None:
            config["mem_limit"] = self.memory_limit

        if self.cap_drop:
            config["cap_drop"] = [cap.value for cap in self.cap_drop]

        if self.cap_add:
            config["cap_add"] = [cap.value for cap in self.cap_add]

        if self.no_new_privileges:
            config["security_opt"] = config.get("security_opt", []) + ["no-new-privileges:true"]

        if self.read_only_rootfs:
            config["read_only"] = True

        return config

    @classmethod
    def secure_default(cls) -> ResourcePolicy:
        """Create a policy with secure defaults.

        Returns a ResourcePolicy with all capabilities dropped and
        privilege escalation disabled. Suitable for untrusted workloads.

        Returns:
            ResourcePolicy with secure defaults.

        Example:
            >>> policy = ResourcePolicy.secure_default()
            >>> Capability.ALL in policy.cap_drop
            True
            >>> policy.no_new_privileges
            True
        """
        return cls(
            cap_drop=[Capability.ALL],
            no_new_privileges=True,
            read_only_rootfs=True,
        )

    @classmethod
    def executor_default(cls) -> ResourcePolicy:
        """Create a policy suitable for the executor container.

        Based on the docker-compose.yaml settings from PR #22:
        - Drops all capabilities
        - Adds NET_BIND_SERVICE for network binding
        - Prevents privilege escalation

        Returns:
            ResourcePolicy configured for executor containers.

        Example:
            >>> policy = ResourcePolicy.executor_default()
            >>> Capability.ALL in policy.cap_drop
            True
            >>> Capability.NET_BIND_SERVICE in policy.cap_add
            True
        """
        return cls(
            cap_drop=[Capability.ALL],
            cap_add=[Capability.NET_BIND_SERVICE],
            no_new_privileges=True,
        )

    def with_timeout(self, timeout_s: float) -> ResourcePolicy:
        """Return a new policy with the specified timeout.

        Args:
            timeout_s: Timeout in seconds.

        Returns:
            New ResourcePolicy with the timeout set.

        Example:
            >>> policy = ResourcePolicy()
            >>> policy_with_timeout = policy.with_timeout(30.0)
            >>> policy_with_timeout.timeout_s
            30.0
        """
        return self.model_copy(update={"timeout_s": timeout_s})

    def with_memory_limit(self, memory_limit: str) -> ResourcePolicy:
        """Return a new policy with the specified memory limit.

        Args:
            memory_limit: Memory limit in Docker format (e.g., "256m", "1g").

        Returns:
            New ResourcePolicy with the memory limit set.

        Example:
            >>> policy = ResourcePolicy()
            >>> policy_with_mem = policy.with_memory_limit("512m")
            >>> policy_with_mem.memory_limit
            '512m'
        """
        return self.model_copy(update={"memory_limit": memory_limit})

    def with_cpu_limit(self, cpu_limit: float) -> ResourcePolicy:
        """Return a new policy with the specified CPU limit.

        Args:
            cpu_limit: CPU cores limit (e.g., 0.5, 2.0).

        Returns:
            New ResourcePolicy with the CPU limit set.

        Example:
            >>> policy = ResourcePolicy()
            >>> policy_with_cpu = policy.with_cpu_limit(2.0)
            >>> policy_with_cpu.cpu_limit
            2.0
        """
        return self.model_copy(update={"cpu_limit": cpu_limit})
