"""Standalone tolokaforge grader service — per ADR-0035.

This package holds the grader-plane surface that the runtime-independence work
(ADR-0022) prepared for: a dedicated gRPC contract (``grader.proto``), a
:class:`GrpcGraderClient` the plug-in seam dials, and a :class:`GraderServiceImpl`
that answers grade requests over the wire. The plug-in seam consumer is
:class:`~tolokaforge.core.trial_grader.GraderRPCTrialGrader`; the CLI entry
point (``python -m tolokaforge.grader``) starts the service standalone.

The wire types live in :mod:`grader_pb2` and mirror runner.proto's Grade
messages: the payload is unchanged across the split — only its hosting is —
so that the two RPCs remain interchangeable at the plug-in seam layer.
"""

from tolokaforge.grader.client import GrpcGraderClient
from tolokaforge.grader.service import GradeDispatch, GraderServiceImpl, JudgeGradeFn

__all__ = [
    "GradeDispatch",
    "GraderServiceImpl",
    "GrpcGraderClient",
    "JudgeGradeFn",
]
