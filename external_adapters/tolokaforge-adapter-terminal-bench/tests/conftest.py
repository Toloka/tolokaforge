# Fixture tasks under tests/fixtures/ ship their own in-container verifier tests
# (e.g. v4_echo/tests/test_answer.py) that are meant to run inside a task's
# verifier container, where paths such as /app/answer.txt exist. They are task
# content, not adapter tests, so they must never be collected by the host run.
collect_ignore = ["fixtures"]
