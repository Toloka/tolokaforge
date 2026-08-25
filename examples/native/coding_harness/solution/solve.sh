#!/bin/bash
# Oracle fix — writes the correct factorial so the verifier gives reward 1.0.
set -euo pipefail

cat > /work/factorial.py << 'PY'
def factorial(n: int) -> int:
    """Return the factorial of a non-negative integer *n*.

    Raises ``ValueError`` for negative input.
    """
    if n < 0:
        raise ValueError("factorial is undefined for negative integers")
    if n <= 1:
        return 1
    return n * factorial(n - 1)
PY
