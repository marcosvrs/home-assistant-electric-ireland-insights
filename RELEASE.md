# Release Process

This document defines the minimum steps required to tag a stable release of the Electric Ireland Insights integration.

## Release Gate

A release tag must only be created after a green GitHub Actions run on the **exact release commit SHA**.

The required workflow is `.github/workflows/validate.yml`. All jobs must pass:

- `validate-hassfest`
- `validate-hacs`
- `ruff`
- `mypy`
- `tests`

## Before Tagging

1. Ensure `CHANGELOG.md` is updated for the release version.
2. Ensure version strings are consistent across:
   - `custom_components/electric_ireland_insights/manifest.json`
   - `custom_components/electric_ireland_insights/const.py`
   - `pyproject.toml`
   - `hacs.json`
3. Push the final candidate commit to GitHub.
4. Wait for the `Validate` workflow to complete successfully on that exact commit SHA.
5. Record the commit SHA and workflow run URL in the release notes.

## Local Verification (Optional)

If a local Python toolchain is available, the following commands should pass before pushing the release candidate:

```bash
pytest tests/ \
  --cov=custom_components/electric_ireland_insights \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=95 \
  -q

mypy custom_components/electric_ireland_insights/ \
  --strict \
  --no-warn-return-any \
  --ignore-missing-imports

ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
```

Local tool absence does not override the Git Actions release gate.

## Migration / Schema Compatibility

For the `v1.0.0` stable release, the integration changed Home Assistant-facing identifiers from the raw account number to a SHA-256 hash of the account number, and the config entry stores additional meter identifiers (`partner_id`, `contract_id`, `premise_id`).

No `async_migrate_entry` hook is provided for `v1.0.0` because the pre-1.0 tags (`v0.0.1` through `v0.2.3`) were early development/pre-release artifacts with no confirmed public installations. If you are upgrading from a pre-1.0 version, remove the existing config entry and add it again rather than expecting an automatic migration.

## Release Evidence

For every stable release, record:

- Release tag (e.g., `v1.0.0`)
- Exact commit SHA
- GitHub Actions run URL
- Date/time the run completed
- Confirmation that all required jobs passed

### v1.0.0

- **Release tag:** `v1.0.0`
- **Exact commit SHA:** `53bb71976b294e0f1981c8b9b337937a305e38e0`
- **GitHub Actions run URL:** https://github.com/marcosvrs/home-assistant-electric-ireland-insights/actions/runs/29024218197
- **Date/time completed:** 2026-07-09 14:11 UTC
- **Status:** All required jobs passed (validate-hassfest, validate-hacs, ruff, mypy, tests)
