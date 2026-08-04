"""Actor-level Protocols for the turn loop.

Home of two sibling seams:

* :class:`~tolokaforge.core.actors.actor.Actor` — the per-turn
  message-producing contract that
  :class:`~tolokaforge.core.llm.client.UserSimulator` satisfies and that
  future actor kinds (adversary, oracle, evaluator) plug into without
  re-introducing a two-party assumption in the loop body.
* :class:`~tolokaforge.core.actors.turn_policy.TurnPolicy` — the
  choreographer of the turn loop, resolved by
  ``TaskConfig.interaction_mode`` through the
  ``tolokaforge.turn_policies`` entry-point registry.
"""
