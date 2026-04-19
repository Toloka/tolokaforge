"""Orchestrator for managing runs and workers"""

import json
import logging
import os
import socket
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Any

from tolokaforge.adapters import BaseAdapter, get_adapter
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.env_state import EnvironmentState
from tolokaforge.core.failure_attribution import (
    attribute_failure,
    is_failed_trajectory,
    summarize_failure_attributions,
)
from tolokaforge.core.logging import get_logger
from tolokaforge.core.metrics import (
    calculate_aggregate_metrics,
    calculate_latency_percentiles,
    calculate_task_metrics,
)
from tolokaforge.core.model_client import LLMClient, UserSimulator
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    ModelConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
    TypeSenseConfig,
)
from tolokaforge.core.output_writer import OutputWriter
from tolokaforge.core.rate_limiter import GlobalRateLimiter
from tolokaforge.core.resume import RunStateManager
from tolokaforge.core.run_queue import AttemptLease, create_run_queue
from tolokaforge.core.runner import TrialRunner
from tolokaforge.core.stuck import StuckDetector


class Orchestrator:
    """Orchestrates benchmark runs across tasks and trials"""

    def __init__(
        self, config: RunConfig, resume: bool = False, verbose: bool = False, strict: bool = False
    ):
        self.config = config
        self.resume = resume
        self.verbose = verbose
        self.strict = strict
        self.tasks: list[TaskConfig] = []
        self.results: list[Trajectory] = []
        self.state_manager: RunStateManager | None = None
        self.adapter: BaseAdapter | None = None

        # Initialize logger
        log_level = logging.DEBUG if verbose else logging.INFO
        self.logger = get_logger("orchestrator", level=log_level, strict=strict)

        # Configure standard Python logging for Docker modules so their
        # progress messages (image building, container startup, health checks)
        # are visible to the user.  These modules use logging.getLogger(__name__)
        # which defaults to WARNING without explicit configuration.
        docker_logger = logging.getLogger("tolokaforge.docker")
        if not docker_logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            docker_logger.addHandler(handler)
        docker_logger.setLevel(log_level)

    def _create_adapter(self) -> BaseAdapter:
        """Create adapter based on configuration"""
        adapter_config = self.config.evaluation.harness_adapter

        if adapter_config:
            adapter_type = adapter_config.type
            params = adapter_config.params.copy()
        else:
            adapter_type = "native"
            params = {}

        # Add tasks_glob to params for both native and other adapters
        params["tasks_glob"] = self.config.evaluation.tasks_glob
        task_packs = list(self.config.evaluation.task_packs)

        # In Docker flows, TASK_PACKS_DIRS can override config paths to container-visible mounts.
        env_task_packs = os.environ.get("TASK_PACKS_DIRS", "").strip()
        if env_task_packs:
            task_packs = [part.strip() for part in env_task_packs.split(",") if part.strip()]
        params["task_packs"] = task_packs

        # Pass TypeSense config to adapter if configured
        typesense_config = self.config.orchestrator.typesense
        if typesense_config and typesense_config.enabled:
            params["typesense"] = typesense_config.model_dump()

        self.logger.info("Creating adapter", type=adapter_type, params=params)
        return get_adapter(adapter_type, params)

    @staticmethod
    def _collect_existing_cost(output_dir: Path) -> float:
        """Aggregate already-recorded trial cost from output artifacts."""
        total_cost = 0.0
        trials_root = output_dir / "trials"
        if not trials_root.exists():
            return total_cost

        import yaml

        for metrics_path in trials_root.glob("*/*/metrics.yaml"):
            try:
                with open(metrics_path) as f:
                    metrics = yaml.safe_load(f) or {}
                total_cost += float(metrics.get("cost_usd_est", 0.0) or 0.0)
            except Exception:
                continue
        return total_cost

    @staticmethod
    def _is_retryable_trajectory(trajectory: Trajectory) -> bool:
        """Classify retryable infrastructure failures."""
        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            return True
        if trajectory.termination_reason in (
            TerminationReason.RATE_LIMIT,
            TerminationReason.API_ERROR,
            TerminationReason.TIMEOUT,
            TerminationReason.ERROR,
        ):
            return True
        return False

    @staticmethod
    def _safe_get_pending(run_queue: Any) -> int:
        """Get pending count from run queue, returning -1 if DB is unreachable."""
        try:
            return run_queue.get_counts().get("pending", 0)
        except Exception:
            return -1

    def _ensure_typesense_started(self) -> None:
        """Start TypeSense server if configured for local mode.

        This must be called before adapter creation to ensure the adapter
        gets resolved port/api_key values.
        """
        typesense_config = self.config.orchestrator.typesense
        if typesense_config and typesense_config.enabled and typesense_config.mode == "local":
            # Check if already resolved (port is int, not "auto")
            if typesense_config.port == "auto" or typesense_config.api_key is None:
                try:
                    from tolokaforge.core.search.typesense_server import create_typesense_server

                    self.logger.info(
                        "Starting local TypeSense server", config=typesense_config.model_dump()
                    )
                    # Create server with individual params from config
                    self._typesense_server = create_typesense_server(
                        port=typesense_config.port,
                        api_key=typesense_config.api_key,
                        data_dir=typesense_config.data_dir,
                        image=typesense_config.image,
                        container_name=typesense_config.container_name,
                        timeout=typesense_config.timeout,
                        cleanup_on_exit=typesense_config.cleanup_on_exit,
                    )
                    if self._typesense_server:
                        self._typesense_server.start()
                        # Update config object with resolved port/api_key for adapter use
                        resolved_config = typesense_config.model_dump()
                        resolved_config["port"] = self._typesense_server.port
                        resolved_config["api_key"] = self._typesense_server.api_key
                        self.config.orchestrator.typesense = TypeSenseConfig(**resolved_config)
                        self.logger.info(
                            "TypeSense server started",
                            host=self._typesense_server.host,
                            port=self._typesense_server.port,
                        )
                    else:
                        raise RuntimeError(
                            "TypeSense server could not be created (Docker not available?). "
                            "TypeSense is configured as enabled; aborting to avoid silent failures."
                        )
                except ImportError as e:
                    raise RuntimeError(
                        f"TypeSense is configured but the server module is not available: {e}"
                    ) from e
                except Exception as e:
                    raise RuntimeError(f"Failed to start TypeSense server: {e}") from e

    def _connect_typesense_to_runner_network(self, service_stack: Any) -> None:
        """Connect TypeSense container to the core stack's Docker network.

        After core_stack starts, the Runner is on 'runner-net'. TypeSense is on
        its own network. We connect TypeSense to runner-net so the Runner can
        reach it via Docker DNS (container name / alias).

        The container port is ALWAYS 8108 inside Docker networks — only the
        host-mapped port differs, and that is irrelevant for inter-container
        communication.
        """
        try:
            import docker as docker_lib

            client = docker_lib.from_env()

            # Get TypeSense container from its stack
            ts_stack = self._typesense_server._stack
            ts_container_obj = ts_stack._containers.get("typesense") if ts_stack else None

            if ts_container_obj is None:
                self.logger.warning("TypeSense container not found for network bridging")
                return

            ts_container_id = ts_container_obj.container_id
            ts_container = client.containers.get(ts_container_id)

            # Get the runner-net network from the core stack
            runner_net = service_stack._networks.get("runner-net")
            if runner_net is None:
                self.logger.warning("Runner network not found for TypeSense bridging")
                return

            docker_network = client.networks.get(runner_net.network_id)

            # Connect TypeSense to runner-net with an alias so it is reachable
            # as "typesense:8108" inside the network.
            docker_network.connect(ts_container, aliases=["typesense"])
            self.logger.info(
                "Connected TypeSense to runner network",
                network=runner_net.name,
                container=ts_container.name,
            )

            # Update TypeSense config to use Docker DNS name for Runner access.
            # Inside Docker networks, containers use the container port (8108)
            # directly — not the host-mapped port.
            typesense_config = self.config.orchestrator.typesense
            if typesense_config:
                resolved_config = typesense_config.model_dump()
                resolved_config["host"] = "typesense"
                resolved_config["port"] = 8108
                self.config.orchestrator.typesense = TypeSenseConfig(**resolved_config)
                self.logger.info(
                    "Updated TypeSense config for Docker networking",
                    host="typesense",
                    port=8108,
                )

                # Propagate Docker-internal connection details to the adapter
                # so that to_task_description() puts Docker-reachable values
                # (typesense:8108) into SearchConfig rather than host-side ones.
                if self.adapter and hasattr(self.adapter, "params"):
                    self.adapter.params["typesense"] = resolved_config
                    self.logger.debug(
                        "Propagated TypeSense Docker config to adapter",
                        host="typesense",
                        port=8108,
                    )

        except Exception as e:
            self.logger.warning("Failed to connect TypeSense to runner network", error=str(e))

    def load_tasks(self) -> None:
        """Load tasks using configured adapter"""
        # Ensure TypeSense is started BEFORE adapter creation
        # This allows the adapter to get resolved port/api_key
        if not hasattr(self, "_typesense_server"):
            self._typesense_server = None
        self._ensure_typesense_started()

        # Create adapter if not already created
        if self.adapter is None:
            self.adapter = self._create_adapter()

        # Get task IDs from adapter
        task_ids = self.adapter.get_task_ids()

        # Load each task
        for task_id in task_ids:
            try:
                task = self.adapter.get_task(task_id)
                self.tasks.append(task)
            except Exception as e:
                self.logger.error("Failed to load task", task_id=task_id, error=str(e))

        self.logger.info("Tasks loaded", count=len(self.tasks), adapter=type(self.adapter).__name__)

    def run(self) -> None:
        """Execute all tasks with configured trials"""
        # Add timestamp to output directory for unique runs
        base_output_dir = self.config.evaluation.output_dir
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path(f"{base_output_dir}_{timestamp}")
        output_dir.mkdir(parents=True, exist_ok=True)

        # Ensure TypeSense is started and tasks are loaded
        if not self.tasks:
            self.load_tasks()

        # Initialize resume state manager
        self.state_manager = RunStateManager(output_dir)

        # Check for existing run state
        run_state = None
        if self.resume:
            run_state = self.state_manager.load_state()
            if run_state:
                resume_info = self.state_manager.get_resume_info()
                if resume_info:
                    self.logger.info(
                        "Resuming run",
                        run_id=run_state.run_id,
                        completed=resume_info["completed_trials"],
                        total=resume_info["total_trials"],
                        pending=resume_info["pending_trials"],
                        failed=resume_info["failed_trials"],
                    )
            else:
                self.logger.info("No resumable run found, starting fresh")
                self.resume = False

        # Initialize new run state if not resuming
        if not run_state:
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            task_ids = [task.task_id for task in self.tasks]
            run_state = self.state_manager.initialize_run(
                run_id=run_id,
                config_path=str(self.config.evaluation.output_dir),
                task_ids=task_ids,
                repeats=self.config.orchestrator.repeats,
            )
            self.logger.info(
                "Starting new run",
                run_id=run_id,
                tasks=len(task_ids),
                repeats=self.config.orchestrator.repeats,
                total_trials=run_state.total_trials,
            )

        # Create agent and user clients
        agent_config = self.config.models.get("agent")
        user_config = self.config.models.get("user")

        if not agent_config:
            self.logger.error("Agent model configuration required")
            raise ValueError("Agent model configuration required")

        # Apply default user model if not configured
        if user_config is None:
            user_config = ModelConfig(
                provider="openrouter",
                name="anthropic/claude-sonnet-4.6",
                temperature=0.2,
            )
            self.logger.info(
                "Using default user model",
                user_model="openrouter/anthropic/claude-sonnet-4.6",
            )

        # Log model configuration for both roles
        self.logger.info(
            "Model configuration",
            agent_model=f"{agent_config.provider}/{agent_config.name}",
            user_model=f"{user_config.provider}/{user_config.name}",
        )

        # Instantiate agent client in orchestrator process
        agent_client = LLMClient(agent_config)
        request_limiter: GlobalRateLimiter | None = None
        if self.config.orchestrator.max_requests_per_second is not None:
            request_limiter = GlobalRateLimiter(self.config.orchestrator.max_requests_per_second)
            self.logger.info(
                "Global request limiter enabled",
                max_requests_per_second=self.config.orchestrator.max_requests_per_second,
            )

        # Auto-start services via ServiceStack if configured
        service_stack = None
        if self.config.orchestrator.auto_start_services:
            try:
                from tolokaforge.docker.stacks import core_stack

                self.logger.info("Auto-starting Docker services via ServiceStack")

                # Detect required Docker features from task configs
                needs_playwright = any(
                    "browser" in (t.tools.agent.get("enabled", []) if t.tools else [])
                    for t in self.tasks
                )
                if needs_playwright:
                    self.logger.info("Browser tool detected in tasks — enabling Playwright")

                # Detect if any task needs the mock-web service
                needs_mock_web = any(
                    t.initial_state.mock_web is not None
                    and t.initial_state.mock_web.get("base_url")
                    for t in self.tasks
                    if t.initial_state
                )
                if needs_mock_web:
                    self.logger.info("Mock-web detected in tasks — enabling mock-web service")

                # Collect task pack paths for mock-web file serving
                task_packs = self.config.evaluation.task_packs if self.config.evaluation else None

                self.logger.info("Creating service stack (db-service + runner)")
                service_stack = core_stack(
                    enable_playwright=needs_playwright,
                    enable_mock_web=needs_mock_web,
                    task_packs=task_packs,
                )
                self.logger.info(
                    "Building Docker images and starting containers "
                    "(this may take a few minutes on first run)..."
                )
                service_stack.start_all(wait=True)
                # Use localhost address — the orchestrator runs on the host,
                # not inside Docker, so Docker container names don't resolve.
                runner_url = service_stack.get_service_url("runner", 50051)
                # get_service_url returns "http://localhost:{port}" — strip scheme for gRPC
                runner_address = runner_url.replace("http://", "")
                self.logger.info("ServiceStack started", runner_address=runner_address)

                # Connect TypeSense to core stack network so Runner can reach it
                if hasattr(self, "_typesense_server") and self._typesense_server:
                    self._connect_typesense_to_runner_network(service_stack)
            except Exception as e:
                self.logger.error("Failed to auto-start services", error=str(e))
                raise
        else:
            runner_address = None

        # Docker runtime (always used)
        from tolokaforge.core.docker_runtime import DockerRuntime

        if runner_address is None:
            runner_address = os.environ.get("EXECUTOR_ADDRESS", "executor:50051")

        docker_runtime = DockerRuntime(runner_address=runner_address)
        docker_runtime.connect()
        self.logger.info("Docker runtime connected")

        executor_healthy = docker_runtime.health_check()
        self.logger.info("Docker runtime health check", executor_healthy=executor_healthy)

        # Build pending task/trial pairs and initialize durable queue.
        pending_trials: list[tuple[str, int]] = []
        task_by_id = {task.task_id: task for task in self.tasks}
        for task in self.tasks:
            for trial_idx in range(self.config.orchestrator.repeats):
                # Skip if already completed
                if self.resume and self.state_manager.is_completed(task.task_id, trial_idx):
                    self.logger.info(
                        "Skipping completed trial", task_id=task.task_id, trial_index=trial_idx
                    )
                    continue
                pending_trials.append((task.task_id, trial_idx))

        run_queue = create_run_queue(
            self.config.orchestrator.queue_backend,
            sqlite_path=output_dir / "run_queue.sqlite",
            max_retries=self.config.orchestrator.max_attempt_retries,
            postgres_dsn=self.config.orchestrator.queue_postgres_dsn,
        )
        run_queue.enqueue_many(pending_trials)
        recovered = run_queue.recover_inflight(
            max_lease_age_s=max(300, self.config.orchestrator.timeouts.episode_s * 2)
        )
        if recovered > 0:
            self.logger.warning("Recovered stale in-flight attempts", recovered=recovered)

        budget_limit = self.config.orchestrator.max_budget_usd
        total_cost_usd = self._collect_existing_cost(output_dir)
        budget_exhausted = False
        total_trials_scheduled = len(pending_trials)
        if total_cost_usd > 0:
            self.logger.info("Loaded existing run spend", total_cost_usd=round(total_cost_usd, 6))
        if budget_limit is not None and total_cost_usd >= budget_limit:
            budget_exhausted = True
            self.logger.warning(
                "Budget already exhausted at run start; no trials will be scheduled",
                budget_limit_usd=budget_limit,
                total_cost_usd=round(total_cost_usd, 6),
            )

        lease_seconds = max(300, self.config.orchestrator.timeouts.episode_s * 2)
        lease_owner = f"orchestrator:{os.getpid()}"

        # Run tasks with parallel workers using the durable queue.
        with ThreadPoolExecutor(max_workers=self.config.orchestrator.workers) as executor:
            active_futures: dict[Any, AttemptLease] = {}

            def submit_one() -> bool:
                if budget_exhausted:
                    return False
                lease = run_queue.lease_next(worker_id=lease_owner, lease_seconds=lease_seconds)
                if lease is None:
                    return False
                task = task_by_id.get(lease.task_id)
                if task is None:
                    # Should never happen; fail-fast and continue scheduling.
                    run_queue.mark_failed(
                        lease.id, f"Task not found in loaded set: {lease.task_id}", retryable=False
                    )
                    run_state.mark_failed(
                        lease.task_id, lease.trial_index, f"Task not found: {lease.task_id}"
                    )
                    self.state_manager.save_state(run_state)
                    return True

                # Mark as running
                run_queue.mark_running(lease.id, lease_owner)
                run_state.mark_running(lease.task_id, lease.trial_index)
                self.state_manager.save_state(run_state)

                future = executor.submit(
                    self._run_trial,
                    task,
                    lease.trial_index,
                    agent_client,
                    user_config,
                    output_dir,
                    docker_runtime,
                    request_limiter,
                )
                active_futures[future] = lease
                return True

            while len(active_futures) < self.config.orchestrator.workers and submit_one():
                pass

            while active_futures:
                done, _ = wait(active_futures.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    lease = active_futures.pop(future)
                    task_id = lease.task_id
                    trial_idx = lease.trial_index
                    try:
                        trajectory = future.result()
                        self.results.append(trajectory)
                        trial_cost = trajectory.metrics.cost_usd_est or 0.0
                        total_cost_usd += trial_cost

                        # Retry transient infra failures based on queue retry policy.
                        if self._is_retryable_trajectory(trajectory):
                            reason = (
                                trajectory.termination_reason.value
                                if trajectory.termination_reason
                                else trajectory.status.value
                            )
                            try:
                                should_retry = run_queue.mark_failed(
                                    lease.id,
                                    f"Retryable failure: {reason}",
                                    retryable=True,
                                )
                            except Exception as db_err:
                                self.logger.error(
                                    "Queue DB error in retryable path; treating as non-retryable",
                                    task_id=task_id,
                                    trial_index=trial_idx,
                                    db_error=str(db_err),
                                )
                                should_retry = False
                            if should_retry:
                                self.logger.warning(
                                    "Retrying trial after transient failure",
                                    task_id=task_id,
                                    trial_index=trial_idx,
                                    retry_count_next=lease.retry_count + 1,
                                    status=trajectory.status.value,
                                    termination_reason=reason,
                                )
                            else:
                                run_state.mark_failed(
                                    task_id,
                                    trial_idx,
                                    f"Retry limit reached after transient failure: {reason}",
                                )
                                self.state_manager.save_state(run_state)
                            self.logger.info(
                                "Trial failed (transient)",
                                task_id=task_id,
                                trial_index=trial_idx,
                                trial_cost_usd=trial_cost,
                                total_cost_usd=round(total_cost_usd, 6),
                            )
                        else:
                            try:
                                run_queue.mark_completed(lease.id, cost_usd=trial_cost)
                            except Exception as db_err:
                                self.logger.error(
                                    "Queue DB error marking completed; run_state still updated",
                                    task_id=task_id,
                                    trial_index=trial_idx,
                                    db_error=str(db_err),
                                )
                            # Update run state
                            if trajectory.grade:
                                run_state.mark_completed(
                                    task_id,
                                    trial_idx,
                                    trajectory.grade.binary_pass,
                                    trajectory.grade.score,
                                )
                            else:
                                run_state.mark_completed(task_id, trial_idx, False, 0.0)
                            self.state_manager.save_state(run_state)

                            self.logger.info(
                                "Trial completed",
                                task_id=trajectory.task_id,
                                trial_index=trajectory.trial_index,
                                status=trajectory.status.value,
                                score=trajectory.grade.score if trajectory.grade else None,
                                trial_cost_usd=trial_cost,
                                total_cost_usd=round(total_cost_usd, 6),
                            )
                    except Exception as e:
                        try:
                            should_retry = run_queue.mark_failed(lease.id, str(e), retryable=True)
                        except Exception as db_err:
                            self.logger.error(
                                "Queue DB error while marking failure; treating as non-retryable",
                                task_id=task_id,
                                trial_index=trial_idx,
                                original_error=str(e),
                                db_error=str(db_err),
                            )
                            should_retry = False
                        self.logger.error(
                            "Trial execution exception",
                            task_id=task_id,
                            trial_index=trial_idx,
                            error=str(e),
                            will_retry=should_retry,
                        )
                        if not should_retry:
                            # Mark as failed only when retries are exhausted.
                            run_state.mark_failed(task_id, trial_idx, str(e))
                            self.state_manager.save_state(run_state)

                    # Stop scheduling new work once budget cap is reached.
                    if budget_limit is not None and total_cost_usd >= budget_limit:
                        if not budget_exhausted:
                            budget_exhausted = True
                            self.logger.warning(
                                "Budget limit reached; no new trials will be scheduled",
                                budget_limit_usd=budget_limit,
                                total_cost_usd=round(total_cost_usd, 6),
                                remaining_trials=self._safe_get_pending(run_queue),
                            )
                        continue

                    while len(active_futures) < self.config.orchestrator.workers and submit_one():
                        pass

        try:
            counts = run_queue.get_counts()
        except Exception:
            counts = {}
        remaining = counts.get("pending", 0) + counts.get("leased", 0) + counts.get("running", 0)
        if budget_exhausted and remaining > 0:
            self.state_manager.mark_run_paused()
            self.logger.warning(
                "Run paused due to budget cap",
                pending_trials=remaining,
                total_scheduled_trials=total_trials_scheduled - remaining,
                budget_limit_usd=budget_limit,
                total_cost_usd=round(total_cost_usd, 6),
            )
        else:
            # Mark run as completed
            self.state_manager.mark_run_completed()

        # Cleanup Docker runtime if used
        if docker_runtime:
            docker_runtime.close()
            self.logger.info("Docker runtime closed")

        # Stop TypeSense BEFORE destroying the ServiceStack.
        # TypeSense is connected to runner-net (via _connect_typesense_to_runner_network),
        # so it must be removed from that network before the stack can tear it down.
        if hasattr(self, "_typesense_server") and self._typesense_server:
            try:
                self._typesense_server.stop()
                self.logger.info("TypeSense server stopped")
            except Exception as e:
                self.logger.warning(f"Failed to stop TypeSense server: {e}")

        # Cleanup ServiceStack if auto-started
        if service_stack is not None:
            try:
                service_stack.destroy()
                self.logger.info("ServiceStack destroyed")
            except Exception as e:
                self.logger.warning("Failed to destroy ServiceStack", error=str(e))

        # Generate reports
        self._generate_reports(output_dir)

    def run_worker(self, output_dir: Path, max_attempts: int | None = None) -> dict[str, Any]:
        """Run as a worker consuming attempts from the durable queue.

        This mode is intended for distributed execution where multiple worker
        processes lease from the same queue backend.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not self.tasks:
            self.load_tasks()
        if not self.tasks:
            raise ValueError("No tasks loaded for worker execution")

        agent_config = self.config.models.get("agent")
        user_config = self.config.models.get("user")
        if not agent_config:
            raise ValueError("Agent model configuration required")

        # Apply default user model if not configured
        if user_config is None:
            user_config = ModelConfig(
                provider="openrouter",
                name="anthropic/claude-sonnet-4.6",
                temperature=0.2,
            )
            self.logger.info(
                "Using default user model",
                user_model="openrouter/anthropic/claude-sonnet-4.6",
            )

        # Log model configuration for both roles
        self.logger.info(
            "Model configuration",
            agent_model=f"{agent_config.provider}/{agent_config.name}",
            user_model=f"{user_config.provider}/{user_config.name}",
        )

        agent_client = LLMClient(agent_config)
        request_limiter: GlobalRateLimiter | None = None
        if self.config.orchestrator.max_requests_per_second is not None:
            request_limiter = GlobalRateLimiter(self.config.orchestrator.max_requests_per_second)

        from tolokaforge.core.docker_runtime import DockerRuntime

        docker_runtime = DockerRuntime(
            runner_address=os.environ.get("EXECUTOR_ADDRESS", "executor:50051")
        )
        docker_runtime.connect()

        task_by_id = {task.task_id: task for task in self.tasks}
        run_queue = create_run_queue(
            self.config.orchestrator.queue_backend,
            sqlite_path=output_dir / "run_queue.sqlite",
            max_retries=self.config.orchestrator.max_attempt_retries,
            postgres_dsn=self.config.orchestrator.queue_postgres_dsn,
        )
        recovered = run_queue.recover_inflight(
            max_lease_age_s=max(300, self.config.orchestrator.timeouts.episode_s * 2)
        )
        if recovered > 0:
            self.logger.warning("Worker recovered stale in-flight attempts", recovered=recovered)

        budget_limit = self.config.orchestrator.max_budget_usd
        total_cost_usd = self._collect_existing_cost(output_dir)
        lease_owner = f"worker:{socket.gethostname()}:{os.getpid()}"
        lease_seconds = max(300, self.config.orchestrator.timeouts.episode_s * 2)

        processed = 0
        completed = 0
        failed = 0
        requeued = 0

        try:
            while True:
                if max_attempts is not None and processed >= max_attempts:
                    break
                if budget_limit is not None and total_cost_usd >= budget_limit:
                    self.logger.warning(
                        "Worker stopping due to budget cap",
                        budget_limit_usd=budget_limit,
                        total_cost_usd=round(total_cost_usd, 6),
                    )
                    break

                lease = run_queue.lease_next(worker_id=lease_owner, lease_seconds=lease_seconds)
                if lease is None:
                    break

                task = task_by_id.get(lease.task_id)
                if task is None:
                    run_queue.mark_failed(
                        lease.id, f"Task not found in loaded set: {lease.task_id}", retryable=False
                    )
                    failed += 1
                    processed += 1
                    continue

                run_queue.mark_running(lease.id, lease_owner)

                try:
                    trajectory = self._run_trial(
                        task=task,
                        trial_idx=lease.trial_index,
                        agent_client=agent_client,
                        user_config=user_config,
                        output_dir=output_dir,
                        docker_runtime=docker_runtime,
                        request_limiter=request_limiter,
                    )
                    self.results.append(trajectory)
                    trial_cost = trajectory.metrics.cost_usd_est or 0.0
                    total_cost_usd += trial_cost

                    if self._is_retryable_trajectory(trajectory):
                        reason = (
                            trajectory.termination_reason.value
                            if trajectory.termination_reason
                            else trajectory.status.value
                        )
                        try:
                            should_retry = run_queue.mark_failed(
                                lease.id, f"Retryable failure: {reason}", retryable=True
                            )
                        except Exception as db_err:
                            self.logger.error(
                                "Queue DB error in retryable path",
                                task_id=lease.task_id,
                                db_error=str(db_err),
                            )
                            should_retry = False
                        if should_retry:
                            requeued += 1
                        else:
                            failed += 1
                    else:
                        try:
                            run_queue.mark_completed(lease.id, cost_usd=trial_cost)
                        except Exception as db_err:
                            self.logger.error(
                                "Queue DB error marking completed",
                                task_id=lease.task_id,
                                db_error=str(db_err),
                            )
                        completed += 1
                except Exception as e:
                    try:
                        should_retry = run_queue.mark_failed(lease.id, str(e), retryable=True)
                    except Exception as db_err:
                        self.logger.error(
                            "Queue DB error while marking failure; treating as non-retryable",
                            task_id=lease.task_id,
                            original_error=str(e),
                            db_error=str(db_err),
                        )
                        should_retry = False
                    if should_retry:
                        requeued += 1
                    else:
                        failed += 1
                    self.logger.error(
                        "Worker attempt failed with exception",
                        task_id=lease.task_id,
                        trial_index=lease.trial_index,
                        error=str(e),
                    )

                processed += 1
        finally:
            if docker_runtime:
                docker_runtime.close()
            if hasattr(self, "_typesense_server") and self._typesense_server:
                try:
                    self._typesense_server.stop()
                except Exception:
                    pass

        summary = {
            "processed_attempts": processed,
            "completed_attempts": completed,
            "failed_attempts": failed,
            "requeued_attempts": requeued,
            "total_cost_usd": round(total_cost_usd, 6),
        }
        self.logger.info("Worker finished", **summary)
        return summary

    def prepare_run(self, output_dir: Path, reset_queue: bool = False) -> dict[str, Any]:
        """Prepare a run directory and seed the durable queue."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not self.tasks:
            self.load_tasks()
        if not self.tasks:
            raise ValueError("No tasks found to enqueue")

        run_queue = create_run_queue(
            self.config.orchestrator.queue_backend,
            sqlite_path=output_dir / "run_queue.sqlite",
            max_retries=self.config.orchestrator.max_attempt_retries,
            postgres_dsn=self.config.orchestrator.queue_postgres_dsn,
        )
        if reset_queue:
            run_queue.clear_all()

        items = []
        for task in self.tasks:
            for trial_idx in range(self.config.orchestrator.repeats):
                items.append((task.task_id, trial_idx))
        run_queue.enqueue_many(items)
        counts = run_queue.get_counts()

        summary = {
            "queued_attempts": len(items),
            "queue_counts": counts,
            "queue_backend": self.config.orchestrator.queue_backend,
        }
        self.logger.info("Run prepared", **summary)
        return summary

    def _run_trial(
        self,
        task: TaskConfig,
        trial_idx: int,
        agent_client: LLMClient | None,
        user_config: ModelConfig | None,
        output_dir: Path,
        docker_runtime: Any,
        request_limiter: GlobalRateLimiter | None = None,
    ) -> Trajectory:
        """Run a single trial with environment state and grading"""
        assert self.adapter is not None

        # Get task directory from adapter (supports both native and tau)
        task_dir = self.adapter.get_task_dir(task.task_id)

        if agent_client is None:
            raise ValueError("Agent client must be provided for trial execution")

        # Per-trial DB namespace for parallel isolation
        db_ns = f"{task.task_id}_{trial_idx}"

        # Initialize environment state
        env_state = EnvironmentState(task_dir, task.initial_state)
        env_state.hydrate()

        # Initialize json-db service with initial state (namespaced for parallel isolation)
        # Skip when Docker runtime is active — the Runner's InMemoryDatabase is the
        # source of truth, and the standalone json-db service is not started.
        if env_state.db_state and not docker_runtime:
            try:
                import httpx

                json_db_reset_urls = [
                    f"http://json-db:8000/ns/{db_ns}",
                    f"http://localhost:8000/ns/{db_ns}",
                ]

                initialized = False
                for reset_url in json_db_reset_urls:
                    try:
                        with httpx.Client(timeout=10.0) as client:
                            response = client.post(f"{reset_url}/reset", json=env_state.db_state)
                        if response.status_code == 200:
                            self.logger.debug(
                                "Initialized json-db service",
                                url=reset_url,
                                namespace=db_ns,
                                tables=len(env_state.db_state),
                            )
                            initialized = True
                            break
                    except Exception:
                        continue

                if not initialized:
                    self.logger.warning("Failed to initialize json-db service")
            except Exception as e:
                self.logger.warning("Could not initialize json-db service", error=str(e))

        # Initialize RAG index if corpus is configured
        if env_state.rag_corpus_dir:
            try:
                import httpx

                rag_service_urls = ["http://rag-service:8001", "http://localhost:8001"]

                corpus_path = str(env_state.rag_corpus_dir)
                container_corpus_path = None
                try:
                    repo_root = Path(__file__).resolve().parents[2]
                    repo_tolokaforge = repo_root / "tolokaforge"
                    if env_state.rag_corpus_dir.is_relative_to(repo_tolokaforge):
                        rel_path = env_state.rag_corpus_dir.relative_to(repo_tolokaforge)
                        container_corpus_path = str(Path("/app/tolokaforge") / rel_path)
                except Exception:
                    container_corpus_path = None
                indexed = False
                for rag_service_url in rag_service_urls:
                    try:
                        request_path = corpus_path
                        if "localhost" in rag_service_url and container_corpus_path:
                            request_path = container_corpus_path

                        with httpx.Client(timeout=10.0) as client:
                            response = client.post(
                                f"{rag_service_url}/index",
                                json={"corpus_path": request_path},
                            )
                        if response.status_code == 200:
                            self.logger.debug(
                                "Indexed RAG corpus", path=corpus_path, url=rag_service_url
                            )
                            indexed = True
                            break
                    except Exception:
                        continue

                if not indexed:
                    self.logger.warning("Failed to index RAG corpus", path=corpus_path)
            except Exception as e:
                self.logger.warning("Could not index RAG corpus", error=str(e))

        # Execute initialization_actions to set correct starting state
        if task.initial_state.initialization_actions:
            init_actions = [
                action.model_dump() for action in task.initial_state.initialization_actions
            ]
            self.logger.debug("Executing initialization actions", count=len(init_actions))

            # Import MCP server to execute actions before trial starts
            mcp_server_ref = task.tools.agent.get("mcp_server")
            if mcp_server_ref:
                mcp_server_path = task_dir / mcp_server_ref
                if mcp_server_path.exists():
                    import importlib.util

                    spec = importlib.util.spec_from_file_location(
                        "mcp_server_init", mcp_server_path
                    )
                    if spec and spec.loader:
                        mcp_module_init = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mcp_module_init)

                        # Sync current env state to MCP server
                        if hasattr(mcp_module_init, "set_data"):
                            mcp_module_init.set_data(env_state.get_db())

                        # Execute each initialization action
                        for action in init_actions:
                            env_type = action.get("env_type")  # "user" or "assistant"
                            func_name = action.get("func_name")
                            arguments = action.get("arguments", {})

                            self.logger.debug(
                                "Executing initialization action",
                                env_type=env_type,
                                func_name=func_name,
                                arguments=arguments,
                            )

                            try:
                                # Invoke helper/tool via MCP server
                                if hasattr(mcp_module_init, "invoke_environment_action"):
                                    result = mcp_module_init.invoke_environment_action(
                                        env_type, func_name, **arguments
                                    )
                                else:
                                    result = mcp_module_init.invoke_tool(func_name, **arguments)
                                self.logger.debug("Init action completed", result=str(result)[:100])
                            except Exception as e:
                                self.logger.warning(
                                    "Init action failed", func_name=func_name, error=str(e)
                                )

                        # Retrieve updated state after initialization
                        if hasattr(mcp_module_init, "get_data"):
                            updated_state = mcp_module_init.get_data()
                            if updated_state:
                                env_state.db_state = updated_state
                                env_state._normalize_db_state()
                                self.logger.debug("Retrieved updated state after initialization")

        # Create trial directory early for video recording
        trial_dir = output_dir / "trials" / task.task_id / str(trial_idx)
        trial_dir.mkdir(parents=True, exist_ok=True)

        # Load adapter environment (needed for state sync with Tau tasks)
        adapter_env = self.adapter.create_environment(task.task_id)

        # Sync adapter environment data to env_state for Tau tasks
        # This ensures adapter data appears in env.yaml and is available for grading
        if adapter_env.data and not isinstance(self.adapter, NativeAdapter):
            env_state.db_state = adapter_env.data
            env_state._normalize_db_state()
            self.logger.debug(
                "Synced adapter env data to env_state",
                tables_count=(len(adapter_env.data) if isinstance(adapter_env.data, dict) else 0),
                tables_sample=(
                    list(adapter_env.data.keys())[:5]
                    if isinstance(adapter_env.data, dict)
                    else "non-dict"
                ),
            )

        # Docker runtime - use executor adapter for tool execution
        from tolokaforge.core.docker_adapter import DockerRunnerAdapter
        from tolokaforge.tools.registry import sanitize_schema_properties

        trial_id = f"{task.task_id}:{trial_idx}"

        tool_executor = DockerRunnerAdapter(
            runner_client=docker_runtime.executor_client, trial_id=trial_id
        )

        # Get TaskDescription from adapter and register trial
        task_desc = self.adapter.to_task_description(task.task_id)
        task_desc_json = task_desc.model_dump_json()

        register_result = tool_executor.register_trial(task_description_json=task_desc_json)
        if not register_result["success"]:
            error = register_result.get("error", "Unknown error")
            raise RuntimeError(
                f"Failed to register trial with executor for trial {trial_id}: {error}"
            )

        # Use tool schemas from register_trial result (converted to OpenAI format)
        # Sanitize property names to match LLM API requirements (^[a-zA-Z0-9_.-]+$)
        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": ts["name"],
                    "description": ts["description"],
                    "parameters": sanitize_schema_properties(ts["parameters"]),
                },
            }
            for ts in register_result["tool_schemas"]
        ]

        self.logger.info(
            "Docker runtime: Registered trial",
            trial_id=trial_id,
            tool_count=register_result.get("num_agent_tools", len(tool_schemas)),
        )

        # User tool executor is not used in Docker mode (Runner handles tools)
        user_tool_executor = None
        user_tool_schemas: list[dict[str, Any]] = []

        # Use backstory from task configuration
        backstory = task.user_simulator.backstory

        # Create user simulator
        user_llm_config = user_config if task.user_simulator.mode == "llm" else None
        user_simulator = UserSimulator(
            mode=task.user_simulator.mode,
            llm_config=user_llm_config,
            persona=task.user_simulator.persona,
            backstory=backstory,
            scripted_flow=task.user_simulator.scripted_flow,
            tool_schemas=user_tool_schemas if user_tool_executor else None,
        )

        # Create stuck detector with configured heuristics
        stuck_detector = None
        if self.config.orchestrator.stuck_heuristics.enabled:
            stuck_detector = StuckDetector(
                max_repeated_tool_calls=self.config.orchestrator.stuck_heuristics.max_repeated_tool_calls,
                max_idle_turns=self.config.orchestrator.stuck_heuristics.max_idle_turns,
            )

        # Build system prompt
        system_prompt = self._build_system_prompt(task, tool_schemas, task_dir)

        # Respect per-task max_turns when provided. Fall back to orchestrator default.
        max_turns = (
            task.max_turns if task.max_turns is not None else self.config.orchestrator.max_turns
        )

        # Scale turn budget for complex multi-app mobile tasks only when task max_turns
        # is not explicitly pinned.
        if task.max_turns is None:
            mobile_cfg = task.tools.agent.get("mobile", {})
            mobile_apps = mobile_cfg.get("apps", {}) if isinstance(mobile_cfg, dict) else {}
            if isinstance(mobile_apps, dict):
                app_count = len(mobile_apps)
                if app_count >= 5:
                    max_turns = max(max_turns, 90)
                elif app_count == 4:
                    max_turns = max(max_turns, 75)

        # Create runner with verbose and strict flags
        runner = TrialRunner(
            task_id=task.task_id,
            trial_index=trial_idx,
            agent_client=agent_client,
            user_simulator=user_simulator,
            tool_executor=tool_executor,
            tool_schemas=tool_schemas,
            max_turns=max_turns,
            turn_timeout_s=self.config.orchestrator.timeouts.turn_s,
            episode_timeout_s=self.config.orchestrator.timeouts.episode_s,
            stuck_detector=stuck_detector,
            user_tool_executor=user_tool_executor,
            request_limiter=request_limiter,
            verbose=self.verbose,
            strict=self.strict,
        )

        # Run trial
        # Use initial_user_message if provided (e.g., tool-use style tasks)
        # Otherwise use task.description which will be interpreted by user simulator (e.g., TAU tasks)
        initial_message = task.initial_user_message if task.initial_user_message else ""
        trajectory = runner.run(system_prompt, initial_message)

        # Sync JSON DB state for native tasks (if no MCP server is used)
        # Skip when Docker runtime is active — state comes from Runner DB service.
        if (
            task.initial_state.json_db
            and not task.tools.agent.get("mcp_server")
            and not docker_runtime
        ):
            try:
                import httpx

                json_db_sync_urls = [
                    f"http://json-db:8000/ns/{db_ns}",
                    f"http://localhost:8000/ns/{db_ns}",
                ]

                synced = False
                for sync_url in json_db_sync_urls:
                    try:
                        with httpx.Client(timeout=10.0) as client:
                            response = client.post(f"{sync_url}/query", json={"jsonpath": "$"})
                        if response.status_code == 200:
                            results = response.json().get("results", [])
                            if results:
                                env_state.db_state = results[0]
                                env_state._normalize_db_state()
                                self.logger.debug(
                                    "Synced json-db state for grading",
                                    url=sync_url,
                                    namespace=db_ns,
                                )
                                synced = True
                                break
                    except Exception:
                        continue

                if not synced:
                    self.logger.warning("Failed to sync json-db state")
            except Exception as e:
                self.logger.warning("Could not sync json-db state", error=str(e))

        # Retrieve final state from Runner DB service (source of truth in Docker mode)
        # The adapter_env.data is a snapshot from create_environment() and does NOT
        # reflect tool-execution changes made through the Runner.
        # For native MCP-server tasks the Runner's GetState RPC now syncs the
        # subprocess state to db-service before reading, so the condition no
        # longer needs to exclude NativeAdapter.
        if docker_runtime:
            try:
                state_result = docker_runtime.executor_client.get_state(trial_id)
                if state_result.get("success") and state_result.get("state_json"):
                    import json as _json

                    runner_state = _json.loads(state_result["state_json"])
                    if isinstance(runner_state, dict) and runner_state:
                        env_state.db_state = runner_state
                        env_state._normalize_db_state()
                        self.logger.debug(
                            "Synced final state from Runner DB service",
                            tables_count=len(runner_state),
                            tables_sample=list(runner_state.keys())[:5],
                        )
                    else:
                        self.logger.debug("Runner DB state empty, falling back to adapter env data")
                        if adapter_env.data:
                            env_state.db_state = adapter_env.data
                            env_state._normalize_db_state()
                else:
                    self.logger.debug(
                        "Failed to fetch Runner DB state, falling back to adapter env data",
                        error=state_result.get("error"),
                    )
                    if adapter_env.data:
                        env_state.db_state = adapter_env.data
                        env_state._normalize_db_state()
            except Exception as e:
                self.logger.warning(
                    "Could not fetch state from Runner, using adapter env data",
                    error=str(e),
                )
                if adapter_env.data:
                    env_state.db_state = adapter_env.data
                    env_state._normalize_db_state()
        elif adapter_env.data and not isinstance(self.adapter, NativeAdapter):
            # Non-Docker mode fallback — adapter is source of truth
            env_state.db_state = adapter_env.data
            env_state._normalize_db_state()
            self.logger.debug(
                "Synced final adapter env data to env_state",
                tables_count=(len(adapter_env.data) if isinstance(adapter_env.data, dict) else 0),
                tables_sample=(
                    list(adapter_env.data.keys())[:5]
                    if isinstance(adapter_env.data, dict)
                    else "non-dict"
                ),
            )

        # Capture final environment state
        final_state = env_state.get_final_state()
        # Pass agent_visible_dir so the agentic judge can read files from disk
        final_state["agent_visible_dir"] = str(env_state.agent_visible_dir)
        trajectory.final_env_state = final_state

        # Check if trial completed successfully - ERROR/TIMEOUT trials should auto-fail
        # This prevents false positives when 429 or other errors occur before any work is done
        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task.task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            grade = Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )
        elif trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            # Stuck agents always fail — even if hash matches
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task.task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            grade = Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )
        else:
            # Grade via Runner's GradeTrial RPC.
            # The Runner has direct access to the agent's filesystem and DB state,
            # supporting hash-based grading, jsonpath file assertions, transcript rules,
            # and LLM judge evaluation.
            grade, judge_cost = self._grade_via_runner_rpc(
                task, trial_idx, docker_runtime, trajectory
            )
            # Add judge cost to trial metrics so it appears in cost_usd_est
            if judge_cost > 0:
                trajectory.metrics.cost_usd_est = (
                    trajectory.metrics.cost_usd_est or 0.0
                ) + judge_cost
        trajectory.grade = grade

        self.logger.info(
            "Trial graded",
            task_id=task.task_id,
            trial_index=trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )

        # Note: Browser cleanup is handled automatically by Playwright when the process ends.
        # The video recording is finalized when the browser context closes.
        # We don't need explicit cleanup here - it was causing event loop issues.
        # The video file is already being written to the videos directory.

        # Save trial outputs using OutputWriter (split files)
        # trial_dir was already created earlier for video recording
        writer = OutputWriter(trial_dir)

        # Get grading config for output (from adapter)
        grading_config = self.adapter.get_grading_config(task.task_id)

        # Prepare task config for output
        task_config_dict = {
            "task_id": task.task_id,
            "trial_index": trial_idx,
            "category": task.category,
            "description": task.description,
            "grading_config": grading_config.model_dump(mode="json") if grading_config else {},
            "tools": task.tools.model_dump(mode="json"),
            "policies": task.policies,
        }

        # Write all split output files
        writer.write_all(trajectory, task_config_dict, final_state, runner.logger)

        self.logger.info(
            "Trial output saved",
            task_id=task.task_id,
            trial_index=trial_idx,
            output_dir=str(trial_dir),
        )

        return trajectory

    def _grade_via_runner_rpc(
        self,
        task: TaskConfig,
        trial_idx: int,
        docker_runtime: Any,
        trajectory: Trajectory | None = None,
    ) -> Grade:
        """Grading via Runner's GradeTrial RPC.

        Passes the conversation transcript so the Runner can evaluate
        transcript rules and LLM judge components.
        """
        trial_id = f"{task.task_id}:{trial_idx}"

        # Serialize transcript messages for the Runner (including tool calls)
        llm_messages_json: str | None = None
        if trajectory and trajectory.messages:
            try:
                messages_data = []
                for m in trajectory.messages:
                    msg_dict: dict[str, Any] = {
                        "role": m.role.value if hasattr(m.role, "value") else str(m.role),
                        "content": m.content or "",
                    }
                    if m.tool_calls:
                        msg_dict["tool_calls"] = [
                            {"name": tc.name, "arguments": tc.arguments} for tc in m.tool_calls
                        ]
                    if m.tool_call_id:
                        msg_dict["tool_call_id"] = m.tool_call_id
                    messages_data.append(msg_dict)
                llm_messages_json = json.dumps(messages_data)
            except Exception as e:
                self.logger.warning("Failed to serialize messages for grading", error=str(e))

        grade_result = docker_runtime.executor_client.grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
        )
        if grade_result["success"] and grade_result["grade"]:
            g = grade_result["grade"]
            state_diff_parsed: dict[str, Any] | None = None
            if g.get("state_diff_json"):
                try:
                    state_diff_parsed = json.loads(g["state_diff_json"])
                except (json.JSONDecodeError, TypeError):
                    pass
            grade = Grade(
                binary_pass=g["binary_pass"],
                score=g["score"],
                components=GradeComponents(
                    state_checks=g["components"].get("state_checks"),
                    transcript_rules=g["components"].get("transcript_rules"),
                    llm_judge=g["components"].get("llm_judge"),
                    custom_checks=g["components"].get("custom_checks"),
                ),
                reasons=g.get("reasons", ""),
                state_diff=state_diff_parsed,
            )

            # Log full grading details for debuggability
            if grade.reasons:
                self.logger.info(
                    "Grading details",
                    task_id=task.task_id,
                    trial_index=trial_idx,
                    reasons=grade.reasons,
                )

            # Log when judge evaluation failed (0.0) vs not configured (-1.0)
            llm_judge_score = g["components"].get("llm_judge", -1.0)
            if llm_judge_score == 0.0:
                self.logger.warning(
                    "LLM judge evaluation failed",
                    task_id=task.task_id,
                    trial_index=trial_idx,
                    reasons=grade.reasons,
                )

            return grade, grade_result.get("judge_cost_usd", 0.0)
        else:
            error_msg = grade_result.get("error", "Unknown grading error")
            self.logger.error(
                "Grading RPC failed",
                task_id=task.task_id,
                trial_index=trial_idx,
                error=error_msg,
            )
            return (
                Grade(
                    binary_pass=False,
                    score=0.0,
                    components=GradeComponents(state_checks=0.0),
                    reasons=f"Grading RPC failed: {error_msg}",
                ),
                0.0,
            )

    def _build_system_prompt(
        self, task: TaskConfig, tool_schemas: list[dict[str, Any]], task_dir: Path
    ) -> str:
        """Build system prompt for task

        Priority:
        1. task.policies['agent_system_prompt'] (inline string)
        2. task.system_prompt == "__adapter__" -> use adapter.get_system_prompt()
        3. task.system_prompt (file path)
        4. main_policy.md pattern (legacy)
        5. Minimal default

        Note: Tool schemas should NOT be in system prompt - they're sent via function calling API
        """

        # 1. Check for inline agent_system_prompt in policies
        if "agent_system_prompt" in task.policies:
            return task.policies["agent_system_prompt"]

        # 2. Check for adapter-based system prompt
        if task.system_prompt == "__adapter__" and self.adapter:
            adapter_prompt = self.adapter.get_system_prompt(task.task_id)
            if adapter_prompt:
                AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy.
"""

                return f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>
<policy>
{adapter_prompt}
</policy>"""

        # 3. Check for system_prompt file path
        if task.system_prompt and task.system_prompt != "__adapter__":
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
                return system_prompt_path.read_text()

        # 3. Check for main_policy.md + additional system prompt file structure (legacy)
        main_policy_path = task_dir.parent / "main_policy.md"  # One level up from task dir
        if not main_policy_path.exists():
            main_policy_path = task_dir / "main_policy.md"  # Try task dir itself

        if main_policy_path.exists() and task.system_prompt:
            # Load main policy
            with open(main_policy_path) as f:
                main_policy = f.read()

            # Load additional policy file
            additional_policy_path = task_dir.parent / task.system_prompt
            if not additional_policy_path.exists():
                additional_policy_path = task_dir / task.system_prompt

            if additional_policy_path.exists():
                with open(additional_policy_path) as f:
                    additional_policy = f.read()

                # Concatenate policies with XML tags
                domain_policy = (
                    "<main_policy>\n"
                    + main_policy
                    + "\n</main_policy>\n"
                    + "<tech_support_policy>\n"
                    + additional_policy
                    + "\n</tech_support_policy>"
                )
            else:
                # Fallback: just use main policy if additional policy file not found
                domain_policy = main_policy

            AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy."""

            # Tools are passed separately to LLM API, NOT in system prompt
            prompt = f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>
<policy>
{domain_policy}
</policy>"""
            return prompt

        # Single-file system prompt
        elif task.system_prompt:
            system_prompt_path = task_dir / task.system_prompt
            if system_prompt_path.exists():
                with open(system_prompt_path) as f:
                    domain_policy = f.read()

                AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call using the provided functions.
You cannot do both at the same time.

When you need to use a tool, use the function calling mechanism - do NOT output JSON in your message text.
Always include every required function argument in the tool call itself (do not omit fields).
Try to be helpful and always follow the policy."""

                prompt = f"""<instructions>
{AGENT_INSTRUCTION}
</instructions>
<policy>
{domain_policy}
</policy>"""
                return prompt

        # 4. Minimal default fallback
        # Tool schemas are sent separately via function calling API, NOT in system prompt
        # Enrich the default prompt with task-specific context when available.
        parts = ["You are a helpful assistant."]

        # Add task guidance from policies
        guidance = task.policies.get("guidance", [])
        if guidance:
            parts.append("\nGuidance:")
            for g in guidance:
                parts.append(f"- {g}")

        # Add browser URL if configured so the agent knows where to navigate
        browser_config = task.tools.agent.get("browser", {}) if task.tools else {}
        if isinstance(browser_config, dict):
            browser_url = browser_config.get("initial_url")
            if browser_url:
                parts.append(f"\nThe web portal is available at: {browser_url}")
                parts.append(
                    "Navigate to this URL to access the portal content. "
                    "Do not guess other URLs or ports."
                )

        return "\n".join(parts)

    def _generate_reports(self, output_dir: Path) -> None:
        """Generate aggregate reports with pass@k"""
        if not self.results:
            self.logger.warning("No results to report")
            return

        # Group trajectories by task
        task_trajectories = {}
        for traj in self.results:
            if traj.task_id not in task_trajectories:
                task_trajectories[traj.task_id] = []
            task_trajectories[traj.task_id].append(traj)
        task_by_id = {task.task_id: task for task in self.tasks}

        # Calculate metrics per task
        all_task_metrics = []
        for task_id, trajectories in task_trajectories.items():
            task_metrics = calculate_task_metrics(trajectories)
            task_metrics["task_id"] = task_id
            task_cfg = task_by_id.get(task_id)
            if task_cfg is not None:
                task_metrics["benchmark_type"] = task_cfg.category
                task_metrics["complexity"] = task_cfg.metadata.complexity
                task_metrics["expected_failure_modes"] = task_cfg.metadata.expected_failure_modes
                task_metrics["tags"] = task_cfg.metadata.tags
            all_task_metrics.append(task_metrics)

        # Calculate aggregate metrics
        aggregate = calculate_aggregate_metrics(all_task_metrics, weighted=True)
        aggregate.update(
            calculate_latency_percentiles([t.metrics.latency_total_s for t in self.results])
        )

        # Metadata-sliced aggregates
        metadata_slices: dict[str, dict[str, Any]] = {
            "by_benchmark_type": {},
            "by_complexity": {},
            "by_tag": {},
            "by_expected_failure_mode": {},
        }
        groups_by_benchmark: dict[str, list[dict[str, Any]]] = {}
        groups_by_complexity: dict[str, list[dict[str, Any]]] = {}
        groups_by_tag: dict[str, list[dict[str, Any]]] = {}
        groups_by_failure_mode: dict[str, list[dict[str, Any]]] = {}

        for task_metrics in all_task_metrics:
            benchmark_type = str(task_metrics.get("benchmark_type") or "unknown")
            complexity = str(task_metrics.get("complexity") or "unspecified")
            groups_by_benchmark.setdefault(benchmark_type, []).append(task_metrics)
            groups_by_complexity.setdefault(complexity, []).append(task_metrics)

            for tag in task_metrics.get("tags", []) or []:
                groups_by_tag.setdefault(str(tag), []).append(task_metrics)
            for failure_mode in task_metrics.get("expected_failure_modes", []) or []:
                groups_by_failure_mode.setdefault(str(failure_mode), []).append(task_metrics)

        for key, group in groups_by_benchmark.items():
            metadata_slices["by_benchmark_type"][key] = calculate_aggregate_metrics(
                group, weighted=True
            )
        for key, group in groups_by_complexity.items():
            metadata_slices["by_complexity"][key] = calculate_aggregate_metrics(
                group, weighted=True
            )
        for key, group in groups_by_tag.items():
            metadata_slices["by_tag"][key] = calculate_aggregate_metrics(group, weighted=True)
        for key, group in groups_by_failure_mode.items():
            metadata_slices["by_expected_failure_mode"][key] = calculate_aggregate_metrics(
                group, weighted=True
            )

        # Save per-task metrics
        with open(output_dir / "per_task_metrics.json", "w") as f:
            json.dump(all_task_metrics, f, indent=2, default=str)

        # Save aggregate report
        with open(output_dir / "aggregate.json", "w") as f:
            json.dump(aggregate, f, indent=2, default=str)
        with open(output_dir / "metadata_slices.json", "w") as f:
            json.dump(metadata_slices, f, indent=2, default=str)

        # Deterministic failure attribution report
        failure_attributions = [
            attribute_failure(traj) for traj in self.results if is_failed_trajectory(traj)
        ]
        failure_summary = summarize_failure_attributions(failure_attributions)
        with open(output_dir / "failure_attribution.json", "w") as f:
            json.dump(
                {"summary": failure_summary, "failures": failure_attributions},
                f,
                indent=2,
                default=str,
            )

        # Log summary
        self.logger.info(
            "Aggregate Results",
            total_trials=aggregate["total_trials"],
            total_tasks=aggregate["total_tasks"],
            success_rate_micro=aggregate.get("success_rate_micro"),
            avg_score_micro=aggregate.get("avg_score_micro"),
            avg_latency_s=aggregate["avg_latency_s"],
            latency_p50_s=aggregate.get("latency_p50_s"),
            latency_p90_s=aggregate.get("latency_p90_s"),
            latency_p99_s=aggregate.get("latency_p99_s"),
            total_cost_usd=aggregate.get("total_cost_usd"),
            avg_turns=aggregate["avg_turns"],
            avg_tool_calls=aggregate["avg_tool_calls"],
            stuck_rate=aggregate["stuck_rate"],
            failed_attempts=failure_summary.get("total_failed_attempts"),
            deterministic_attribution_coverage=failure_summary.get(
                "deterministic_attribution_coverage"
            ),
        )
