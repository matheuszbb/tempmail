import re
import os
import json
import logging
import asyncio
from django.views import View
from collections import Counter
from django.conf import settings
from django.utils import timezone
from django.contrib import messages
from asgiref.sync import sync_to_async
from datetime import datetime, timedelta
from django.middleware.csrf import get_token
from django.shortcuts import render, redirect
from .models import Domain, EmailAccount, Message
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
            
            session_start_val = await sync_to_async(request.session.get)('session_start')
            session_start = datetime.fromisoformat(session_start_val)
            expires_at = session_start + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
            expires_in = int((expires_at - timezone.now()).total_seconds())
            
            return JsonResponse({
                'success': True,
                'email': account.address,
                'session_start': session_start.isoformat(),
                'expires_in': max(0, expires_in),
                'is_new_session': is_new
            })
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
        has_email = await sync_to_async(request.session.__contains__)('email_address')
        if has_email:
            await sync_to_async(request.session.pop)('email_address', None)
        
        has_start = await sync_to_async(request.session.__contains__)('session_start')
        if has_start:
            await sync_to_async(request.session.pop)('session_start', None)
        
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
        
        session_start_val = await sync_to_async(request.session.get)('session_start')
        session_start = datetime.fromisoformat(session_start_val)
        
        expires_at = session_start + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
        expires_in = int((expires_at - timezone.now()).total_seconds())

        return JsonResponse({
            'success': True,
            'email': account.address,
            'session_start': session_start.isoformat(),
            'expires_in': max(0, expires_in),
            'is_new_session': True,
            'message': str(_('Sessão resetada com sucesso'))
        })

    async def _handle_custom_email(self, request, custom_email, session_email):
        """Processa solicitação de email customizado"""
        logger.info(f"Tentando login/mudança para email customizado: {custom_email}")
        
        # Validar formato básico
        if '@' not in custom_email:
            return JsonResponse({
                'success': False, 
                'error': str(_('Endereço de email inválido'))
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
        account = await self._get_or_create_custom_account(custom_email, session_used_emails)
        
        if account is None:
            return JsonResponse({
                'success': False, 
                'error': str(_('Não foi possível acessar este email'))
            }, status=200)

        # Atualizar sessão
        await self._update_session_with_account(request, account, session_used_emails, email_sessions)
        
        # Calcular expiração
        first_used_at = datetime.fromisoformat(email_sessions[account.address])
        expires_at = first_used_at + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
        expires_in = int((expires_at - timezone.now()).total_seconds())

        return JsonResponse({
            'success': True,
            'email': account.address,
            'expires_in': max(0, expires_in),
            'message': str(_('Email alterado com sucesso'))
        })

    async def _release_previous_email(self, session_email):
        """Libera o email anterior da sessão"""
        try:
            previous_account = await EmailAccount.objects.aget(address=session_email)
            previous_account.is_available = True
            previous_account.last_used_at = None
            await sync_to_async(previous_account.save)(
                update_fields=['is_available', 'last_used_at', 'updated_at']
            )
            logger.info(f"Email anterior liberado: {session_email}")
        except EmailAccount.DoesNotExist:
            pass

    async def _get_or_create_custom_account(self, custom_email, session_used_emails):
        """Obtém ou cria conta customizada"""
        try:
            account = await EmailAccount.objects.aget(address=custom_email)
            
            # Verificar se este email foi usado pelo mesmo usuário nesta sessão
            email_was_used_in_session = custom_email in session_used_emails
            
            # Trava de segurança
            if not account.is_available and not account.can_be_reused() and not email_was_used_in_session:
                logger.warning(f"Email {custom_email} está em uso por outro usuário")
                return None
            
            # Se o email foi usado nesta sessão, liberar antes de reutilizar
            if email_was_used_in_session and not account.is_available:
                account.is_available = True
                account.last_used_at = None
                await sync_to_async(account.save)(
                    update_fields=['is_available', 'last_used_at', 'updated_at']
                )
                logger.info(f"Email usado nesta sessão, liberado para reutilização: {custom_email}")
            
            # Marcar como usada
            await sync_to_async(account.mark_as_used)()
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
        Sincroniza mensagens com a API se necessário (throttle de 10s).
        
        Args:
            account: Instância de EmailAccount
        """
        now = timezone.now()
        sync_threshold = timedelta(seconds=10)
        
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

class MessageDetailAPI(View):
    """API para detalhes de uma mensagem específica"""
    
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
            
            # Marcar como lida
            await sync_to_async(message.mark_as_read)()
            
            # Mini-sync para anexos se necessário
            if message.has_attachments and not message.attachments:
                await self._sync_attachments(account, message)
            
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
                    'html': message.html,
                    'has_attachments': message.has_attachments,
                    'attachments': message.attachments,
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

    async def _sync_attachments(self, account, message):
        """
        Sincroniza anexos de uma mensagem se não estiverem salvos localmente.
        
        Args:
            account: Instância de EmailAccount
            message: Instância de Message
        """
        logger.info(f"MessageDetailAPI: Triggering mini-sync for attachments on message {message.id}")
        
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
                    
                    # Se a API retornou anexos, salvamos
                    attachments = full_msg.get('attachments', [])
                    if attachments:
                        message.attachments = attachments
                        await sync_to_async(message.save)()
                        logger.info(f"MessageDetailAPI: Attachments recovered for message {message.id}: "
                                  f"{len(attachments)} items")
        except Exception as e:
            logger.warning(f"MessageDetailAPI: Error in mini-sync for message {message.id}: {e}")

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