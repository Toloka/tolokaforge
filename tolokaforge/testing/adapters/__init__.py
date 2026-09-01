"""Reusable pytest suites external adapter authors subclass.

External adapter authors ``pip install tolokaforge`` and subclass one suite
class per contract to pin their adapter against the engine's declared shape.
Today the subpackage ships :class:`AdapterGradingContractSuite` — 11 test
methods locking the six
:class:`~tolokaforge.adapters.grading_contract.AdapterGradingContract`
methods, three capability flags, emit-payload schema, and preferred-kind
registry resolution.

Adoption pattern (~5 lines)::

    from tolokaforge.testing.adapters import AdapterGradingContractSuite

    class TestMyAdapterGradingContract(AdapterGradingContractSuite):
        expected_requires_docker_cli_in_runner = True  # only if declared

        @pytest.fixture
        def adapter(self):
            return MyAdapter({"base_dir": ..., "tasks_glob": ...})

        @pytest.fixture
        def task_and_dir(self):
            return load_task_yaml(A_REAL_TASK_YAML)

The base class carries no ``Test`` prefix so pytest does not collect it; the
subclass runs the 11 test methods against the fixtures it supplies.
"""

from .grading_contract import AdapterGradingContractSuite

__all__ = ["AdapterGradingContractSuite"]
