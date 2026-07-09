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

## Release Evidence

For every stable release, record:

- Release tag (e.g., `v1.0.0`)
- Exact commit SHA
- GitHub Actions run URL
- Date/time the run completed
- Confirmation that all required jobs passed

### v1.0.0

- **Release tag:** `v1.0.0` (pending)
- **Exact commit SHA:** `4c8cae1fecf1397caa180206f1ecf0a31f4c1772`
- **GitHub Actions run URL:** https://github.com/marcosvrs/home-assistant-electric-ireland-insights/actions/runs/29023173737
- **Date/time completed:** 2026-07-09 UTC
- **Status:** All required jobs passed (validate-hassfest, validate-hacs, ruff, mypy, tests)
