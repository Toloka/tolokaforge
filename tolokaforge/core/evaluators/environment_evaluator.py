"""Environment evaluator - checks environment state and assertions"""

import importlib
import importlib.util
from pathlib import Path
from typing import Any

from tolokaforge.core.grading.state_checks import consistent_hash, to_hashable
from tolokaforge.core.models import EnvAssertion, StateChecksConfig


class EnvAssertionResult:
    """Result of environment assertion check"""

    def __init__(
        self,
        assertion: EnvAssertion,
        passed: bool,
        actual_value: Any = None,
        error: str | None = None,
    ):
        self.assertion = assertion
        self.passed = passed
        self.actual_value = actual_value
        self.error = error

    def __repr__(self):
        if self.passed:
            return f"EnvAssertionResult(passed=True, func={self.assertion.func_name})"
        else:
            return f"EnvAssertionResult(passed=False, func={self.assertion.func_name}, error={self.error})"


class StateCheckResult:
    """Result of all state checks"""

    def __init__(
        self,
        score: float,
        env_assertion_results: list[EnvAssertionResult] = None,
        db_hash_match: bool | None = None,
        reasons: list[str] = None,
    ):
        self.score = score
        self.env_assertion_results = env_assertion_results or []
        self.db_hash_match = db_hash_match
        self.reasons = reasons or []

    def __repr__(self):
        return f"StateCheckResult(score={self.score:.2f}, assertions={len(self.env_assertion_results)}, reasons={len(self.reasons)})"


class EnvironmentEvaluator:
    """Evaluates environment state including assertions and DB hash"""

    def __init__(self):
        self.assertion_modules = {}  # Cache loaded modules

    def load_assertion_function(self, func_name: str, domain: str = "general") -> callable:
        """
        Load an assertion function dynamically

        Args:
            func_name: Name of the assertion function
            domain: Domain to load from (e.g. airline, retail)

        Returns:
            Callable assertion function
        """
        # Try to load from domain-specific assertions module
        module_key = f"{domain}.assertions"

        if module_key not in self.assertion_modules:
            try:
                # Try importing from installed package
                module = importlib.import_module(f"tolokaforge.tasks.{domain}.assertions")
                self.assertion_modules[module_key] = module
            except ImportError:
                # Try loading from file path
                try:
                    module_path = (
                        Path(__file__).parent.parent.parent / "tasks" / domain / "assertions.py"
                    )
                    if module_path.exists():
                        spec = importlib.util.spec_from_file_location(module_key, module_path)
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        self.assertion_modules[module_key] = module
                    else:
                        raise ImportError(f"Assertion module not found: {module_path}")
                except Exception as e:
                    raise ImportError(f"Failed to load assertions module for domain {domain}: {e}")

        module = self.assertion_modules[module_key]

        if not hasattr(module, func_name):
            raise AttributeError(f"Assertion function '{func_name}' not found in {module_key}")

        return getattr(module, func_name)

    def evaluate_env_assertion(
        self,
        assertion: EnvAssertion,
        agent_env_state: dict[str, Any],
        user_env_state: dict[str, Any],
        domain: str = "general",
    ) -> EnvAssertionResult:
        """
        Evaluate a single environment assertion

        Args:
            assertion: Environment assertion to check
            agent_env_state: Agent/assistant environment state
            user_env_state: User environment state
            domain: Domain for loading assertion functions

        Returns:
            EnvAssertionResult with pass/fail and details
        """
        try:
            # Select the correct environment
            env_state = agent_env_state if assertion.env_type == "assistant" else user_env_state

            # Load and call the assertion function
            func = self.load_assertion_function(assertion.func_name, domain)
            actual_value = func(env_state, **assertion.arguments)

            # Check if result matches expected
            passed = actual_value == assertion.assert_value

            return EnvAssertionResult(assertion=assertion, passed=passed, actual_value=actual_value)

        except Exception as e:
            return EnvAssertionResult(
                assertion=assertion,
                passed=False,
                error=f"Error evaluating assertion: {str(e)}",
            )

    def evaluate_state_checks(
        self,
        final_state: dict[str, Any],
        state_checks_config: StateChecksConfig,
        expected_db_hash: str | None = None,
        domain: str = "general",
    ) -> StateCheckResult:
        """
        Evaluate all state checks including env assertions and DB hash

        Args:
            final_state: Final environment state dict with 'agent' and 'user' keys
            state_checks_config: State checks configuration
            expected_db_hash: Expected DB hash for comparison (overrides config)
            domain: Domain for loading assertion functions

        Returns:
            StateCheckResult with score and details
        """
        reasons = []
        env_assertion_results = []

        # Extract agent and user states
        agent_env_state = final_state.get("agent", {})
        user_env_state = final_state.get("user", {})

        # Evaluate environment assertions
        if state_checks_config.env_assertions:
            for assertion in state_checks_config.env_assertions:
                result = self.evaluate_env_assertion(
                    assertion, agent_env_state, user_env_state, domain
                )
                env_assertion_results.append(result)

                if not result.passed:
                    msg = (
                        assertion.message
                        or f"Assertion failed: {assertion.func_name}({assertion.arguments})"
                    )
                    if result.error:
                        msg += f" - {result.error}"
                    reasons.append(msg)

        # Calculate assertion score (product of all assertion results)
        assertion_score = 1.0
        for result in env_assertion_results:
            if not result.passed:
                assertion_score *= 0.0

        # Check DB hash if enabled
        db_hash_match = None
        db_score = 1.0

        if state_checks_config.db_hash_check or expected_db_hash:
            hash_to_check = expected_db_hash
            if not hash_to_check and state_checks_config.hash:
                hash_to_check = state_checks_config.hash.get("expected_state_hash")

            if hash_to_check:
                try:
                    # Compute hash of agent DB state
                    agent_db = agent_env_state.get("db", agent_env_state)
                    actual_hash = consistent_hash(to_hashable(agent_db))
                    db_hash_match = actual_hash == hash_to_check

                    if db_hash_match:
                        db_score = 1.0
                    else:
                        db_score = 0.0
                        reasons.append(
                            f"DB hash mismatch: expected {hash_to_check[:16]}..., "
                            f"got {actual_hash[:16]}..."
                        )
                except Exception as e:
                    db_score = 0.0
                    reasons.append(f"Error computing DB hash: {str(e)}")

        # Combine scores (both must pass for full score)
        if state_checks_config.env_assertions and (
            state_checks_config.db_hash_check or expected_db_hash
        ):
            # Both assertions and DB hash
            final_score = assertion_score * db_score
        elif state_checks_config.env_assertions:
            # Only assertions
            final_score = assertion_score
        elif state_checks_config.db_hash_check or expected_db_hash:
            # Only DB hash
            final_score = db_score
        else:
            # No checks configured
            final_score = 1.0

        return StateCheckResult(
            score=final_score,
            env_assertion_results=env_assertion_results,
            db_hash_match=db_hash_match,
            reasons=reasons,
        )
