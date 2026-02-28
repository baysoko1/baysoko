"""
WSGI config for baysoko project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os
try:
	# Ensure .env is loaded when running WSGI in development
	from dotenv import load_dotenv
	load_dotenv()
except Exception:
	pass

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'baysoko.settings')

application = get_wsgi_application()
