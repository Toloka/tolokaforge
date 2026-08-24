"""Integration proof for :mod:`tolokaforge_coding_harnesses` against real containers.

* ``test_container_injection_docker`` — ``DockerExecInjector`` writes files into
  a live ``alpine:3.20`` container: a missing parent directory is created, a
  ``$``- and backtick-carrying credential survives a real shell, the requested
  mode lands, and an injection-shaped path stays a filename.

The suite lives under the root ``tests/integration/`` tree rather than beside
the package because CI invokes the integration lane **by directory**
(``uv run pytest tests/integration/ …``). A package-local integration root
would be collected locally and run nowhere.

Marked ``integration`` + ``docker`` — requires a real Docker daemon.
"""
