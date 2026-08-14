# Security policy

## Scope

GeoFusion processes operational datasets that may contain addresses, coordinates, work-order identifiers, and other sensitive information. The repository is not a place to publish those datasets, credentials, or personal data.

## Reporting a vulnerability

Do not open a public issue for secrets, personal data, or an exploitable vulnerability. Use GitHub's private vulnerability reporting for the repository when available; otherwise contact the repository maintainer through the repository profile and request a private channel.

Include the affected file or component, reproduction steps that do not expose sensitive data, and the potential impact. Redact tokens, identifiers, addresses, and raw records from screenshots and logs.

## Data handling

Keep raw inputs, generated outputs, caches, checkpoints, and human-review files under the ignored `data/` directories. Review generated files for sensitive fields and absolute local paths before sharing them in an issue, pull request, or release.
