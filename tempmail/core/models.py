import string
import secrets
import random
from django.db import models
from .name_data import _NAMES
from datetime import timedelta
from django.conf import settings
from django.utils import timezone

class Domain(models.Model):
    """Domínios disponíveis do SMTP.dev"""
    smtp_id = models.CharField(max_length=255, unique=True, help_text="ID do domínio na API SMTP.dev")
    domain = models.CharField(max_length=255, unique=True, help_text="Nome do domínio (ex: example.com)")
    is_active = models.BooleanField(default=True, help_text="Domínio está ativo?")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Domínio"
        verbose_name_plural = "Domínios"
        ordering = ['-is_active', 'domain']

    def __str__(self):
        return self.domain


class EmailAccount(models.Model):
    """Contas de email temporárias reutilizáveis"""
    smtp_id = models.CharField(max_length=255, unique=True, help_text="ID da conta na API SMTP.dev")
    address = models.EmailField(unique=True, help_text="Endereço completo (ex: user@example.com)")
    password = models.CharField(max_length=255, help_text="Senha da conta")
    domain = models.ForeignKey(Domain, on_delete=models.CASCADE, related_name='accounts')
    
    # Controle de reutilização
    last_used_at = models.DateTimeField(null=True, blank=True, help_text="Último uso da conta")
    is_available = models.BooleanField(default=True, help_text="Conta disponível para uso?")
    session_expires_at = models.DateTimeField(null=True, blank=True, help_text="Quando a sessão atual expira")
    cooldown_until = models.DateTimeField(null=True, blank=True, help_text="Conta em cooldown até esta data (2h após expiração)")
    last_session_key = models.CharField(max_length=255, null=True, blank=True, help_text="Session key do último usuário (para permitir reutilização)")
    
    # Metadados
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(null=True, blank=True, help_text="Última sincronização com a API externa")

    class Meta:
        verbose_name = "Conta de Email"
        verbose_name_plural = "Contas de Email"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['is_available', 'last_used_at']),
            models.Index(fields=['address']),
        ]

    def __str__(self):
        return self.address

    def can_be_reused(self):
        """Verifica se a conta pode ser reutilizada (sessão expirou)"""
        if not self.session_expires_at:
            return True  # Nunca foi usada ou sessão não foi registrada
        
        # A conta só pode ser reutilizada se a sessão atual tiver expirado
        return timezone.now() >= self.session_expires_at
    
    def is_session_active(self):
        """Verifica se a sessão atual ainda está ativa"""
        if not self.session_expires_at:
            return False
        return timezone.now() < self.session_expires_at

    def mark_as_used(self, session_key=None, session_duration_seconds=None):
        """Marca a conta como em uso e define expiração da sessão"""        
        self.is_available = False
        self.last_used_at = timezone.now()
        self.last_session_key = session_key  # Salvar session key do usuário
        self.cooldown_until = None  # Limpar cooldown ao reutilizar
        
        # Definir quando a sessão expira
        duration = session_duration_seconds or settings.TEMPMAIL_SESSION_DURATION
        self.session_expires_at = timezone.now() + timedelta(seconds=duration)
        
        self.save(update_fields=['is_available', 'last_used_at', 'last_session_key', 'cooldown_until', 'session_expires_at', 'updated_at'])

    def release(self):
        """Libera a conta para reutilização (após cooldown)"""
        if self.can_be_reused():
            self.is_available = True
            self.save(update_fields=['is_available', 'updated_at'])
            return True
        return False

    def start_cooldown(self, cooldown_hours=2):
        """Inicia cooldown após expiração da sessão"""
        self.is_available = True  # Disponível, mas em cooldown
        self.cooldown_until = timezone.now() + timedelta(hours=cooldown_hours)
        self.save(update_fields=['is_available', 'cooldown_until', 'updated_at'])

    def can_be_used_by(self, session_key):
        """Verifica se pode ser usada por este usuário"""
        # Conta em uso por outro usuário
        if not self.is_available and self.is_session_active():
            return False
        
        # Conta disponível sem cooldown
        if not self.cooldown_until:
            return True
        
        # Conta em cooldown
        if timezone.now() < self.cooldown_until:
            # Permitir se for o último usuário
            return self.last_session_key == session_key
        
        # Cooldown expirado
        return True

    @staticmethod
    def generate_random_username(length=10):
        """
        Gera um username humanizado usando combinações de nomes e sobrenomes comuns.
        
        Exemplos:
        - lucas.silva
        - ana_santos42
        - pedro.costa
        - maria_oliveira7
        
        Args:
            length: Parâmetro mantido por compatibilidade, mas não usado na nova implementação
            
        Returns:
            str: Username humanizado
        """
        # Usar listas ampliadas de nomes/sobrenomes (arquivo separado)
        names = _NAMES        
        separators = ['.', '_']

        # Padrões variados para aumentar diversidade
        # Probabilidades: 55% -> first.sep.last(+num), 20% -> first_lastinitial(+num),
        # 15% -> firstlast(+num), 10% -> single_first(+num)
        pattern_roll = random.random()

        first = random.choice(names)
        last = random.choice(names)
        sep = random.choice(separators)

        add_number = random.random() < 0.6  # 60% chance de número
        number = str(random.randint(1, 99999999999)) if add_number else ''

        if pattern_roll < 0.55:
            username = f"{first}{sep}{last}{number}"
        elif pattern_roll < 0.75:
            # first + sobrenome inicial
            username = f"{first}{sep}{last[0]}{number}"
        elif pattern_roll < 0.90:
            # concatenado (sem separador)
            username = f"{first}{last}{number}"
        else:
            # apenas primeiro nome (com chance de número)
            username = f"{first}{number}"

        # Normalizar: remover espaços, deixar minúsculo
        return username.lower()

    @staticmethod
    def generate_random_password(length=16):
        """Gera uma senha aleatória segura"""
        chars = string.ascii_letters + string.digits + string.punctuation
        return ''.join(secrets.choice(chars) for _ in range(length))

class Message(models.Model):
    """Mensagens de email recebidas"""
    smtp_id = models.CharField(max_length=255, unique=True, help_text="ID da mensagem na API SMTP.dev")
    account = models.ForeignKey(EmailAccount, on_delete=models.CASCADE, related_name='messages')
    
    # Informações do remetente
    from_address = models.EmailField(help_text="Email do remetente")
    from_name = models.CharField(max_length=255, blank=True, help_text="Nome do remetente")
    
    # Destinatários
    to_addresses = models.JSONField(default=list, help_text="Lista de destinatários")
    cc_addresses = models.JSONField(default=list, blank=True, help_text="Lista de CC")
    bcc_addresses = models.JSONField(default=list, blank=True, help_text="Lista de BCC")
    
    # Conteúdo
    subject = models.CharField(max_length=500, blank=True, help_text="Assunto do email")
    text = models.TextField(blank=True, help_text="Conteúdo em texto puro")
    html = models.TextField(blank=True, help_text="Conteúdo em HTML")
    
    # Anexos
    has_attachments = models.BooleanField(default=False)
    attachments = models.JSONField(default=list, blank=True, help_text="Metadados dos anexos")
    
    # Status
    is_read = models.BooleanField(default=False, help_text="Mensagem foi lida?")
    is_flagged = models.BooleanField(default=False, help_text="Mensagem marcada?")
    
    # Timestamps
    received_at = models.DateTimeField(help_text="Quando a mensagem foi recebida")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Mensagem"
        verbose_name_plural = "Mensagens"
        ordering = ['-received_at']
        indexes = [
            models.Index(fields=['account', 'received_at']),
            models.Index(fields=['account', 'is_read']),
            models.Index(fields=['-received_at']),
            models.Index(fields=['smtp_id']),  # Otimização: Índice para buscas por smtp_id
        ]

    def __str__(self):
        return f"{self.from_address} - {self.subject[:50]}"

    def mark_as_read(self):
        """Marca a mensagem como lida"""
        if not self.is_read:
            self.is_read = True
            self.save(update_fields=['is_read', 'updated_at'])

    def get_first_name_initial(self):
        """Retorna a primeira letra do nome do remetente para avatar"""
        if self.from_name:
            return self.from_name[0].upper()
        elif self.from_address:
            return self.from_address[0].upper()
        return '?'

    @classmethod
    def get_messages_for_session(cls, account, session_start, session_duration_hours=1):
        """
        Retorna mensagens recebidas durante a sessão do usuário
        
        Args:
            account: EmailAccount instance
            session_start: datetime quando a sessão começou
            session_duration_hours: duração da sessão em horas (padrão: 1h)
        """
        session_end = session_start + timedelta(hours=session_duration_hours)
        return cls.objects.filter(
            account=account,
            received_at__gte=session_start,
            received_at__lte=session_end
        )
