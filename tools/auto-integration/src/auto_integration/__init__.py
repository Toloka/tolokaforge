"""Arena model auto-integration tool.

Onboards a candidate model into the arena eval: OBSERVE its tool-calling quirks on
the default preset, RESOLVE a preset + ModelCertificate (a deterministic loop driving
a short Opus agent), and FINALIZE the integration onto the PR branch. The GitHub
Actions workflow is a thin caller of these subcommands; the logic (loop, gates,
notifications) lives here so it is testable and portable off Actions.

See ``auto_integration.cli`` for the ``auto-integration`` command surface.
"""

__version__ = "0.1.0"
