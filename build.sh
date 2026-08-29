#!/usr/bin/env bash
# Runs once during every Render deploy, before the server starts.
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input

# Safe to run on every deploy: migrate only applies migrations that
# haven't run yet, so this is a no-op once the schema is already current.
python manage.py migrate
