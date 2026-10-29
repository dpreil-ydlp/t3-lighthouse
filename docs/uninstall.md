# Updating and uninstalling

[Back to the README](../README.md)

Running the install one-liner again does not reinstall. It starts the existing install if it is stopped and prints the address and token again. The same one-liner with `bash -s -- --update` or `bash -s -- --uninstall` updates or removes it. Database migrations run on every start, so an update needs no manual step.

## What uninstall removes

`lighthouse uninstall` removes the containers, the database volume, the Lighthouse images, and the install folder (`~/t3-lighthouse`). On a Mac it also removes what this install set up there:

- the `ai.lighthouse.collector` LaunchAgent and its token, state, and log in `~/.config/lighthouse`;
- the T3 Code connection: it revokes Lighthouse's T3 token with `t3 auth session revoke` and deletes `~/.config/lighthouse/secrets/t3-mcp-token`.

It keeps, and lists in its output:

- the `postgres` image, if it was on the machine before the install;
- a collector LaunchAgent that runs from another folder, and its files;
- the `ai.lighthouse.briefs` LaunchAgent, which you set up by hand;
- a T3 token that another install folder, or you, saved;
- anything else in `~/.config/lighthouse`.

## When uninstall stops

If Docker is not running, uninstall stops before deleting anything, because the database would otherwise be left behind in a Docker volume. Start Docker and run it again.

Install, start, and uninstall also stop without changing anything when the Docker Compose project name is in use by another install, either by its containers or by a database volume this install did not create. Give the install its own `COMPOSE_PROJECT_NAME` in `~/t3-lighthouse/.env`. If the volume is this install's own data and only the record of it is missing, run the command again with `LIGHTHOUSE_VOLUMES_ARE_MINE=1` in the environment.

## One collector per Mac

A Mac runs one collector. If one from another install folder is already there, the installer asks before replacing it; without a terminal it stops unless you add `--replace-collector`.
