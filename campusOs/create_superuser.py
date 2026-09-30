"""
Create or reset the admin superuser for campusOS.
Runs during Render's build. Safe to re-run — always syncs with env vars.
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'campusOs.settings')
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()

username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
email = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')

print("=" * 50)
print("Admin setup")
print("=" * 50)
print(f"Username env: {username}")
print(f"Email env: {email}")
print(f"Password env set: {'YES' if password else 'NO — using fallback'}")

if not password:
    password = 'Admin12345'
    print("WARNING: DJANGO_SUPERUSER_PASSWORD not set. Using fallback: Admin12345")

user, created = User.objects.get_or_create(
    username=username,
    defaults={'email': email},
)

user.email = email
user.role = 'ADMIN'
user.is_staff = True
user.is_superuser = True
user.is_active = True
user.set_password(password)
user.save()

action = "CREATED" if created else "RESET"
print(f"==> {action} superuser: {username}")
print(f"==> Login with: {username} / {password}")
print(f"==> Total users: {User.objects.count()}")
print("=" * 50)
