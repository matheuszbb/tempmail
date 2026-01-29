import os
import sys
import logging
from django.apps import AppConfig
from django.conf import settings

# logger específico para mensagens de startup (usa logger dedicado 'core.startup')
logger = logging.getLogger('core.startup')

class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        import core.signals 
        self.executar_script_inicial()

    def executar_script_inicial(self):
        # 1. Se estivermos rodando um comando administrativo (migrate, etc), paramos aqui.
        if 'manage.py' in sys.argv and 'runserver' not in sys.argv:
            return

        # 2. Evita o processo de "reload" do ambiente de desenvolvimento
        if os.environ.get('RUN_MAIN') == 'true':
            return

        # 3. GARANTIA PARA PRODUÇÃO:
        # Se você usa múltiplos workers (Gunicorn), e quer que rode APENAS UMA VEZ
        # no servidor inteiro, você pode usar um lock simples ou variável de ambiente.
        if os.environ.get('SCRIPT_JA_EXECUTADO'):
            return
        
        os.environ['SCRIPT_JA_EXECUTADO'] = 'true'
        # Registrar apenas em produção (DEBUG == False)
        try:
            is_debug = bool(getattr(settings, 'DEBUG', True))
        except Exception:
            is_debug = True

        # sys.stderr garante que o log saia mesmo em sistemas com buffer pesado (Docker/Cloud)
        logger.info(f"🚀 Sistema ON 🚀")