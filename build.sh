#!/bin/bash
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# 1. Create the database tables in Neon
python manage.py migrate --noinput

# 2. Collect static files
python manage.py collectstatic --noinput

# 3. Run your custom superuser script
python create_superuser.py
