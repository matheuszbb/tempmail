from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views import View
from django.shortcuts import render
from django.http import HttpResponse, JsonResponse, HttpResponseForbidden, HttpResponseServerError, HttpResponseNotFound
from django.utils import timezone
from django.conf import settings
from asgiref.sync import sync_to_async
from datetime import datetime, timedelta
import logging
import json
from .models import Domain, EmailAccount, Message
from .services.smtplabs_client import SMTPLabsClient, SMTPLabsAPIError

logger = logging.getLogger(__name__)

class HeartCheckView(View):
    async def get(self, request):
        return JsonResponse({"status": "OK"}, status=200)

class Robots_txtView(View):
    async def get(self, request):
        robots_txt_content = f"""\
User-Agent: *
Allow: /
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
</url>
</urlset>
"""
        return HttpResponse(sitemap_xml_content, content_type="application/xml", status=200)
    
@method_decorator(ensure_csrf_cookie, name='dispatch')
class IndexView(View):
    async def get(self, request):
        email_address = await sync_to_async(request.session.get)('email_address')
        messages = []
        
        if email_address:
            try:
                account = await EmailAccount.objects.aget(address=email_address)
                
                # Buscar mensagens da sessão atual (última 1 hora)
                session_start_val = await sync_to_async(request.session.get)('session_start')
                if session_start_val:
                    session_start = datetime.fromisoformat(session_start_val)
                    messages_qs = Message.objects.filter(
                        account=account,
                        received_at__gte=session_start
                    ).order_by('-received_at')
                    
                    # Converter em lista para o template
                    async for msg in messages_qs:
                        messages.append(msg)
            except EmailAccount.DoesNotExist:
                pass
                
        return await sync_to_async(render)(request, 'core/index.html', {
            'initial_messages': messages
        })


# ==================== TEMPMAIL API VIEWS (ASYNC CBVs) ====================

async def _get_or_create_temp_email_async(request) -> tuple[EmailAccount, bool]:
    """
    Obtém ou cria um email temporário para a sessão atual (Assíncrono)
    """
    client = SMTPLabsClient()
    
    # Verificar se já existe email na sessão
    session_email = await sync_to_async(request.session.get)('email_address')
    session_start = await sync_to_async(request.session.get)('session_start')
    
    if session_email and session_start:
        # Verificar se sessão ainda é válida (1 hora)
        session_start_dt = datetime.fromisoformat(session_start)
        session_duration = timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
        
        if timezone.now() < session_start_dt + session_duration:
            try:
                # Usando .aget() do Django 4.1+
                account = await EmailAccount.objects.aget(address=session_email)
                return account, False
            except EmailAccount.DoesNotExist:
                pass
    
    # Buscar conta disponível para reutilização
    # Importante: querysets não são async-iterators por padrão, usamos .afirst()
    available_account = await EmailAccount.objects.filter(
        is_available=True
    ).afirst()
    
    if available_account and available_account.can_be_reused():
        # Reutilizar conta existente (mark_as_used ainda é síncrono no model, chamamos via sync_to_async)
        await sync_to_async(available_account.mark_as_used)()
        
        await sync_to_async(request.session.__setitem__)('email_address', available_account.address)
        await sync_to_async(request.session.__setitem__)('session_start', timezone.now().isoformat())
        await sync_to_async(request.session.save)()
        logger.info(f"Reutilizando conta: {available_account.address}")
        return available_account, True
    
    # Criar nova conta
    try:
        domains = Domain.objects.filter(is_active=True)
        
        if not await domains.aexists():
            logger.info("Sincronizando domínios da API...")
            domains_response = await client.get_domains(is_active=True)
            
            # Garante que temos uma lista para iterar
            domains_list = domains_response if isinstance(domains_response, list) else domains_response.get('member', [])
            
            for domain_data in domains_list:
                await Domain.objects.aupdate_or_create(
                    smtp_id=domain_data['id'],
                    defaults={
                        'domain': domain_data['domain'],
                        'is_active': domain_data.get('isActive', True)
                    }
                )
            domains = Domain.objects.filter(is_active=True)
        
        domain = await domains.afirst()
        if not domain:
            raise Exception("Nenhum domínio disponível")
        
        # Gerar credenciais
        username = EmailAccount.generate_random_username()
        password = EmailAccount.generate_random_password()
        address = f"{username}@{domain.domain}"
        
        # Criar conta na API (Async)
        logger.info(f"Criando nova conta: {address}")
        account_response = await client.create_account(address, password)
        
        # Salvar no banco
        account = await EmailAccount.objects.acreate(
            smtp_id=account_response['id'],
            address=address,
            password=password,
            domain=domain,
            is_available=False,
            last_used_at=timezone.now()
        )
        
        # Salvar na sessão
        await sync_to_async(request.session.__setitem__)('email_address', account.address)
        await sync_to_async(request.session.__setitem__)('session_start', timezone.now().isoformat())
        await sync_to_async(request.session.save)()
        
        logger.info(f"Conta criada com sucesso: {address}")
        return account, True
        
    except SMTPLabsAPIError as e:
        logger.error(f"Erro ao criar conta: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Erro inesperado ao criar conta: {str(e)}")
        raise


class TempEmailAPI(View):
    """API para gerenciar emails temporários"""
    
    async def get(self, request):
        """Retorna email temporário da sessão atual ou cria um novo"""
        try:
            account, is_new = await _get_or_create_temp_email_async(request)
            
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
                'error': 'Erro ao criar email temporário'
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
                    'message': 'Você já está usando este endereço de e-mail'
                })

            # Se for um reset (POST vazio ou sem email)
            if not custom_email:
                has_email = await sync_to_async(request.session.__contains__)('email_address')
                if has_email:
                    await sync_to_async(request.session.pop)('email_address', None)
                
                has_start = await sync_to_async(request.session.__contains__)('session_start')
                if has_start:
                    await sync_to_async(request.session.pop)('session_start', None)
                
                # Gerar novo email imediatamente (Atomic Reset)
                logger.info("Sessão limpa. Gerando novo email imediatamente...")
                account, is_new = await _get_or_create_temp_email_async(request)

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
                    'message': 'Sessão resetada com sucesso'
                })

            # Se for para definir um email específico
            logger.info(f"Tentando login/mudança para email customizado: {custom_email}")
            
            # Validar formato básico
            if '@' not in custom_email:
                return JsonResponse({'success': False, 'error': 'Endereço de email inválido'}, status=400)

            # 1. Verificar se a conta já existe no nosso banco
            try:
                account = await EmailAccount.objects.aget(address=custom_email)
            except EmailAccount.DoesNotExist:
                # 2. Se não existe, precisamos criar. 
                # Primeiro pegamos o domínio do banco
                domain_part = custom_email.split('@')[1]
                try:
                    domain = await Domain.objects.aget(domain=domain_part)
                except Domain.DoesNotExist:
                    return JsonResponse({'success': False, 'error': 'Domínio não suportado'}, status=400)
                
                # 3. Tentar criar na API do SMTP.dev
                client = SMTPLabsClient()
                password = EmailAccount.generate_random_password()
                
                try:
                    account_response = await client.create_account(custom_email, password)
                    
                    # Salvar no banco
                    account = await EmailAccount.objects.acreate(
                        smtp_id=account_response['id'],
                        address=custom_email,
                        password=password,
                        domain=domain,
                        is_available=False,
                        last_used_at=timezone.now()
                    )
                except SMTPLabsAPIError as e:
                    # Se der erro porque já existe lá mas não aqui
                    if "already used" in str(e).lower() or "value is already used" in str(e).lower():
                        logger.info(f"Email {custom_email} já existe na API. Tentando recuperar...")
                        
                        # Buscar informações da conta existente
                        accounts_search = await client.get_accounts(address=custom_email)
                        accounts_list = accounts_search if isinstance(accounts_search, list) else accounts_search.get('member', [])
                        
                        if accounts_list:
                            api_account = accounts_list[0]
                            # Salvar no banco local os dados de lá
                            account = await EmailAccount.objects.acreate(
                                smtp_id=api_account['id'],
                                address=custom_email,
                                password=password, # Usamos a senha que tentamos gerar ou ignore
                                domain=domain,
                                is_available=False,
                                last_used_at=timezone.now()
                            )
                        else:
                            return JsonResponse({'success': False, 'error': 'E-mail em uso por outro usuário ou domínio inválido'}, status=422)
                    else:
                        logger.error(f"Erro ao criar conta customizada na API: {str(e)}")
                        return JsonResponse({'success': False, 'error': 'Não foi possível criar esta conta na API'}, status=500)

            # 4. Atualizar sessão
            await sync_to_async(request.session.__setitem__)('email_address', account.address)
            await sync_to_async(request.session.__setitem__)('session_start', timezone.now().isoformat())
            await sync_to_async(request.session.save)()
            
            session_start_val = await sync_to_async(request.session.get)('session_start')
            session_start = datetime.fromisoformat(session_start_val)
            expires_at = session_start + timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
            expires_in = int((expires_at - timezone.now()).total_seconds())

            return JsonResponse({
                'success': True,
                'email': account.address,
                'expires_in': max(0, expires_in),
                'message': 'Email alterado com sucesso'
            })

        except Exception as e:
            logger.exception("Erro ao processar POST em TempEmailAPI")
            return JsonResponse({
                'success': False,
                'error': 'Erro interno ao processar requisição'
            }, status=500)


class MessageListAPI(View):
    """API para listar e atualizar mensagens"""
    
    async def get(self, request):
        """Lista mensagens da sessão atual e sincroniza se necessário (Throttle de 10s)"""
        try:
            session_email = await sync_to_async(request.session.get)('email_address')
            session_start = await sync_to_async(request.session.get)('session_start')
            
            if not session_email or not session_start:
                return JsonResponse({'success': False, 'error': 'Sessão não encontrada'}, status=400)
            
            account = await EmailAccount.objects.aget(address=session_email)
            
            # --- Lógica de Sincronização Inteligente (Throttle de 10s) ---
            now = timezone.now()
            sync_threshold = timedelta(seconds=10)
            
            should_sync = False
            if not account.last_synced_at:
                should_sync = True
            elif now >= account.last_synced_at + sync_threshold:
                should_sync = True
            
            if should_sync:
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
                        api_messages = []

                    for msg_data in api_messages:
                        if not isinstance(msg_data, dict):
                            logger.warning(f"Mensagem ignorada (formato inválido): {type(msg_data)}")
                            continue
                            
                        smtp_id = msg_data.get('id')
                        if not smtp_id:
                            continue

                        existing_msg = await Message.objects.filter(smtp_id=smtp_id).afirst()
                        
                        # Precisamos buscar detalhes se:
                        # 1. A mensagem é nova (não está no banco)
                        # 2. A mensagem já existe mas tem anexos que não foram salvos localmente
                        needs_detail = not existing_msg or (msg_data.get('hasAttachments') and not (existing_msg.attachments))
                        
                        if needs_detail:
                            # FETCH FULL CONTENT: Listar apenas não traz o corpo/anexos completos
                            try:
                                mailbox_id = msg_data.get('mailboxId')
                                if not mailbox_id:
                                    inbox_data = await client.get_inbox_mailbox(account.smtp_id)
                                    if inbox_data:
                                        mailbox_id = inbox_data.get('id')

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

                            # PROCESSAMENTO DE DETALHES
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
                            
                            logger.info(f"Syncing Message {smtp_id}: hasAttachments={data_to_save['has_attachments']}, attachment_count={len(data_to_save['attachments'])}")

                            if existing_msg:
                                # Atualizar se necessário (foco nos anexos)
                                for key, value in data_to_save.items():
                                    setattr(existing_msg, key, value)
                                await sync_to_async(existing_msg.save)()
                            else:
                                # Criar nova
                                data_to_save['smtp_id'] = smtp_id
                                data_to_save['account'] = account
                                data_to_save['received_at'] = datetime.fromisoformat(msg_data['createdAt'].replace('Z', '+00:00')) if msg_data.get('createdAt') else now
                                await Message.objects.acreate(**data_to_save)
                    
                    # Atualizar timestamp de sincronização
                    account.last_synced_at = now
                    await sync_to_async(account.save)(update_fields=['last_synced_at', 'updated_at'])
                except Exception as e:
                    logger.error(f"Erro na sincronização automática: {str(e)}")
            # ----------------------------------------------------------

            session_start_dt = datetime.fromisoformat(session_start)
            session_end = session_start_dt + timedelta(hours=1)
            
            messages_qs = Message.objects.filter(
                account=account,
                received_at__gte=session_start_dt,
                received_at__lte=session_end
            )
            
            messages_data = []
            async for msg in messages_qs:
                messages_data.append({
                    'id': msg.id,
                    'smtp_id': msg.smtp_id,
                    'from_address': msg.from_address,
                    'from_name': msg.from_name,
                    'subject': msg.subject,
                    'text_preview': msg.text[:100] if msg.text else '',
                    'has_attachments': msg.has_attachments,
                    'is_read': msg.is_read,
                    'received_at': msg.received_at.isoformat(),
                })
            
            return JsonResponse({
                'success': True,
                'messages': messages_data,
                'total': len(messages_data),
                'last_sync': account.last_synced_at.isoformat() if account.last_synced_at else None
            })
        except EmailAccount.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Conta não encontrada'}, status=404)
        except Exception as e:
            logger.exception("Erro ao listar mensagens")
            return JsonResponse({'success': False, 'error': 'Erro ao buscar mensagens'}, status=500)

class MessageDetailAPI(View):
    """API para detalhes de uma mensagem"""
    
    async def get(self, request, message_id):
        try:
            session_email = await sync_to_async(request.session.get)('email_address')
            if not session_email:
                return JsonResponse({'success': False, 'error': 'Sessão não encontrada'}, status=400)
            
            account = await EmailAccount.objects.aget(address=session_email)
            message = await Message.objects.aget(id=message_id, account=account)
            
            # Marcar como lida
            await sync_to_async(message.mark_as_read)()
            
            # MINI-SYNC: Se tem anexos mas não foram salvos localmente (ex: mensagem antiga)
            if message.has_attachments and not message.attachments:
                logger.info(f"MessageDetailAPI: Triggering mini-sync for attachments on message {message_id}")
                try:
                    client = SMTPLabsClient()
                    inbox_data = await client.get_inbox_mailbox(account.smtp_id)
                    if inbox_data:
                        mailbox_id = inbox_data.get('id')
                        if mailbox_id:
                            full_msg = await client.get_message(account.smtp_id, mailbox_id, message.smtp_id)
                            # Se a API retornou anexos, salvamos
                            attachments = full_msg.get('attachments', [])
                            if attachments:
                                message.attachments = attachments
                                await sync_to_async(message.save)()
                                logger.info(f"MessageDetailAPI: Attachments recovered for message {message_id}: {len(attachments)} items")
                except Exception as e:
                    logger.warning(f"MessageDetailAPI: Error in mini-sync for message {message_id}: {e}")
            
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
            return JsonResponse({'success': False, 'error': 'Não encontrado'}, status=404)
        except Exception as e:
            logger.exception("Erro ao buscar detalhes da mensagem")
            return JsonResponse({'success': False, 'error': 'Erro interno'}, status=500)

class MessageDownloadAPI(View):
    """API para download do código fonte da mensagem (.eml)"""
    
    async def get(self, request, message_id):
        # 1. Recuperar email da sessão
        email_address = await sync_to_async(request.session.get)('email_address')
        
        if not email_address:
            return HttpResponseForbidden("Sessão não encontrada")

        try:
            # 2. Buscar conta e validar
            account = await EmailAccount.objects.aget(address=email_address)
            
            # 3. Buscar mensagem no banco (para garantir que pertence à conta)
            message = await Message.objects.select_related('account').aget(id=message_id, account=account)
            
            # 4. Buscar mailbox ID (necessário para a API Fonte)
            client = SMTPLabsClient()
            inbox = await client.get_inbox_mailbox(account.smtp_id)
            
            if not inbox:
                return HttpResponseServerError("Mailbox não encontrada")
                
            mailbox_id = inbox.get('id')
            
            # 5. Buscar fonte usando o SMTP ID da mensagem
            source_content = await client.get_message_source(account.smtp_id, mailbox_id, message.smtp_id)
            
            logger.info(f"Download Message ID {message_id}: source_content length={len(source_content) if source_content else 0}")
            if not source_content:
                # Vamos logar o que o client retornaria se pudéssemos, mas vamos confiar no logger do client se eu o adicionar lá também.
                return HttpResponseServerError("Conteúdo vazio")
            
            # 6. Retornar como arquivo
            response = HttpResponse(source_content, content_type='message/rfc822')
            response['Content-Disposition'] = f'attachment; filename="message_{message.id}.eml"'
            return response
            
        except (EmailAccount.DoesNotExist, Message.DoesNotExist):
            return HttpResponseNotFound("Mensagem não encontrada")
        except Exception as e:
            logger.error(f"Erro no download da mensagem: {e}", exc_info=True)
            return HttpResponseServerError("Não foi possível processar o download da mensagem.")

class AttachmentDownloadAPI(View):
    """API para download de um anexo individual"""
    
    async def get(self, request, message_id, attachment_id):
        # 1. Recuperar email da sessão
        email_address = await sync_to_async(request.session.get)('email_address')
        
        if not email_address:
            return HttpResponseForbidden("Sessão não encontrada")

        try:
            # 2. Buscar conta e validar
            account = await EmailAccount.objects.aget(address=email_address)
            
            # 3. Buscar mensagem no banco (para garantir que pertence à conta)
            message = await Message.objects.select_related('account').aget(id=message_id, account=account)
            
            # 4. Encontrar metadados do anexo na lista salva
            attachments = message.attachments or []
            att_metadata = next((a for a in attachments if str(a.get('id')) == str(attachment_id)), None)
            
            if not att_metadata:
                return HttpResponseNotFound("Anexo não encontrado nos metadados da mensagem")
            
            # 5. Buscar mailbox ID
            client = SMTPLabsClient()
            inbox = await client.get_inbox_mailbox(account.smtp_id)
            if not inbox:
                return HttpResponseServerError("Mailbox não encontrada")
            mailbox_id = inbox.get('id')
            
            # 6. Buscar conteúdo do anexo usando o ID do anexo da API
            # O ID no metadata é o ID real na SMTPLabs (e.g. 1, 2, 3...)
            content = await client.get_attachment_content(
                account.smtp_id, 
                mailbox_id, 
                message.smtp_id, 
                attachment_id
            )
            
            if not content:
                return HttpResponseServerError("Conteúdo do anexo vazio ou não disponível")
            
            # 7. Retornar como arquivo
            response = HttpResponse(content, content_type=att_metadata.get('contentType', 'application/octet-stream'))
            filename = att_metadata.get('filename', f'attachment_{attachment_id}')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response
            
        except (EmailAccount.DoesNotExist, Message.DoesNotExist):
            return HttpResponseNotFound("Arquivo não encontrado")
        except Exception as e:
            logger.error(f"Erro no download do anexo: {e}", exc_info=True)
            return HttpResponseServerError("Não foi possível processar o download do arquivo.")
