<!-- Keep PRs focused: one concern each. Explain the why, not just the what. -->

## What and why

<!-- What does this change, and what problem does it solve? -->

## Type of change

- [ ] Bug fix
- [ ] Documentation
- [ ] Packaging / CI / repository health
- [ ] Feature (please open an issue first to discuss)

## Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] `mypy gateway` passes
- [ ] `pytest` passes
- [ ] I did not weaken any guarantee (deterministic, idempotent, never-grow, content-blind logging)
- [ ] New behavior has test coverage (property tests for transformer changes)
