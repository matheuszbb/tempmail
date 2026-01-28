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
        Cria nova conta de email temporário (não reutiliza automaticamente).
        Reutilização só via edição manual pelo usuário.
        
        Args:
            request: Objeto HttpRequest
            
        Returns:
            tuple: (EmailAccount | None, bool) onde bool indica se é uma conta nova
                   Retorna (None, False) em caso de erro
        """
        # Limpar sessões expiradas periodicamente
        await self._cleanup_expired_sessions()
        
        # Verificar se já existe email NA SESSÃO ATUAL
        session_email = await sync_to_async(request.session.get)('email_address')
        
        if session_email:
            try:
                account = await EmailAccount.objects.aget(address=session_email)
                
                # Verificar se ainda está válida
                if account.is_session_active():
                    return account, False
                else:
                    # Expirou, iniciar cooldown
                    await sync_to_async(account.start_cooldown)(cooldown_hours=2)
                    logger.info(f"Conta {account.address} expirou, iniciando cooldown de 2h")
                    # Limpar da sessão
                    await sync_to_async(request.session.pop)('email_address', None)
                    await sync_to_async(request.session.pop)('session_start', None)
            except EmailAccount.DoesNotExist:
                pass
        
        # Sempre criar nova conta
        try:
            account = await self._create_new_account()
            
            # Obter ou criar session key
            session_key = request.session.session_key
            if not session_key:
                await sync_to_async(request.session.create)()
                session_key = request.session.session_key
            
            await self._mark_account_as_used(request, account, session_key)
            logger.info(f"Nova conta criada: {account.address}")
            return account, True
        except Exception as e:
            logger.error(f"Erro ao criar nova conta: {str(e)}")
            return None, False
    
    async def _mark_account_as_used(self, request, account: EmailAccount, session_key: str):
        """Marca uma conta como em uso e registra na sessão."""
        await sync_to_async(account.mark_as_used)(
            session_key=session_key,
            session_duration_seconds=settings.TEMPMAIL_SESSION_DURATION
        )
        
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
    
    async def _handle_orphaned_account(self, account: 'EmailAccount'):
        """Remove conta local que não existe mais na API remota"""
        from .models import EmailAccount
        logger.warning(f"Conta {account.address} não existe mais na API remota. Removendo do banco local...")
        await sync_to_async(account.delete)()
        logger.info(f"Conta órfã {account.address} removida do banco local")
    
    async def _cleanup_expired_sessions(self):
        """Limpa sessões expiradas e inicia cooldown de 2h"""
        from .models import EmailAccount
        now = timezone.now()
        
        expired_accounts = EmailAccount.objects.filter(
            session_expires_at__lt=now,
            is_available=False
        )
        
        count = 0
        async for account in expired_accounts:
            # Iniciar cooldown de 2h
            await sync_to_async(account.start_cooldown)(cooldown_hours=2)
            count += 1
        
        if count > 0:
            logger.info(f"Limpeza: {count} sessões expiradas, cooldown de 2h iniciado")