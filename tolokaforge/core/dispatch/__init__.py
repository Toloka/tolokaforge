"""The dispatch-adapter seam: where a whole trial runs.

A :class:`~tolokaforge.core.dispatch.adapter.DispatchAdapter` takes one trial's
:class:`~tolokaforge.core.trial.TrialNodeInputs` and returns its graded
:class:`~tolokaforge.core.trial.TrialResult`. Adapters register under the
``tolokaforge.dispatch_adapters`` entry-point group and raise the
transport-neutral exceptions in :mod:`tolokaforge.core.dispatch.errors`.

Submodules are imported explicitly by their users; this package imports nothing.
"""
