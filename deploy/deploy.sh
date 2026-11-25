#!/usr/bin/env bash
# Deploys whatever origin/main points at, on a server (staging or prod).
# Idempotent, so re-running after a failure is always the right move.
#
#   /srv/apps/wravel-server/deploy/deploy.sh
#
# This is the whole deploy, first or hundredth. Nothing here assumes a human is
# watching -- it either finishes or exits non-zero -- so the GitHub Actions
# Deploy workflow runs it over ssh unchanged on every merge to main.

set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

# How many pre-migration database dumps to keep in backups/.
KEEP_BACKUPS=5

if [[ ! -f .env ]]; then
    echo "no .env in $PWD -- run deploy/newenv.sh <staging|prod> first" >&2
    exit 1
fi
if ! grep -q '^COMPOSE_PROFILES=server' .env; then
    echo ".env in $PWD is not a server one (no COMPOSE_PROFILES=server)" >&2
    exit 1
fi

# The app refuses to start without it; say so here rather than mid-deploy.
if ! grep -q '^TRIP_INBOXES=.' .env; then
    echo ".env in $PWD has no TRIP_INBOXES -- re-run deploy/newenv.sh <staging|prod> --force" >&2
    exit 1
fi

# --ff-only: the server checkout is not a place to make edits, and if it has
# diverged from origin this stops rather than quietly merging.
git pull --ff-only --quiet
sha="$(git rev-parse --short HEAD)"
echo "deploying $sha to $(sed -n 's/^ENVIRONMENT=//p' .env): $(git log -1 --pretty=%s)"

# --pull refreshes the python base image too, so its security fixes arrive.
docker compose build --pull app

docker compose up -d --wait db

# Dump the database before a deploy that migrates it, so a migration that
# mangles data can be undone (README, "Rolling back"). Only then: seeding only
# adds rows, and a dump on every merge would fill the disk for nothing.
# `alembic current` prints "<rev> (head)" when the new code has nothing to
# apply, and nothing at all on an empty database, which has nothing to lose.
# -T throughout: under ssh from CI there is no terminal to attach.
current="$(docker compose run --rm --no-deps -T app alembic current)"
if [[ -n "$current" && "$current" != *"(head)"* ]]; then
    # 700/600: the dump holds every user's data.
    mkdir -p -m 700 backups
    dump="backups/$(date +%Y%m%d-%H%M%S)-before-$sha.dump"
    (
        umask 077
        docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$dump.tmp"
    )
    mv "$dump.tmp" "$dump"
    echo "migrations pending (database at ${current%% *}); dumped it to $dump"
    # Names start with the time, so reverse order is newest first.
    ls -1 backups/*.dump | sort -r | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -f
fi

# Migrate and seed with the new image before the running one is replaced. If
# this fails, the old API keeps serving: Alembic applies every pending
# migration in one transaction, so a failure leaves the schema as it was.
# bootstrap.py is idempotent: new migrations, plus whatever the seed lists
# gained; it never edits or removes existing rows.
docker compose run --rm --no-deps -T app python scripts/bootstrap.py

docker compose up -d --wait --remove-orphans

# Layers superseded by the build above; nothing in use is touched.
docker image prune -f >/dev/null

docker compose ps
