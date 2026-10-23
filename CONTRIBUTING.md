# Contributing

Thanks for taking the time to improve ENMA.

Useful contributions include bug fixes, tests, clearer documentation and small changes that make the application safer or easier to understand. Please avoid empty commits or changes made only to increase activity.

## Before you start

Check the existing issues and pull requests first. If the change is large, open an issue so we can agree on the problem and the safest approach before a lot of work is done.

Never include API keys, personal information, client data or local runtime files in a contribution.

## Running the project

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

ENMA's desktop packaging is built for Windows. If your change affects packaging or startup, please test it on Windows and say exactly what you verified.

## Sending a change

- Keep the branch and pull request focused on one problem.
- Add tests when behaviour changes.
- Explain why the change is needed, not only what files changed.
- Mention the commands you ran and the results.
- Be honest about anything you could not test.
- Keep permission checks and failure messages intact unless the change specifically improves them.

Commit messages should be short and specific. For example:

```text
fix(router): stop fallback after authentication failure
test(tasks): cover cancellation during tool execution
docs(security): explain local execution limits
```

A generated plan or mock result is not proof that something worked. Please only claim the checks you actually ran.
