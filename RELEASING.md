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
   | Environment name | `pypi` | `pypi` |

3. On GitHub, go to **Settings → Environments → New environment**, name it `pypi` and save. You can require your own approval before each release.

A pending publisher does not reserve the name. Publish soon after setting it up.

### npm (for the TypeScript SDK)

1. Create an account at [npmjs.com](https://www.npmjs.com/signup) and turn on two-factor authentication.
2. Go to **Access Tokens → Generate New Token → Granular Access Token**. Give it read and write access to packages, and copy it.
3. On GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `NPM_TOKEN` and paste the token.
4. Go to **Settings → Environments → New environment**, name it `npm` and save.

After the first release you can switch to npm trusted publishing (package settings on npmjs.com → **Trusted publishing** → GitHub Actions, workflow `release.yml`, environment `npm`) and delete the token.

## Each release

1. Set the same version in `sdk/python/pyproject.toml`, `server/pyproject.toml` and `sdk/typescript/package.json`. Also update `SDK_VERSION` in `sdk/python/agent_tracedown/tracer.py` and `sdk/typescript/src/version.ts`, and `__version__` in `server/tracedown_server/__init__.py`.
2. Commit and push, then tag:
   ```bash
   git tag v0.3.0 && git push origin v0.3.0
   ```
3. The `release` workflow builds all three packages, checks them and uploads them. Then `pip install agent-tracedown-server` and `npm install agent-tracedown` work for everyone.

To try the build locally first: `pip install build twine && python -m build sdk/python --outdir dist && python -m build server --outdir dist && twine check dist/*`.
