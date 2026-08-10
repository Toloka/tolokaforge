"""Parametrised capability probe suite — public engine seam.

Every module here declares a ``test_<capability>`` body parametrised
over :data:`tolokaforge.testing.certify.ALL_MODELS`. Collectable via::

    pytest --pyargs tolokaforge.testing.certify.suite -k <model_id>

Out-of-tree callers ``pip install tolokaforge`` and drive the same
suite against their own ``ALL_MODELS``-shaped tuple by re-exporting
the fixtures from :mod:`tolokaforge.testing.certify.fixtures` in their
own conftest.
"""
