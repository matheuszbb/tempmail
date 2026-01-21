from django.contrib import admin
from django.urls import path, include
from django.conf.urls.i18n import i18n_patterns
from django.views.i18n import JavaScriptCatalog
from django.conf.urls.static import static
from django.conf import settings

from .views import (
    IndexView, Robots_txtView, Sitemap_xmlView, HeartCheckView,
    TempEmailAPI, MessageListAPI, MessageDetailAPI, MessageDownloadAPI,
    AttachmentDownloadAPI, DadosView, PrivacidadeView, TermosView, ContatoView, SobreView
)

# 🌍 Rotas que não precisam de tradução (como arquivos técnicos e APIs)
urlpatterns = [
    path('robots.txt', Robots_txtView.as_view(), name='robots_txt'),
    path('sitemap.xml', Sitemap_xmlView.as_view(), name='sitemap_xml'),
    path('health/', HeartCheckView.as_view(), name='health_check'),

    # API endpoints (não precisam de tradução)
    path('api/email/', TempEmailAPI.as_view(), name='temp_email_api'),
    path('api/messages/', MessageListAPI.as_view(), name='message_list_api'),
    path('api/messages/<int:message_id>/', MessageDetailAPI.as_view(), name='api-message-detail'),
    path('api/messages/<int:message_id>/download/', MessageDownloadAPI.as_view(), name='api-message-download'),
    path('api/messages/<int:message_id>/attachments/<str:attachment_id>/download/', AttachmentDownloadAPI.as_view(), name='api-attachment-download'),

    # Rota para troca de idioma
    path('i18n/', include('django.conf.urls.i18n')),
    path('jsi18n/', JavaScriptCatalog.as_view(), name='javascript-catalog'),
]

# 🌐 Rotas que devem ser traduzíveis (prefixadas com /pt-br/, /en/, etc.)
urlpatterns += i18n_patterns(
    path('django-emailrustadmin-django/', admin.site.urls),
    path('', IndexView.as_view(), name='index'),
    path('sobre/', SobreView.as_view(), name='sobre'),
    path('privacidade/', PrivacidadeView.as_view(), name='privacidade'),
    path('termos/', TermosView.as_view(), name='termos'),
    path('contato/', ContatoView.as_view(), name='contato'),
    path('dados/', DadosView.as_view(), name='dados_admin'),
    prefix_default_language=True  # ❗️Evita prefixo para o idioma padrão (pt-br)
)

# Arquivos estáticos e de mídia
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)