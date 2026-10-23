# Security

ENMA can work with local files, tools and model providers, so security issues are taken seriously.

If you find a vulnerability, please email **siddharthsinhjvaghela@gmail.com** instead of opening a public issue. Include what you found, how to reproduce it and which part of ENMA is affected. A small proof of concept is helpful, but please remove real credentials and personal data.

I will acknowledge the report as soon as I can, normally within seven days. The time needed for a fix will depend on the impact and whether the problem can be reproduced.

## A note about ENMA's boundaries

ENMA has permission checks, audit records, cancellation and validation, but it is not a complete sandbox for untrusted code. Anything running locally still needs to be treated according to the access it has on the computer.

Please never commit API keys, passwords, tokens, private keys, personal data or client information. Use the ignored `.env` file for local development secrets. If a real secret ever reaches Git history, revoke it immediately before cleaning up the repository.

Security fixes are made against the current `main` branch.
