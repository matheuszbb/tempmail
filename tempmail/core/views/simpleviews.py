import re
import os
import json
import base64
import hashlib
import logging
import asyncio
import unicodedata
from html import escape as html_escape
from django.views import View
from django.urls import reverse
from collections import Counter
from django.conf import settings
from django.utils import timezone
from django.contrib import messages
from asgiref.sync import sync_to_async
from datetime import datetime, timedelta
from django.middleware.csrf import get_token
from django.shortcuts import render, redirect
from ..models import Domain, EmailAccount, Message
from django.core.validators import EmailValidator
from django.core.exceptions import ValidationError
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_control
from ..services.smtplabs_client import SMTPLabsClient, SMTPLabsAPIError
from ..mixins import AdminRequiredMixin, DateFilterMixin, EmailAccountService
from django.http import HttpResponse, JsonResponse, HttpResponseForbidden, HttpResponseServerError, HttpResponseNotFound, HttpResponseBadRequest

logger = logging.getLogger(__name__)

class HeartCheckView(View):
    async def get(self, request):
        return JsonResponse({"status": "OK"}, status=200)

class ChromeDevToolsStubView(View):
    async def get(self, request):
        return JsonResponse({}, status=200)

class Robots_txtView(View):
    async def get(self, request):
        robots_txt_content = f"""\
User-Agent: *
Allow: /
Allow: /sobre
Allow: /privacidade
Allow: /termos
Allow: /contato
Sitemap: {request.build_absolute_uri('/sitemap.xml')}
"""
        return HttpResponse(robots_txt_content, content_type="text/plain", status=200)

class Sitemap_xmlView(View):
    async def get(self, request):
        site_url = request.build_absolute_uri('/')[:-1]  # Remove a última barra se houver
        sitemap_xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url>
    <loc>{site_url}</loc>
    <priority>1.0</priority>
    <changefreq>daily</changefreq>
</url>
<url>
    <loc>{site_url}/sobre</loc>
    <priority>0.8</priority>
    <changefreq>monthly</changefreq>
</url>
<url>
    <loc>{site_url}/privacidade</loc>
    <priority>0.6</priority>
    <changefreq>monthly</changefreq>
</url>
<url>
    <loc>{site_url}/termos</loc>
    <priority>0.6</priority>
    <changefreq>monthly</changefreq>
</url>
<url>
    <loc>{site_url}/contato</loc>
    <priority>0.4</priority>
    <changefreq>yearly</changefreq>
</url>
</urlset>
"""
        return HttpResponse(sitemap_xml_content, content_type="application/xml", status=200)
     
class SobreView(View):
    """Página Sobre o EmailRush"""
    async def get(self, request):
        return await sync_to_async(render)(request, 'sobre.html')

class PrivacidadeView(View):
    """Página de Política de Privacidade"""
    async def get(self, request):
        return await sync_to_async(render)(request, 'privacidade.html')

class TermosView(View):
    """Página de Termos de Serviço"""
    async def get(self, request):
        return await sync_to_async(render)(request, 'termos.html')

class ContatoView(AdminRequiredMixin, View):
    """
    Página de Contato
    Usa AdminRequiredMixin para verificação de superuser
    """
    
    # Não fazer verificação automática no dispatch
    skip_admin_check = True

    async def _response_user(self, request):
        """Envia resposta de sucesso ao usuário"""
        await sync_to_async(messages.success)(
            request, 
            str(_("Mensagem enviada com sucesso! Responderemos em breve."))
        )
        return await sync_to_async(render)(request, "contato.html")

    async def get(self, request):
        """Renderiza formulário de contato"""
        return await sync_to_async(render)(request, "contato.html")
    
    async def post(self, request):
        """
        Processa formulário de contato.
        Se for admin ou email especial, redireciona ao admin.
        Caso contrário, exibe mensagem de sucesso.
        """
        email = request.POST.get('email', '').strip()

        # Verificar se é admin
        user_is_superuser = await self._check_user_is_superuser(request)
        
        if user_is_superuser:
            return await self._response_user(request)
        else:
            # Verificar se é email do super usuário configurado
            super_user_email = os.getenv("SUPER_USER_EMAIL", "")
            if email and email == super_user_email:
                return redirect('admin:index')
            
            return await self._response_user(request)