"""A tiny Inspect AI task used as a test fixture.

Runnable with ``mockllm/model`` at $0: the mock completion contains the word
"output", so the ``includes()`` scorer marks each sample CORRECT.
"""

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, system_message


@task
def poc_smoke():
    return Task(
        dataset=MemoryDataset(
            [
                Sample(input="Say anything.", target="output", id="s1"),
                Sample(input="Say anything else.", target="output", id="s2"),
            ]
        ),
        solver=[system_message("You are a terse assistant."), generate()],
        scorer=includes(),
    )
