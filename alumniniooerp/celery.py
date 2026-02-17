"""
Celery configuration.
This file tells Django how to use Celery.
"""

import os
from celery import Celery

# Point to Django settings
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alumniniooerp.settings')
# Whenever Celery starts, it knows which Django project it belongs to

# Create Celery app
app = Celery('alumniniooerp')

# Load settings from Django (anything starting with CELERY_)
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-find tasks in all apps
app.autodiscover_tasks()