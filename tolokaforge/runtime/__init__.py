"""Substrate-agnostic helpers for the runtime layer.

The ``core`` package owns orchestration and lifecycle; ``runtime`` owns
substrate-neutral utilities that runtime backends and the project loader
both consume. Modules here must not depend on any concrete backend
(docker, kubernetes, ...) — they operate on the manifest types declared
in :mod:`tolokaforge.runner.models`.
"""
