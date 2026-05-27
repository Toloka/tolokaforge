Parser hardening brief

The parser is fed values from mixed systems and currently crashes on common formats.

Edge cases that must be handled safely:
- "1,200"
- "2_500"
- " 42 "
- "-3"
- "3.14"
- "N/A"
- ""
- None

Expected behavior:
- Positive integers parse to integer value.
- Negative, decimal, empty, and non-numeric inputs return 0.
