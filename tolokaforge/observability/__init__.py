"""Live tracing seam (ADR-0046): a ``TrialObserver`` the conductor and the tool-calling loop report
to, deterministic ids shared with the offline bundle uploader, and model-name resolution as
configuration. The OTLP exporter lives in :mod:`tolokaforge.observability.otel` behind the ``otel``
extra; core imports nothing from it."""
