#!/usr/bin/env python3
"""Run an actual Claude Code edit/test task against the local Notion gateway."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    run = ROOT / ".local" / ("acceptance-" + time.strftime("%Y%m%d-%H%M%S"))
    run.mkdir(parents=True, mode=0o700)
    original = 'def parse_duration(text):\n    """Return a duration in milliseconds."""\n    return int(text[:-1]) * 1000\n'
    (run / "duration.py").write_text(original)
    (run / "README.md").write_text(
        "Repair parse_duration(text) in duration.py. Accept a non-negative decimal number followed by "
        "ms, s, or m (milliseconds, seconds, minutes). Leading/trailing whitespace is allowed. "
        "Return an integer number of milliseconds. A fractional result smaller than a whole millisecond "
        "must raise ValueError; never round it. Reject malformed strings, missing units, signs, exponent "
        "notation and negative durations with ValueError. Non-string inputs raise TypeError. "
        "Zero is valid. Do not modify the tests. Run python3 -m unittest -v.\n"
    )
    (run / "test_duration.py").write_text('''import unittest
from duration import parse_duration

class DurationTests(unittest.TestCase):
    def test_units(self):
        for value, expected in [("500ms",500),("2s",2000),("1.5m",90000),(" 0s ",0),("0.125s",125)]:
            with self.subTest(value=value): self.assertEqual(parse_duration(value),expected)
    def test_invalid(self):
        for value in ["", "12", "-1s", "+2s", "1e3s", "2ss", "0.1ms"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError): parse_duration(value)
    def test_type(self):
        for value in [None, 2, True]:
            with self.subTest(value=value):
                with self.assertRaises(TypeError): parse_duration(value)

if __name__ == "__main__": unittest.main()
''')
    baseline = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=run, capture_output=True, text=True)
    (run / "baseline-tests.log").write_text(baseline.stdout + baseline.stderr)
    if baseline.returncode == 0:
        raise SystemExit("Invalid fixture: baseline unexpectedly passes")
    tests_hash = hashlib.sha256((run / "test_duration.py").read_bytes()).hexdigest()
    command = [sys.executable, str(ROOT / "scripts/claude_notion.py"), "--print",
               "--safe-mode", "--restricted", "--setting-sources", "", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--no-session-persistence",
               "--permission-mode", "acceptEdits", "--tools", "Read,Edit,Write,Bash,Glob,Grep",
               "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(python3 -m unittest*),Bash(ls*)",
               "--output-format", "stream-json", "--verbose",
               "Inspect this small Python project and repair duration.py according to README.md. "
               "Use your local tools to read the files, make the actual code changes, and run the tests. "
               "Do not modify tests or README. Report the test result after running it."]
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    start = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=run, env=env, capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        raise SystemExit("Claude Code acceptance timed out; inspect gateway audit")
    (run / "claude-events.jsonl").write_text(completed.stdout)
    (run / "claude-stderr.log").write_text(completed.stderr)
    after = (run / "duration.py").read_text()
    (run / "changes.diff").write_text("".join(difflib.unified_diff(original.splitlines(True),after.splitlines(True),
                                                                 fromfile="a/duration.py",tofile="b/duration.py")))
    tests = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=run, capture_output=True, text=True)
    (run / "final-tests.log").write_text(tests.stdout + tests.stderr)
    # This independent verification is not part of the client prompt or its working files.
    verifier = '''from duration import parse_duration as p
assert p("2.5m") == 150000
assert p("0007ms") == 7
assert p("0.001s") == 1
assert p("12.345s") == 12345
for s in ["NaNs", "infs", "2 m", "1.001ms", "1e-3s", "1s junk"]:
 try: p(s)
 except ValueError: pass
 else: raise AssertionError(s)
print("independent checks passed")
'''
    independent = subprocess.run([sys.executable, "-c", verifier], cwd=run, capture_output=True, text=True)
    (run / "independent-checks.log").write_text(independent.stdout + independent.stderr)
    events = []
    for line in completed.stdout.splitlines():
        try: events.append(json.loads(line))
        except ValueError: pass
    tool_names = [b.get("name") for e in events if e.get("type") == "assistant"
                  for b in e.get("message",{}).get("content",[]) if b.get("type") == "tool_use"]
    results = [e for e in events if e.get("type") == "result"]
    unchanged = hashlib.sha256((run / "test_duration.py").read_bytes()).hexdigest() == tests_hash
    passed = (completed.returncode == 0 and original != after and tests.returncode == 0
              and independent.returncode == 0 and unchanged and "Bash" in tool_names
              and bool({"Edit","Write"}.intersection(tool_names)))
    report = {"passed": passed, "run_dir": str(run), "duration_s": round(time.monotonic()-start,2),
              "baseline_failed": baseline.returncode != 0, "client_exit_code": completed.returncode,
              "changed_code": original != after, "tests_unchanged": unchanged,
              "visible_tests_passed": tests.returncode == 0, "independent_checks_passed": independent.returncode == 0,
              "client_tool_calls": tool_names, "client_result": results[-1] if results else None,
              "stderr_tail": completed.stderr[-1500:]}
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
