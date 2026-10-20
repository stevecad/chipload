# Chipload

Chipload is growing into usable, deployable software for machine shops, starting with a shared model of what a shop has and what each resource can report or do. A shop writes one YAML or JSON description that different tools can read, instead of maintaining separate machine lists and vocabularies in every tool.

[Read the v0.1.0 specification](SPEC.md) for the current data model and a shop example. It covers shops, sites, resources, capabilities, and timestamped status reports. Runtime features, integrations, and deployment will build on that foundation as the project develops.

The repository currently contains the specification and contributor tooling. There is no runtime implementation or shop-file validator yet.

## Agent development setup

Install Git, Python 3.9 or newer, and [GitHub CLI](https://cli.github.com/). Log in using your own GitHub account before asking an agent to work:

```sh
gh auth login
gh auth status
git clone https://github.com/stevecad/chipload.git
cd chipload
```

Read [AGENTS.md](AGENTS.md) for repository rules and validation. From an agent or your terminal:

```sh
# Check open issues, oldest first (default limit: 100).
./scripts/agent-work list
./scripts/agent-work list --json

# Read an issue and its comments without changing the working tree.
./scripts/agent-work brief 1

# Create agent/issue-1 from origin's latest default branch and print a task brief.
./scripts/agent-work start 1

# Alternatively, select the oldest unassigned open issue.
./scripts/agent-work start next
```

Give your coding agent this instruction to begin:

> Read AGENTS.md and SPEC.md. Run `./scripts/agent-work list`, review a suitable issue and existing PRs, then run `./scripts/agent-work start NUMBER`. Implement the issue using the printed task brief, run the required checks, and report the result.

The script works with any agent that can run terminal commands. It prepares work; your agent does the implementation. It requires a valid `gh` login and access to the repository configured as `origin`. Listing and previewing are read-only. Starting requires a clean working tree, fetches the default branch, and switches to a new local issue branch. Existing branches are preserved; use `git switch agent/issue-NUMBER` and `brief NUMBER` to resume.

`next` does not check dependencies or claim an issue. Review assignments and open PRs before implementing, and use a separate clone or worktree for each concurrent agent. No eligible issue is a successful no-op. The script does not launch agents, assign issues, push code, open PRs, or close issues. Publish only as authorized by your development task; a PR that resolves an issue should include `Closes #NUMBER`.

## Check the tooling

```sh
python3 -m unittest discover -s tests -v
git diff --check
```

The tests use temporary Git repositories and a fake GitHub CLI. They do not need a GitHub login or change remote issues.

## License

Chipload is licensed under the [MIT License](LICENSE).
