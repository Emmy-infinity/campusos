#!/bin/bash
set -o errexit

# Upgrade pip and install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Apply database migrations
python manage.py migrate --noinput

# Collect static files for Whitenoise/Django admin
python manage.py collectstatic --noinput
