# Run Ultimate Assistant with Docker

This runs the assistant web app and a Herald Router in separate containers. The web UI listens on `127.0.0.1:8765` on the computer running Docker; the Router is reachable only on the Compose network. The image installs the exact Herald wheels pinned in `apps/assistant/herald.json` and verifies their SHA-256 hashes.

From the repository root, create a key once and keep it for subsequent starts:

```sh
if [ -e .env ]; then
  echo 'Keep your existing .env; add HERALD_API_KEY if it is missing.'
else
  (umask 077; printf 'HERALD_API_KEY=%s\n' "$(openssl rand -hex 32)" > .env)
fi
docker compose up --build -d
```

Open <http://127.0.0.1:8765>, then connect a supported model or provider in the assistant. An API key is not a model account. The generated `.env` is ignored by Git; keep it private. On a system without `openssl`, put a long random value in `.env` as `HERALD_API_KEY=...` instead. To change the host port, set `ULTIMATE_ASSISTANT_PORT` in the environment or `.env` before starting.

```sh
docker compose logs -f
docker compose down
```

The named volumes persist Herald state, assistant state, assistant files, and a shared encrypted credential vault across container restarts and `docker compose down`. Both containers use the same vault so a provider key entered in UA can be resolved by its Router. Back up all volumes together before removing them. Container updates use `docker compose up --build -d`; rebuild from the revision you want to run.

The Docker archive attached to the UA release includes the pinned Herald wheels. Their hashes are checked before installation. A source checkout without bundled wheels downloads the same files from the Herald release. Neither form bundles account credentials.

The containers have their own filesystem and network. They do not control the host desktop, use the host's microphone, access host files, or see programs installed on the host unless you explicitly configure those integrations. Local model servers and provider CLI logins also need separate configuration; the container's `localhost` is not the host computer. Desktop, microphone, GPU, browser automation, and device-specific workflows are not set up by this Compose file.
