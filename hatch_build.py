"""Build hook: the wheel's dependencies come from requirements.txt.

There is deliberately one dependency list, not two. requirements.txt is what the
documented venv flow installs (`pip install -r requirements.txt`) and what
mac/runtime.py pours into the app's embedded interpreter; pyproject.toml is what
a `pipx install funkuino` resolves. Duplicating the list would mean the packaged
app and the pip install could drift apart by a forgotten edit — and the symptom
would be a missing module at runtime in whichever half was not updated.

An in-tree hook keeps this free of an external build plugin: hatchling loads
this file itself, so the only build requirement stays hatchling.
"""
from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface


class RequirementsMetadataHook(MetadataHookInterface):
    def update(self, metadata: dict) -> None:
        requirements = []
        for raw in (Path(self.root) / "requirements.txt").read_text().splitlines():
            # Strip the trailing "# what it is for" comments the file uses.
            line = raw.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                requirements.append(line)
        metadata["dependencies"] = requirements
