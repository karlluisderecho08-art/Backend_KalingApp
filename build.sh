#!/usr/bin/env bash
# Runs once during every Render deploy, before the server starts.
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# Safe to run on every deploy: migrate only applies migrations that
# haven't run yet, so this is a no-op once the schema is already current.
python manage.py migrate

# Creates the table backing CACHES (see config/settings/base.py) if it
# doesn't exist yet. Idempotent, and required before any request is
# throttled -- the rate limits are stored there.
python manage.py createcachetable

# All four of these are idempotent (get_or_create / "skip if exists"),
# so running them on every deploy is safe -- this is the free-tier
# workaround for not having Shell access to run one-off commands by
# hand. Real content only gets created once; never duplicated, and
# ensure_admin never touches a password that already exists.
python manage.py seed_articles
python manage.py seed_support_contacts
python manage.py seed_facilities
python manage.py seed_facility_staff
python manage.py ensure_admin

# Builds Kali's retrieval index over the knowledge base and embeds every
# passage (see chat/retrieval.py). Must come after seed_articles, since
# it indexes what that just created.
#
# Not strictly required -- retrieval reconciles passages on every query
# and embeds lazily, a few per message -- but doing it here means the
# cost lands on the deploy instead of on whichever mother sends the first
# message after it. Idempotent: unchanged documents are skipped and
# already-embedded passages are not re-embedded. Exits cleanly (with a
# warning) when GEMINI_API_KEY is unset, which is why errexit above does
# not make this a deploy-blocking step.
python manage.py reindex_knowledge
