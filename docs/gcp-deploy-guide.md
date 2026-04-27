# GCP Deployment Guide

A field-tested checklist for deploying a Docker Compose stack (backend + frontend + Postgres + Caddy) to a single GCE VM with a private GHCR registry, basic auth, and nip.io TLS. Based on hard-won lessons from deploying this project.

Target audience: small teams (1–3 devs) who want autodeploy without Kubernetes overhead.

---

## 0. Prerequisites

- Private GitHub repo with Dockerfiles for each service
- A GCP project with billing enabled
- Your local machine can run `gcloud`, `ssh`, `git`
- A working `docker compose` stack that runs locally

## 1. Architecture decisions

**Single VM + docker compose** — cheapest, simplest. Upgrade path later is k3s or Cloud Run. Don't reach for Kubernetes for 2 devs.

**Caddy reverse proxy** — free automatic TLS, simple config, basic auth built in. Put it in front of everything.

**nip.io for hostnames before you own a domain** — `<ip-with-dashes>.nip.io` resolves to your VM IP automatically, and Let's Encrypt will happily issue certs for it. Swap in a real domain later by editing one env var.

**GHCR (`ghcr.io`) for images** — free for private repos, integrates with GitHub Actions via `GITHUB_TOKEN`. No separate registry to manage.

**Deploy key for VM → GitHub pulls** — read-only SSH key added to the repo. No PAT rotation headaches.

## 2. VM provisioning

- **Machine type:** `e2-small` (2 vCPU burst, 2 GB RAM) — enough for a Python API + nginx + Postgres + Caddy, ~$13/mo in europe-west1
- **Region:** whatever's cheapest near your users (`europe-west1`, `us-central1`)
- **Image:** Debian 12 (Bookworm)
- **Disk:** 20 GB standard persistent
- **Networking:** static external IP, firewall allowing 80/443 from `0.0.0.0/0` and 22 from your IP only

**Gotcha:** the default VM image does *not* have `git` installed. `apt-get install -y git` first before anything else.

## 3. GitHub deploy key setup

The VM needs to clone a private repo. A deploy key is cleaner than a PAT (no expiration, scoped to one repo).

```bash
# On the VM, as root:
sudo ssh-keygen -t ed25519 -f /root/.ssh/github_deploy -N "" -C "vm-deploy"
sudo cat /root/.ssh/github_deploy.pub
```

Add the public key at `https://github.com/<org>/<repo>/settings/keys` → Add deploy key, read-only.

Configure SSH to use it:
```bash
echo -e "Host github.com\n\tHostName github.com\n\tUser git\n\tIdentityFile /root/.ssh/github_deploy" | sudo tee /root/.ssh/config
sudo ssh -o StrictHostKeyChecking=accept-new -T git@github.com
```

**Gotcha (terminal line-wrapping):** If you paste a multi-line heredoc into GCP's SSH-in-browser, long lines can get wrapped and silently corrupt the config. Prefer one-line `printf` / `echo -e` commands, and always `cat` the file back to verify.

**Gotcha (two users):** If your service runs as a non-root user (e.g. `eodyne`), *that user* needs access to the key too — not just root. Copy the key to `/home/<user>/.ssh/` (or wherever the app dir lives) and fix up the SSH config's `IdentityFile` path. Otherwise `sudo -u eodyne git fetch` will die with `Permission denied (publickey)`.

## 4. Bootstrap script

Write a single `deploy/vm-bootstrap.sh` that's idempotent and:
1. Installs Docker Engine from the official Debian repo (not the distro version — it's usually ancient)
2. Creates a system user (`--system`, no login shell) for the app
3. Clones the repo to `/opt/<app>/` and chowns it to that user
4. Creates a `.env` template with clearly-marked `REPLACE-ME` placeholders
5. Schedules nightly backup cron

Key snippet — Docker installation (don't use the `docker.io` package):
```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Run with `sudo bash /opt/<app>/deploy/vm-bootstrap.sh`. After first run, edit `.env` and `basicauth.users` with real values.

## 5. Caddy reverse proxy

Single `Caddyfile` in `deploy/`, mounted read-only into the caddy container. Use `handle` blocks for first-match routing — do **not** use bare route matchers, they all fire simultaneously:

```
{$DOMAIN} {
    handle /health { reverse_proxy backend:8000 }

    # Public endpoints (no auth)
    @public {
        path /api/v1/public/*
    }
    handle @public { reverse_proxy backend:8000 }

    # Protected API
    handle /api/v1/* {
        basicauth { import /etc/caddy/basicauth.users }
        reverse_proxy backend:8000 {
            header_up X-Remote-User {http.auth.user.id}
        }
    }

    # SPA fallback
    handle { reverse_proxy frontend:80 }
}
```

The `X-Remote-User` header lets the backend know who's authenticated — cheapest possible SSO. Parse it in a middleware, store in a `ContextVar`, use for audit logs.

Generate basicauth hashes:
```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'yourpassword'
```

## 6. docker-compose.yml for production

```yaml
services:
  backend:
    image: ghcr.io/<org>/<app>-backend:latest
    env_file: .env
    depends_on: [postgres]

  frontend:
    image: ghcr.io/<org>/<app>-frontend:latest

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes: [pg_data:/var/lib/postgresql/data]

  caddy:
    image: caddy:2-alpine
    ports: ["80:80", "443:443", "443:443/udp"]
    volumes:
      - ./deploy/Caddyfile:/etc/caddy/Caddyfile:ro
      - ./deploy/basicauth.users:/etc/caddy/basicauth.users:ro
      - caddy_data:/data
      - caddy_config:/config
    environment: [DOMAIN]

volumes: {pg_data: {}, caddy_data: {}, caddy_config: {}}
```

**Gotcha:** caddy_data is the Let's Encrypt cert store. **Never** delete it — Let's Encrypt rate-limits cert issuance and you'll lock yourself out.

## 7. Backend Dockerfile pitfalls

```dockerfile
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app
COPY pyproject.toml ./
RUN pip install --no-cache-dir <explicit list of deps>
COPY . .
CMD ["sh", "-c", "alembic upgrade head && uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 2"]
```

- **`PYTHONPATH=/app`** is required for alembic to find your app module when invoked from `/app`.
- Run `alembic upgrade head` in the CMD, not in a separate step. Your container needs to be self-healing on restart.
- If `pip install -e .` fails with weird build-backend errors, verify `[build-system]` in `pyproject.toml` — `build-backend = "setuptools.build_meta"` is the correct value. Anything else is probably a hallucination.

## 8. Frontend Dockerfile pitfalls

Multi-stage: node builds the SPA, nginx serves it. Two non-obvious things:

1. **nginx SPA fallback:** `try_files $uri $uri/ /index.html;` in the `location /` block. Without it, deep links (e.g. `/invoices/123`) 404.
2. **Healthcheck must use `127.0.0.1`, not `localhost`:** busybox wget resolves `localhost` to `::1`, but nginx binds IPv4 only by default. Healthcheck becomes flaky and the container cycles.

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
```

## 9. GitHub Actions: CI + Deploy

Two workflows:

- **`ci.yml`** — runs on PR and push to main. Pytest + frontend typecheck/build. This is your quality gate.
- **`deploy.yml`** — runs on push to main only. Builds images, pushes to GHCR, SSHes to VM to pull + restart.

`deploy.yml` skeleton:
```yaml
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions: {contents: read, packages: write}
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: backend/Dockerfile
          push: true
          tags: |
            ghcr.io/<owner>/<app>-backend:latest
            ghcr.io/<owner>/<app>-backend:${{ github.sha }}
          cache-from: type=gha,scope=backend
          cache-to: type=gha,mode=max,scope=backend
      # repeat for frontend

  deploy-vm:
    needs: build-and-push
    runs-on: ubuntu-latest
    steps:
      - uses: appleboy/ssh-action@v1.2.0
        with:
          host: ${{ secrets.VM_HOST }}
          username: ${{ secrets.VM_USER }}
          key: ${{ secrets.VM_SSH_KEY }}
          script: |
            set -euo pipefail
            cd /opt/<app>
            sudo -u <app-user> git fetch --depth=1 origin main
            sudo -u <app-user> git reset --hard origin/main
            echo '${{ secrets.GITHUB_TOKEN }}' | sudo -u <app-user> docker login ghcr.io -u '${{ github.actor }}' --password-stdin
            sudo -u <app-user> docker compose pull
            sudo -u <app-user> docker compose up -d
            sudo -u <app-user> docker image prune -f
```

GitHub repo secrets needed:
- `VM_HOST` — external IP
- `VM_USER` — the Linux user GH Actions SSHes as (must have passwordless sudo for the app user OR be the app user directly)
- `VM_SSH_KEY` — the entire private key, including `-----BEGIN`/`-----END` lines

**Gotcha:** the `GITHUB_TOKEN` auto-provided to Actions has `packages:write` when the job declares it, but its `packages:read` perms expire immediately when the job ends — so the VM can't use that token for long-lived pulls. Either (a) re-login every deploy, as shown above, or (b) generate a PAT with `read:packages` and store it as a secret and log in once manually on the VM. The first-time manual login is usually simpler.

## 10. Private GHCR images on the VM

First-time login, one-off:
```bash
echo 'ghp_xxx' | sudo -u <app-user> docker login ghcr.io -u <github-username> --password-stdin
```

Where the PAT has **`read:packages` only** (no other scopes needed — ignore `repo`, it's irrelevant to container pulls).

**Gotcha:** docker login writes credentials to `$HOME/.docker/config.json` of the user running the command. If that user's home is owned by someone else (e.g. you made it a system user with home=`/opt/<app>/` but forgot to chown), the login silently fails with `mkdir: permission denied`. Make sure the home dir is writable by the app user.

## 11. First deploy — manual smoke test

Don't trust the autodeploy until you've seen it work once by hand:
```bash
cd /opt/<app>
sudo -u <app-user> docker compose pull
sudo -u <app-user> docker compose up -d
sudo -u <app-user> docker compose ps       # all services "Up"
sudo -u <app-user> docker compose logs caddy --tail=50
curl -v http://localhost                   # should 301 → https
```

Open `https://<ip-dashed>.nip.io/` in a browser. First request takes 30–60s while Caddy provisions the Let's Encrypt cert. You'll get a basic auth prompt, then your frontend.

If `ERR_EMPTY_RESPONSE`, your GCP firewall isn't allowing 80/443 inbound. Enable via Compute Engine → VM → Edit → check "Allow HTTP/HTTPS traffic".

## 12. Backups

`pg_dump | gzip | curl -T - <nextcloud-webdav-url>` in a nightly cron, reading creds from the same `.env` the app uses. Put it in `deploy/backup.sh`, register via `crontab -u root`. Log to `/var/log/<app>-backup.log` and rotate with logrotate.

Rotation strategy: keep 7 daily, 4 weekly, 12 monthly on Nextcloud side. This is a civilized minimum for a small business.

## 13. Test rot warning

When you migrate a project to CI for the first time, expect a handful of tests to be broken on stale fixtures — tests that happened to pass locally on your dev machine because of environment differences, or tests from before a policy change that nobody re-ran. You have two choices:

- **Fix them all first.** Virtuous but slow.
- **Skip them in CI temporarily, file a cleanup ticket, ship.** Pragmatic.

Use `pytest.ini_options.addopts = "--ignore=<path> --deselect=<test>"` in `pyproject.toml` to quarantine, with a comment explaining why. Track the cleanup as tech debt.

## 14. Upgrading to a real domain later

When you're ready to move off nip.io:

1. Register domain, point A record at the VM's external IP
2. On the VM, edit `/opt/<app>/.env`: change `DOMAIN=your-real-domain.com`
3. `sudo -u <app-user> docker compose restart caddy`
4. Caddy automatically issues a new Let's Encrypt cert for the real domain
5. (Optional) delete the old nip.io cert from `caddy_data`

No other changes needed. That's the whole point of using env vars for the domain.

---

## Common failure modes (cheat sheet)

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'backend'` during alembic | Missing `PYTHONPATH=/app` in Dockerfile | Add ENV var |
| `StringDataRightTruncation` in Postgres | VARCHAR too small, SQLite silently allowed it | Widen column in a migration |
| Frontend container cycling unhealthy | Healthcheck uses `localhost` (IPv6) but nginx binds IPv4 | Use `127.0.0.1` |
| `ERR_EMPTY_RESPONSE` in browser | GCP firewall blocks 80/443 | Enable in VM settings |
| `error from registry: unauthorized` on compose pull | Not logged into GHCR | `docker login ghcr.io` with PAT |
| `mkdir .docker: permission denied` on docker login | App user can't write its home | `chown -R <user>:<user> <home-dir>` |
| `git@github.com: Permission denied (publickey)` | Deploy key not in this user's `.ssh/` | Copy key + config to user's home |
| `pip._vendor.pyproject_hooks BackendUnavailable` | Invalid `build-backend` in pyproject | Use `setuptools.build_meta` |
| Caddy routes all matching in parallel, wrong service responds | Using bare matchers instead of `handle` | Rewrite with `handle` blocks |
| Bash heredoc in browser SSH produces broken config file | Terminal wrapped a long line mid-directive | Use single-line `printf`/`echo -e`, always `cat` to verify |
