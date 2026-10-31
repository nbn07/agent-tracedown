# Public demo

A read-only Tracedown with the eight demo failures loaded: what people see when they click "Try the live demo". Visitors can browse every page and re-run the analysis. They cannot send or paste runs, and nothing they do is kept: the database is created fresh on every start.

The image installs the released package from PyPI, so publish the release first and keep `TRACEDOWN_VERSION` in the Dockerfile in step with it.

Try it locally:

```bash
docker build -t tracedown-demo deploy/demo
docker run --rm -p 7860:7860 tracedown-demo     # http://localhost:7860
```

## Option 1: Hugging Face Spaces (free, recommended)

1. Sign in at huggingface.co, then **New → Space**.
2. Name it `tracedown`, licence Apache-2.0, **SDK: Docker**, template **Blank**, hardware **CPU basic (free)**, visibility **Public**. Create the Space.
3. In the Space, **Files → Add file → Upload files**. Upload `Dockerfile` from this folder, and `space-README.md` from this folder **renamed to `README.md`** (it holds the settings the Space reads). Commit.
4. The Space builds (2 to 3 minutes) and starts. Your demo is at `https://<your-name>-tracedown.hf.space`.
5. Put that address in the repository's **Website** field (the gear next to "About") and in the README's "Try the live demo" link.

A free Space sleeps after 48 hours without visitors and wakes on the next visit, which takes about 30 seconds.

## Option 2: Render (free)

1. Sign in at render.com with GitHub, then **New → Web Service** and pick `nbn07/agent-tracedown`.
2. **Language: Docker**, **Dockerfile path: `deploy/demo/Dockerfile`**, **Docker build context: `deploy/demo`**, instance type **Free**.
3. Create the service. Render sets `PORT` and the image listens on it. The address is `https://<name>.onrender.com`.

A free Render service sleeps after 15 minutes without visitors; the first visit after that takes up to a minute.

## Updating

After each release, change `TRACEDOWN_VERSION` in the Dockerfile. On Hugging Face, upload the new Dockerfile. Render rebuilds on every push to `main`.
