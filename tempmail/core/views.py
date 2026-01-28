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
from .models import Domain, EmailAccount, Message
from django.core.validators import EmailValidator
from django.core.exceptions import ValidationError
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import cache_control
from .services.smtplabs_client import SMTPLabsClient, SMTPLabsAPIError
from .mixins import AdminRequiredMixin, DateFilterMixin, EmailAccountService
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

class EmailInUseError(Exception):
    """Exceção levantada quando um e-mail já está sendo usado por outro usuário."""
    pass

class EmailInCooldownError(Exception):
    """Exceção levantada quando um e-mail está em cooldown."""
    pass

class EmailNotFoundError(Exception):
    """Exceção levantada quando um e-mail não existe."""
    pass

class IndexView(View):
    async def get(self, request):
        email_address = await sync_to_async(request.session.get)('email_address')
        messages = []
        
        if email_address:
            try:
                account = await EmailAccount.objects.aget(address=email_address)
                
                # Buscar mensagens desde a primeira vez que este email foi usado na sessão
                email_sessions = await sync_to_async(request.session.get)('email_sessions', {})
                session_start_val = await sync_to_async(request.session.get)('session_start')
                
                # Usar o timestamp da primeira vez que este email foi usado, se disponível
                if isinstance(email_sessions, dict) and email_address in email_sessions:
                    session_start_str = email_sessions[email_address]
                elif session_start_val:
                    session_start_str = session_start_val
                else:
                    session_start_str = None
                
                if session_start_str:
                    session_start = datetime.fromisoformat(session_start_str)
                    messages_qs = Message.objects.filter(
                        account=account,
                        received_at__gte=session_start
                    ).order_by('-received_at')
                    
                    # ✅ CORRIGIDO: Converter QuerySet em lista de forma assíncrona
                    # ao invés de usar async for em iterador síncrono
                    messages = await sync_to_async(list)(messages_qs)
                    
            except EmailAccount.DoesNotExist:
                pass
                
        response = await sync_to_async(render)(request, 'core/index.html', {
            'initial_messages': messages
        })
        
        get_token(request)
        
        return response
    
class TempEmailAPI(View):
    """
    API para gerenciar emails temporários.
    Refatorada para usar EmailAccountService.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.email_service = EmailAccountService()
    
    async def get(self, request):
        """Retorna email temporário da sessão atual ou cria um novo"""
        try:
            account, is_new = await self.email_service.get_or_create_temp_email(request)

            # Verificar se houve erro na criação da conta
            if account is None:
                return JsonResponse({
                    'success': False,
                    'error': str(_('Serviço temporariamente indisponível. Tente novamente em alguns minutos.'))
                }, status=200)
            
            # ✅ Salvar no histórico se for novo ou se não estiver no histórico ainda
            if is_new or account.address not in await sync_to_async(request.session.get)('email_history', []):
                await self._save_to_history(request, account.address)
            
            session_start_val = await sync_to_async(request.session.get)('session_start')
            
            # Se não há session_start (refresh), usar last_used_at da conta
            if session_start_val:
                session_start = datetime.fromisoformat(session_start_val)
            elif account.last_used_at:
                session_start = account.last_used_at
            else:
                session_start = timezone.now()
            
            expires_at = session_start + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
            expires_in = int((expires_at - timezone.now()).total_seconds())
            
            # Salvar fingerprint no cookie
            browser_fingerprint = self._get_browser_fingerprint(request)
            response = JsonResponse({
                'success': True,
                'email': account.address,
                'session_start': session_start.isoformat(),
                'expires_in': max(0, expires_in),
                'is_new_session': is_new
            })
            
            # Atualizar cookie com fingerprints
            self._save_fingerprint_to_cookie(response, request, account.address, browser_fingerprint)
            return response
        except Exception as e:
            logger.exception("Erro ao obter email temporário")
            return JsonResponse({
                'success': False,
                'error': str(_('Erro ao criar email temporário'))
            }, status=500)

    async def post(self, request):
        """
        Limpa a sessão atual OU define um email customizado se fornecido no JSON.
        JSON format: {"email": "customuser@domain.com"}
        """
        try:
            data = {}
            if request.body:
                try:
                    data = json.loads(request.body)
                except json.JSONDecodeError:
                    pass

            custom_email = data.get('email')

            # Verificar se é o mesmo email já em uso na sessão
            session_email = await sync_to_async(request.session.get)('email_address')
            if custom_email and session_email == custom_email:
                return JsonResponse({
                    'success': True,
                    'email': session_email,
                    'message': str(_('Você já está usando este endereço de e-mail'))
                })

            # Se for um reset (POST vazio ou sem email)
            if not custom_email:
                return await self._handle_reset(request)

            # Se for para definir um email específico
            return await self._handle_custom_email(request, custom_email, session_email)

        except SMTPLabsAPIError as e:
            return self._handle_smtp_error(e)

        except Exception as e:
            logger.exception("Erro inesperado ao processar POST em TempEmailAPI")
            return JsonResponse({
                'success': False,
                'error': str(_('Erro interno do servidor. Nossa equipe foi notificada.'))
            }, status=500)

    async def _handle_reset(self, request):
        """Limpa a sessão e gera novo email"""
        # Guardar email anterior para evitar reutilização imediata
        previous_email = await sync_to_async(request.session.get)('email_address')
        
        has_email = await sync_to_async(request.session.__contains__)('email_address')
        if has_email:
            await sync_to_async(request.session.pop)('email_address', None)
        
        has_start = await sync_to_async(request.session.__contains__)('session_start')
        if has_start:
            await sync_to_async(request.session.pop)('session_start', None)
        
        # Armazenar email anterior na sessão para exclusão
        if previous_email:
            await sync_to_async(request.session.__setitem__)('previous_email', previous_email)
        
        # Gerar novo email imediatamente (Atomic Reset)
        logger.info("Sessão limpa. Gerando novo email imediatamente...")
        account, is_new = await self.email_service.get_or_create_temp_email(request)

        # Verificar se houve erro na criação da conta
        if account is None:
            return JsonResponse({
                'success': False,
                'error': str(_('Serviço temporariamente indisponível. Tente novamente em alguns minutos.'))
            }, status=200)

        # Registrar o novo email no histórico
        email_sessions = await sync_to_async(request.session.get)('email_sessions', {})
        if not isinstance(email_sessions, dict):
            email_sessions = {}
        
        if account.address not in email_sessions:
            email_sessions[account.address] = timezone.now().isoformat()
        await sync_to_async(request.session.__setitem__)('email_sessions', email_sessions)
        
        # ✅ Salvar no histórico
        await self._save_to_history(request, account.address)
        
        session_start_val = await sync_to_async(request.session.get)('session_start')
        session_start = datetime.fromisoformat(session_start_val)
        
        expires_at = session_start + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
        expires_in = int((expires_at - timezone.now()).total_seconds())

        # Salvar fingerprint no cookie
        browser_fingerprint = self._get_browser_fingerprint(request)
        response = JsonResponse({
            'success': True,
            'email': account.address,
            'session_start': session_start.isoformat(),
            'expires_in': max(0, expires_in),
            'is_new_session': True,
            'message': str(_('Sessão resetada com sucesso'))
        })
        
        # Atualizar cookie com fingerprints
        self._save_fingerprint_to_cookie(response, request, account.address, browser_fingerprint)
        return response

    async def _handle_custom_email(self, request, custom_email, session_email):
        """Processa solicitação de email customizado"""
        logger.info(f"Tentando login/mudança para email customizado: {custom_email!r}")
        
        # ✅ VALIDAÇÃO: Formato básico
        if '@' not in custom_email:
            return JsonResponse({
                'success': False, 
                'error': str(_('Endereço de email inválido'))
            }, status=200)
        
        # ✅ VALIDAÇÃO: Usar validador do Django        
        email_validator = EmailValidator(message=_('Endereço de email inválido'))
        try:
            email_validator(custom_email)
        except ValidationError:
            return JsonResponse({
                'success': False,
                'error': str(_('Endereço de email inválido. Verifique o formato.'))
            }, status=200)
        
        # ✅ VALIDAÇÃO: Verificar caracteres válidos na parte local (antes do @)
        local_part = custom_email.split('@')[0]
        domain_part = custom_email.split('@')[1]
        
        # Regex para caracteres válidos: letras ASCII, números, pontos, hífens e underscores
        valid_pattern = r'^[a-zA-Z0-9._-]+$'
        
        # Sempre tentar normalizar primeiro (ç→c, á→a, etc)
        local_part_normalized = unicodedata.normalize('NFKD', local_part)
        local_part_normalized = ''.join([c for c in local_part_normalized if not unicodedata.combining(c)])
        
        # Se houve mudança, logar
        if local_part != local_part_normalized:
            logger.info(f"Email normalizado: {local_part!r} → {local_part_normalized!r}")
        
        # Verificar se após normalização está válido
        if not re.match(valid_pattern, local_part_normalized):
            return JsonResponse({
                'success': False,
                'error': str(_('Nome de usuário contém caracteres inválidos. Use apenas letras, números, pontos, hífens e underscores.'))
            }, status=200)
        
        # Usar sempre a versão normalizada
        local_part = local_part_normalized
        custom_email = f"{local_part}@{domain_part}"
        
        # ✅ VALIDAÇÃO: Não pode começar ou terminar com ponto
        if local_part.startswith('.') or local_part.endswith('.'):
            return JsonResponse({
                'success': False,
                'error': str(_('Nome de usuário não pode começar ou terminar com ponto'))
            }, status=200)

        # Obter histórico de emails usados nesta sessão
        session_used_emails = await sync_to_async(request.session.get)('used_emails', [])
        if not isinstance(session_used_emails, list):
            session_used_emails = []
        
        # Obter histórico de quando cada email foi usado pela primeira vez
        email_sessions = await sync_to_async(request.session.get)('email_sessions', {})
        if not isinstance(email_sessions, dict):
            email_sessions = {}
        
        # Liberar o email anterior da sessão (se houver)
        if session_email and session_email != custom_email:
            await self._release_previous_email(session_email)

        # Verificar se a conta já existe no nosso banco
        try:
            account = await self._get_or_create_custom_account(request, custom_email, session_used_emails)
        except EmailInCooldownError as e:
            return JsonResponse({
                'success': False,
                'error': str(e)
            }, status=200)
        except EmailInUseError:
            return JsonResponse({
                'success': False,
                'error': str(_('Este endereço de e-mail já está sendo usado por outro usuário'))
            }, status=200)
        
        if account is None:
            return JsonResponse({
                'success': False, 
                'error': str(_('Não foi possível acessar este email'))
            }, status=200)

        # Atualizar sessão
        await self._update_session_with_account(request, account, session_used_emails, email_sessions)
        
        # ✅ Salvar no histórico
        await self._save_to_history(request, account.address)
        
        # Calcular expiração
        first_used_at = datetime.fromisoformat(email_sessions[account.address])
        expires_at = first_used_at + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
        expires_in = int((expires_at - timezone.now()).total_seconds())

        # Salvar fingerprint no cookie para persistir entre sessões
        browser_fingerprint = self._get_browser_fingerprint(request)
        response = JsonResponse({
            'success': True,
            'email': account.address,
            'expires_in': max(0, expires_in),
            'message': str(_('Email alterado com sucesso'))
        })
        
        # Atualizar cookie com fingerprints
        self._save_fingerprint_to_cookie(response, request, account.address, browser_fingerprint)
        return response

    async def _release_previous_email(self, session_email):
        """Libera o email anterior da sessão"""
        try:
            previous_account = await EmailAccount.objects.aget(address=session_email)
            previous_account.is_available = True
            previous_account.session_expires_at = None  # Limpar expiração da sessão
            # Manter last_used_at para auditoria
            await sync_to_async(previous_account.save)(
                update_fields=['is_available', 'session_expires_at', 'updated_at']
            )
            logger.info(f"Email anterior liberado: {session_email}")
        except EmailAccount.DoesNotExist:
            pass

    async def _get_or_create_custom_account(self, request, custom_email, session_used_emails):
        """Obtém ou cria conta customizada com validação de cooldown"""
        try:
            account = await EmailAccount.objects.aget(address=custom_email)
            
            # Obter session key
            session_key = request.session.session_key
            if not session_key:
                await sync_to_async(request.session.create)()
                session_key = request.session.session_key
            
            # Verificar se este email foi usado pelo mesmo usuário nesta sessão
            email_was_used_in_session = custom_email in session_used_emails
            
            # Verificar se este usuário pode usar esta conta (cooldown + session_key)
            can_use = await sync_to_async(account.can_be_used_by)(session_key)
            
            if not can_use:
                # ANTES de rejeitar, verificar fingerprint do navegador
                browser_fingerprint = self._get_browser_fingerprint(request)
                
                # Buscar fingerprints salvos no COOKIE (persiste entre sessões)
                email_fingerprints_cookie = request.COOKIES.get('email_fps', '{}')
                try:
                    email_fingerprints = json.loads(email_fingerprints_cookie)
                except:
                    email_fingerprints = {}
                
                saved_fingerprint = email_fingerprints.get(custom_email)
                
                # Se for o mesmo navegador (fingerprint match), permitir reutilização
                if saved_fingerprint and saved_fingerprint == browser_fingerprint:
                    logger.info(f"✅ Fingerprint match para {custom_email!r}, permitindo reutilização mesmo com sessão diferente")
                    can_use = True  # Permitir uso
                else:
                    # Verificar se está em cooldown
                    if account.cooldown_until and timezone.now() < account.cooldown_until:
                        time_left = account.cooldown_until - timezone.now()
                        minutes = int(time_left.total_seconds() / 60)
                        logger.warning(f"Email {custom_email!r} em cooldown por mais {minutes} minutos")
                        raise EmailInCooldownError(f"Este email está em cooldown. Disponível em {minutes} minutos.")
                    else:
                        logger.warning(f"Email {custom_email!r} possui sessão ativa de outro usuário (fingerprint diferente)")
                        raise EmailInUseError()
            
            # Se o email foi usado nesta sessão, liberar antes de reutilizar
            if email_was_used_in_session and not account.is_available:
                account.is_available = True
                account.last_used_at = None
                await sync_to_async(account.save)(
                    update_fields=['is_available', 'last_used_at', 'updated_at']
                )
                logger.info(f"Email usado nesta sessão, liberado para reutilização: {custom_email}")
            
            # Marcar como usada (reseta timer para 60min)
            await sync_to_async(account.mark_as_used)(
                session_key=session_key,
                session_duration_seconds=settings.TEMPMAIL_SESSION_DURATION
            )
            
            # Salvar fingerprint na sessão para permitir reutilização
            browser_fingerprint = self._get_browser_fingerprint(request)
            email_fingerprints = await sync_to_async(request.session.get)('email_fingerprints', {})
            email_fingerprints[custom_email] = browser_fingerprint
            await sync_to_async(request.session.__setitem__)('email_fingerprints', email_fingerprints)
            
            logger.info(f"Usuário assumiu conta existente: {custom_email}")
            return account

        except EmailAccount.DoesNotExist:
            # Criar nova conta
            return await self._create_custom_account(custom_email)

    async def _create_custom_account(self, custom_email):
        """Cria uma nova conta customizada"""
        domain_part = custom_email.split('@')[1]
        
        try:
            domain = await Domain.objects.aget(domain=domain_part)
        except Domain.DoesNotExist:
            logger.warning(f"Domínio não suportado: {domain_part}")
            return None
        
        client = SMTPLabsClient()
        password = EmailAccount.generate_random_password()
        
        try:
            account_response = await client.create_account(custom_email, password)
            
            account = await EmailAccount.objects.acreate(
                smtp_id=account_response['id'],
                address=custom_email,
                password=password,
                domain=domain,
                is_available=False,
                last_used_at=timezone.now()
            )
            logger.info(f"Nova conta customizada criada: {custom_email}")
            return account
            
        except SMTPLabsAPIError as e:
            # Se já existe na API
            if "already used" in str(e).lower() or "value is already used" in str(e).lower():
                return await self._recover_existing_account(client, custom_email, password, domain)
            else:
                logger.error(f"Erro ao criar conta customizada na API: {str(e)}")
                return None

    async def _recover_existing_account(self, client, custom_email, password, domain):
        """Recupera conta que já existe na API"""
        logger.info(f"Email {custom_email} já existe na API. Tentando recuperar...")
        
        accounts_search = await client.get_accounts(address=custom_email)
        accounts_list = accounts_search if isinstance(accounts_search, list) else accounts_search.get('member', [])
        
        if accounts_list:
            api_account = accounts_list[0]
            account = await EmailAccount.objects.acreate(
                smtp_id=api_account['id'],
                address=custom_email,
                password=password,
                domain=domain,
                is_available=False,
                last_used_at=timezone.now()
            )
            logger.info(f"Conta recuperada da API: {custom_email}")
            return account
        
        return None

    async def _update_session_with_account(self, request, account, session_used_emails, email_sessions):
        """Atualiza a sessão com a conta selecionada"""
        await sync_to_async(request.session.__setitem__)('email_address', account.address)
        
        # Adicionar email ao histórico de emails usados nesta sessão
        if account.address not in session_used_emails:
            session_used_emails.append(account.address)
        await sync_to_async(request.session.__setitem__)('used_emails', session_used_emails)
        
        # Registrar quando este email foi usado pela primeira vez
        if account.address not in email_sessions:
            email_sessions[account.address] = timezone.now().isoformat()
        await sync_to_async(request.session.__setitem__)('email_sessions', email_sessions)
        
        # Usar o timestamp da primeira vez que este email foi usado
        first_used_at = datetime.fromisoformat(email_sessions[account.address])
        await sync_to_async(request.session.__setitem__)('session_start', first_used_at.isoformat())
        
        await sync_to_async(request.session.save)()

    async def _save_to_history(self, request, email_address):
        """Salva email no histórico da sessão (últimos 5)"""
        history = await sync_to_async(request.session.get)('email_history', [])
        
        # Remover se já existe (evitar duplicatas)
        if email_address in history:
            history.remove(email_address)
        
        # Adicionar no início
        history.insert(0, email_address)
        
        # Manter apenas últimos 5
        history = history[:5]
        
        await sync_to_async(request.session.__setitem__)('email_history', history)
        logger.debug(f"Histórico atualizado: {history}")

    async def _get_email_history(self, request):
        """Retorna histórico de emails com status de disponibilidade"""
        history = await sync_to_async(request.session.get)('email_history', [])
        
        result = []
        for email in history:
            try:
                account = await EmailAccount.objects.aget(address=email)
                
                # Verificar disponibilidade
                is_available = account.is_available
                is_in_cooldown = (
                    account.cooldown_until and 
                    timezone.now() < account.cooldown_until
                )
                is_active = account.is_session_active()
                
                # Verificar se é o mesmo usuário (session key ou fingerprint salvo na sessão)
                session_key = request.session.session_key
                browser_fingerprint = self._get_browser_fingerprint(request)
                
                # Buscar fingerprint salvo na sessão para este email
                email_fingerprints = await sync_to_async(request.session.get)('email_fingerprints', {})
                saved_fingerprint = email_fingerprints.get(email)
                
                can_reuse = (
                    account.last_session_key == session_key or
                    (saved_fingerprint and saved_fingerprint == browser_fingerprint)
                )
                
                result.append({
                    'address': email,
                    'available': is_available and not is_active,
                    'in_cooldown': is_in_cooldown,
                    'can_reuse': can_reuse,  # Mesmo usuário pode reusar
                    'expires_at': account.session_expires_at.isoformat() if account.session_expires_at else None,
                    'cooldown_until': account.cooldown_until.isoformat() if account.cooldown_until else None,
                })
            except EmailAccount.DoesNotExist:
                # Email não existe mais
                result.append({
                    'address': email,
                    'available': False,
                    'in_cooldown': False,
                    'can_reuse': False,
                    'error': 'Email não encontrado'
                })
        
        return result

    def _handle_smtp_error(self, e):
        """Trata erros da API SMTPLabs"""
        logger.error(f"Erro na API externa SMTPLabs: {str(e)}")

        error_message = str(_('Erro interno ao processar requisição'))

        if '504' in str(e) or 'Gateway Timeout' in str(e):
            error_message = str(_('Serviço temporariamente indisponível. Tente novamente em alguns minutos.'))
        elif '500' in str(e) or 'Internal Server Error' in str(e):
            error_message = str(_('Erro temporário no servidor. Tente novamente em alguns instantes.'))
        elif '429' in str(e) or 'Too Many Requests' in str(e):
            error_message = str(_('Muitas tentativas. Aguarde alguns minutos antes de tentar novamente.'))
        elif 'timeout' in str(e).lower():
            error_message = str(_('Conexão lenta. Verifique sua internet e tente novamente.'))

        return JsonResponse({
            'success': False,
            'error': error_message
        }, status=200)
    
    async def _save_to_history(self, request, email_address):
        """Salva email no histórico da sessão (últimos 5)"""
        history = await sync_to_async(request.session.get)('email_history', [])
        
        # Remover se já existe (evitar duplicatas)
        if email_address in history:
            history.remove(email_address)
        
        # Adicionar no início
        history.insert(0, email_address)
        
        # Manter apenas últimos 5
        history = history[:5]
        
        await sync_to_async(request.session.__setitem__)('email_history', history)
        logger.debug(f"Histórico atualizado: {history}")
    
    def _get_browser_fingerprint(self, request):
        """
        Gera fingerprint único do navegador com fallback para cookie
        """
        
        # 1. Tentar obter fingerprint do cookie (mais confiável)
        fingerprint_cookie = request.COOKIES.get('browser_fp')
        if fingerprint_cookie:
            logger.debug(f"Fingerprint recuperado do cookie: {fingerprint_cookie[:8]}...")
            return fingerprint_cookie
        
        # 2. Gerar novo fingerprint baseado em headers
        import hashlib
        
        # Sanitizar headers - manter caracteres normais mas limitar tamanho
        # Não remover parênteses/ponto-e-vírgula pois são parte normal do user-agent
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
        accept_language = request.META.get('HTTP_ACCEPT_LANGUAGE', '')[:100]
        accept_encoding = request.META.get('HTTP_ACCEPT_ENCODING', '')[:100]
        
        # Adicionar IP (mais estável)
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')
        
        # Validar formato de IP (manter apenas dígitos, pontos e dois-pontos para IPv6)
        ip = re.sub(r'[^\d.:]', '', ip)[:45]
        
        # Combinar headers estáveis (hash já protege contra injection)
        fingerprint_data = f"{user_agent}|{accept_language}|{accept_encoding}|{ip}"
        
        # Hash para não expor dados sensíveis
        fingerprint = hashlib.sha256(fingerprint_data.encode()).hexdigest()[:32]
        
        logger.debug(f"Novo fingerprint gerado: {fingerprint[:8]}...")
        return fingerprint
    
    def _save_fingerprint_to_cookie(self, response, request, email_address, browser_fingerprint):
        """Salva o fingerprint de um email em um cookie para persistir entre sessões"""
        # Buscar fingerprints existentes
        email_fingerprints_cookie = request.COOKIES.get('email_fps', '{}')
        try:
            email_fingerprints = json.loads(email_fingerprints_cookie)
        except:
            email_fingerprints = {}
        
        # Adicionar novo fingerprint
        email_fingerprints[email_address] = browser_fingerprint
        
        # Manter apenas últimos 10 emails para não crescer infinitamente
        if len(email_fingerprints) > 10:
            # Remover os mais antigos
            emails_list = list(email_fingerprints.items())
            email_fingerprints = dict(emails_list[-10:])
        
        # Salvar no cookie (válido por 7 dias)
        response.set_cookie(
            'email_fps',
            json.dumps(email_fingerprints),
            max_age=7*24*60*60,  # 7 dias
            httponly=True,
            samesite='Lax'
        )
        logger.debug(f"Fingerprint salvo no cookie para {email_address}")

class EmailHistoryAPI(View):
    """API para buscar histórico de emails usados"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.email_service = EmailAccountService()
    
    async def get(self, request):
        """Retorna últimos 5 emails usados pelo usuário"""
        try:
            history = await self._get_email_history(request)
            return JsonResponse({
                'success': True,
                'history': history,
                'count': len(history)
            }, status=200)
        except Exception as e:
            logger.error(f"Erro ao buscar histórico: {str(e)}")
            return JsonResponse({
                'success': False,
                'error': str(_('Erro ao buscar histórico'))
            }, status=500)
    
    async def _get_email_history(self, request):
        """Retorna histórico de emails com status de disponibilidade"""
        history = await sync_to_async(request.session.get)('email_history', [])
        
        result = []
        for email in history:
            try:
                account = await EmailAccount.objects.aget(address=email)
                
                # Verificar disponibilidade
                is_available = account.is_available
                is_in_cooldown = (
                    account.cooldown_until and 
                    timezone.now() < account.cooldown_until
                )
                is_active = account.is_session_active()
                
                # Verificar se é o mesmo usuário (session key ou fingerprint do cookie)
                session_key = request.session.session_key
                browser_fingerprint = self._get_browser_fingerprint(request)
                
                # Buscar fingerprint salvo no COOKIE (persiste entre sessões)
                email_fingerprints_cookie = request.COOKIES.get('email_fps', '{}')
                try:
                    email_fingerprints = json.loads(email_fingerprints_cookie)
                except:
                    email_fingerprints = {}
                
                saved_fingerprint = email_fingerprints.get(email)
                
                can_reuse = (
                    account.last_session_key == session_key or
                    (saved_fingerprint and saved_fingerprint == browser_fingerprint)
                )
                
                result.append({
                    'address': email,
                    'available': is_available and not is_active,
                    'in_cooldown': is_in_cooldown,
                    'can_reuse': can_reuse,  # Mesmo usuário pode reusar
                    'expires_at': account.session_expires_at.isoformat() if account.session_expires_at else None,
                    'cooldown_until': account.cooldown_until.isoformat() if account.cooldown_until else None,
                })
            except EmailAccount.DoesNotExist:
                # Email não existe mais
                result.append({
                    'address': email,
                    'available': False,
                    'in_cooldown': False,
                    'can_reuse': False,
                    'error': 'Email não encontrado'
                })
        
        return result
    
    def _get_browser_fingerprint(self, request):
        """Gera fingerprint único do navegador com fallback para cookie"""
        
        # 1. Tentar obter fingerprint do cookie (mais confiável)
        fingerprint_cookie = request.COOKIES.get('browser_fp')
        if fingerprint_cookie:
            logger.debug(f"Fingerprint recuperado do cookie: {fingerprint_cookie[:8]}...")
            return fingerprint_cookie
        
        # 2. Gerar novo fingerprint baseado em headers
        # Sanitizar headers - manter caracteres normais mas limitar tamanho
        # Não remover parênteses/ponto-e-vírgula pois são parte normal do user-agent
        user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
        accept_language = request.META.get('HTTP_ACCEPT_LANGUAGE', '')[:100]
        accept_encoding = request.META.get('HTTP_ACCEPT_ENCODING', '')[:100]
        
        # Adicionar IP (mais estável)
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0].strip()
        else:
            ip = request.META.get('REMOTE_ADDR', '')
        
        # Validar formato de IP (manter apenas dígitos, pontos e dois-pontos para IPv6)
        ip = re.sub(r'[^\d.:]', '', ip)[:45]
        
        # Combinar headers estáveis (hash já protege contra injection)
        fingerprint_data = f"{user_agent}|{accept_language}|{accept_encoding}|{ip}"
        
        # Hash para não expor dados sensíveis
        fingerprint = hashlib.sha256(fingerprint_data.encode()).hexdigest()[:32]
        
        logger.debug(f"Novo fingerprint gerado: {fingerprint[:8]}...")
        return fingerprint
    
    async def _check_browser_fingerprint(self, account, fingerprint):
        """DEPRECATED: Fingerprint agora é verificado via sessão, não banco"""
        # Mantido para compatibilidade, mas sempre retorna False
        return False

class MessageListAPI(View):
    """API para listar e atualizar mensagens"""
    
    async def get(self, request):
        """Lista mensagens da sessão atual e sincroniza se necessário (Throttle de 10s)"""
        try:
            session_email = await sync_to_async(request.session.get)('email_address')
            session_start = await sync_to_async(request.session.get)('session_start')
            email_sessions = await sync_to_async(request.session.get)('email_sessions', {})
            
            if not session_email:
                return JsonResponse({
                    'success': False, 
                    'error': str(_('Sessão não encontrada'))
                }, status=200)
            
            account = await EmailAccount.objects.aget(address=session_email)
            
            # Usar o timestamp da primeira vez que este email foi usado
            if isinstance(email_sessions, dict) and session_email in email_sessions:
                session_start_str = email_sessions[session_email]
            elif session_start:
                session_start_str = session_start
            else:
                return JsonResponse({
                    'success': False, 
                    'error': str(_('Sessão não encontrada'))
                }, status=200)
            
            session_start_dt = datetime.fromisoformat(session_start_str)
            
            # Sincronização inteligente com throttle
            await self._sync_messages_if_needed(account)
            
            # Buscar mensagens do período da sessão
            session_end = session_start_dt + timedelta(hours=1)
            
            messages_qs = Message.objects.filter(
                account=account,
                received_at__gte=session_start_dt,
                received_at__lte=session_end
            )
            
            # ✅ CORRIGIDO: Converter QuerySet para lista de forma assíncrona
            messages_list = await sync_to_async(list)(messages_qs)
            
            # Construir lista de dados das mensagens
            messages_data = [
                {
                    'id': msg.id,
                    'smtp_id': msg.smtp_id,
                    'from_address': msg.from_address,
                    'from_name': msg.from_name,
                    'subject': msg.subject,
                    'text_preview': msg.text[:100] if msg.text else '',
                    'has_attachments': msg.has_attachments,
                    'is_read': msg.is_read,
                    'received_at': msg.received_at.isoformat(),
                }
                for msg in messages_list
            ]
            
            return JsonResponse({
                'success': True,
                'messages': messages_data,
                'total': len(messages_data),
                'last_sync': account.last_synced_at.isoformat() if account.last_synced_at else None
            })
            
        except EmailAccount.DoesNotExist:
            return JsonResponse({
                'success': False, 
                'error': str(_('Conta não encontrada'))
            }, status=404)
        except Exception as e:
            logger.exception("Erro ao listar mensagens")
            return JsonResponse({
                'success': False, 
                'error': str(_('Erro ao buscar mensagens'))
            }, status=500)

    async def _sync_messages_if_needed(self, account):
        """
        Sincroniza mensagens com a API se necessário (throttle de 8s).
        
        Args:
            account: Instância de EmailAccount
        """
        now = timezone.now()
        sync_threshold = timedelta(seconds=8)
        
        should_sync = False
        if not account.last_synced_at:
            should_sync = True
        elif now >= account.last_synced_at + sync_threshold:
            should_sync = True
        
        if not should_sync:
            return
        
        client = SMTPLabsClient()
        logger.info(f"Sincronizando inbox para {account.address} (Auto-sync GET)")
        
        try:
            api_response = await client.get_all_inbox_messages(account.smtp_id)
            
            # Garantir que api_messages seja uma lista
            api_messages = []
            if isinstance(api_response, list):
                api_messages = api_response
            elif isinstance(api_response, dict):
                api_messages = api_response.get('member', [])
            
            if not isinstance(api_messages, list):
                logger.error(f"Formato inesperado da API para mensagens: {type(api_messages)}")
                return

            for msg_data in api_messages:
                if not isinstance(msg_data, dict):
                    logger.warning(f"Mensagem ignorada (formato inválido): {type(msg_data)}")
                    continue
                    
                smtp_id = msg_data.get('id')
                if not smtp_id:
                    continue

                existing_msg = await Message.objects.filter(smtp_id=smtp_id).afirst()
                
                # Buscar detalhes se necessário
                needs_detail = not existing_msg or (
                    msg_data.get('hasAttachments') and 
                    not (existing_msg.attachments if existing_msg else False)
                )
                
                if needs_detail:
                    await self._fetch_and_save_message(client, account, msg_data, existing_msg, now)
            
            # Atualizar timestamp de sincronização
            account.last_synced_at = now
            await sync_to_async(account.save)(update_fields=['last_synced_at', 'updated_at'])
            
        except Exception as e:
            # Se a conta não existir mais na API (404), remover do banco local
            if "404" in str(e):
                logger.warning(f"Conta {account.address} (ID: {account.smtp_id}) não existe mais na API remota durante sync")
                try:
                    await sync_to_async(account.delete)()
                    logger.info(f"Conta órfã {account.address} removida durante sync de mensagens")
                except Exception as delete_error:
                    logger.error(f"Erro ao deletar conta órfã: {delete_error}")
            else:
                logger.error(f"Erro na sincronização automática: {str(e)}")

    async def _fetch_and_save_message(self, client, account, msg_data, existing_msg, now):
        """
        Busca detalhes completos da mensagem e salva no banco.
        
        Args:
            client: Instância de SMTPLabsClient
            account: Instância de EmailAccount
            msg_data: Dados da mensagem da API
            existing_msg: Mensagem existente no banco (ou None)
            now: Datetime atual
        """
        smtp_id = msg_data.get('id')
        
        try:
            # Buscar ID da mailbox
            mailbox_id = msg_data.get('mailboxId')
            if not mailbox_id:
                inbox_data = await client.get_inbox_mailbox(account.smtp_id)
                if inbox_data:
                    mailbox_id = inbox_data.get('id')

            # Buscar detalhes completos
            if mailbox_id:
                full_msg = await client.get_message(account.smtp_id, mailbox_id, smtp_id)
                msg_data.update(full_msg)
        except Exception as e:
            logger.warning(f"Não foi possível buscar detalhes da mensagem {smtp_id}: {e}")

        # Processar HTML
        html_content = ''
        html_raw = msg_data.get('html')
        if isinstance(html_raw, list) and html_raw:
            html_content = html_raw[0]
        elif isinstance(html_raw, str):
            html_content = html_raw

        # Preparar dados para salvar
        data_to_save = {
            'from_address': msg_data.get('from', {}).get('address', '') if isinstance(msg_data.get('from'), dict) else '',
            'from_name': msg_data.get('from', {}).get('name', '') if isinstance(msg_data.get('from'), dict) else '',
            'to_addresses': msg_data.get('to', []),
            'subject': msg_data.get('subject', ''),
            'text': msg_data.get('text') or msg_data.get('body', {}).get('text') or '',
            'html': html_content or msg_data.get('body', {}).get('html') or '',
            'has_attachments': msg_data.get('hasAttachments', False),
            'attachments': msg_data.get('attachments', []),
            'is_read': msg_data.get('isRead', False),
        }
        
        logger.info(f"Syncing Message {smtp_id}: hasAttachments={data_to_save['has_attachments']}, "
                   f"attachment_count={len(data_to_save['attachments'])}")

        if existing_msg:
            # Atualizar mensagem existente
            for key, value in data_to_save.items():
                setattr(existing_msg, key, value)
            await sync_to_async(existing_msg.save)()
        else:
            # Criar nova mensagem
            data_to_save['smtp_id'] = smtp_id
            data_to_save['account'] = account
            data_to_save['received_at'] = (
                datetime.fromisoformat(msg_data['createdAt'].replace('Z', '+00:00')) 
                if msg_data.get('createdAt') else now
            )
            await Message.objects.acreate(**data_to_save)

class InlineAttachmentAPI(View):
    """
    API para servir anexos inline (imagens, vídeos, áudio, PDFs)
    com cache e streaming otimizado
    """
    
    # Configurações de cache
    CACHE_DURATION = 3600  # 1 hora
    
    async def get(self, request, message_id, attachment_id):
        """
        Serve o conteúdo de um anexo inline.
        
        Suporta:
        - Imagens (PNG, JPG, GIF, WebP, SVG)
        - Vídeos (MP4, WebM, OGG)
        - Áudio (MP3, WAV, OGG)
        - PDFs
        - Qualquer outro tipo (download genérico)
        """
        try:
            # Validar sessão
            session_email = await sync_to_async(request.session.get)('email_address')
            if not session_email:
                return HttpResponseForbidden(_("Sessão não encontrada"))
            
            # Buscar e validar acesso à mensagem
            account = await EmailAccount.objects.aget(address=session_email)
            message = await Message.objects.select_related('account').aget(
                id=message_id, 
                account=account
            )
            
            # Buscar anexo nos metadados da mensagem
            attachment = self._find_attachment(message.attachments, attachment_id)
            if not attachment:
                return HttpResponseNotFound(_("Anexo não encontrado"))
            
            # Buscar conteúdo via API SMTPLabs
            client = SMTPLabsClient()
            inbox_data = await client.get_inbox_mailbox(account.smtp_id)
            
            if not inbox_data:
                return HttpResponseServerError(_("Mailbox não encontrada"))
            
            mailbox_id = inbox_data.get('id')
            
            # Download do conteúdo do anexo
            content = await client.get_attachment_content(
                account.smtp_id,
                mailbox_id,
                message.smtp_id,
                attachment_id
            )
            
            if not content:
                return HttpResponseNotFound(_("Conteúdo não disponível"))
            
            # Determinar Content-Type correto
            content_type = attachment.get('contentType', 'application/octet-stream')
            filename = attachment.get('filename', 'attachment')
            
            # Criar resposta HTTP
            response = HttpResponse(content, content_type=content_type)
            
            # Headers de otimização
            response['Cache-Control'] = f'private, max-age={self.CACHE_DURATION}'
            response['X-Content-Type-Options'] = 'nosniff'
            response['Content-Disposition'] = f'inline; filename="{filename}"'
            response['Content-Length'] = len(content)
            
            # Headers adicionais para tipos específicos
            if content_type.startswith('video/') or content_type.startswith('audio/'):
                response['Accept-Ranges'] = 'bytes'
            
            logger.info(f"Servindo anexo inline: {filename} ({len(content)} bytes, {content_type})")
            
            return response
            
        except (EmailAccount.DoesNotExist, Message.DoesNotExist):
            return HttpResponseNotFound(_("Mensagem não encontrada"))
        except SMTPLabsAPIError as e:
            logger.error(f"Erro na API SMTPLabs: {e}")
            return HttpResponseServerError(_("Erro ao buscar anexo"))
        except Exception as e:
            logger.exception(f"Erro ao servir anexo inline: {e}")
            return HttpResponseServerError(_("Erro interno do servidor"))
    
    def _find_attachment(self, attachments, attachment_id):
        """
        Encontra um anexo específico na lista de anexos.
        
        Args:
            attachments: Lista de anexos da mensagem
            attachment_id: ID do anexo procurado
            
        Returns:
            dict: Dados do anexo ou None se não encontrado
        """
        if not attachments:
            return None
        
        for att in attachments:
            if att.get('id') == attachment_id:
                return att
        
        return None

class MessageDetailAPI(View):
    """
    API para detalhes de uma mensagem específica
    VERSÃO PREMIUM - Com skeleton loaders e UX profissional
    """
    
    # Configurações
    DATA_URL_MAX_SIZE = 500 * 1024  # 500KB
    IMAGE_TYPES = {'image/jpeg', 'image/jpg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml'}
    VIDEO_TYPES = {'video/mp4', 'video/webm', 'video/ogg'}
    AUDIO_TYPES = {'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/ogg', 'audio/webm'}
    
    async def _separate_inline_and_regular_attachments(self, attachments):
        """
        Separa anexos inline (imagens com contentId) de anexos regulares.
        
        ESTRATÉGIA: Anexos inline são processados no HTML com a estratégia HÍBRIDA:
        - Imagens pequenas (< 500KB): convertidas para Data URL (base64 inline)
        - Imagens grandes (> 500KB): lazy load via endpoint /api/messages/<id>/inline/<att_id>/
        - Vídeos/Áudio: servidos via endpoint com player HTML5 nativo
        - PDFs: servidos com <object> tag para visualização
        - Outros: placeholder elegante com ícone e informações
        
        Anexos regulares (sem contentId) são retornados para renderização na seção "Anexos"
        como links de download, sem aparecer no corpo do email.
        
        Args:
            attachments: Lista de todos os anexos
            
        Returns:
            tuple: (attachments_regulares, attachments_inline)
        """
        regular = []
        inline = []
        
        for att in attachments:
            # Anexos inline têm contentId e geralmente disposition='inline'
            content_id = att.get('contentId', '').strip('<>')
            disposition = att.get('disposition', '').lower()
            
            if content_id or disposition == 'inline':
                inline.append(att)
            else:
                regular.append(att)
        
        logger.info(f"Anexos separados: {len(regular)} regulares, {len(inline)} inline")
        return regular, inline    

    async def get(self, request, message_id):
        """Retorna detalhes completos de uma mensagem"""
        try:
            session_email = await sync_to_async(request.session.get)('email_address')
            if not session_email:
                return JsonResponse({
                    'success': False, 
                    'error': str(_('Sessão não encontrada'))
                }, status=400)
            
            account = await EmailAccount.objects.aget(address=session_email)
            message = await Message.objects.aget(id=message_id, account=account)
            
            await sync_to_async(message.mark_as_read)()
            
            if message.has_attachments and not message.attachments:
                await self._sync_attachments(account, message)
            
            html_content = message.html
            all_attachments = message.attachments or []
            
            # Separar anexos inline de regulares para processamento
            # Mas TODOS serão retornados para o usuário baixar
            regular_attachments, inline_attachments = await self._separate_inline_and_regular_attachments(
                all_attachments
            )
            
            # Processar imagens/vídeos/áudio inline para exibição no corpo
            if html_content and inline_attachments:
                html_content = await self._process_inline_attachments_hybrid(
                    html_content,
                    inline_attachments,
                    account,
                    message
                )
            
            # Retornar TODOS os anexos na seção "Anexos" (inline + regular)
            # Usuário pode baixar qualquer um
            all_attachments_for_ui = all_attachments
            
            return JsonResponse({
                'success': True,
                'message': {
                    'id': message.id,
                    'smtp_id': message.smtp_id,
                    'from_address': message.from_address,
                    'from_name': message.from_name,
                    'to_addresses': message.to_addresses,
                    'cc_addresses': message.cc_addresses,
                    'bcc_addresses': message.bcc_addresses,
                    'subject': message.subject,
                    'text': message.text,
                    'html': html_content,  # HTML com imagens/vídeos/áudio renderizados
                    'has_attachments': len(all_attachments_for_ui) > 0,
                    'attachments': all_attachments_for_ui,  # TODOS os arquivos para download
                    'is_read': message.is_read,
                    'is_flagged': message.is_flagged,
                    'received_at': message.received_at.isoformat(),
                }
            })
            
        except (EmailAccount.DoesNotExist, Message.DoesNotExist):
            return JsonResponse({
                'success': False, 
                'error': str(_('Não encontrado'))
            }, status=404)
        except Exception as e:
            logger.exception("Erro ao buscar detalhes da mensagem")
            return JsonResponse({
                'success': False, 
                'error': str(_('Erro interno'))
            }, status=500)
    
    async def _process_inline_attachments_hybrid(self, html_content, attachments, account, message):
        """
        Processa anexos inline com estratégia híbrida + skeleton loaders
        
        Estratégia:
        - Imagens pequenas (< 500KB): Data URL (base64 inline)
        - Imagens grandes (> 500KB): Lazy load com skeleton
        - Vídeos: HTML5 video player com thumbnail
        - Áudio: HTML5 audio player elegante
        - PDFs: Object viewer com fallback
        - Outros: Placeholder elegante por tipo
        """
        if not html_content or not attachments:
            return html_content
        
        # Padrão melhorado para encontrar AMBOS cid: e attachment:
        # Suporta: 
        # - src="cid:xxx", src='cid:xxx', src=cid:xxx (sem aspas)
        # - src="attachment:xxx", src='attachment:xxx', src=attachment:xxx
        # - Com espaços: src = "..."
        cid_pattern = r'src\s*=\s*["\']?(cid|attachment):([^\s"\'<>]+)["\']?'
        cid_matches = re.findall(cid_pattern, html_content, re.IGNORECASE)
        
        if not cid_matches:
            logger.debug(f"Nenhuma imagem inline encontrada no HTML")
            return html_content
        
        # Remover duplicatas
        cid_matches = list(set(cid_matches))
        
        logger.info(f"✓ Processando {len(cid_matches)} anexos inline únicos")
        
        # Criar dois mapas: um para CID, outro para ID de attachment
        cid_to_attachment = {}
        id_to_attachment = {}
        
        for att in attachments:
            # Mapear por CID (padrão Gmail original)
            content_id = att.get('cid', '').strip('<>')
            if content_id:
                content_id = content_id.strip()
                cid_to_attachment[content_id] = att
                logger.debug(f"  ✓ Mapeado CID '{content_id}' → {att.get('filename')}")
            
            # Mapear por ID de attachment (padrão SMTP Labs)
            att_id = att.get('id', '')
            if att_id:
                id_to_attachment[att_id] = att
                logger.debug(f"  ✓ Mapeado ID '{att_id}' → {att.get('filename')}")
        
        client = SMTPLabsClient()
        inbox_data = await client.get_inbox_mailbox(account.smtp_id)
        
        if not inbox_data:
            logger.warning(f"Mailbox não encontrada para {account.address}")
            return html_content
        
        mailbox_id = inbox_data.get('id')
        
        # Processar cada match encontrado (pode ser cid: ou attachment:)
        for match_type, match_id in cid_matches:
            # Determinar qual mapa usar baseado no tipo (cid ou attachment)
            if match_type.lower() == 'cid':
                att = cid_to_attachment.get(match_id)
                src_pattern = f'cid:{match_id}'
            else:  # attachment
                att = id_to_attachment.get(match_id)
                src_pattern = f'attachment:{match_id}'
            
            if not att:
                logger.warning(f"  ⚠️  {match_type.upper()} '{match_id}' não encontrado nos anexos")
                continue
            
            content_type = att.get('contentType', 'application/octet-stream')
            size = att.get('size', 0)
            
            strategy = self._determine_loading_strategy(content_type, size)
            
            logger.debug(f"  → {att.get('filename')} ({content_type}, {size} bytes) → {strategy}")
            
            try:
                if strategy == 'data_url':
                    html_content = await self._replace_with_data_url_new(
                        html_content, src_pattern, att, account, mailbox_id, message, client
                    )
                elif strategy == 'lazy_image':
                    html_content = self._replace_with_lazy_image_skeleton_new(html_content, src_pattern, att, message)
                elif strategy == 'video':
                    html_content = self._replace_with_video_player_skeleton_new(html_content, src_pattern, att, message)
                elif strategy == 'audio':
                    html_content = self._replace_with_audio_player_new(html_content, src_pattern, att, message)
                elif strategy == 'pdf':
                    html_content = self._replace_with_pdf_viewer_new(html_content, src_pattern, att, message)
                else:
                    html_content = self._replace_with_elegant_placeholder(html_content, src_pattern, att)
                    
            except Exception as e:
                logger.error(f"  ❌ Erro ao processar {att.get('filename')}: {str(e)}")
                html_content = self._replace_with_error_placeholder(html_content, src_pattern, att)
        
        logger.info(f"✓ Processamento de anexos inline finalizado")
        return html_content
    
    def _determine_loading_strategy(self, content_type, size):
        """Determina estratégia de carregamento"""
        if content_type in self.IMAGE_TYPES:
            return 'data_url' if size <= self.DATA_URL_MAX_SIZE else 'lazy_image'
        if content_type in self.VIDEO_TYPES:
            return 'video'
        if content_type in self.AUDIO_TYPES:
            return 'audio'
        if content_type == 'application/pdf':
            return 'pdf'
        return 'placeholder'
    
    async def _replace_with_data_url_new(self, html, src_pattern, att, account, mailbox_id, message, client):
        """Data URL inline (imagens pequenas) - mais rápido e sem requisições extras"""
        att_id = att.get('id')
        content_type = att.get('contentType', 'image/png')
        filename = att.get('filename', 'image')
        
        try:
            content = await client.get_attachment_content(
                account.smtp_id, mailbox_id, message.smtp_id, att_id
            )
            
            if content:
                base64_data = base64.b64encode(content).decode('utf-8')
                data_url = f"data:{content_type};base64,{base64_data}"
                
                # Padrão melhorado para substituição que suporta cid: e attachment:
                pattern = rf'src\s*=\s*["\']?{re.escape(src_pattern)}["\']?'
                html = re.sub(
                    pattern,
                    f'src="{data_url}" style="max-width: 100%; height: auto; display: block; border-radius: 8px;"',
                    html,
                    flags=re.IGNORECASE
                )
                
                logger.info(f"✅ Data URL: {filename}")
            
        except Exception as e:
            logger.error(f"Erro ao gerar data URL para {filename}: {e}")
        
        return html
    
    def _replace_image_src_pattern(self, html, src_pattern, replacement_html):
        """
        Substitui qualquer padrão src (cid: ou attachment:) por conteúdo de replacement
        Funciona com qualquer padrão que tenha src="cid:xxx" ou src="attachment:xxx"
        """
        # Padrão que encontra tags img completas com qualquer src
        pattern = rf'<img[^>]*?src\s*=\s*["\']?{re.escape(src_pattern)}["\']?[^>]*?>'
        html = re.sub(
            pattern,
            replacement_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        return html
    
    def _replace_lazy_image_src_pattern(self, html, src_pattern, replacement_html):
        """Substitui padrão src para lazy image com skeleton"""
        return self._replace_image_src_pattern(html, src_pattern, replacement_html)
    
    def _replace_with_lazy_image_skeleton_new(self, html, src_pattern, att, message):
        """
        Lazy load com skeleton loader - sem scripts inline (carregamento será feito no parent)
        """
        att_id = att.get('id')
        filename = att.get('filename', 'imagem')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        # Container com skeleton loader animado (estilos inline para evitar problemas com DOMPurify)
        image_html = f'''
        <div class="inline-image-container" data-image-url="{url}" data-filename="{filename}" style="position: relative; margin: 16px 0; border-radius: 12px; overflow: hidden; min-height: 300px; background: linear-gradient(110deg, #e0e0e0 8%, #f5f5f5 18%, #e0e0e0 33%); background-size: 200% 100%; animation: shimmer-effect 1.5s ease-in-out infinite;">
            <style>
            @keyframes shimmer-effect {{
                0% {{ background-position: -200% 0; }}
                100% {{ background-position: 200% 0; }}
            }}
            @keyframes spinner-rotate {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            </style>
            <div style="padding-bottom: 56.25%; position: relative;">
                <div class="loading-placeholder" style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; color: #6b7280; z-index: 10;">
                    <div style="width: 48px; height: 48px; border: 4px solid #e5e7eb; border-top-color: #f97316; border-radius: 50%; animation: spinner-rotate 0.8s linear infinite; margin: 0 auto 12px;"></div>
                    <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px; color: #374151;">Carregando imagem...</p>
                    <p class="progress-info" style="font-size: 11px; margin: 0; opacity: 0.8; color: #6b7280;">{size_mb:.1f} MB</p>
                </div>
            </div>
        </div>
        '''
        
        html = self._replace_lazy_image_src_pattern(html, src_pattern, image_html)
        
        logger.info(f"🔄 Lazy image com skeleton: {filename} ({size_mb:.1f}MB)")
        return html
    
    def _replace_with_lazy_image_skeleton(self, html, cid, att, message):
        """
        Lazy load com skeleton loader - sem scripts inline (carregamento será feito no parent)
        """
        att_id = att.get('id')
        filename = att.get('filename', 'imagem')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        # Container com skeleton loader animado (estilos inline para evitar problemas com DOMPurify)
        image_html = f'''
        <div class="inline-image-container" data-image-url="{url}" data-filename="{filename}" style="position: relative; margin: 16px 0; border-radius: 12px; overflow: hidden; min-height: 300px; background: linear-gradient(110deg, #e0e0e0 8%, #f5f5f5 18%, #e0e0e0 33%); background-size: 200% 100%; animation: shimmer-effect 1.5s ease-in-out infinite;">
            <style>
            @keyframes shimmer-effect {{
                0% {{ background-position: -200% 0; }}
                100% {{ background-position: 200% 0; }}
            }}
            @keyframes spinner-rotate {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            </style>
            <div style="padding-bottom: 56.25%; position: relative;">
                <div class="loading-placeholder" style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; color: #6b7280; z-index: 10;">
                    <div style="width: 48px; height: 48px; border: 4px solid #e5e7eb; border-top-color: #f97316; border-radius: 50%; animation: spinner-rotate 0.8s linear infinite; margin: 0 auto 12px;"></div>
                    <p style="font-size: 13px; font-weight: 600; margin: 0 0 6px; color: #374151;">Carregando imagem...</p>
                    <p class="progress-info" style="font-size: 11px; margin: 0; opacity: 0.8; color: #6b7280;">{size_mb:.1f} MB</p>
                </div>
            </div>
        </div>
        '''
        
        html = re.sub(
            rf'<img[^>]*?src\s*=\s*["\']?cid:{re.escape(cid)}["\']?[^>]*?>',
            image_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        
        logger.info(f"🔄 Lazy image com skeleton: {filename} ({size_mb:.1f}MB)")
        return html
    
    def _replace_with_video_player_skeleton_new(self, html, src_pattern, att, message):
        """Video player com thumbnail skeleton (nova versão com src_pattern)"""
        att_id = att.get('id')
        content_type = att.get('contentType', 'video/mp4')
        filename = att.get('filename', 'vídeo')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        filename_safe = html_escape(filename)  # Sanitizar para prevenir XSS
        
        video_html = f'''
        <div class="video-container" style="position: relative; margin: 16px 0; border-radius: 12px; overflow: hidden; background: #1f2937;">
            <video 
                controls 
                preload="metadata"
                poster="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 450'%3E%3Crect fill='%231f2937' width='800' height='450'/%3E%3Cg fill='%23374151'%3E%3Ccircle cx='400' cy='225' r='60'/%3E%3Cpath d='M380 190 L380 260 L440 225 Z' fill='%239ca3af'/%3E%3C/g%3E%3Ctext x='400' y='320' text-anchor='middle' fill='%239ca3af' font-family='sans-serif' font-size='16'%3E{filename_safe}%3C/text%3E%3Ctext x='400' y='345' text-anchor='middle' fill='%236b7280' font-family='sans-serif' font-size='14'%3E{size_mb:.1f} MB%3C/text%3E%3C/svg%3E"
                style="width: 100%; max-width: 100%; height: auto; display: block; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
            >
                <source src="{url}" type="{content_type}">
                <div style="padding: 40px; text-align: center; background: #fee2e2; border-radius: 12px;">
                    <p style="margin: 0; color: #991b1b; font-weight: 600;">❌ Seu navegador não suporta reprodução de vídeo</p>
                    <p style="margin: 8px 0 0; color: #7f1d1d; font-size: 12px;">Tente baixar o arquivo na seção de anexos</p>
                </div>
            </video>
            <div style="position: absolute; bottom: 8px; right: 8px; background: rgba(0,0,0,0.7); color: white; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;">
                🎬 {size_mb:.1f} MB
            </div>
        </div>
        '''
        
        html = self._replace_image_src_pattern(html, src_pattern, video_html)
        logger.info(f"🎬 Video player: {filename}")
        return html
    
    def _replace_with_audio_player_new(self, html, src_pattern, att, message):
        """Audio player elegante (nova versão com src_pattern)"""
        att_id = att.get('id')
        content_type = att.get('contentType', 'audio/mpeg')
        filename = att.get('filename', 'áudio')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        filename_safe = html_escape(filename)  # Sanitizar para prevenir XSS
        
        audio_html = f'''
        <div class="audio-container" style="margin: 16px 0; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
                <div style="width: 48px; height: 48px; background: rgba(255,255,255,0.2); border-radius: 50%; display: flex; align-items: center; justify-content: center;">
                    <svg style="width: 24px; height: 24px; color: white;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"></path>
                    </svg>
                </div>
                <div style="flex: 1; color: white;">
                    <p style="margin: 0; font-weight: 600; font-size: 14px;">{filename_safe}</p>
                    <p style="margin: 4px 0 0; font-size: 12px; opacity: 0.8;">🎵 {size_mb:.1f} MB</p>
                </div>
            </div>
            <audio controls preload="metadata" style="width: 100%; border-radius: 8px;">
                <source src="{url}" type="{content_type}">
                Seu navegador não suporta reprodução de áudio.
            </audio>
        </div>
        '''
        
        html = self._replace_image_src_pattern(html, src_pattern, audio_html)
        logger.info(f"🎵 Audio player: {filename}")
        return html
    
    def _replace_with_pdf_viewer_new(self, html, src_pattern, att, message):
        """PDF viewer com fallback elegante (nova versão com src_pattern)"""
        att_id = att.get('id')
        filename = att.get('filename', 'documento.pdf')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        pdf_html = f'''
        <div class="pdf-container" style="margin: 16px 0; border: 2px solid #e5e7eb; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="padding: 16px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; display: flex; align-items: center; justify-content: space-between;">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <svg style="width: 32px; height: 32px; color: #ef4444;" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clip-rule="evenodd"></path>
                    </svg>
                    <div>
                        <p style="margin: 0; font-weight: 600; font-size: 14px; color: #111827;">{filename}</p>
                        <p style="margin: 2px 0 0; font-size: 12px; color: #6b7280;">📄 PDF • {size_mb:.1f} MB</p>
                    </div>
                </div>
                <a href="{url}" target="_blank" style="padding: 8px 16px; background: #f97316; color: white; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; transition: background 0.2s;">
                    Abrir ↗
                </a>
            </div>
            <object data="{url}" type="application/pdf" style="width: 100%; height: 600px;">
                <div style="padding: 60px 40px; text-align: center; background: #fef3c7;">
                    <svg style="width: 64px; height: 64px; margin: 0 auto 16px; color: #f59e0b;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path>
                    </svg>
                    <p style="margin: 0 0 8px; font-weight: 600; color: #92400e; font-size: 16px;">Não foi possível visualizar o PDF inline</p>
                    <p style="margin: 0 0 16px; color: #78350f; font-size: 14px;">Clique no botão abaixo para abrir em uma nova aba</p>
                    <a href="{url}" target="_blank" style="display: inline-block; padding: 12px 24px; background: #f97316; color: white; border-radius: 8px; text-decoration: none; font-weight: 600;">
                        📄 Abrir PDF
                    </a>
                </div>
            </object>
        </div>
        '''
        
        html = self._replace_image_src_pattern(html, src_pattern, pdf_html)
        logger.info(f"📄 PDF viewer: {filename}")
        return html
    
    def _replace_with_video_player_skeleton(self, html, cid, att, message):
        """
        Video player com thumbnail skeleton
        MELHORADO: Mostra placeholder até carregar
        """
        att_id = att.get('id')
        content_type = att.get('contentType', 'video/mp4')
        filename = att.get('filename', 'vídeo')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        filename_safe = html_escape(filename)  # Sanitizar para prevenir XSS
        
        video_html = f'''
        <div class="video-container" style="position: relative; margin: 16px 0; border-radius: 12px; overflow: hidden; background: #1f2937;">
            <video 
                controls 
                preload="metadata"
                poster="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 800 450'%3E%3Crect fill='%231f2937' width='800' height='450'/%3E%3Cg fill='%23374151'%3E%3Ccircle cx='400' cy='225' r='60'/%3E%3Cpath d='M380 190 L380 260 L440 225 Z' fill='%239ca3af'/%3E%3C/g%3E%3Ctext x='400' y='320' text-anchor='middle' fill='%239ca3af' font-family='sans-serif' font-size='16'%3E{filename_safe}%3C/text%3E%3Ctext x='400' y='345' text-anchor='middle' fill='%236b7280' font-family='sans-serif' font-size='14'%3E{size_mb:.1f} MB%3C/text%3E%3C/svg%3E"
                style="width: 100%; max-width: 100%; height: auto; display: block; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);"
            >
                <source src="{url}" type="{content_type}">
                <div style="padding: 40px; text-align: center; background: #fee2e2; border-radius: 12px;">
                    <p style="margin: 0; color: #991b1b; font-weight: 600;">❌ Seu navegador não suporta reprodução de vídeo</p>
                    <p style="margin: 8px 0 0; color: #7f1d1d; font-size: 12px;">Tente baixar o arquivo na seção de anexos</p>
                </div>
            </video>
            <div style="position: absolute; bottom: 8px; right: 8px; background: rgba(0,0,0,0.7); color: white; padding: 4px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;">
                🎬 {size_mb:.1f} MB
            </div>
        </div>
        '''
        
        html = re.sub(
            rf'<img[^>]*?src\s*=\s*["\']?cid:{re.escape(cid)}["\']?[^>]*?>',
            video_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        
        logger.info(f"🎬 Video player: {filename}")
        return html
    
    def _replace_with_audio_player(self, html, cid, att, message):
        """Audio player elegante"""
        att_id = att.get('id')
        content_type = att.get('contentType', 'audio/mpeg')
        filename = att.get('filename', 'áudio')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        filename_safe = html_escape(filename)  # Sanitizar para prevenir XSS
        
        audio_html = f'''
        <div class="audio-container" style="margin: 16px 0; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px;">
                <div style="width: 48px; height: 48px; background: rgba(255,255,255,0.2); border-radius: 50%; display: flex; align-items: center; justify-center;">
                    <svg style="width: 24px; height: 24px; color: white;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"></path>
                    </svg>
                </div>
                <div style="flex: 1; color: white;">
                    <p style="margin: 0; font-weight: 600; font-size: 14px;">{filename_safe}</p>
                    <p style="margin: 4px 0 0; font-size: 12px; opacity: 0.8;">🎵 {size_mb:.1f} MB</p>
                </div>
            </div>
            <audio controls preload="metadata" style="width: 100%; border-radius: 8px;">
                <source src="{url}" type="{content_type}">
                Seu navegador não suporta reprodução de áudio.
            </audio>
        </div>
        '''
        
        html = re.sub(
            rf'<img[^>]*?src\s*=\s*["\']?cid:{re.escape(cid)}["\']?[^>]*?>',
            audio_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        
        logger.info(f"🎵 Audio player: {filename}")
        return html
    
    def _replace_with_pdf_viewer(self, html, cid, att, message):
        """PDF viewer com fallback elegante"""
        att_id = att.get('id')
        filename = att.get('filename', 'documento.pdf')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024)
        
        url = reverse('api-inline-attachment', kwargs={
            'message_id': message.id,
            'attachment_id': att_id
        })
        
        pdf_html = f'''
        <div class="pdf-container" style="margin: 16px 0; border: 2px solid #e5e7eb; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="padding: 16px; background: #f9fafb; border-bottom: 1px solid #e5e7eb; display: flex; align-items: center; justify-content: space-between;">
                <div style="display: flex; align-items: center; gap: 12px;">
                    <svg style="width: 32px; height: 32px; color: #ef4444;" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M4 4a2 2 0 012-2h4.586A2 2 0 0112 2.586L15.414 6A2 2 0 0116 7.414V16a2 2 0 01-2 2H6a2 2 0 01-2-2V4zm2 6a1 1 0 011-1h6a1 1 0 110 2H7a1 1 0 01-1-1zm1 3a1 1 0 100 2h6a1 1 0 100-2H7z" clip-rule="evenodd"></path>
                    </svg>
                    <div>
                        <p style="margin: 0; font-weight: 600; font-size: 14px; color: #111827;">{filename}</p>
                        <p style="margin: 2px 0 0; font-size: 12px; color: #6b7280;">📄 PDF • {size_mb:.1f} MB</p>
                    </div>
                </div>
                <a href="{url}" target="_blank" style="padding: 8px 16px; background: #f97316; color: white; border-radius: 6px; text-decoration: none; font-size: 13px; font-weight: 600; transition: background 0.2s;">
                    Abrir ↗
                </a>
            </div>
            <object data="{url}" type="application/pdf" style="width: 100%; height: 600px;">
                <div style="padding: 60px 40px; text-align: center; background: #fef3c7;">
                    <svg style="width: 64px; height: 64px; margin: 0 auto 16px; color: #f59e0b;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"></path>
                    </svg>
                    <p style="margin: 0 0 8px; font-weight: 600; color: #92400e; font-size: 16px;">Não foi possível visualizar o PDF inline</p>
                    <p style="margin: 0 0 16px; color: #78350f; font-size: 14px;">Clique no botão abaixo para abrir em uma nova aba</p>
                    <a href="{url}" target="_blank" style="display: inline-block; padding: 12px 24px; background: #f97316; color: white; border-radius: 8px; text-decoration: none; font-weight: 600;">
                        📄 Abrir PDF
                    </a>
                </div>
            </object>
        </div>
        '''
        
        html = re.sub(
            rf'<img[^>]*?src\s*=\s*["\']?cid:{re.escape(cid)}["\']?[^>]*?>',
            pdf_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        
        logger.info(f"📄 PDF viewer: {filename}")
        return html
    
    def _replace_with_elegant_placeholder(self, html, cid, att):
        """
        Placeholder elegante para tipos não suportados inline
        MELHORADO: Design profissional com ícones por tipo
        """
        filename = att.get('filename', 'arquivo')
        content_type = att.get('contentType', 'desconhecido')
        size = att.get('size', 0)
        size_mb = size / (1024 * 1024) if size > 0 else 0
        
        # Determinar ícone e cor baseado no tipo
        icon_data = self._get_file_icon_data(content_type, filename)
        
        placeholder_html = f'''
        <div class="file-placeholder" style="margin: 16px 0; padding: 24px; background: linear-gradient(135deg, {icon_data['gradient_from']} 0%, {icon_data['gradient_to']} 100%); border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
            <div style="display: flex; align-items: center; gap: 16px;">
                <div style="flex-shrink: 0; width: 64px; height: 64px; background: rgba(255,255,255,0.2); border-radius: 12px; display: flex; align-items: center; justify-center; font-size: 32px;">
                    {icon_data['emoji']}
                </div>
                <div style="flex: 1; color: white;">
                    <p style="margin: 0; font-weight: 700; font-size: 16px; text-shadow: 0 1px 2px rgba(0,0,0,0.2);">{filename}</p>
                    <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">
                        {icon_data['label']} • {size_mb:.1f} MB
                    </p>
                    <p style="margin: 8px 0 0; font-size: 12px; opacity: 0.8; line-height: 1.4;">
                        💡 Este tipo de arquivo não pode ser exibido inline.<br>
                        Você pode baixá-lo na seção "Anexos" abaixo.
                    </p>
                </div>
            </div>
        </div>
        '''
        
        html = re.sub(
            rf'<img[^>]*?src\s*=\s*["\']?cid:{re.escape(cid)}["\']?[^>]*?>',
            placeholder_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        
        logger.info(f"📎 Elegant placeholder: {filename} ({content_type})")
        return html
    
    def _replace_with_error_placeholder(self, html, cid, att):
        """Placeholder de erro elegante"""
        filename = att.get('filename', 'arquivo')
        
        error_html = f'''
        <div class="error-placeholder" style="margin: 16px 0; padding: 24px; background: #fee2e2; border: 2px dashed #fca5a5; border-radius: 12px;">
            <div style="display: flex; align-items: center; gap: 16px;">
                <div style="flex-shrink: 0; width: 64px; height: 64px; background: #fecaca; border-radius: 50%; display: flex; align-items: center; justify-center;">
                    <svg style="width: 32px; height: 32px; color: #dc2626;" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                    </svg>
                </div>
                <div style="flex: 1;">
                    <p style="margin: 0; font-weight: 700; color: #991b1b; font-size: 16px;">Erro ao carregar anexo</p>
                    <p style="margin: 4px 0 0; color: #7f1d1d; font-size: 14px;">{filename}</p>
                    <p style="margin: 8px 0 0; color: #991b1b; font-size: 12px;">
                        ⚠️ Não foi possível processar este arquivo. Tente baixá-lo na seção de anexos.
                    </p>
                </div>
            </div>
        </div>
        '''
        
        html = re.sub(
            rf'<img[^>]*?src\s*=\s*["\']?cid:{re.escape(cid)}["\']?[^>]*?>',
            error_html,
            html,
            flags=re.IGNORECASE | re.DOTALL
        )
        
        logger.error(f"❌ Error placeholder: {filename}")
        return html
    
    def _get_file_icon_data(self, content_type, filename):
        """
        Retorna dados de ícone, cor e label baseado no tipo de arquivo
        """
        # Por extensão do arquivo
        ext = filename.split('.')[-1].lower() if '.' in filename else ''
        
        # Documentos Office
        if ext in ['doc', 'docx'] or 'word' in content_type:
            return {
                'emoji': '📝',
                'label': 'Documento Word',
                'gradient_from': '#2b5797',
                'gradient_to': '#1e3a5f'
            }
        
        if ext in ['xls', 'xlsx'] or 'excel' in content_type or 'spreadsheet' in content_type:
            return {
                'emoji': '📊',
                'label': 'Planilha Excel',
                'gradient_from': '#217346',
                'gradient_to': '#185c37'
            }
        
        if ext in ['ppt', 'pptx'] or 'powerpoint' in content_type or 'presentation' in content_type:
            return {
                'emoji': '📽️',
                'label': 'Apresentação PowerPoint',
                'gradient_from': '#d24726',
                'gradient_to': '#a93820'
            }
        
        # Arquivos compactados
        if ext in ['zip', 'rar', '7z', 'tar', 'gz'] or 'compressed' in content_type or 'zip' in content_type:
            return {
                'emoji': '🗜️',
                'label': 'Arquivo Compactado',
                'gradient_from': '#8b5cf6',
                'gradient_to': '#6d28d9'
            }
        
        # Código fonte
        if ext in ['py', 'js', 'java', 'cpp', 'c', 'html', 'css', 'php', 'rb']:
            return {
                'emoji': '💻',
                'label': 'Código Fonte',
                'gradient_from': '#059669',
                'gradient_to': '#047857'
            }
        
        # Texto
        if ext in ['txt', 'md', 'log'] or 'text' in content_type:
            return {
                'emoji': '📄',
                'label': 'Arquivo de Texto',
                'gradient_from': '#6b7280',
                'gradient_to': '#4b5563'
            }
        
        # Outros
        return {
            'emoji': '📎',
            'label': 'Arquivo Anexo',
            'gradient_from': '#f59e0b',
            'gradient_to': '#d97706'
        }
    
    async def _sync_attachments(self, account, message):
        """Sincroniza anexos"""
        logger.info(f"Mini-sync de anexos para mensagem {message.id}")
        
        try:
            client = SMTPLabsClient()
            inbox_data = await client.get_inbox_mailbox(account.smtp_id)
            
            if inbox_data:
                mailbox_id = inbox_data.get('id')
                if mailbox_id:
                    full_msg = await client.get_message(
                        account.smtp_id, 
                        mailbox_id, 
                        message.smtp_id
                    )
                    
                    attachments = full_msg.get('attachments', [])
                    if attachments:
                        message.attachments = attachments
                        await sync_to_async(message.save)()
                        logger.info(f"Anexos sincronizados: {len(attachments)} itens")
        except Exception as e:
            logger.warning(f"Erro no mini-sync: {e}")

class MessageDownloadAPI(View):
    """API para download do código fonte da mensagem (.eml)"""
    
    async def get(self, request, message_id):
        """Faz download do arquivo .eml da mensagem"""
        # Recuperar email da sessão
        email_address = await sync_to_async(request.session.get)('email_address')
        
        if not email_address:
            return HttpResponseForbidden(str(_("Sessão não encontrada")))

        try:
            # Buscar conta e validar
            account = await EmailAccount.objects.aget(address=email_address)
            
            # Buscar mensagem no banco
            message = await Message.objects.select_related('account').aget(
                id=message_id, 
                account=account
            )
            
            # Buscar mailbox ID
            client = SMTPLabsClient()
            inbox = await client.get_inbox_mailbox(account.smtp_id)
            
            if not inbox:
                return HttpResponseServerError(str(_("Mailbox não encontrada")))
                
            mailbox_id = inbox.get('id')
            
            # Buscar fonte usando o SMTP ID da mensagem
            source_content = await client.get_message_source(
                account.smtp_id, 
                mailbox_id, 
                message.smtp_id
            )
            
            logger.info(f"Download Message ID {message_id}: "
                       f"source_content length={len(source_content) if source_content else 0}")
            
            if not source_content:
                return HttpResponseServerError(str(_("Conteúdo vazio")))
            
            # Retornar como arquivo
            response = HttpResponse(source_content, content_type='message/rfc822')
            response['Content-Disposition'] = f'attachment; filename="message_{message.id}.eml"'
            return response
            
        except (EmailAccount.DoesNotExist, Message.DoesNotExist):
            return HttpResponseNotFound(str(_("Mensagem não encontrada")))
        except Exception as e:
            logger.error(f"Erro no download da mensagem: {e}", exc_info=True)
            return HttpResponseServerError(
                str(_("Não foi possível processar o download da mensagem."))
            )

class AttachmentDownloadAPI(View):
    """API para download de um anexo individual"""
    
    async def get(self, request, message_id, attachment_id):
        """Faz download de um anexo específico"""
        # Recuperar email da sessão
        email_address = await sync_to_async(request.session.get)('email_address')
        
        if not email_address:
            return HttpResponseForbidden(str(_("Sessão não encontrada")))

        try:
            # Buscar conta e validar
            account = await EmailAccount.objects.aget(address=email_address)
            
            # Buscar mensagem no banco
            message = await Message.objects.select_related('account').aget(
                id=message_id, 
                account=account
            )
            
            # Encontrar metadados do anexo
            attachments = message.attachments or []
            att_metadata = next(
                (a for a in attachments if str(a.get('id')) == str(attachment_id)), 
                None
            )
            
            if not att_metadata:
                return HttpResponseNotFound(
                    str(_("Anexo não encontrado nos metadados da mensagem"))
                )
            
            # Buscar mailbox ID
            client = SMTPLabsClient()
            inbox = await client.get_inbox_mailbox(account.smtp_id)
            
            if not inbox:
                return HttpResponseServerError(str(_("Mailbox não encontrada")))
            
            mailbox_id = inbox.get('id')
            
            # Buscar conteúdo do anexo
            content = await client.get_attachment_content(
                account.smtp_id, 
                mailbox_id, 
                message.smtp_id, 
                attachment_id
            )
            
            if not content:
                return HttpResponseServerError(
                    str(_("Conteúdo do anexo vazio ou não disponível"))
                )
            
            # Retornar como arquivo
            response = HttpResponse(
                content, 
                content_type=att_metadata.get('contentType', 'application/octet-stream')
            )
            filename = att_metadata.get('filename', f'attachment_{attachment_id}')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
            
        except (EmailAccount.DoesNotExist, Message.DoesNotExist):
            return HttpResponseNotFound(str(_("Arquivo não encontrado")))
        except Exception as e:
            logger.error(f"Erro no download do anexo: {e}", exc_info=True)
            return HttpResponseServerError(
                str(_("Não foi possível processar o download do arquivo."))
            )

class DadosView(AdminRequiredMixin, DateFilterMixin, View):
    """
    Dashboard Administrativo com estatísticas do sistema.
    
    Herda de:
    - AdminRequiredMixin: Verificação de permissão de admin
    - DateFilterMixin: Processamento de filtros de data
    
    Melhorias implementadas:
    - Validação robusta de domínios de email
    - Melhor separação de responsabilidades
    - Otimização de queries com select_related/prefetch_related
    - Cache de consultas pesadas
    - Tratamento de erros mais granular
    """
    
    # Skip da verificação automática de admin no dispatch para fazer manualmente no get
    skip_admin_check = True
    
    # Limites de segurança para consultas
    MAX_ACCOUNTS_QUERY = 10000
    MAX_MESSAGES_QUERY = 50000
    MAX_DOMAIN_LENGTH = 253
    MAX_LABEL_LENGTH = 63
    
    @staticmethod
    def extrair_dominio_seguro(email):
        """
        Extrai e valida domínio de um email de forma segura.
        
        Args:
            email: String com endereço de email
            
        Returns:
            str | None: Domínio validado ou None se inválido
            
        Examples:
            >>> DadosView.extrair_dominio_seguro("user@example.com")
            'example.com'
            >>> DadosView.extrair_dominio_seguro("invalid@")
            None
        """
        if not email or '@' not in email:
            return None
        
        try:
            # Extrair a parte após o @
            dominio = email.split('@')[-1].lower().strip()
            
            # Validações básicas de tamanho
            if not dominio or len(dominio) > DadosView.MAX_DOMAIN_LENGTH:
                return None
            
            # Remover espaços e caracteres de controle/não imprimíveis
            dominio = ''.join(c for c in dominio if c.isprintable() and not c.isspace())
            
            # Validar estrutura: precisa ter pelo menos domínio.tld
            partes = dominio.split('.')
            if len(partes) < 2:
                return None
            
            # Validar cada label (parte entre pontos)
            for parte in partes:
                # Label não pode estar vazio ou exceder 63 caracteres
                if not parte or len(parte) > DadosView.MAX_LABEL_LENGTH:
                    return None
                
                # Label não pode começar ou terminar com hífen
                if parte.startswith('-') or parte.endswith('-'):
                    return None
                
                # Label só pode conter alfanuméricos e hífen
                if not all(c.isalnum() or c == '-' for c in parte):
                    return None
            
            return dominio
            
        except Exception as e:
            logger.debug(f"Erro ao extrair domínio de '{email}': {e}")
            return None
    
    def _validate_filter_param(self, filter_param):
        """
        Valida o parâmetro filter para prevenir ataques.
        
        Args:
            filter_param: String com o filtro solicitado
            
        Returns:
            str: Filtro validado ('all', 'top10', ou 'top50')
        """
        allowed_filters = {'all', 'top10', 'top50'}

        if not filter_param:
            return 'all'

        # Sanitização básica
        filter_clean = str(filter_param).strip().lower()

        if filter_clean not in allowed_filters:
            logger.warning(f"Parâmetro filter inválido recebido: {filter_param}")
            return 'all'

        return filter_clean
    
    async def _get_statistics_counts(self, data_inicio_dt, data_fim_dt):
        """
        Coleta contagens básicas de forma otimizada.
        
        Args:
            data_inicio_dt: Data inicial com timezone
            data_fim_dt: Data final com timezone
            
        Returns:
            tuple: (total_contas, contas_ativas, total_mensagens, mensagens_com_anexos)
        """
        # ✅ Executar todas as queries em paralelo
        results = await asyncio.gather(
            # Total de contas no período
            EmailAccount.objects.filter(
                created_at__gte=data_inicio_dt,
                created_at__lte=data_fim_dt
            ).acount(),
            
            # ✅ CORRIGIDO: Usar acount() ao invés de async for
            # Contas ativas (disponíveis para reutilização)
            EmailAccount.objects.filter(
                is_available=True,
                last_used_at__gte=data_inicio_dt,
                last_used_at__lte=data_fim_dt
            ).acount(),
            
            # Total de mensagens
            Message.objects.filter(
                received_at__gte=data_inicio_dt,
                received_at__lte=data_fim_dt
            ).acount(),
            
            # Mensagens com anexos
            Message.objects.filter(
                has_attachments=True,
                received_at__gte=data_inicio_dt,
                received_at__lte=data_fim_dt
            ).acount()
        )
        
        return results
    
    async def _get_domain_statistics(self, data_inicio_dt, data_fim_dt):
        """
        Coleta estatísticas de domínios de forma otimizada.
        
        Args:
            data_inicio_dt: Data inicial com timezone
            data_fim_dt: Data final com timezone
            
        Returns:
            tuple: (total_dominios, dominios_ativos, contas_por_dominio)
        """
        # ✅ CORRIGIDO: Coletar IDs únicos de domínios usados no período
        # Converter QuerySet para lista primeiro
        contas_qs = EmailAccount.objects.filter(
            created_at__gte=data_inicio_dt,
            created_at__lte=data_fim_dt
        ).only('domain_id')[:self.MAX_ACCOUNTS_QUERY]
        
        contas_list = await sync_to_async(list)(contas_qs)
        
        dominios_ativos_ids = set()
        for conta in contas_list:
            if conta.domain_id:
                dominios_ativos_ids.add(conta.domain_id)

        total_dominios = len(dominios_ativos_ids)
        
        # Contar domínios ativos
        if dominios_ativos_ids:
            dominios_ativos = await Domain.objects.filter(
                id__in=dominios_ativos_ids,
                is_active=True
            ).acount()
        else:
            dominios_ativos = 0

        # ✅ CORRIGIDO: Distribuição de contas por domínio
        # Converter QuerySet para lista primeiro
        dominios_qs = Domain.objects.filter(is_active=True).only('id', 'domain')
        dominios_list = await sync_to_async(list)(dominios_qs)
        
        contas_por_dominio = []
        for dominio in dominios_list:
            count = await dominio.accounts.filter(
                created_at__gte=data_inicio_dt,
                created_at__lte=data_fim_dt
            ).acount()
            
            if count > 0:
                contas_por_dominio.append({
                    'dominio': dominio.domain,
                    'quantidade': count
                })
        
        # Ordenar por quantidade (decrescente)
        contas_por_dominio.sort(key=lambda x: x['quantidade'], reverse=True)
        
        return total_dominios, dominios_ativos, contas_por_dominio

    async def _process_messages_statistics(self, data_inicio_dt, data_fim_dt):
        """
        Processa estatísticas de mensagens, anexos e domínios remetentes.
        
        Args:
            data_inicio_dt: Data inicial com timezone
            data_fim_dt: Data final com timezone
            
        Returns:
            tuple: (total_anexos, tipos_anexo, dominios_remetentes)
        """
        total_anexos = 0
        tipos_anexo = Counter()
        dominios_remetentes = Counter()

        # Processar mensagens em uma única iteração otimizada
        query = Message.objects.filter(
            received_at__gte=data_inicio_dt,
            received_at__lte=data_fim_dt
        ).only('from_address', 'attachments', 'has_attachments')[:self.MAX_MESSAGES_QUERY]

        # ✅ CORRIGIDO: Converter QuerySet para lista de forma assíncrona
        messages_list = await sync_to_async(list)(query)
        
        for msg in messages_list:
            # Processar domínio do remetente com validação robusta
            if msg.from_address:
                dominio = self.extrair_dominio_seguro(msg.from_address)
                if dominio:
                    dominios_remetentes[dominio] += 1

            # Processar anexos apenas se existirem
            if msg.has_attachments and msg.attachments:
                total_anexos += len(msg.attachments)
                
                for anexo in msg.attachments:
                    content_type = anexo.get('contentType', 'unknown')
                    
                    # Extrair tipo principal (ex: 'application/pdf' -> 'pdf')
                    if '/' in content_type:
                        tipo_principal = content_type.split('/')[-1]
                    else:
                        tipo_principal = content_type
                    
                    # Limitar tamanho e sanitizar
                    tipo_principal = tipo_principal[:20].lower().strip()
                    
                    if tipo_principal:
                        tipos_anexo[tipo_principal] += 1

        return total_anexos, tipos_anexo, dominios_remetentes

    def _get_top_sites_limit(self, filter_sites, total_sites):
        """
        Determina o limite de sites a retornar baseado no filtro.
        
        Args:
            filter_sites: Tipo de filtro ('all', 'top10', 'top50')
            total_sites: Total de sites disponíveis
            
        Returns:
            int: Limite a ser aplicado
        """
        limits = {
            'top10': 10,
            'top50': 50,
            'all': 100
        }
        
        requested_limit = limits.get(filter_sites, 100)
        return min(requested_limit, total_sites)
    
    def _build_context(self, data_inicio, data_fim, filter_sites, stats):
        """
        Constrói o contexto para o template.
        
        Args:
            data_inicio: Data inicial do filtro
            data_fim: Data final do filtro
            filter_sites: Filtro de sites aplicado
            stats: Dicionário com todas as estatísticas coletadas
            
        Returns:
            dict: Contexto completo para o template
        """
        total_contas, contas_ativas, total_mensagens, mensagens_com_anexos = stats['counts']
        total_dominios, dominios_ativos, contas_por_dominio = stats['domains']
        total_anexos, tipos_anexo, dominios_remetentes = stats['messages']
        
        # Calcular contas em uso
        contas_em_uso = total_contas - contas_ativas
        
        # Aplicar filtro de sites com validação de limite
        limit = self._get_top_sites_limit(filter_sites, len(dominios_remetentes))
        
        top_100_sites = [
            {'dominio': dominio, 'quantidade': count}
            for dominio, count in dominios_remetentes.most_common(limit)
        ]
        
        return {
            # Estatísticas principais
            'total_contas': total_contas,
            'contas_ativas': contas_ativas,
            'contas_em_uso': contas_em_uso,
            'total_mensagens': total_mensagens,
            'mensagens_com_anexos': mensagens_com_anexos,
            
            # Estatísticas de domínios
            'total_dominios': total_dominios,
            'dominios_ativos': dominios_ativos,
            'contas_por_dominio': contas_por_dominio,
            
            # Estatísticas de anexos
            'total_anexos': total_anexos,
            'tipos_anexo': dict(tipos_anexo.most_common(10)),  # Top 10 tipos
            
            # Top sites
            'top_100_sites': top_100_sites,
            
            # Estatísticas temporais
            'contas_periodo': total_contas,
            'mensagens_periodo': total_mensagens,
            
            # Informações de filtro
            'data_inicio': data_inicio,
            'data_fim': data_fim,
            'dias_periodo': (data_fim - data_inicio).days + 1,
            'filter_sites': filter_sites,
        }
    
    def _get_template_name(self, request):
        """
        Determina qual template usar baseado no tipo de requisição.
        
        Args:
            request: Objeto HttpRequest
            
        Returns:
            str: Nome do template a ser usado
        """
        # Requisição HTMX parcial
        if request.headers.get('HX-Request'):
            # Requisição das abas de filtro (apenas tabela de sites)
            if request.GET.get('filter'):
                return 'core/parciais/dados/_dadosTop.html'
            # Requisição HTMX geral (todo o conteúdo interno)
            else:
                return 'core/parciais/dados/_dados_conteudo.html'
        
        # Requisição normal (página completa)
        return 'core/dados.html'
    
    @method_decorator(cache_control(no_cache=True, no_store=True, must_revalidate=True))
    async def get(self, request):
        """
        Dashboard administrativo - apenas admins podem acessar.
        
        Fluxo:
        1. Verificação de permissões
        2. Validação de parâmetros
        3. Coleta de estatísticas em paralelo
        4. Construção do contexto
        5. Renderização do template apropriado
        """
        try:
            # 1. Verificar se é admin
            user_is_superuser = await self._check_user_is_superuser(request)
            if not user_is_superuser:
                logger.warning(f"Tentativa de acesso não autorizado ao dashboard por IP: {request.META.get('REMOTE_ADDR')}")
                return HttpResponseNotFound()

            # 2. Validar e obter parâmetros
            data_inicio, data_fim = await self._get_date_filters(request)
            filter_sites = self._validate_filter_param(request.GET.get('filter'))

        except Exception as e:
            logger.error(f"Erro no processamento de parâmetros da requisição: {e}", exc_info=True)
            return HttpResponseServerError(str(_("Erro ao processar requisição")))

        try:
            # 3. Converter datas para datetime com timezone
            data_inicio_dt = timezone.make_aware(datetime.combine(data_inicio, datetime.min.time()))
            data_fim_dt = timezone.make_aware(datetime.combine(data_fim, datetime.max.time()))

            # 4. Coletar todas as estatísticas em paralelo
            counts_task = self._get_statistics_counts(data_inicio_dt, data_fim_dt)
            domains_task = self._get_domain_statistics(data_inicio_dt, data_fim_dt)
            messages_task = self._process_messages_statistics(data_inicio_dt, data_fim_dt)
            
            # Aguardar todas as tarefas em paralelo
            counts, domains, messages = await asyncio.gather(
                counts_task,
                domains_task,
                messages_task
            )
            
            # Organizar estatísticas
            stats = {
                'counts': counts,
                'domains': domains,
                'messages': messages
            }
            
            # 5. Construir contexto para o template
            context = self._build_context(data_inicio, data_fim, filter_sites, stats)
            
            # 6. Determinar e renderizar template apropriado
            template_name = self._get_template_name(request)
            response = await sync_to_async(render)(request, template_name, context)
            
            # Log de auditoria
            logger.info(
                f"Dashboard acessado - Período: {data_inicio} até {data_fim}, "
                f"Filtro: {filter_sites}, "
                f"Contas: {counts[0]}, Mensagens: {counts[2]}"
            )

            return response

        except Exception as e:
            logger.error(f"Erro ao processar dados do dashboard: {e}", exc_info=True)
            return HttpResponseServerError(str(_("Erro ao carregar estatísticas")))
            
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