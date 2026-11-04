"""What stays out of the public repository (B3): answer keys, simulated-user facts, evaluation data (checks, event
triggers, approval behaviour, traps, filing rules), the tests and demo that encode task rubrics, review documents, and
the saved Ruby run. The ignore rules are checked with git itself in a scratch repository, so the patterns are tested
as git reads them; the public persona configs are checked for evaluation fields."""
import json
import shutil
import subprocess

import pytest

from tab import ROOT

PRIVATE = ["personas/S000005/hidden/answer_key.json", "personas/S000005/hidden/simulated_user.json",
           "personas/S000007/hidden/evaluation.json", "INTERNAL_NOTES.md", "runtime/current/run.json",
           "tests/golden/saved_run/results/verify_001.json", "tests/golden/saved_run/submissions/001.json",
           "tests/hidden/test_consumer.py", "internal/tools/demo.py", "internal/docs/reviews/review.md"]
PUBLIC = ["personas/S000005/persona.json", "personas/S000005/task.md.tmpl", "personas/S000007/task_K3.md.tmpl",
          "src/tab/cli.py", "src/tab/consumer.py", "tests/conftest.py", "tests/test_agent_tools.py",
          "client/tab_client.py", "tools/agent_run.py", "README.md"]

# What a public persona.json may hold for a task and for a return's filing; everything else is evaluation data.
PUBLIC_TASK_KEYS = {"kind", "start", "template"}
PUBLIC_FILING_KEYS = {"name", "form", "balance_due", "payee"}
EVALUATION_TOP_KEYS = {"traps", "traps_not_instrumented"}


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignore_keeps_evaluation_data_out(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    shutil.copyfile(ROOT / ".gitignore", tmp_path / ".gitignore")
    for rel in PRIVATE + PUBLIC:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"x")
    result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=tmp_path,
                            input="\n".join(PRIVATE + PUBLIC).encode("utf-8"), capture_output=True)
    ignored = set(result.stdout.decode("utf-8").split())
    assert ignored == set(PRIVATE)


@pytest.mark.parametrize("persona_id", ["S000005", "S000007"])
def test_public_persona_config_holds_no_evaluation_data(persona_id):
    data = json.loads((ROOT / "personas" / persona_id / "persona.json").read_text("utf-8"))
    assert not EVALUATION_TOP_KEYS & set(data)
    for task_id, task in data["tasks"].items():
        assert set(task) <= PUBLIC_TASK_KEYS, (task_id, set(task) - PUBLIC_TASK_KEYS)
    for rid, ret in data["filing"].items():
        assert set(ret) <= PUBLIC_FILING_KEYS, (rid, set(ret) - PUBLIC_FILING_KEYS)
    assert data["evaluation"].startswith("hidden/") and data["simulated_user"].startswith("hidden/")
