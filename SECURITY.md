# Security Policy

## Supported versions

Gatekeeper is pre-release (0.0.x). Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

- Once this repository is on GitHub, use **Security → Report a vulnerability** (private vulnerability reporting).
- Until then, contact the maintainers at `<SECURITY CONTACT>`.

Include the affected commit, the steps to reproduce and the impact. We aim to acknowledge reports within 7 days.

## Scope and design commitments

Gatekeeper processes untrusted GitHub content with language models. Reports are especially welcome on:

- **Prompt injection**: PR, issue or comment text that changes the model's instructions or the actions taken. All such text must be treated as data.
- **Contributor-blindness leaks**: author identity data reaching the model or the score.
- **Unsafe actions**: any path that closes, merges, deletes or otherwise acts without the configured policy, or that auto-closes a first PR.
- **Secret exposure**: secrets in logs, audit events, test fixtures or error messages.
- **Webhook verification**: bypass of `GITHUB_WEBHOOK_SECRET` signature checks.

## Handling secrets

Secrets live only in a local `.env` file that git ignores. `make init-env` creates it and never prints a value. Private keys belong in `secrets/`, which is ignored too. CI and the pre-commit hooks run [gitleaks](https://github.com/gitleaks/gitleaks). If you leak a secret, rotate it first, then report it.
