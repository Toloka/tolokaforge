"""Adapter-package pytest configuration.

Kept as a boundary marker: the reusable
:class:`~tolokaforge.testing.adapters.AdapterGradingContractSuite` needs no
shared fixtures beyond the two the subclass supplies. A later suite that does
declares its ``pytest_plugins`` here.
"""
