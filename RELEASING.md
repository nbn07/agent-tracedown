# Releasing to PyPI

Two packages are published from this repository: `agent-tracedown` (the SDK, in `sdk/python`) and `agent-tracedown-server` (in `server`). They share a version number.

## One-time setup (about 10 minutes)

1. Create an account at [pypi.org](https://pypi.org/account/register/) and turn on two-factor authentication. PyPI requires it.
2. Go to **Your account → Publishing → Add a new pending publisher**. Add one pending publisher for each package:

   | Field | First package | Second package |
   |---|---|---|
   | PyPI project name | `agent-tracedown` | `agent-tracedown-server` |
   | Owner | `nbn07` | `nbn07` |
   | Repository name | `agent-tracedown` | `agent-tracedown` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `pypi` |

3. On GitHub, go to **Settings → Environments → New environment**, name it `pypi` and save. You can require your own approval before each release.

A pending publisher does not reserve the name. Publish soon after setting it up.

## Each release

1. Set the same version in `sdk/python/pyproject.toml` and `server/pyproject.toml`. Also update `SDK_VERSION` in `sdk/python/agent_tracedown/tracer.py` and `__version__` in `server/tracedown_server/__init__.py`.
2. Commit and push, then tag:
   ```bash
   git tag v0.3.0 && git push origin v0.3.0
   ```
3. The `release` workflow builds both packages, checks them and uploads them to PyPI. Then `pip install agent-tracedown-server` works for everyone.

To try the build locally first: `pip install build twine && python -m build sdk/python --outdir dist && python -m build server --outdir dist && twine check dist/*`.
