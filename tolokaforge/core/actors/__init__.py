"""Actor-level Protocols for the turn loop.

Home of the :class:`~tolokaforge.core.actors.actor.Actor` behavioural
contract — the per-turn message-producing seam that
:class:`~tolokaforge.core.llm.client.UserSimulator` satisfies today and
that future actor kinds (adversary, oracle, evaluator) will plug into
without re-introducing a two-party assumption in the loop body.
"""
