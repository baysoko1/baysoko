#!/usr/bin/env python
import os
import django

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'baysoko.settings')
django.setup()

from users.models import User

def create_superuser():
    email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '').strip()
    password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '').strip()
    username = (os.environ.get('DJANGO_SUPERUSER_USERNAME') or email.split('@')[0] or 'admin').strip()

    if not email or not password:
        print("Skipping superuser creation because DJANGO_SUPERUSER_EMAIL or DJANGO_SUPERUSER_PASSWORD is missing.")
        return

    user = User.objects.filter(email__iexact=email).first()
    if not user and username:
        user = User.objects.filter(username=username).first()

    if not user:
        User.objects.create_superuser(
            username=username,
            password=password,
            email=email
        )
        print(f"Superuser '{username}' created successfully.")
        return

    updates = []
    if user.email != email:
        user.email = email
        updates.append('email')
    if username and user.username != username:
        user.username = username
        updates.append('username')
    if not user.is_superuser:
        user.is_superuser = True
        updates.append('is_superuser')
    if not user.is_staff:
        user.is_staff = True
        updates.append('is_staff')
    user.set_password(password)
    updates.append('password')
    user.save()
    print(f"Superuser '{user.username}' ensured successfully.")

if __name__ == '__main__':
    create_superuser()
