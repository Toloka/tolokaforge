"""Example Inspect AI task, runnable under `tolokaforge run` (offline with mockllm).

The mockllm provider's default completion contains the word "output", so the
`includes()` scorer marks the sample correct — this task passes at $0 with
`--model mockllm/model` and needs no API key.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, system_message


@task
def hello():
    return Task(
        dataset=MemoryDataset([Sample(input="Say hello.", target="output", id="s1")]),
        solver=[system_message("Be brief."), generate()],
        scorer=includes(),
    )
