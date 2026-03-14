---
name: commit
description: Create clean git commits with conventional messages; use when asked to commit or at logical checkpoints.
---

# Commit

## Goals

- Produce focused, logical commits that reflect actual code changes.
- Follow conventional commit conventions with proper prefixes.
- Include rationale and test status in commit body.

## Steps

1. Inspect changes: `git status`, `git diff`, `git diff --staged`.
2. Stage intended changes. Sanity-check: no build artifacts, no .env files, no temp files.
3. Choose conventional prefix matching the change:
   - `feat:` — new feature
   - `fix:` — bug fix
   - `refactor:` — code restructuring without behavior change
   - `test:` — adding or updating tests only
   - `docs:` — documentation changes only
   - `perf:` — performance improvement
   - `chore:` — maintenance, dependencies
4. Write subject line: imperative mood, ≤72 characters, no trailing period.
5. Write body with:
   - Summary of key changes (what changed)
   - Rationale (why it changed)
   - Test status: `Tests: <command run> — <result>`
6. Append trailer: `Co-authored-by: Claude <noreply@anthropic.com>`
7. Wrap body lines at 72 characters.
8. Use heredoc for commit message (avoid `-m` with newlines):

```bash
git commit -m "$(cat <<'EOF'
<type>: <short summary>

Summary:
- <what changed>

Rationale:
- <why>

Tests:
- cd backend && .venv/bin/python -m pytest tests/ -x -q — passed

Co-authored-by: Claude <noreply@anthropic.com>
EOF
)"
```

## Rules

- One logical change per commit. Do NOT batch unrelated changes.
- If staged diff includes unrelated files, fix the index first.
- Never commit .env files, API keys, or credentials.
