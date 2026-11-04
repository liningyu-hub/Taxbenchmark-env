"""Port of test/reset_test.rb (2 tests)."""
import pytest

from conftest import needs_sources
from tab import TabError, environment, manifest
from tab.run import Run

pytestmark = needs_sources


def test_reset_restores_the_initial_state_and_archives_the_old_run(initialized_env):
    from conftest import key_submission_json, run_agent  # the agent CLI helpers arrive with Task 6
    run = initialized_env
    old_id = run.run_id
    initial_fingerprint = run.info["fingerprint"]
    run_agent("write", "Documents/Taxes/2025/notes.md", stdin="work in progress")
    run_agent("submit", "-", stdin=key_submission_json())
    (run.workspace / "Email/tampered.md").write_bytes(b"x")

    result = environment.reset()
    fresh = Run.current()
    assert result["fingerprint_match"]
    assert fresh.run_id != old_id
    assert manifest.fingerprint(manifest.scan(fresh.workspace)) == initial_fingerprint
    assert fresh.submission_ids() == []
    assert fresh.integrity()["clean"]
    assert not (fresh.workspace / "Documents/Taxes/2025/notes.md").exists()
    assert [a["tool"] for a in fresh.actions()] == ["init", "reset"]

    archived = Run(result["archived_to"])
    assert archived.submission_ids() == [1], "the old run and its submissions are kept in the archive"
    assert (archived.workspace / "Documents/Taxes/2025/notes.md").is_file()


def test_reset_without_a_run(home):
    with pytest.raises(TabError):
        environment.reset()
