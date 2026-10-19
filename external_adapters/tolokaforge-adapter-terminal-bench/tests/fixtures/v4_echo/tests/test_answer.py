def test_answer() -> None:
    with open("/app/answer.txt") as handle:
        assert handle.read().strip() == "42"
