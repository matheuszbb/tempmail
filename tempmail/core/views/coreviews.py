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
                        logger.warning(f"❌ Email {custom_email!r} em uso por outro navegador (fingerprints diferentes)")
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
        
        # 1. Salvar fingerprint do NAVEGADOR (persiste por 1 ano)
        response.set_cookie(
            'browser_fp',
            browser_fingerprint,
            max_age=365*24*60*60,  # 1 ano
            httponly=True,
            samesite='Lax'
        )
        
        # 2. Salvar mapeamento email -> fingerprint
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
        logger.debug(f"Fingerprints salvos no cookie para {email_address}")

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
        Sincroniza mensagens com a API se necessário (throttle de 4s).
        
        Args:
            account: Instância de EmailAccount
        """
        now = timezone.now()
        sync_threshold = timedelta(seconds=4)
        
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
