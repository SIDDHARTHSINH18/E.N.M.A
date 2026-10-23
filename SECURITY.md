# Security Policy

## Reporting a vulnerability

Please do not report suspected vulnerabilities through a public GitHub issue.

Email **siddharthsinhjvaghela@gmail.com** with:

- the affected component and version or commit;
- a concise description of the issue and its impact;
- reproduction steps or a minimal proof of concept;
- any suggested mitigation, if available.

Do not include real credentials, personal information, or data belonging to another person.

You should receive an acknowledgement within seven days. Fix timelines depend on severity, reproducibility, and the affected execution boundary.

## Security boundaries

ENMA is a local-first desktop application that can coordinate models, tools, filesystem operations, and local processes. Permission checks, audit records, cancellation, and post-operation validation reduce risk, but they do not make the host an unrestricted sandbox.

Treat locally executed tools and third-party model providers according to their actual privileges and privacy policies. Use test data during development and grant only the permissions required for a task.

## Credentials and sensitive data

- Never commit API keys, access tokens, passwords, private keys, or credential-bearing configuration.
- Store development secrets in ignored local configuration such as `.env`.
- Use placeholders in examples and documentation.
- Revoke and rotate any credential that may have entered Git history.
- Remove personal, client, and production data from bug reports and test fixtures.

## Supported code

Security fixes are applied to the current `main` branch. Historical releases and local forks may not receive updates.

## Responsible disclosure

Please allow reasonable time to investigate and correct a confirmed issue before publishing technical details.
