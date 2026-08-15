# Contributing to Multi-Bus Gateway

Thanks for your interest! This is a live, safety-relevant gateway (it can feed a
real energy-storage system), so contributions are held to a practical bar:
**tests stay green, changes are scoped, and behaviour that affects a running
site is opt-in and reversible.**

## Ways to contribute

- **Report a bug** or **request a feature** via the issue templates.
- **Add a device template** — a new meter/inverter register map. See
  `docs/device-catalog.md` and the CSV import flow; name registers from the
  canonical dictionary (`docs/canonical-fields.md`) so output stays uniform.
- **Improve docs** — the manual, API reference, and setup guides.
- **Fix code** — see below.
- **Security issues:** do NOT open a public issue — see [SECURITY.md](SECURITY.md).

## Dev setup

Requires Python 3.11.

```bash
git clone https://github.com/sm26449/multi-bus-gateway.git
cd multi-bus-gateway
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q          # run the full suite (should be all green)
ruff check .                 # lint — CI enforces a clean run
```

CI runs exactly these plus a coverage floor (`--cov-fail-under=72`) and
**random test order** (`pytest-randomly`) — a test that depends on another
test running first will fail there even if it passes locally, so run
`python -m pytest -p randomly -q` before pushing if you touched fixtures
or module-level state. The seed is printed in the log header;
`--randomly-seed=<seed>` reproduces a failing order.

To run the whole stack locally: `docker compose up -d` (see the README Quick
Start). The UI is at http://localhost:8080.

## Making a change

1. **Branch** off `main`.
2. **Keep it focused** — one logical change per PR; don't bundle unrelated edits.
3. **Add/adjust tests.** New behaviour needs a test; a bug fix should come with a
   test that fails before and passes after. The suite must stay green
   (`python -m pytest -q`).
4. **Match the surrounding style.** The codebase values the Zen of Python —
   readable, flat, explicit. No new lint noise.
5. **Don't break the live-safety contract.** Anything that changes how a device
   is polled or how a virtual meter serves data must fail safe (never serve
   wrong/stale values as live) and be opt-in where it affects existing setups.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`.

```
feat(templates): add ABB B24 register map
fix(modbus): clamp write value to declared bounds
docs: correct the RTU wiring table
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`.

## Pull requests

Open a PR against `main`, fill in the template, and make sure CI is green. A
maintainer will review. Please be responsive to review comments.

## License

By contributing you agree that your contributions are licensed under the project
license, **AGPL-3.0-or-later** (see [LICENSE](LICENSE)). Don't submit code you
don't have the right to license this way.
