"""Reference terminal front-end for tolokaforge.

Consumes the ``RunDisplayEvents`` seam (see ``tolokaforge.core.run_display_events``
and ADR-0019) and renders human-facing output on the terminal — Rich panels,
banners, dry-run rendering, and the Click command tree.

Everything under this namespace is optional at import time: the library
core (``tolokaforge.core.*``) never depends on this package. Installers
opt in via ``pip install 'tolokaforge[dx]'``.
"""
