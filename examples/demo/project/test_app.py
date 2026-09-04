"""The demo project's test suite: plain asserts, no test framework needed."""

from app import welcome_line


def main() -> int:
    expected = "hello, world (visit #1)"
    actual = welcome_line("world", 0)
    if actual != expected:
        print(f"FAIL: expected {expected!r}, got {actual!r}")
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
