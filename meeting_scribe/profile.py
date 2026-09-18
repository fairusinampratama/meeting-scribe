"""Profile loading. All project-specific and person-identifying config lives
here, loaded from a gitignored YAML file -- never hardcoded."""
import os
import re

import yaml

DEFAULTS = {
    "project": "meeting",
    "language": "en",
    "glossary": "",
    "fixes": [],
    "tracks": {"mix": 1, "desktop": None, "mic": None},
    "speakers": {"mic_label": "ME", "other_label": "CALL", "mic_threshold": 0.8},
    "model": {"name": "large-v3-turbo", "compute_type": "int8", "threads": 0},
}


class Profile:
    def __init__(self, data):
        d = {**DEFAULTS, **(data or {})}
        for k in ("tracks", "speakers", "model"):
            d[k] = {**DEFAULTS[k], **(d.get(k) or {})}

        self.project = str(d["project"]).lower()
        self.language = d["language"]
        self.glossary = (d["glossary"] or "").strip()
        self.tracks = d["tracks"]
        self.speakers = d["speakers"]
        self.model = d["model"]

        if not self.model.get("threads"):
            self.model["threads"] = max(1, (os.cpu_count() or 4) - 2)

        self.fixes = []
        for i, entry in enumerate(d["fixes"] or []):
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                raise ValueError(f"fixes[{i}] must be [regex, replacement]")
            pattern, replacement = entry
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"fixes[{i}] bad regex {pattern!r}: {exc}") from exc
            self.fixes.append((pattern, replacement))

        words = len(self.glossary.split())
        self.glossary_warning = (
            f"glossary is {words} words; over ~80 tends to cause repetition loops"
            if words > 80 else None
        )

    @classmethod
    def load(cls, path=None):
        """Load profile.yaml. Falls back to defaults with an empty glossary,
        which still transcribes fine -- just without domain-term help."""
        if path is None:
            for cand in ("profile.yaml", "profile.yml"):
                if os.path.exists(cand):
                    path = cand
                    break
        if path is None:
            return cls({})
        with open(path, encoding="utf-8") as fh:
            return cls(yaml.safe_load(fh))

    def apply_fixes(self, text, counter=None):
        for pattern, replacement in self.fixes:
            text, n = re.subn(pattern, replacement, text, flags=re.I)
            if n and counter is not None:
                counter[replacement] += n
        return text
