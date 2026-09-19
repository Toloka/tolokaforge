"""Live tracing seam (ADR-0047): a ``TrialObserver`` the conductor and the tool-calling loop report
to, deterministic ids shared with the offline bundle uploader, and the factory that asks the
installed trial-observer plugins (entry-point group ``tolokaforge.trial_observers``) for the run's
observer. The Langfuse observer lives in the ``tolokaforge-langfuse`` distribution (the ``otel``
extra); core imports nothing from it."""
