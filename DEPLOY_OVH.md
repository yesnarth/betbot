# Deploying BetBot on a fresh OVH VPS (Ubuntu 22.04 / 24.04)

A complete, copy-paste, step-by-step guide to take a brand-new OVH VPS from
zero to a hardened, HTTPS-served BetBot stack behind Caddy + Let's Encrypt.

Follow the sections in order. Commands prefixed `local$` run on **your laptop**;
commands prefixed `vps$` run **on the VPS** (over SSH). Replace every
`YOURDOMAIN`, `you@example.com`, and placeholder secret with your real values.

---

## 0. Prerequisites + sizing

**VPS sizing (minimum):**

| Resource | Minimum |
|----------|---------|
| vCPU     | **2**   |
| RAM      | **4 GB** |
| Disk     | **40 GB SSD** |
| OS       | Ubuntu 22.04 LTS or 24.04 LTS |

The stack runs 8 containers (`db`, `redis`, `migrate`, `worker`, `api`,
`dashboard`, `backup`, `caddy`). `api` runs 2 uvicorn workers and the Streamlit
`dashboard` is memory-hungry, so do not go below 4 GB RAM. The `db_data`,
`betbot_data`, and rolling `./backups` directory grow over time — 40 GB gives
comfortable headroom.

**You also need:**

- A **domain name** you control (e.g. `betbot.example.com`) with access to its
  DNS zone to create an A record (Section 4).
- Your VPS **public IPv4 address** (OVH control panel → your VPS → IP).
- An **SSH key pair** on your laptop. If you don't have one:

  ```bash
  local$ ssh-keygen -t ed25519 -C "you@example.com"
  # Accept the default path (~/.ssh/id_ed25519); set a passphrase.
  ```

- Valid API keys:
  - **The Odds API** — https://the-odds-api.com (`ODDS_API_KEY`)
  - **football-data.org** — https://www.football-data.org (`FOOTBALL_DATA_API_KEY`)

First connection to the new VPS (OVH emails you a root password, or you set a
key at order time):

```bash
local$ ssh root@YOUR_VPS_IP
```

---

## 1. Initial server hardening

> Run everything in this section as **root** on the VPS, until you switch to the
> new user at the end.

### 1.1 Update the system

```bash
vps$ apt update && apt -y full-upgrade
```

### 1.2 Create a non-root sudo user

```bash
vps$ adduser deploy
# Set a password and fill (or skip) the prompts.
vps$ usermod -aG sudo deploy
```

### 1.3 Set up SSH key login for the new user

From your **laptop**, copy your public key to the `deploy` account:

```bash
local$ ssh-copy-id deploy@YOUR_VPS_IP
# If ssh-copy-id is unavailable:
local$ cat ~/.ssh/id_ed25519.pub | ssh deploy@YOUR_VPS_IP \
  "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

Test it in a **new terminal** before locking anything down:

```bash
local$ ssh deploy@YOUR_VPS_IP   # must log in with NO password prompt
```

### 1.4 Disable root login + password auth

Once key login works for `deploy`, harden SSH (still as root):

```bash
vps$ sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
vps$ sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
vps$ systemctl restart ssh
```

> Keep your current root session open until you've confirmed `deploy` can still
> SSH in **after** the restart. If you get locked out, use OVH's KVM/rescue
> console to revert.

### 1.5 Firewall — allow ONLY 22 / 80 / 443

```bash
vps$ apt -y install ufw
vps$ ufw default deny incoming
vps$ ufw default allow outgoing
vps$ ufw allow 22/tcp     # SSH
vps$ ufw allow 80/tcp     # HTTP (Let's Encrypt challenge + redirect)
vps$ ufw allow 443/tcp    # HTTPS
vps$ ufw allow 443/udp    # HTTP/3 (Caddy publishes 443/udp)
vps$ ufw enable
vps$ ufw status verbose    # confirm only 22, 80, 443 are open
```

These three ports are exactly what Caddy publishes (`80:80`, `443:443`,
`443:443/udp`). Everything else — Postgres 5432, Redis 6379, the API on 8000,
and Streamlit on 8501 — **must** stay closed. In the production overlay those
services use `ports: !reset []`, so they are reachable only on the internal
compose network and are never published to the host or the internet. Do **not**
add firewall rules for 5432 / 6379 / 8000 / 8501.

### 1.6 (Optional) fail2ban

Bans IPs after repeated failed SSH logins:

```bash
vps$ apt -y install fail2ban
vps$ systemctl enable --now fail2ban
vps$ fail2ban-client status sshd   # verify the sshd jail is active
```

From here on, **log in as `deploy`** and use `sudo` for privileged commands:

```bash
local$ ssh deploy@YOUR_VPS_IP
```

---

## 2. Install Docker Engine + the Compose plugin

Use Docker's official convenience script (from get.docker.com):

```bash
vps$ curl -fsSL https://get.docker.com -o get-docker.sh
vps$ sudo sh get-docker.sh
```

This installs Docker Engine, the CLI, containerd, and the **Compose v2 plugin**
(`docker compose`, not the legacy `docker-compose`).

Let the `deploy` user run Docker without `sudo`:

```bash
vps$ sudo usermod -aG docker deploy
vps$ newgrp docker          # apply the new group in the current shell
```

Verify:

```bash
vps$ docker --version
vps$ docker compose version
vps$ docker run --rm hello-world
```

---

## 3. Get the code

Clone the repository into the `deploy` user's home directory:

```bash
vps$ cd ~
vps$ git clone <YOUR_BETBOT_REPO_URL> betbot
vps$ cd ~/betbot
```

> Replace `<YOUR_BETBOT_REPO_URL>` with your actual repo URL (the directory that
> contains `docker-compose.yml`, `docker-compose.prod.yml`, and the `Caddyfile`).
> All remaining `vps$` commands run from inside `~/betbot`.

### 3.1 Confirm `.env` is git-ignored

You are about to put real secrets (Postgres + API Basic-auth passwords, API
keys) in `.env`. It must **never** be committed. The repo already ships a
`.gitignore` that ignores it — verify before going further:

```bash
vps$ grep -n '^\.env$' .gitignore     # must print a line: ".env"
vps$ git check-ignore -v .env         # must report .gitignore matched .env
```

If for any reason `.env` is **not** listed, add it now and never push without it:

```bash
vps$ printf '\n.env\n' >> .gitignore
```

---

## 4. DNS — point your domain at the VPS

In your DNS provider's zone for the domain, create an **A record**:

| Type | Name (host)        | Value (points to) | TTL          |
|------|--------------------|-------------------|--------------|
| A    | `betbot` (or `@`)  | `YOUR_VPS_IP`     | 300 (or auto) |

- Use `@` for a bare apex domain (`example.com`), or a subdomain label like
  `betbot` for `betbot.example.com`.
- The full hostname here is what you'll set as `BETBOT_DOMAIN` in Section 5.

Wait for propagation, then verify from your laptop that the name resolves to the
VPS IP **before** launching Caddy (Let's Encrypt validation will fail otherwise):

```bash
local$ dig +short YOURDOMAIN
# Must print YOUR_VPS_IP. If empty or wrong, wait and retry.
```

---

## 5. Create the production `.env`

The repository ships an environment template at **`.env.example`**. Copy it to
`.env` (the production config the containers actually read):

```bash
vps$ cp .env.example .env
```

### 5.1 Generate strong secrets

Run these on the VPS and copy each output into `.env`:

```bash
# Strong Postgres password
vps$ openssl rand -base64 32

# Strong API Basic-auth password
vps$ openssl rand -base64 32
```

### 5.2 Edit `.env` and set the required production values

```bash
vps$ nano .env
```

Set / uncomment the following. These are the variables the prod overlay's header
comment lists as required (`BETBOT_DOMAIN`, `ACME_EMAIL`, `POSTGRES_PASSWORD`,
`API_BASIC_PASSWORD`), plus your real API keys. Note that in `.env.example`
several of these (e.g. `POSTGRES_PASSWORD`, `API_BASIC_USER`,
`API_BASIC_PASSWORD`, `BETBOT_HSTS_ENABLED`, `BETBOT_DOMAIN`) ship **commented
out** — you must **uncomment** them and give them real values:

```ini
# --- TLS / domain (Caddy + Let's Encrypt) ---
BETBOT_DOMAIN=YOURDOMAIN
ACME_EMAIL=you@example.com

# --- Database ---
# Paste the FIRST `openssl rand -base64 32` output here:
POSTGRES_PASSWORD=PASTE_FIRST_OPENSSL_OUTPUT

# --- REST API auth (CRITICAL — see warning below) ---
API_BASIC_USER=betbot
# Paste the SECOND `openssl rand -base64 32` output here:
API_BASIC_PASSWORD=PASTE_SECOND_OPENSSL_OUTPUT

# --- External APIs (your real keys) ---
ODDS_API_KEY=your_real_the_odds_api_key
FOOTBALL_DATA_API_KEY=your_real_football_data_org_key

# --- Security headers (enable HSTS over HTTPS in production) ---
BETBOT_HSTS_ENABLED=1
```

> ### ⚠️ `API_BASIC_PASSWORD` MUST be set
> If `API_BASIC_PASSWORD` is left **empty**, authentication is **disabled** and
> your dashboard and API are open to the **entire internet** — anyone who finds
> your domain can read your bankroll, predictions, and trigger endpoints. In a
> public deployment this is non-negotiable: set a strong `API_BASIC_PASSWORD`
> (or configure JWT auth via `BETBOT_JWT_SECRET` + `BETBOT_PASSWORD_HASH`, the
> recommended option — generate the bcrypt hash with the `passlib` snippet in
> `.env.example`). Do **not** rely on the plaintext `BETBOT_PASSWORD` fallback;
> it is refused unless `BETBOT_ALLOW_PLAINTEXT_PASSWORD=1`, which is for local
> dev only.
> Likewise, `POSTGRES_PASSWORD` must be a strong random value — never leave the
> `betbot_dev_pwd` default in production.

> ### Security: HSTS + CORS
> - `BETBOT_HSTS_ENABLED=1` is safe **only** because Caddy serves real HTTPS
>   here; never enable it on a plain-HTTP host. (Caddy also emits its own HSTS
>   header with a 1-year `max-age`.)
> - Setting `BETBOT_DOMAIN` does double duty: it tells Caddy which hostname to
>   get a cert for **and** restricts the API's CORS policy to
>   `https://<your-domain>`. Always set it to your real production domain so the
>   API does not accept cross-origin calls from arbitrary sites. Never set it to
>   `*` or leave it as the `localhost` default in production.

Save and exit (`Ctrl+O`, `Enter`, `Ctrl+X` in nano).

> The `ANTHROPIC_API_KEY` is optional — without it the API runs fine but
> `POST /agent/recommend` returns 503. CLV snapshots and injury feeds are
> enabled later (see Section 9).

---

## 6. Migrate existing data (from your local machine)

If you have an existing BetBot database on your laptop, restore its latest
backup onto the VPS **before** you bring up the full stack. Skip this whole
section for a clean install.

Backups follow the pattern `betbot_YYYYMMDD_HHMMSS.sql.gz` (a plain-SQL
`pg_dump --format=plain` piped through `gzip -9`) and live in `./backups`.

### 6.1 Copy the latest backup to the VPS

On your **laptop**, find the newest dump and `scp` it over (create the target
directory on the VPS first):

```bash
vps$   mkdir -p ~/betbot/backups
local$ ls -t ./backups/betbot_*.sql.gz | head -1     # newest backup
local$ scp ./backups/betbot_<TIMESTAMP>.sql.gz deploy@YOUR_VPS_IP:~/betbot/backups/
```

### 6.2 Start ONLY the database first

You must restore into a running, **empty** `db` (a fresh `db_data` volume)
*before* the rest of the stack — and especially the one-shot `migrate`
(`alembic upgrade head`) job — ever touches the schema:

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d db
```

Wait for it to become healthy:

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
# db should show "healthy"
```

### 6.3 Restore the dump

Pipe the gzipped SQL straight into `psql` inside the `db` container. The dump
was created for PG user **`betbot`** and database **`betbot`**, so:

```bash
vps$ gunzip -c ./backups/betbot_<TIMESTAMP>.sql.gz \
  | docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db psql -U betbot -d betbot
```

> If you also copied the matching `data_<TIMESTAMP>.tar.gz` (tennis ELO,
> basketball stats, ML calibrator), restore it into the `betbot_data` volume.
> The compose project name is `betbot` (set by `name: betbot` in
> `docker-compose.yml`), so the Docker volume is `betbot_betbot_data`:
> ```bash
> vps$ docker run --rm -v betbot_betbot_data:/app/data -v "$PWD/backups":/b alpine \
>   sh -c "cd /app/data && tar -xzf /b/data_<TIMESTAMP>.tar.gz"
> ```

Once the restore finishes, continue to Section 7 to bring up the rest of the
stack. The one-shot `migrate` service (`alembic upgrade head`) will apply any
newer schema migrations on top of the restored data.

---

## 7. Launch the full stack with the prod overlay

Bring everything up using **both** compose files (base + production overlay):

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

This is the canonical launch command. The prod overlay (`docker-compose.prod.yml`):

- removes the dev `127.0.0.1` host-port bindings from `db`, `redis`, `api`, and
  `dashboard` (`ports: !reset []`) so only Caddy is internet-facing;
- adds the `caddy` service, publishing **`80:80`**, **`443:443`**, and
  **`443:443/udp`** (HTTP/3) — the only ports reachable from the internet;
- the first build of the `betbot:latest` image may take a few minutes.

Check that everything is up:

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Expected services: `db`, `redis`, `worker`, `api`, `dashboard`, `backup`,
`caddy` (all `restart: unless-stopped`). The `migrate` job runs once and exits
`0` — it has `restart: "no"`, so seeing it `Exited (0)` is normal.

---

## 8. Verify the deployment

### 8.1 Watch Caddy obtain the Let's Encrypt certificate

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy
```

Look for lines about `certificate obtained successfully` / `serving HTTPS` for
your domain. `Ctrl+C` to stop following. Do not proceed to the browser/`curl`
checks until Caddy reports a successful certificate. If issuance fails, see
Section 10.

### 8.2 The dashboard loads over HTTPS

In a browser, open:

```
https://YOURDOMAIN
```

Your browser should show a valid padlock (real Let's Encrypt cert) and load the
Streamlit dashboard (Caddy routes everything except `/api/*` to
`dashboard:8501`, with WebSocket upgrade for Streamlit).

### 8.3 The API health endpoint works

```bash
local$ curl -i https://YOURDOMAIN/api/health
```

Caddy strips the `/api` prefix and proxies to `api:8000`, so this hits the
FastAPI `/health` route. Expect `HTTP/2 200`.

### 8.4 Basic auth prompts on protected API routes

```bash
# Without credentials → expect 401 Unauthorized:
local$ curl -i https://YOURDOMAIN/api/events

# With credentials → expect 200:
local$ curl -i -u betbot:'YOUR_API_BASIC_PASSWORD' https://YOURDOMAIN/api/events
```

A `401` when unauthenticated confirms `API_BASIC_PASSWORD` is correctly
enforced. If you get a `200` without credentials, your password is empty — go
back to Section 5 immediately; your stack is exposed.

---

## 9. Operations

### 9.1 Update to a new version

```bash
vps$ cd ~/betbot
vps$ git pull
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`--build` rebuilds the `betbot:latest` image; the `migrate` service runs
`alembic upgrade head` automatically (it must complete successfully before
`worker`/`api` start), applying any new DB migrations. Compose recreates only
the changed containers.

### 9.2 View logs

```bash
# All services, follow:
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f

# A single service (e.g. the worker, api, or caddy):
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f worker
```

### 9.3 Where backups land

The `backup` service runs a one-shot dump on startup, then again daily at
**03:00 UTC**. Dumps are written to **`./backups`** on the host
(`~/betbot/backups`):

- `betbot_<YYYYMMDD_HHMMSS>.sql.gz` — Postgres dump (PG user/db `betbot`/`betbot`)
- `data_<YYYYMMDD_HHMMSS>.tar.gz` — the `betbot_data` volume (tennis ELO,
  basketball stats, ML calibrator)

Local retention is **30 days** rolling (`BACKUP_RETENTION_DAYS`). For offsite
copies, set `BACKUP_REMOTE_TARGET` (rclone) and mount an `rclone.conf` via a
`docker-compose.override.yml` — see the "Offsite backups" block in
`.env.example`.

```bash
vps$ ls -lh ~/betbot/backups/
```

To restore one of these dumps later, use the same command as Section 6.3:

```bash
vps$ gunzip -c ./backups/betbot_<TIMESTAMP>.sql.gz \
  | docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db psql -U betbot -d betbot
```

### 9.4 Enable CLV + injuries later (on the paid plan)

When you upgrade your data plan, edit `.env` and add:

```ini
# Closing-line-value snapshots
CLV_SNAPSHOT_ENABLED=1

# api-football.com injury / lineup feed
API_FOOTBALL_KEY=your_api_football_key
```

Then redeploy so the `worker` and `api` pick up the new variables:

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Without `API_FOOTBALL_KEY`, the local agent simply skips the injury check; with
it, injury/lineup data is folded into recommendations. (Optionally also set
`TAVILY_API_KEY` to enable the live news check.)

---

## 10. Troubleshooting

### Certificate issuance fails (rate limits) → use the Let's Encrypt **staging** CA first

Let's Encrypt's **production** CA has strict rate limits. While you're still
debugging DNS/firewall, switch Caddy to the staging CA so failed attempts don't
burn your real-cert quota. In the `Caddyfile`, uncomment the staging line inside
the global block:

```caddyfile
{
    email {$ACME_EMAIL:admin@example.com}
    acme_ca https://acme-staging-v02.api.letsencrypt.org/directory
}
```

Reload Caddy:

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d caddy
```

Staging certs are **not trusted by browsers** (expect a warning) but prove the
issuance flow works. Once you see a staging cert succeed, **re-comment** that
line and recreate Caddy to get a real, trusted production cert. If a bad cert
got cached, clear Caddy's state:

```bash
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml down
vps$ docker volume rm betbot_caddy_data    # forces a fresh ACME run
vps$ docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### DNS not propagated

Caddy cannot get a cert until `YOURDOMAIN` resolves to the VPS:

```bash
local$ dig +short YOURDOMAIN          # must equal YOUR_VPS_IP
```

If it's wrong/empty, fix the A record (Section 4) and wait for the TTL to
expire. The Caddy logs will show repeated ACME challenge failures until this
resolves.

### Port 80 / 443 blocked by the firewall

Let's Encrypt's HTTP-01 challenge needs inbound **80**, and clients need **443**:

```bash
vps$ sudo ufw status verbose          # 80/tcp, 443/tcp, 443/udp must be ALLOW
```

If they're missing, re-run the `ufw allow` commands from Section 1.5. Also
confirm OVH's own network firewall / security group (if you enabled one in the
OVH panel) permits 80, 443, and 22.

### Low disk

Images, the `db_data` volume, and the rolling `./backups` dir can fill the disk:

```bash
vps$ df -h /                          # check free space
vps$ docker system df                 # what Docker is using
vps$ docker system prune -a           # remove unused images/containers (safe; keeps named volumes)
vps$ du -sh ~/betbot/backups          # backups are pruned at 30 days; delete old ones if needed
```

Never `docker volume prune` blindly — `db_data` and `betbot_data` hold your
live database and model state. If disk pressure is chronic, resize the VPS or
lower `BACKUP_RETENTION_DAYS` in `.env`.

---

### Quick reference

| Item | Value |
|------|-------|
| Launch command | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` |
| Services | `db`, `redis`, `migrate` (one-shot), `worker`, `api`, `dashboard`, `backup`, `caddy` |
| Public ports (Caddy) | `80:80`, `443:443`, `443:443/udp` — **only** these on the firewall |
| Internal-only (never published in prod) | Postgres 5432, Redis 6379, API 8000, dashboard 8501 |
| Named volumes | `db_data`, `redis_data`, `betbot_data`, `caddy_data`, `caddy_config` |
| Docker volume for data restore | `betbot_betbot_data` (project name `betbot`) |
| PG user / db | `betbot` / `betbot` |
| Backups dir | `./backups` (host) — daily 03:00 UTC, 30-day retention |
| Backup files | `betbot_<ts>.sql.gz` (Postgres) + `data_<ts>.tar.gz` (data volume), `ts = YYYYMMDD_HHMMSS` |
| Restore | `gunzip -c ./backups/betbot_<ts>.sql.gz \| docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T db psql -U betbot -d betbot` |
| Required secrets | `POSTGRES_PASSWORD` (strong), `API_BASIC_PASSWORD` (or JWT), `BETBOT_DOMAIN`, `ACME_EMAIL`, `ODDS_API_KEY`, `FOOTBALL_DATA_API_KEY` |
