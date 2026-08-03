# Security Policy

## Supported State

Research Control Plane is pre-release software. Security fixes target the
latest default branch; no long-term support or backport policy is currently
promised.

## Reporting A Vulnerability

Use GitHub private vulnerability reporting for this repository. If that channel
is unavailable, open a non-sensitive issue asking the maintainers to establish
a private contact. Do not include exploit details, credentials, private paths,
Session capability tokens, webhook payloads, or personal data in a public issue.

Include the affected version or commit, the violated trust boundary, a minimal
reproduction, and the expected impact. Maintainers will acknowledge a private
report, assess severity, and coordinate disclosure after a fix is available.

## Credential Handling

Never commit `.env` files, API keys, SSH keys, webhook secrets, Session tokens,
or local runtime databases. Linear, GitHub, cloud, and Agent credentials belong
only in their documented trusted deployment boundaries. Suspected exposure must
be rotated at the provider even if repository history is later rewritten.
