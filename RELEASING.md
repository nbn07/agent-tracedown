# Releasing to PyPI and npm

Three packages are published from this repository, all with the same version number:

- `agent-tracedown` on PyPI: the Python SDK, in `sdk/python`
- `agent-tracedown-server` on PyPI: the server, in `server`
- `agent-tracedown` on npm: the TypeScript SDK, in `sdk/typescript`

## One-time setup (about 10 minutes)

1. Create an account at [pypi.org](https://pypi.org/account/register/) and turn on two-factor authentication. PyPI requires it.
2. Go to **Your account → Publishing → Add a new pending publisher**. Add one pending publisher for each package:

   | Field | First package | Second package |
   |---|---|---|
   | PyPI project name | `agent-tracedown` | `agent-tracedown-server` |
   | Owner | `nbn07` | `nbn07` |
   | Repository name | `agent-tracedown` | `agent-tracedown` |
   | Workflow name | `release.yml` | `release.yml` |
   | Environment name | `pypi` | `pypi-server` |

   The environments differ on purpose: PyPI accepts only one pending publisher per repository, workflow and environment.
3. On GitHub, go to **Settings → Environments → New environment** twice, to create `pypi` and `pypi-server`. You can require your own approval before each release; leave "Prevent self-review" off, or you can't approve your own release.

A pending publisher does not reserve the name. Publish soon after setting it up.

### npm (for the TypeScript SDK)

npm uses trusted publishing too: no token is stored.

1. Create an account at [npmjs.com](https://www.npmjs.com/signup) and turn on two-factor authentication.
2. On GitHub, go to **Settings → Environments → New environment**, name it `npm` and save.
3. On npmjs.com, open the package → **Settings → Trusted Publisher → GitHub Actions**, and enter owner `nbn07`, repository `agent-tracedown`, workflow `release.yml`, environment `npm`. Allow **npm publish**.
4. Optional, recommended: package **Settings → Publishing access → Require two-factor authentication and disallow tokens**.

A trusted publisher can only be added to a package that already exists. Version 0.3.0 was published once with a short-lived token for that reason; the token has been deleted. A brand-new package would need the same first step.

## Each release

1. Set the same version in `sdk/python/pyproject.toml`, `server/pyproject.toml` and `sdk/typescript/package.json`. Also update `SDK_VERSION` in `sdk/python/agent_tracedown/tracer.py` and `sdk/typescript/src/version.ts`, and `__version__` in `server/tracedown_server/__init__.py`.
2. Commit and push, then tag:
   ```bash
   git tag v0.3.0 && git push origin v0.3.0
   ```
3. The `release` workflow builds all three packages, checks them and uploads them. Then `pip install agent-tracedown-server` and `npm install agent-tracedown` work for everyone.

To try the build locally first: `pip install build twine && python -m build sdk/python --outdir dist && python -m build server --outdir dist && twine check dist/*`.
