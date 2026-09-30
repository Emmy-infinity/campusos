"""
Create a superuser for the campusOS project.

WARNING: Has a default password for quick testing.
Set DJANGO_SUPERUSER_PASSWORD as an env var in production.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'campusOs.settings')
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

# Read credentials from environment variables with defaults
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'Admin12345')  # ← default here

if User.objects.filter(username=username).exists():
    print(f"Superuser '{username}' already exists. Skipping creation.")
else:
    print(f"Creating superuser: {username}")
    user = User.objects.create_superuser(
        username=username,
        email=email,
        password=password,
    )
    user.role = User.Role.ADMIN
    user.save(update_fields=['role'])
    print(f"Superuser created: {username} (role={user.role})")

print(f"Total users: {User.objects.count()}")
