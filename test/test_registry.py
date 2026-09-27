import pytest

from similar_files import Extractor, ExtractorUnavailable, extractors, get_extractor, register


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
