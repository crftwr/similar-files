import os
import sys
import threading
import time

import pytest

from similar_files import (
    ExtractionFailed,
    Extractor,
    ExtractorUnavailable,
    extractors,
    get_extractor,
    register,
    run_program,
)
from similar_files.model import Cancelled
from similar_files.registry import _cancel_scope


class Missing(Extractor):
    name = "test-missing"
    version = 1
    requires = ("a_module_that_is_not_installed_anywhere",)
    extra = "nothing"


def test_unavailable_extractor_registers_and_explains():
    register(Missing)
    assert "test-missing" in extractors()
    assert not Missing.is_available()
    with pytest.raises(ExtractorUnavailable) as err:
        get_extractor("test-missing")
    assert 'pip install "similar-files[nothing]"' in str(err.value)


def test_unknown_extractor():
    with pytest.raises(KeyError):
        get_extractor("no-such-thing")


def test_image_extractor_is_always_registered():
    # Registered even on a bare install; importing it must not need Pillow.
    assert "image" in extractors()


def test_params_are_checked_and_coerced():
    class P(Extractor):
        name = "test-params"
        version = 1
        default_params = {"size": 8, "fast": False}

    assert P(size="16", fast="yes").params == {"size": 16, "fast": True}
    with pytest.raises(ValueError):
        P(colour=1)
    assert P().params_digest == P(size=8).params_digest != P(size=9).params_digest


def test_a_missing_program_makes_an_extractor_unavailable(monkeypatch):
    from similar_files import Extractor, ExtractorUnavailable, get_extractor, register
    from similar_files import registry

    @register
    class NeedsTool(Extractor):
        name = "needs-tool"
        version = 1
        requires_programs = ("surely-not-a-real-program-xyz",)

    try:
        assert not NeedsTool.is_available()
        assert "surely-not-a-real-program-xyz" in NeedsTool.install_hint()
        with pytest.raises(ExtractorUnavailable):
            get_extractor("needs-tool")
    finally:
        registry._REGISTRY.pop("needs-tool", None)


SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_run_program_returns_output_and_exit_status():
    proc = run_program([sys.executable, "-c", "import sys; print('out'); sys.exit(3)"], timeout=30)
    assert proc.returncode == 3 and proc.stdout.strip() == b"out"


def test_run_program_timeout_is_transient():
    started = time.monotonic()
    with pytest.raises(ExtractionFailed) as err:
        run_program(SLEEP, timeout=0.5)
    assert err.value.transient and time.monotonic() - started < 10


def test_run_program_that_cannot_start_is_transient():
    with pytest.raises(ExtractionFailed) as err:
        run_program(["a-program-that-is-not-on-path-anywhere"], timeout=5)
    assert err.value.transient


def test_run_program_is_killed_when_the_scan_is_cancelled():
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    with _cancel_scope(stop), pytest.raises(Cancelled):
        run_program(SLEEP, timeout=60)
    assert time.monotonic() - started < 10


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_run_program_runs_outside_our_process_group():
    # A terminal's Ctrl-C goes to the foreground process group: ours, not the program's.
    proc = run_program([sys.executable, "-c", "import os; print(os.getpgrp())"], timeout=30)
    assert int(proc.stdout) != os.getpgrp()
