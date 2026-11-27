#!/usr/bin/env python3
"""Stop hook: once per session, after the 10th turn, ask the agent to save what is worth keeping
to Cortex. Short sessions rarely produce a lasting fact, and asking every turn would nag, so it
asks once. Fails silent: a hook error must never break a session. Install: clients/README.md.

Contract (https://code.claude.com/docs/en/hooks, "Stop input" and "Stop decision control"):
stdin carries session_id and stop_hook_active ("true when Claude Code is already continuing as a
result of a stop hook"); printing {"decision": "block", "reason": ...} on exit 0 keeps Claude
going with the reason as its instruction. Stop "does not run if the stoppage occurred due to a
user interrupt".

Turns are counted here rather than parsed from transcript_path: the docs do not describe the
transcript format and say it is written asynchronously. A turn is a new prompt_id ("UUID
identifying the user prompt currently being processed"), because background tasks and crons can
wake a session and stop again within one prompt. Without prompt_id every Stop counts. An
interrupted turn fires no Stop, so the count may run late.
"""

import json
import re
import sys
from pathlib import Path

TURNS = 10
REASON = ("If this session produced a lasting fact about the owner, a decision or a preference, "
          "save at most 3 through Cortex write/update/supersede; code and service facts go to "
          "their repo; otherwise do nothing.")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # becomes a file name, so no paths


def decide(event, state_dir: Path) -> dict | None:
    """Count this Stop and return the block output on the 10th turn, else None."""
    if not isinstance(event, dict) or event.get("stop_hook_active"):
        return None  # a continuation we caused is not a turn; blocking again could loop
    session = event.get("session_id")
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return None
    fired = state_dir / f"{session}.fired"
    if fired.exists():
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    counter = state_dir / f"{session}.turns"
    state = json.loads(counter.read_text()) if counter.exists() else {"turns": 0, "prompt": None}
    prompt = event.get("prompt_id")
    if prompt is not None and prompt == state["prompt"]:
        return None  # another Stop within the same user prompt
    turns = state["turns"] + 1
    if turns < TURNS:
        counter.write_text(json.dumps({"turns": turns, "prompt": prompt}))
        return None
    fired.touch()  # marker first: if printing fails, we still never fire twice
    counter.unlink(missing_ok=True)
    return {"decision": "block", "reason": REASON}


def main() -> None:
    output = decide(json.load(sys.stdin), Path("~/.cache/cortex-capture").expanduser())
    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
