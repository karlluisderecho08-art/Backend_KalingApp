#!/usr/bin/env bash
# Runs once during every Render deploy, before the server starts.
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# Safe to run on every deploy: migrate only applies migrations that
# haven't run yet, so this is a no-op once the schema is already current.
python manage.py migrate

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
