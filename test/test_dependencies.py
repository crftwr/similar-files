"""Pillow and numpy are installed where they have wheels, and left out where pip would build them.

A wrong marker would silently drop similar images or fast video grouping on
a main platform, or fail every install on a machine without wheels, so both
lists are spelled out, with the machine names Python reports.
"""

import pathlib

import pytest

tomllib = pytest.importorskip("tomllib")  # Python 3.11+
requirements = pytest.importorskip("packaging.requirements")

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"

_WITH_WHEELS = [
    ("darwin", "arm64"), ("darwin", "x86_64"),
    ("linux", "x86_64"), ("linux", "aarch64"),
    ("win32", "AMD64"), ("win32", "ARM64"), ("win32", "x86"),
]
_WITHOUT_WHEELS = [("linux", m) for m in ("armv7l", "armv6l", "i686", "ppc64le", "s390x", "riscv64")]


def _installed(sys_platform, machine):
    with open(_PYPROJECT, "rb") as f:
        deps = tomllib.load(f)["project"]["dependencies"]
    env = {"sys_platform": sys_platform, "platform_machine": machine, "extra": ""}
    return {r.name.lower() for r in map(requirements.Requirement, deps) if r.marker is None or r.marker.evaluate(env)}


@pytest.mark.parametrize("sys_platform, machine", _WITH_WHEELS)
def test_pillow_and_numpy_come_with_the_package_where_they_have_wheels(sys_platform, machine):
    assert _installed(sys_platform, machine) == {"pillow", "numpy"}


@pytest.mark.parametrize("sys_platform, machine", _WITHOUT_WHEELS)
def test_nothing_is_required_where_pip_would_build_it(sys_platform, machine):
    assert _installed(sys_platform, machine) == set()
