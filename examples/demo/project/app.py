"""The demo application: a tiny consumer of brokenlib and okpkg."""

from brokenlib import greet
from okpkg import add


def welcome_line(name: str, visits: int) -> str:
    return f"{greet(name)} (visit #{add(visits, 1)})"


if __name__ == "__main__":
    print(welcome_line("world", 0))
