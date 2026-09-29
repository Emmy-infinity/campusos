import os
import django

# 1. Point to your Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'campusOs.settings') # Replace 'myproject' with your folder name
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

# 2. Get credentials from environment variables
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'SecretPassword123')

# 3. Create user if they do not exist
if not User.objects.filter(username=username).exists():
    print(f"Creating superuser: {username}")
    User.objects.create_superuser(username=username, email=email, password=password)
    print("Superuser created successfully!")
else:
    print(f"Superuser '{username}' already exists. Skipping.")