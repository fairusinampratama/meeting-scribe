import os

import pytest
import yaml

from meeting_scribe.profile import Profile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_defaults_when_no_file():
    p = Profile({})
    assert p.language == "en"
    assert p.fixes == []
    assert p.model["threads"] >= 1          # derived from cpu_count, never 0
    assert p.tracks["mix"] == 1


def test_example_profile_is_valid():
    """The committed example must actually load -- it is the only profile a new
    user sees, and a broken one is the worst possible first impression."""
    p = Profile.load(os.path.join(HERE, "profile.example.yaml"))
    assert p.project == "acme"
    assert p.fixes, "example profile should demonstrate at least one fix"
    assert p.glossary_warning is None, "example glossary should be within the safe length"


def test_example_profile_fixes_actually_fire():
    from collections import Counter
    p = Profile.load(os.path.join(HERE, "profile.example.yaml"))
    counter = Counter()
    out = p.apply_fixes("the wij it and the ay pee eye", counter)
    assert out == "the WIDGET and the API"
    assert counter == {"WIDGET": 1, "API": 1}


def test_fixes_are_case_insensitive_and_counted():
    from collections import Counter
    p = Profile({"fixes": [[r"\bfoo ?bar\b", "FooBar"]]})
    counter = Counter()
    assert p.apply_fixes("FOO BAR and foobar", counter) == "FooBar and FooBar"
    assert counter["FooBar"] == 2


def test_bad_regex_is_rejected_at_load():
    """A malformed fix should fail loudly at load, not silently skip mid-run."""
    with pytest.raises(ValueError, match="bad regex"):
        Profile({"fixes": [["(unclosed", "x"]]})


def test_malformed_fix_entry_is_rejected():
    with pytest.raises(ValueError, match=r"fixes\[0\]"):
        Profile({"fixes": [["only-one-element"]]})


def test_long_glossary_warns():
    """Over-prompting causes repetition loops; the warning is the only guard."""
    p = Profile({"glossary": " ".join(["word"] * 200)})
    assert p.glossary_warning is not None
    assert "repetition" in p.glossary_warning


def test_partial_sections_merge_with_defaults():
    p = Profile({"model": {"name": "tiny"}, "tracks": {"mic": None}})
    assert p.model["name"] == "tiny"
    assert p.model["compute_type"] == "int8"   # default survives
    assert p.tracks["mix"] == 1                # default survives


def test_example_yaml_parses_as_yaml():
    with open(os.path.join(HERE, "profile.example.yaml"), encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert set(data) >= {"project", "glossary", "fixes", "tracks", "model"}


def test_load_records_where_the_profile_came_from(tmp_path):
    """A run must be able to record the exact profile it used. The glossary is
    the initial_prompt and the prompt changes the decoding, so a profile that
    leaves no trace makes a run impossible to reproduce or diff."""
    p = tmp_path / "profile.yaml"
    p.write_text('project: demo\nglossary: alpha beta gamma\n', encoding="utf-8")
    prof = Profile.load(str(p))
    assert prof.source_path == str(p.resolve())


def test_defaults_have_no_source_path():
    assert Profile({}).source_path is None
