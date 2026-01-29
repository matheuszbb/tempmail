import re
import os
import json
import base64
import hashlib
import logging
import asyncio
import unicodedata
from django.views import View
from django.urls import reverse
from collections import Counter
from django.conf import settings
from django.utils import timezone
from django.contrib import messages
from html import escape as html_escape
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
from ..rate_limiter import api_rate_limiter

logger = logging.getLogger(__name__)

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
            
            # Verificar rate limit antes de fazer chamadas à API
            if not api_rate_limiter.can_make_request():
                return JsonResponse({
                    'success': False,
                    'error': str(_('Sistema temporariamente ocupado. Tente novamente em alguns segundos.'))
                }, status=429)
            
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
            if "429" in str(e):
                api_rate_limiter.record_429_error()
                return JsonResponse({
                    'success': False,
                    'error': str(_('API temporariamente indisponível. Aguarde alguns segundos.'))
                }, status=429)
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
            
            # Verificar rate limit antes de sync de anexos
            if message.has_attachments and not message.attachments:
                if not api_rate_limiter.can_make_request():
                    return JsonResponse({
                        'success': False,
                        'error': str(_('Sistema temporariamente ocupado. Tente novamente em alguns segundos.'))
                    }, status=429)
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
        except SMTPLabsAPIError as e:
            logger.error(f"Erro na API SMTPLabs: {e}")
            if "429" in str(e):
                api_rate_limiter.record_429_error()
                return JsonResponse({
                    'success': False,
                    'error': str(_('API temporariamente indisponível. Aguarde alguns segundos.'))
                }, status=429)
            return JsonResponse({
                'success': False, 
                'error': str(_('Erro ao processar mensagem'))
            }, status=500)
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
            
            # Verificar rate limit antes de buscar mailbox
            if not api_rate_limiter.can_make_request():
                return JsonResponse({
                    'success': False,
                    'error': str(_('Sistema temporariamente ocupado. Tente novamente em alguns segundos.'))
                }, status=429)
            
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
        except SMTPLabsAPIError as e:
            logger.error(f"Erro na API SMTPLabs: {e}")
            if "429" in str(e):
                api_rate_limiter.record_429_error()
                return JsonResponse({
                    'success': False,
                    'error': str(_('API temporariamente indisponível. Aguarde alguns segundos.'))
                }, status=429)
            return HttpResponseServerError(
                str(_("Erro ao processar download da mensagem."))
            )
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
            
            # Verificar rate limit antes de buscar anexo
            if not api_rate_limiter.can_make_request():
                return JsonResponse({
                    'success': False,
                    'error': str(_('Sistema temporariamente ocupado. Tente novamente em alguns segundos.'))
                }, status=429)
            
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
        except SMTPLabsAPIError as e:
            logger.error(f"Erro na API SMTPLabs: {e}")
            if "429" in str(e):
                api_rate_limiter.record_429_error()
                return JsonResponse({
                    'success': False,
                    'error': str(_('API temporariamente indisponível. Aguarde alguns segundos.'))
                }, status=429)
            return HttpResponseServerError(
                str(_("Erro ao processar download do anexo."))
            )
        except Exception as e:
            logger.error(f"Erro no download do anexo: {e}", exc_info=True)
            return HttpResponseServerError(
                str(_("Não foi possível processar o download do arquivo."))
            )
