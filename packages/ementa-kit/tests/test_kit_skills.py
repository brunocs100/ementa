from pathlib import Path

import pytest

from ementa_kit.skills import (
    SKILL_FILE,
    ScriptNotAllowedError,
    SkillError,
    SkillLibrary,
    SkillSummary,
    UnknownSkillError,
    read_skill,
)


def write_skill(root: Path, name: str, front_matter: str = "", body: str = "") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    front_matter = front_matter or f"name: {name}\ndescription: Faz {name}."
    body = body or f"# {name}\n\nPasso 1."
    (folder / SKILL_FILE).write_text(f"---\n{front_matter}\n---\n{body}\n", encoding="utf-8")
    return folder


def write_script(skill_folder: Path, name: str, code: str) -> None:
    scripts = skill_folder / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / name).write_text(code, encoding="utf-8")


def test_index_has_only_name_and_description(tmp_path: Path) -> None:
    write_skill(tmp_path, "zeta", "name: zeta\ndescription: 'Use quando: o pedido for zeta.'")
    write_skill(tmp_path, "alpha")
    (tmp_path / "notes").mkdir()  # a folder without SKILL.md is not a skill

    library = SkillLibrary(tmp_path)

    assert library.index() == [
        SkillSummary("alpha", "Faz alpha."),
        SkillSummary("zeta", "Use quando: o pedido for zeta."),
    ]
    assert library.render_index() == "- alpha: Faz alpha.\n- zeta: Use quando: o pedido for zeta."


def test_load_skill_returns_the_full_instructions(tmp_path: Path) -> None:
    folder = write_skill(
        tmp_path,
        "deadline",
        "name: deadline\ndescription: >\n  Calcula prazo\n  com a ferramenta.\nlicense: MIT\n"
        "metadata:\n  owner: ementa",
        body="# Prazo\n\nUse a ferramenta.",
    )
    write_script(folder, "check.py", "print('ok')\n")

    skill = SkillLibrary(tmp_path).load_skill("deadline")

    assert skill.description == "Calcula prazo com a ferramenta."
    assert skill.body == "# Prazo\n\nUse a ferramenta."
    assert skill.path == folder
    assert skill.scripts == ("check.py",)


def test_unknown_skill_names_the_available_ones(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")

    with pytest.raises(UnknownSkillError, match="'beta'; available: alpha"):
        SkillLibrary(tmp_path).load_skill("beta")


@pytest.mark.parametrize(
    ("folder", "content", "error"),
    [
        ("a", "# sem front matter", "missing YAML front matter"),
        ("a", "---\nname: a\ndescription: b\n", "not closed"),
        ("a", "---\nname: a\ndescription: Use quando: falta aspas\n---\nx", "not valid YAML"),
        ("a", "---\n- a\n---\nx", "must be a mapping"),
        ("a", "---\nname: a\n---\nx", "description must have"),
        ("a", "---\nname: a\ndescription: ''\n---\nx", "description must have"),
        ("a", f"---\nname: a\ndescription: {'x' * 1025}\n---\nx", "description must have"),
        ("Alpha", "---\nname: Alpha\ndescription: b\n---\nx", "name must have"),
        ("a--b", "---\nname: a--b\ndescription: b\n---\nx", "name must have"),
        ("-a", "---\nname: -a\ndescription: b\n---\nx", "name must have"),
        ("a", "---\nname: 7\ndescription: b\n---\nx", "name must have"),
        ("a", "---\nname: b\ndescription: b\n---\nx", "must match the folder name 'a'"),
        ("a", "---\nname: a\ndescription: b\nversion: 1\n---\nx", "unexpected fields.*version"),
        ("a", "---\nname: a\ndescription: b\nmetadata:\n  n: 1\n---\nx", "metadata must map"),
        ("a", "---\nname: a\ndescription: b\nlicense: [MIT]\n---\nx", "license must be"),
        ("a", "---\nname: a\ndescription: b\n---\n\n", "no instructions"),
    ],
)
def test_malformed_skill_is_rejected(tmp_path: Path, folder: str, content: str, error: str) -> None:
    skill_dir = tmp_path / folder
    skill_dir.mkdir()
    (skill_dir / SKILL_FILE).write_text(content, encoding="utf-8")

    with pytest.raises(SkillError, match=error):
        read_skill(skill_dir)


def test_library_fails_fast_on_a_malformed_skill_or_a_missing_folder(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha")
    write_skill(tmp_path, "beta", "name: beta")

    with pytest.raises(SkillError, match="beta"):
        SkillLibrary(tmp_path)
    with pytest.raises(SkillError, match="not found"):
        SkillLibrary(tmp_path / "missing")


SHOW = (
    "import os, sys\n"
    "key = os.environ.get('OPENAI_API_KEY')\n"
    "print(' '.join(sys.argv[1:]), os.path.basename(os.getcwd()), key)\n"
    "sys.exit(3 if '--fail' in sys.argv else 0)\n"
)


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    calc = write_skill(tmp_path, "calc")
    write_script(calc, "show.py", SHOW)
    write_script(calc, "slow.py", "import time\ntime.sleep(30)\n")
    write_script(calc, "run.sh", "echo hi\n")
    write_script(write_skill(tmp_path, "other"), "show.py", SHOW)
    return tmp_path


@pytest.fixture
def library(skills_root: Path) -> SkillLibrary:
    allowed = ["calc/show.py", "calc/run.sh", "calc/gone.py"]
    return SkillLibrary(skills_root, allowed_scripts=allowed)


def test_allowed_script_runs_in_the_skill_folder_without_the_secrets(
    library: SkillLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")

    result = library.run_script("calc", "show.py", ["a", "b c"])
    failed = library.run_script("calc", "show.py", ["--fail"])

    assert (result.exit_code, result.stdout.strip()) == (0, "a b c calc None")
    assert failed.exit_code == 3


@pytest.mark.parametrize(
    ("skill", "script"),
    [("other", "show.py"), ("calc", "../../other/scripts/show.py"), ("nope", "show.py")],
)
def test_script_outside_the_allowlist_is_refused(
    library: SkillLibrary, skill: str, script: str
) -> None:
    with pytest.raises(ScriptNotAllowedError, match="not in the allowed skill scripts"):
        library.run_script(skill, script)


@pytest.mark.parametrize("script", ["gone.py", "run.sh"])
def test_allowed_entry_that_is_not_a_python_script_fails(
    library: SkillLibrary, script: str
) -> None:
    with pytest.raises(SkillError, match="not a Python script"):
        library.run_script("calc", script)


def test_script_that_runs_past_the_timeout_is_stopped(skills_root: Path) -> None:
    library = SkillLibrary(skills_root, allowed_scripts=["calc/slow.py"], script_timeout_s=0.5)

    with pytest.raises(TimeoutError, match=r"calc/slow\.py did not finish in 0\.5 s"):
        library.run_script("calc", "slow.py")
