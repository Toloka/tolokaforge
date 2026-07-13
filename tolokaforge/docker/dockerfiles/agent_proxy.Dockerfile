FROM python:3.12-slim

COPY tolokaforge/harnesses/forward_proxy.py /opt/forward_proxy.py

USER 65534:65534
EXPOSE 8080
CMD ["python", "/opt/forward_proxy.py"]

