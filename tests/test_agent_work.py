"""Exercise the CLI with real local Git operations and a fake GitHub service."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent-work"

FAKE_GH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text())
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] == ["auth", "status"]:
    sys.exit(1 if state.get("auth_failed") else 0)
elif args[:2] == ["repo", "view"]:
    result = {"nameWithOwner": "example/chipload", "url": "https://github.com/example/chipload",
              "defaultBranchRef": {"name": "main"}}
elif args[:2] == ["issue", "list"]:
    result = [issue for issue in state["issues"] if issue["state"] == "OPEN"]
    if "no:assignee" in args[args.index("--search") + 1]:
        result = [issue for issue in result if not issue["assignees"]]
    result = result[:int(args[args.index("--limit") + 1])]
elif args[:2] == ["issue", "view"]:
    result = next((issue for issue in state["issues"] if str(issue["number"]) == args[2]), None)
    if result is None:
        print("Issue not found", file=sys.stderr)
        sys.exit(1)
else:
    print("Unexpected GitHub operation", file=sys.stderr)
    sys.exit(1)
print(json.dumps(result))
'''


def issue(number=1, **overrides):
    value = {
        "number": number, "title": "Write a readme", "body": "Describe setup.",
        "state": "OPEN", "url": f"https://github.com/example/chipload/issues/{number}",
        "labels": [], "assignees": [], "comments": [],
    }
    value.update(overrides)
    return value


class AgentWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.seed = self.base / "seed"
        self.checkout = self.base / "checkout"
        self.state_path = self.base / "state.json"
        self.log_path = self.base / "gh.log"
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.env = dict(os.environ)
        # Do not inherit credentials or the contributor's Git configuration.
        self.env.update({
            "PATH": str(bin_dir) + os.pathsep + self.env["PATH"],
            "HOME": str(self.base), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "FAKE_GH_STATE": str(self.state_path), "FAKE_GH_LOG": str(self.log_path),
        })
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("init", "--initial-branch=main", str(self.seed))
        (self.seed / "tracked.txt").write_text("initial\n")
        self.commit(self.seed)
        self.git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        self.git("push", "origin", "main", cwd=self.seed)
        self.git("clone", str(self.remote), str(self.checkout))
        self.set_state(issues=[issue()])

    def git(self, *args, cwd=None):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.base, env=self.env,
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def commit(self, directory):
        self.git("add", ".", cwd=directory)
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false", "commit", "-m", "Fixture", cwd=directory,
        )

    def set_state(self, **state):
        self.state_path.write_text(json.dumps(state))

    def invoke(self, *args, cwd=None):
        return subprocess.run(
            [str(SCRIPT), *args], cwd=cwd or self.checkout, env=self.env,
            capture_output=True, text=True,
        )

    def branch(self):
        return self.git("branch", "--show-current", cwd=self.checkout)

    def test_list_json_and_limit(self):
        self.set_state(issues=[issue(1), issue(2)])
        result = self.invoke("list", "--json", "--limit", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry["number"] for entry in json.loads(result.stdout)], [1])
        self.assertEqual(self.branch(), "main")

    def test_brief_includes_comments_and_preserves_dirty_tree(self):
        self.set_state(issues=[issue(comments=[{
            "author": None, "body": "Keep $(shell text) as text.",
        }])])
        (self.checkout / "untracked.txt").write_text("in progress")
        result = self.invoke("brief", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Keep $(shell text) as text.", result.stdout)
        self.assertIn("Comment by deleted user", result.stdout)
        self.assertIn("Read AGENTS.md and SPEC.md", result.stdout)
        self.assertEqual(self.branch(), "main")
        self.assertEqual((self.checkout / "untracked.txt").read_text(), "in progress")

    def test_start_uses_latest_remote_default_not_current_branch(self):
        self.git("switch", "-c", "other-work", cwd=self.checkout)
        (self.checkout / "local.txt").write_text("local work")
        self.commit(self.checkout)
        (self.seed / "upstream.txt").write_text("new upstream work")
        self.commit(self.seed)
        self.git("push", "origin", "main", cwd=self.seed)
        expected = self.git("rev-parse", "HEAD", cwd=self.seed)
        result = self.invoke("start", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.branch(), "agent/issue-1")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.checkout), expected)
        self.assertTrue((self.checkout / "upstream.txt").exists())
        self.assertFalse((self.checkout / "local.txt").exists())
        self.assertIn("Closes #1", result.stdout)
        self.assertIn("other-work", self.git("branch", "--list", cwd=self.checkout))

    def test_start_next_skips_assigned_issues(self):
        self.set_state(issues=[issue(1, assignees=[{"login": "someone"}]), issue(2)])
        result = self.invoke("start", "next")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.branch(), "agent/issue-2")

    def test_no_eligible_issues_is_success_without_branch_change(self):
        self.set_state(issues=[issue(1, assignees=[{"login": "someone"}])])
        result = self.invoke("start", "next")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing to start", result.stdout)
        self.assertEqual(self.branch(), "main")

    def test_empty_issue_list(self):
        self.set_state(issues=[])
        result = self.invoke("list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No open issues", result.stdout)

    def test_start_refuses_tracked_and_untracked_changes(self):
        for name in ("tracked.txt", "untracked.txt"):
            with self.subTest(name=name):
                path = self.checkout / name
                path.write_text("unfinished work\n")
                result = self.invoke("start", "1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Working tree is not clean", result.stderr)
                self.assertEqual(path.read_text(), "unfinished work\n")
                self.assertEqual(self.branch(), "main")
                if name == "tracked.txt":
                    self.git("restore", name, cwd=self.checkout)
                else:
                    path.unlink()

    def test_existing_branch_is_preserved(self):
        self.git("branch", "agent/issue-1", cwd=self.checkout)
        before = self.git("rev-parse", "agent/issue-1", cwd=self.checkout)
        result = self.invoke("start", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(self.git("rev-parse", "agent/issue-1", cwd=self.checkout), before)

    def test_closed_and_missing_issues_do_not_change_branch(self):
        self.set_state(issues=[issue(state="CLOSED")])
        for number, message in (("1", "not open"), ("2", "Issue not found")):
            with self.subTest(number=number):
                result = self.invoke("start", number)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertEqual(self.branch(), "main")

    def test_auth_failure_explains_how_to_login(self):
        self.set_state(issues=[issue()], auth_failed=True)
        result = self.invoke("start", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gh auth login", result.stderr)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(len(self.log_path.read_text().splitlines()), 1)

    def test_fetch_failure_does_not_create_or_switch_branch(self):
        self.git("remote", "set-url", "origin", str(self.base / "missing.git"), cwd=self.checkout)
        result = self.invoke("start", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("git fetch failed", result.stderr)
        self.assertEqual(self.branch(), "main")
        self.assertEqual(self.git("branch", "--list", "agent/issue-1", cwd=self.checkout), "")

    def test_invalid_selectors_fail_before_github_calls(self):
        for value in ("0", "-1", "invalid", "1;echo bad"):
            with self.subTest(value=value):
                result = self.invoke("start", value)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.log_path.exists())

    def test_works_from_subdirectory_and_pins_repository_to_origin(self):
        subdir = self.checkout / "nested"
        subdir.mkdir()
        result = self.invoke("brief", "1", cwd=subdir)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        repo_call = next(call for call in calls if call[:2] == ["repo", "view"])
        self.assertEqual(repo_call[2], str(self.remote))
        issue_call = next(call for call in calls if call[:2] == ["issue", "view"])
        self.assertEqual(issue_call[issue_call.index("--repo") + 1], "github.com/example/chipload")


if __name__ == "__main__":
    unittest.main()
