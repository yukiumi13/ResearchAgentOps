# Contributing

Research Control Plane is a pre-release Agent harness with explicit authority
and reproducibility contracts. Changes should preserve those contracts before
adding new integration surface.

## Development

Python 3.12 is required.

```bash
python -m pip install -e '.[dev]'
python -m pytest
ruff check src tests
```

Keep human CLI flags, strict Agent JSON, and supported Python automation on the
same `ApplicationService` request and authorization path. Preserve canonical
serialization, exact identity binding, idempotency, and fail-closed behavior.
Add focused tests for behavior changes and broader tests for shared contracts.

Do not commit credentials, runtime databases, raw private conversations, local
host identities, or private repository paths. Compatibility repositories and
other external workspaces are read-only references; tests and tools must write
only inside this checkout or an explicit temporary directory.

Pull requests should explain authority changes, failure/recovery behavior, and
verification performed. Both `researchctl/source-tests` and
`researchctl/exact-head` are required once repository branch rules are enabled.
