"""Example Inspect AI task, runnable under `tolokaforge run`.

Deterministic across models: the prompt asks for the exact word "output", which a
real model returns and which the mockllm provider's default completion also
contains — so the `includes()` scorer passes both with a real cheap model and
offline at $0 with `mockllm/model`.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, system_message


@task
def hello():
    return Task(
        dataset=MemoryDataset(
            [Sample(input="Reply with exactly one word: output", target="output", id="s1")]
        ),
        solver=[
            system_message("Reply with exactly the requested word and nothing else."),
            generate(),
        ],
        scorer=includes(),
    )
