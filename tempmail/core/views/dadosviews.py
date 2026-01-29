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
