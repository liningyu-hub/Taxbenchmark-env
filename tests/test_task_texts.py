"""Each task's text gives the person's request exactly as Task_Scope.md words it (Build Guide 2.3: the agent receives
the prompt as written, and nothing else about the task). The tool reference and the submission format stay: they are
how this environment is used, not hints about the task."""
import json
import re

import pytest

from conftest import agent, needs_sources
from tab import config, environment
from tab.persona import Persona

pytestmark = needs_sources
PERSONAS = {"S000005": "S000005_Thomas_Gonzalez", "S000007": "S000007_Carlos_Jones"}
_HEADING = re.compile(r'^\*\*([TKC]\d)\. (?:Standing rule: )?"(.+?)"\*\*', re.MULTILINE)


def prompts() -> dict:
    """(persona id, task id) -> the quoted request, from Task_Scope.md's task headings, section by persona."""
    text = (config.day1_dir() / "Task_Scope.md").read_text("utf-8")
    found, persona = {}, None
    for line in text.split("\n"):
        persona = next((pid for pid in PERSONAS if line.startswith(f"## {pid}")), persona)
        m = _HEADING.match(line)
        if m and persona:
            found[(persona, m.group(1))] = m.group(2)
    return found


def cases():
    out = []
    for pid in PERSONAS:
        for tid in json.loads((config.personas_dir() / pid / "persona.json").read_text("utf-8"))["tasks"]:
            out.append((pid, tid))
    return out


@pytest.mark.parametrize("persona_id, task_id", cases())
def test_every_task_text_quotes_its_task_scope_prompt(home, persona_id, task_id):
    if not (config.day1_dir() / PERSONAS[persona_id]).is_dir():
        pytest.skip(f"needs {persona_id}'s drafts")
    environment.init(persona_id, task_id=task_id)
    assert f"> {prompts()[(persona_id, task_id)]}" in agent("task")[1]


@pytest.mark.parametrize("persona_id, task_id", [("S000005", "T1"), ("S000005", "T3"), ("S000007", "K1"),
                                                 ("S000007", "K2")])
def test_tax_task_texts_name_no_forms_or_steps(home, persona_id, task_id):
    if not (config.day1_dir() / PERSONAS[persona_id]).is_dir():
        pytest.skip(f"needs {persona_id}'s drafts")
    environment.init(persona_id, task_id=task_id)
    opening = agent("task")[1].split("## How to work")[0]
    assert not re.search(r"Form 1040|Form 540|IT-201|approval|e-file|pay any balance", opening), opening


def test_k3_text_adds_nothing_to_the_question(home):
    if not (config.day1_dir() / PERSONAS["S000007"]).is_dir():
        pytest.skip("needs S000007's drafts")
    environment.init("S000007", task_id="K3")
    assert "taxable" not in agent("task")[1].split("## How to work")[0]
