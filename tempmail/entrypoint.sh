#!/bin/sh

set -e

if [ ! -f "core/__init__.py" ]; then
    django-admin startproject core .
fi

python manage.py collectstatic --noinput
python manage.py makemigrations --noinput 
python manage.py migrate --noinput

if [ "$DEBUG" = "1" ]; then
python - <<'EOF'
import os
from core import settings

for code, _ in settings.LANGUAGES:
    parts = code.split('-')
    if len(parts) == 2:
        folder = f"{parts[0]}_{parts[1].upper()}"
    else:
        folder = code

    path = os.path.join(settings.BASE_DIR, 'locale', folder, 'LC_MESSAGES')
    os.makedirs(path, exist_ok=True)
EOF

python manage.py makemessages -a -v 0
python manage.py makemessages -d djangojs -a -v 0
python manage.py compilemessages -v 0
fi

if [ -n "$SUPER_USER_NAME" ]; then
  python manage.py shell <<EOF
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username="${SUPER_USER_NAME}").exists():
    User.objects.create_superuser(
        username="${SUPER_USER_NAME}",
        email="${SUPER_USER_EMAIL}",
        password="${SUPER_USER_PASSWORD}"
    )
EOF
fi

if [ "$DEBUG" = "1" ]; then
    python manage.py runserver 0.0.0.0:8000
    #uvicorn core.asgi:application --host 0.0.0.0 --port 8000 --workers 1 --lifespan off --loop uvloop --http httptools --timeout-keep-alive 5 --reload
else
    uvicorn core.asgi:application --host 0.0.0.0 --port 8000 --workers 1 --log-level warning --lifespan off --loop uvloop --http httptools --timeout-keep-alive 5 --use-colors
fi
