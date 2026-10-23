# Contributing to ENMA

Thank you for helping improve ENMA. Contributions should strengthen real functionality, verification, documentation, testing, or security—not activity metrics.

## Before starting

1. Check existing issues and pull requests for overlapping work.
2. For a large change, open an issue describing the problem, proposed boundary, risks, and validation plan.
3. Keep credentials, personal data, proprietary material, and local runtime files out of the repository.

## Development setup

ENMA currently targets a Windows desktop workflow while most backend and frontend development can be performed independently.

### Backend

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

### Frontend

```bat
cd frontend
npm ci
npm test
npm run build
```

### Desktop

```bat
cd desktop
npm ci
npm test
```

Packaging and host-integration changes must also be validated on Windows. Do not report a packaged build as verified when only platform-independent tests ran.

## Working agreement

- Create a focused branch such as `fix/router-auth-failure` or `docs/security-boundary`.
- Keep each pull request limited to one coherent outcome.
- Add or update tests for behavioral changes.
- Preserve truthful failure states and existing permission boundaries.
- Avoid drive-by dependency upgrades and unrelated formatting changes.
- Do not modify production credentials, local data, or another process to make a test pass.
- Document limitations that remain after the change.

## Commit messages

Use a concise, descriptive subject:

```text
type(scope): outcome
```

Examples:

- `fix(router): stop fallback after authentication failure`
- `test(tasks): cover cancellation during tool execution`
- `docs(security): clarify local execution boundary`

## Pull requests

Include:

- the problem being solved;
- the implementation approach;
- tests and commands executed;
- security, privacy, or compatibility effects;
- known limitations and rollback notes when relevant.

A plan, generated explanation, or passing mock alone is not proof that an external or packaged operation succeeded. Report only the validation that actually ran.
