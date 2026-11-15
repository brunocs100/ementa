"""Runtime skills in the open Agent Skills format (https://agentskills.io/specification).

A skill is a folder `<root>/<name>/` with a `SKILL.md`: YAML front matter with `name` and
`description`, then the Markdown instructions. An optional `scripts/` folder holds code.

Progressive disclosure: the context gets only the index (name and description of each skill),
`load_skill` returns the full instructions when the agent needs them, and a script runs only
when `<skill>/<script>` is in the allowlist from the settings, with a timeout.
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ementa_kit.telemetry import get_logger, get_tracer

SKILL_FILE = "SKILL.md"
NAME_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500

_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
_NAME = re.compile(NAME_PATTERN)
_SCRIPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.py$")
# A script gets what Python needs to start, never the secrets of the process (API keys, database).
_SCRIPT_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "LANG")

tracer = get_tracer(__name__)
log = get_logger(__name__)


class SkillError(ValueError):
    """A SKILL.md outside the format, or a script that cannot run."""


class UnknownSkillError(LookupError):
    pass


class ScriptNotAllowedError(PermissionError):
    pass


@dataclass(frozen=True)
class SkillSummary:
    """What the context gets for each skill: name and description, nothing else."""

    name: str
    description: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    scripts: tuple[str, ...] = ()

    @property
    def summary(self) -> SkillSummary:
        return SkillSummary(self.name, self.description)


@dataclass(frozen=True)
class ScriptResult:
    skill: str
    script: str
    exit_code: int
    stdout: str
    stderr: str


def read_skill(directory: Path) -> Skill:
    """Read and check `directory/SKILL.md`. Raises SkillError naming the file and the problem."""
    path = directory / SKILL_FILE
    meta, body = _split(path.read_text(encoding="utf-8"), path)
    _check(meta, body, directory.name, path)
    scripts = sorted(p.name for p in (directory / "scripts").glob("*") if p.is_file())
    return Skill(meta["name"], meta["description"].strip(), body, directory, tuple(scripts))


def _split(raw: str, path: Path) -> tuple[dict[Any, Any], str]:
    lines = raw.split("\n")
    if lines[0].rstrip() != "---":
        raise SkillError(f"{path}: missing YAML front matter")
    end = next((i for i, line in enumerate(lines[1:], start=1) if line.rstrip() == "---"), None)
    if end is None:
        raise SkillError(f"{path}: front matter is not closed with ---")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise SkillError(f"{path}: front matter is not valid YAML: {error}") from None
    if not isinstance(meta, dict):
        raise SkillError(f"{path}: front matter must be a mapping with name and description")
    return meta, "\n".join(lines[end + 1 :]).strip()


def _check(meta: dict[Any, Any], body: str, folder: str, path: Path) -> None:
    unknown = sorted(str(key) for key in meta.keys() - _FIELDS)
    if unknown:
        raise SkillError(f"{path}: unexpected fields in front matter: {', '.join(unknown)}")
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or len(name) > MAX_NAME or not _NAME.match(name):
        raise SkillError(
            f"{path}: name must have 1 to {MAX_NAME} lowercase letters, digits and single hyphens"
        )
    if name != folder:
        raise SkillError(f"{path}: name {name!r} must match the folder name {folder!r}")
    if not isinstance(description, str) or not 0 < len(description.strip()) <= MAX_DESCRIPTION:
        raise SkillError(f"{path}: description must have 1 to {MAX_DESCRIPTION} characters")
    for key in ("license", "compatibility", "allowed-tools"):
        if key in meta and not isinstance(meta[key], str):
            raise SkillError(f"{path}: {key} must be a string")
    if len(meta.get("compatibility", "")) > MAX_COMPATIBILITY:
        raise SkillError(f"{path}: compatibility must have at most {MAX_COMPATIBILITY} characters")
    metadata = meta.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise SkillError(f"{path}: metadata must map strings to strings")
    if not body:
        raise SkillError(f"{path}: no instructions after the front matter")


def _script_env() -> dict[str, str]:
    env = {key: value for key in _SCRIPT_ENV if (value := os.environ.get(key)) is not None}
    env["PYTHONUTF8"] = "1"  # the same text encoding on Windows and Linux
    return env


class SkillLibrary:
    """The skills under `root`, read and checked once: a malformed skill fails here."""

    def __init__(
        self,
        root: Path,
        allowed_scripts: Iterable[str] = (),
        script_timeout_s: float = 10.0,
    ) -> None:
        if not root.is_dir():
            raise SkillError(f"{root}: skills folder not found")
        skills = (read_skill(path.parent) for path in sorted(root.glob(f"*/{SKILL_FILE}")))
        self._skills = {skill.name: skill for skill in skills}
        self._allowed = frozenset(allowed_scripts)
        self.script_timeout_s = script_timeout_s

    def index(self) -> list[SkillSummary]:
        """Name and description of every skill, by name. This is what goes into the context."""
        return [self._skills[name].summary for name in sorted(self._skills)]

    def render_index(self) -> str:
        """The index as text, one skill per line, for the skills section of the context."""
        return "\n".join(f"- {s.name}: {s.description}" for s in self.index())

    def load_skill(self, name: str) -> Skill:
        """The full skill, with its instructions, for when the agent decides to use it."""
        try:
            return self._skills[name]
        except KeyError:
            known = ", ".join(sorted(self._skills)) or "none"
            raise UnknownSkillError(f"unknown skill {name!r}; available: {known}") from None

    def run_script(self, skill: str, script: str, args: Sequence[str] = ()) -> ScriptResult:
        """Run `scripts/<script>` of a skill with this Python, if `<skill>/<script>` is allowed.

        No shell, the skill folder as working directory, a reduced environment and the timeout
        from the settings. Raises ScriptNotAllowedError for anything outside the allowlist.
        """
        key = f"{skill}/{script}"
        with tracer.start_as_current_span(
            f"execute_tool {key}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": key,
                "ementa.skill.name": skill,
            },
        ) as span:
            allowed = key in self._allowed
            span.set_attribute("ementa.skill.script.allowed", allowed)
            if not allowed:
                log.warning("skill_script_denied", skill=skill, script=script)
                raise ScriptNotAllowedError(f"{key} is not in the allowed skill scripts")
            path = self._script_path(skill, script)
            try:
                done = subprocess.run(
                    [sys.executable, str(path), *args],
                    cwd=path.parent.parent,
                    env=_script_env(),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.script_timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise TimeoutError(f"{key} did not finish in {self.script_timeout_s} s") from None
            span.set_attribute("ementa.skill.script.exit_code", done.returncode)
            return ScriptResult(skill, script, done.returncode, done.stdout, done.stderr)

    def _script_path(self, skill: str, script: str) -> Path:
        folder = (self.load_skill(skill).path / "scripts").resolve()
        path = folder / script
        # A plain .py file name, and no symlink that leads out of scripts/.
        if not _SCRIPT.match(script) or not path.is_file() or path.resolve().parent != folder:
            raise SkillError(f"{skill}/{script}: not a Python script in {folder}")
        return path
