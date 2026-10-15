"""Trial node service — a node that owns one whole trial.

The control plane ships serialized :class:`~tolokaforge.core.trial.TrialNodeInputs`
over :class:`~tolokaforge.trial_node.client.TrialNodeClient`;
:class:`~tolokaforge.trial_node.service.TrialNodeServiceImpl` runs the trial on the
node (conductor, agent loop and substrate all node-side) and answers with the graded
:class:`~tolokaforge.core.trial.TrialResult`. The wire contract is
``trial_node.proto`` and its version gate is
:data:`~tolokaforge.trial_node.protocol.NODE_PROTOCOL_VERSION`; the CLI entry point
(``tolokaforge trial-node``, or ``python -m tolokaforge.trial_node``) starts the
service. This package is the service; the trial body it runs lives in
:mod:`tolokaforge.core.trial_node`.

The package init imports nothing: the control plane's dispatch code reads
:mod:`tolokaforge.trial_node.protocol` while the service module imports the trial
body, so an eager init would close an import cycle.
"""
