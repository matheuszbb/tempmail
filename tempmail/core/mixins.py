"""
Mixins e utilitários reutilizáveis para views
"""
from django.db import models
from django.http import JsonResponse
from django.contrib.auth import get_user_model
from asgiref.sync import sync_to_async
from datetime import datetime, timedelta
from django.utils import timezone
from django.conf import settings
from .models import Domain, EmailAccount
from .services.smtplabs_client import SMTPLabsClient, SMTPLabsAPIError
import logging

logger = logging.getLogger(__name__)


class AdminRequiredMixin:
    """
    Mixin para verificar se o usuário é superuser.
    Reutilizável em qualquer view que precise de verificação de admin.
    """
    
    async def _check_user_is_superuser(self, request):
        """
        Verifica se o usuário é superuser de forma segura em contexto async.
        
        Returns:
            bool: True se o usuário é superuser e está ativo, False caso contrário
        """
        # Verificar se há um user_id na sessão
        session_user_id = await sync_to_async(lambda: request.session.get('_auth_user_id'))()

        if not session_user_id:
            return False

        # Acessar o usuário diretamente do banco para evitar problemas com lazy loading
        User = get_user_model()
        try:
            user = await User.objects.aget(pk=session_user_id)
            return user.is_superuser and user.is_active
        except (User.DoesNotExist, ValueError):
            return False
    
    async def dispatch(self, request, *args, **kwargs):
        """
        Override dispatch para verificar permissões antes de processar a requisição.
        Views que herdam este mixin podem desabilitar a verificação automática
        definindo `skip_admin_check = True`.
        """
        if not getattr(self, 'skip_admin_check', False):
            is_admin = await self._check_user_is_superuser(request)
            if not is_admin:
                return JsonResponse({'error': 'Acesso negado'}, status=403)
        
        return await super().dispatch(request, *args, **kwargs)


class DateFilterMixin:
    """
    Mixin para processar filtros de data em views.
    Fornece validação e normalização de datas.
    """
    
    async def _get_date_filters(self, request):
        """
        Extrai e valida filtros de data da requisição.
        
        Args:
            request: Objeto HttpRequest
            
        Returns:
            tuple: (data_inicio, data_fim) como objetos date
            
        Raises:
            ValueError: Se as datas forem inválidas
        """
        # Obter parâmetros de data
        data_inicio_str = request.GET.get('data_inicio')
        data_fim_str = request.GET.get('data_fim')

        # Valores padrão: últimos 30 dias
        hoje = timezone.now().date()
        data_inicio_default = hoje - timedelta(days=30)
        data_fim_default = hoje

        try:
            if data_inicio_str:
                data_inicio = datetime.strptime(data_inicio_str, '%Y-%m-%d').date()
                # Validar que não é data futura
                if data_inicio > hoje:
                    data_inicio = data_inicio_default
            else:
                data_inicio = data_inicio_default

            if data_fim_str:
                data_fim = datetime.strptime(data_fim_str, '%Y-%m-%d').date()
                # Validar que não é data futura
                if data_fim > hoje:
                    data_fim = data_fim_default
            else:
                data_fim = data_fim_default

            # Garantir que data_inicio <= data_fim
            if data_inicio > data_fim:
                data_inicio, data_fim = data_fim, data_inicio

            # Limitar período máximo a 1 ano para performance
            if (data_fim - data_inicio).days > 365:
                logger.warning(f"Período muito longo solicitado: {(data_fim - data_inicio).days} dias")
                data_inicio = data_fim - timedelta(days=365)

            return data_inicio, data_fim

        except (ValueError, TypeError) as e:
            logger.warning(f"Erro ao processar filtros de data: {e}")
            return data_inicio_default, data_fim_default


class EmailAccountService:
    """
    Serviço para gerenciar criação e reutilização de contas de email temporárias.
    Encapsula a lógica de negócio relacionada a EmailAccount.
    """
    
    def __init__(self):
        self.client = SMTPLabsClient()
    
    async def get_or_create_temp_email(self, request) -> tuple[EmailAccount | None, bool]:
        """
        Obtém ou cria um email temporário para a sessão atual.
        
        Args:
            request: Objeto HttpRequest
            
        Returns:
            tuple: (EmailAccount | None, bool) onde bool indica se é uma conta nova/reutilizada
                   Retorna (None, False) em caso de erro
        """
        # Verificar se já existe email na sessão
        session_email = await sync_to_async(request.session.get)('email_address')
        session_start = await sync_to_async(request.session.get)('session_start')
        
        if session_email and session_start:
            # Verificar se sessão ainda é válida
            session_start_dt = datetime.fromisoformat(session_start)
            session_duration = timedelta(seconds=settings.TEMPMAIL_SESSION_DURATION)
            
            if timezone.now() < session_start_dt + session_duration:
                try:
                    account = await EmailAccount.objects.aget(address=session_email)
                    return account, False
                except EmailAccount.DoesNotExist:
                    pass
        
        # Tentar reutilizar conta disponível (excluindo a anterior se houver)
        previous_email = await sync_to_async(request.session.get)('previous_email')
        
        # Filtrar contas que estão marcadas como disponíveis
        available_accounts = EmailAccount.objects.filter(is_available=True)
        
        # Aplicar cooldown de 2 horas na consulta para performance
        cooldown_threshold = timezone.now() - timedelta(hours=2)
        available_accounts = available_accounts.filter(
            models.Q(last_used_at__isnull=True) | models.Q(last_used_at__lte=cooldown_threshold)
        )
        
        # Excluir email anterior da seleção
        if previous_email:
            available_accounts = available_accounts.exclude(address=previous_email)
        
        # Pegar a conta que não foi usada há mais tempo
        available_account = await available_accounts.order_by('last_used_at').afirst()
        
        if available_account and available_account.can_be_reused():
            # Limpar previous_email da sessão após uso
            if previous_email:
                await sync_to_async(request.session.pop)('previous_email', None)
            
            await self._mark_account_as_used(request, available_account)
            logger.info(f"Reutilizando conta: {available_account.address}")
            return available_account, True
        
        # Criar nova conta
        try:
            account = await self._create_new_account()
            await self._mark_account_as_used(request, account)
            logger.info(f"Nova conta criada: {account.address}")
            return account, True
        except Exception as e:
            logger.error(f"Erro ao criar nova conta: {str(e)}")
            return None, False
    
    async def _mark_account_as_used(self, request, account: EmailAccount):
        """Marca uma conta como em uso e registra na sessão."""
        await sync_to_async(account.mark_as_used)()
        
        # Registrar no histórico de emails
        email_sessions = await sync_to_async(request.session.get)('email_sessions', {})
        if not isinstance(email_sessions, dict):
            email_sessions = {}
        if account.address not in email_sessions:
            email_sessions[account.address] = timezone.now().isoformat()
        
        await sync_to_async(request.session.__setitem__)('email_sessions', email_sessions)
        await sync_to_async(request.session.__setitem__)('email_address', account.address)
        await sync_to_async(request.session.__setitem__)('session_start', email_sessions[account.address])
        await sync_to_async(request.session.save)()
    
    async def _create_new_account(self) -> EmailAccount:
        """
        Cria uma nova conta de email.
        
        Returns:
            EmailAccount: Conta criada
            
        Raises:
            Exception: Se não houver domínios disponíveis ou erro na API
        """
        # Garantir que temos domínios
        domains = Domain.objects.filter(is_active=True)
        
        if not await domains.aexists():
            await self._sync_domains()
            domains = Domain.objects.filter(is_active=True)
        
        domain = await domains.afirst()
        if not domain:
            raise Exception("Nenhum domínio disponível")
        
        # Gerar credenciais
        username = EmailAccount.generate_random_username()
        password = EmailAccount.generate_random_password()
        address = f"{username}@{domain.domain}"
        
        try:
            # Criar conta na API
            logger.info(f"Criando nova conta: {address}")
            account_response = await self.client.create_account(address, password)
            
            # Criar conta no banco
            account = await EmailAccount.objects.acreate(
                smtp_id=account_response['id'],
                address=address,
                password=password,
                domain=domain,
                is_available=False,
                last_used_at=timezone.now()
            )
            
            logger.info(f"Conta criada com sucesso: {address}")
            return account
            
        except SMTPLabsAPIError as e:
            logger.error(f"Erro ao criar conta na API: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Erro inesperado ao criar conta: {str(e)}")
            raise
    
    async def _sync_domains(self):
        """Sincroniza domínios da API."""
        logger.info("Sincronizando domínios da API...")
        domains_response = await self.client.get_domains(is_active=True)
        
        domains_list = domains_response if isinstance(domains_response, list) else domains_response.get('member', [])
        
        for domain_data in domains_list:
            await Domain.objects.aupdate_or_create(
                smtp_id=domain_data['id'],
                defaults={
                    'domain': domain_data['domain'],
                    'is_active': domain_data.get('isActive', True)
                }
            )