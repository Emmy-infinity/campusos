#!/bin/bash
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt

# 1. Wipe out the old static directory to prevent duplicate-file crashes
rm -rf staticfiles

# 2. Run migrations
python manage.py migrate --noinput

# 3. Collect static files
python manage.py collectstatic --noinput

# 4. Create the admin user
python create_superuser.py
