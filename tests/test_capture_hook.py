"""The Claude Code capture hook (clients/claude-code/hooks/cortex-capture.py) must fire once per
session, only after the 10th turn, and never loop or break a session on bad input."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parent.parent / "clients/claude-code/hooks/cortex-capture.py"
spec = importlib.util.spec_from_file_location("cortex_capture", HOOK)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def stop(session="s1", active=False, prompt=None):
    event = {"session_id": session, "hook_event_name": "Stop", "stop_hook_active": active}
    if prompt is not None:
        event["prompt_id"] = prompt
    return event


def test_stops_within_one_prompt_count_once(tmp_path):
    # Background tasks and crons wake a session and stop again with no new user prompt.
    for _ in range(30):
        assert capture.decide(stop(prompt="p1"), tmp_path) is None
    outputs = [capture.decide(stop(prompt=f"p{n}"), tmp_path) for n in range(2, 11)]
    assert outputs[:8] == [None] * 8
    assert outputs[8] == {"decision": "block", "reason": capture.REASON}


def test_ten_distinct_prompts_fire(tmp_path):
    outputs = [capture.decide(stop(prompt=f"p{n}"), tmp_path) for n in range(10)]
    assert outputs[:9] == [None] * 9 and outputs[9] is not None


def test_silent_before_turn_ten(tmp_path):
    assert [capture.decide(stop(), tmp_path) for _ in range(9)] == [None] * 9


def test_fires_once_at_turn_ten(tmp_path):
    for _ in range(9):
        capture.decide(stop(), tmp_path)
    output = capture.decide(stop(), tmp_path)
    assert output == {"decision": "block", "reason": capture.REASON}
    assert "write/update/supersede" in output["reason"]
    assert [capture.decide(stop(), tmp_path) for _ in range(5)] == [None] * 5


def test_never_fires_when_stop_hook_active(tmp_path):
    for _ in range(20):
        assert capture.decide(stop(active=True), tmp_path) is None
    # Continuations are not turns: the count did not move.
    assert [capture.decide(stop(), tmp_path) for _ in range(9)] == [None] * 9


def test_sessions_are_independent(tmp_path):
    for _ in range(10):
        capture.decide(stop("a"), tmp_path)
    assert capture.decide(stop("b"), tmp_path) is None
    assert [capture.decide(stop("b"), tmp_path) for _ in range(9)][-1] is not None


def test_bad_session_ids_are_ignored(tmp_path):
    for session in [None, "", 7, "../x", "a/b", ".."]:
        for _ in range(12):
            assert capture.decide(stop(session), tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_non_dict_event_is_ignored(tmp_path):
    assert capture.decide(["not", "a", "dict"], tmp_path) is None


def run_hook(stdin, tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, env=env, timeout=10)


def test_bad_input_exits_zero_silently(tmp_path):
    for stdin in ["", "not json", "[]", '{"session_id": 3}']:
        result = run_hook(stdin, tmp_path)
        assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_main_prints_block_on_tenth_stop(tmp_path):
    outputs = [run_hook(json.dumps(stop()), tmp_path).stdout for _ in range(10)]
    assert outputs[:9] == [""] * 9
    assert json.loads(outputs[9]) == {"decision": "block", "reason": capture.REASON}
    assert (tmp_path / ".cache/cortex-capture").is_dir()
