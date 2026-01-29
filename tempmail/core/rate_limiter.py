"""
Rate Limiter inteligente para controlar chamadas à API externa
"""
import time
import logging
from collections import deque
from datetime import datetime, timedelta
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


class APIRateLimiter:
    """
    Rate limiter para API externa com backoff exponencial.
    
    Limites da API smtp.dev:
    - 2048 queries por segundo (QPS) por IP
    
    Estratégia:
    - Manter controle local de requests para não exceder 80% do limite
    - Implementar backoff exponencial quando receber 429
    - Cache agressivo de respostas
    """
    
    def __init__(self, max_qps=1600, window_seconds=1):
        """
        Args:
            max_qps: Máximo de queries por segundo (80% do limite da API)
            window_seconds: Janela de tempo para contagem
        """
        self.max_qps = max_qps
        self.window_seconds = window_seconds
        self.cache_key_prefix = 'api_rate_limit'
        self.backoff_key = 'api_backoff_until'
    
    def can_make_request(self) -> tuple[bool, float]:
        """
        Verifica se pode fazer uma requisição agora.
        
        Returns:
            tuple: (pode_fazer, tempo_de_espera_em_segundos)
        """
        # 1. Verificar se estamos em backoff
        backoff_until = cache.get(self.backoff_key)
        if backoff_until:
            wait_time = (backoff_until - timezone.now()).total_seconds()
            if wait_time > 0:
                logger.warning(f"⏳ API em backoff. Aguardar {wait_time:.1f}s")
                return False, wait_time
            else:
                # Backoff expirou
                cache.delete(self.backoff_key)
        
        # 2. Verificar rate limit local
        cache_key = f"{self.cache_key_prefix}:requests"
        request_times = cache.get(cache_key, [])
        
        # Limpar requests antigos (fora da janela)
        now = time.time()
        cutoff = now - self.window_seconds
        request_times = [t for t in request_times if t > cutoff]
        
        # Verificar se excedeu o limite
        if len(request_times) >= self.max_qps:
            # Calcular quanto tempo esperar
            oldest_request = min(request_times)
            wait_time = self.window_seconds - (now - oldest_request)
            logger.warning(f"⚠️ Rate limit local atingido: {len(request_times)}/{self.max_qps} QPS")
            return False, max(0.1, wait_time)
        
        return True, 0
    
    def record_request(self):
        """Registra uma requisição feita."""
        cache_key = f"{self.cache_key_prefix}:requests"
        request_times = cache.get(cache_key, [])
        
        # Adicionar novo timestamp
        now = time.time()
        request_times.append(now)
        
        # Limpar antigos
        cutoff = now - self.window_seconds
        request_times = [t for t in request_times if t > cutoff]
        
        # Salvar no cache (TTL de 5 segundos)
        cache.set(cache_key, request_times, timeout=5)
    
    def record_429_error(self, retry_after: int = None):
        """
        Registra um erro 429 e ativa backoff exponencial.
        
        Args:
            retry_after: Tempo em segundos indicado pela API (header Retry-After)
        """
        # Obter contador de erros consecutivos
        error_count_key = f"{self.cache_key_prefix}:error_count"
        error_count = cache.get(error_count_key, 0) + 1
        
        # Backoff exponencial: 2^n segundos (máximo 8s para UX)
        if retry_after:
            backoff_seconds = min(retry_after, 8)
        else:
            backoff_seconds = min(2 ** error_count, 8)
        
        backoff_until = timezone.now() + timedelta(seconds=backoff_seconds)
        
        cache.set(self.backoff_key, backoff_until, timeout=backoff_seconds + 10)
        cache.set(error_count_key, error_count, timeout=300)  # Reset após 5min
        
        logger.error(
            f"🔴 API retornou 429. Backoff de {backoff_seconds}s ativado "
            f"(erro #{error_count}). Retry em {backoff_until.strftime('%H:%M:%S')}"
        )
    
    def reset_error_count(self):
        """Reseta contador de erros após requisição bem-sucedida."""
        error_count_key = f"{self.cache_key_prefix}:error_count"
        cache.delete(error_count_key)


class MessageSyncThrottler:
    """
    Throttler específico para sincronização de mensagens.
    Evita múltiplas sincronizações simultâneas para a mesma conta.
    """
    
    def __init__(self, min_interval_seconds=4):
        """
        Args:
            min_interval_seconds: Intervalo mínimo entre sincronizações (4s)
        """
        self.min_interval_seconds = min_interval_seconds
        self.cache_key_prefix = 'sync_throttle'
    
    def can_sync(self, account_address: str) -> tuple[bool, float]:
        """
        Verifica se pode sincronizar esta conta agora.
        
        Returns:
            tuple: (pode_sincronizar, tempo_desde_ultima_sync)
        """
        cache_key = f"{self.cache_key_prefix}:{account_address}"
        last_sync_timestamp = cache.get(cache_key)
        
        if not last_sync_timestamp:
            return True, 0
        
        time_since_last_sync = time.time() - last_sync_timestamp
        
        if time_since_last_sync < self.min_interval_seconds:
            wait_time = self.min_interval_seconds - time_since_last_sync
            logger.debug(
                f"⏱️ Sync throttled para {account_address}. "
                f"Última sync há {time_since_last_sync:.1f}s, aguardar {wait_time:.1f}s"
            )
            return False, time_since_last_sync
        
        return True, time_since_last_sync
    
    def record_sync(self, account_address: str):
        """Registra que uma sincronização foi realizada."""
        cache_key = f"{self.cache_key_prefix}:{account_address}"
        cache.set(cache_key, time.time(), timeout=self.min_interval_seconds + 5)
        logger.debug(f"✅ Sync registrada para {account_address}")


# Instâncias globais
api_rate_limiter = APIRateLimiter(max_qps=1600)  # 80% do limite de 2048
message_sync_throttler = MessageSyncThrottler(min_interval_seconds=4)  # 4s para melhor UX
