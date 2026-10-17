# Releasing Kayden

Kayden publishes the `kayden` distribution to PyPI from GitHub Releases. The
workflow uses PyPI Trusted Publishing, so the repository does not store an API
token.

## One-time setup

1. Create the `pypi` environment in the GitHub repository.
2. In PyPI, add a pending trusted publisher for:
   - Owner: `K5031`
   - Repository: `Kayden`
   - Workflow: `publish.yml`
   - Environment: `pypi`

PyPI project names are allocated on first upload. If `kayden` becomes
unavailable before the first release, change `[project].name` and the workflow's
environment URL before publishing.

## Release

1. Update `[project].version` in `pyproject.toml`.
2. Run `pytest`, `python -m build`, and `python -m twine check dist/*`.
3. Commit and push the version change.
4. Publish a GitHub Release tagged `v<version>`, such as `v0.1.0`.

The release workflow verifies that the tag matches the package version, reruns
the tests, builds both distribution formats, checks their metadata, and uploads
them to PyPI.
