#!/bin/sh

set -e

if [ ! -f "core/__init__.py" ]; then
    echo "Projeto Django não encontrado. Criando projeto 'core'..."
    django-admin startproject core .
fi

python manage.py collectstatic --noinput
python manage.py makemigrations --noinput 
python manage.py migrate --noinput

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
    uvicorn core.asgi:application --host 0.0.0.0 --port 8000 --workers 4 --lifespan off --loop uvloop --http httptools --timeout-keep-alive 5
fi
