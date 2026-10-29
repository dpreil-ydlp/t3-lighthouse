# Install by hand

[Back to the README](../README.md)

## Server on another machine

Install only the collector on the Mac. Copy the agent token from the server (`lighthouse agent-token` prints it) into a file, then:

```sh
curl -fsSL https://raw.githubusercontent.com/dpreil-ydlp/t3-lighthouse/main/install.sh \
  | bash -s -- --collector-only --api-url http://SERVER:51821 --agent-token-file ./agent-token
```

## Every step of the installer

What the installer does, step by step, from a clone of this repository. Use this if you want to see each step or run the server somewhere the script does not fit.

1. Create the configuration and secrets.

   ```sh
   cp .env.example .env
   mkdir -m 700 secrets
   openssl rand -hex 32 > secrets/db-password
   openssl rand -hex 32 > secrets/dashboard-session-key
   openssl rand -hex 32 > secrets/t3-webhook-hmac
   chmod 600 secrets/*
   ```

   `t3-webhook-hmac` signs notices for the optional webhook channel described under [the webhook channel](configuration.md#the-webhook-channel). Compose mounts it into the worker, so create it even if you never use that channel.

2. Start the database and the API. The `migrate` service applies the database migrations before the API starts, and on every later start.

   ```sh
   docker compose up -d db api
   ```

   To build the images from this checkout instead of pulling them, add `-f docker-compose.yml -f docker-compose.build.yml` to every `docker compose` command and `--build` to `up`.

3. Create the owner login and the agent credential. Each command writes a new token file into the secrets directory and does not print the token.

   ```sh
   docker compose exec -T api admin-entrypoint bootstrap --name owner --token-file /host-secrets/owner-token
   docker compose exec -T api admin-entrypoint issue-principal --name agent --kind agent --token-file /host-secrets/agent-token
   ```

   On Linux with rootful Docker, the API container writes these files as root. Read them with `sudo cat`, or `sudo chown` them to your user, in the steps below.

4. Start the rest.

   ```sh
   docker compose up -d
   curl -fsS http://127.0.0.1:51821/healthz
   ```

   Open the dashboard at `http://127.0.0.1:51823` and log in with the contents of `secrets/owner-token`.

5. Install the collector on the Mac. Put the agent token at `~/.config/lighthouse/secrets/agent-token`, then run:

   ```sh
   mkdir -p -m 700 ~/.config/lighthouse/secrets
   cp secrets/agent-token ~/.config/lighthouse/secrets/agent-token
   chmod 600 ~/.config/lighthouse/secrets/agent-token
   scripts/install-mac-collector.sh --host 127.0.0.1 --port 51821
   ```

   This writes and loads a LaunchAgent named `ai.lighthouse.collector`. On a Mac with T3 Code, then run `bin/lighthouse t3-connect` so notices can go into T3 threads ([T3 Code](t3-code.md)). If the server is another machine, copy `secrets/agent-token` from the server instead, for example with `scp`, and use the server's address for `--host`.

An install made by hand before the `migrate` service existed is picked up as it is: on its first run, `migrate` records the migrations the database already has and applies only the missing ones.
