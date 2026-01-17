from django.contrib import admin
from django.urls import path
from .views import (
    IndexView, Robots_txtView, Sitemap_xmlView, HeartCheckView,
    TempEmailAPI, MessageListAPI, MessageDetailAPI, MessageDownloadAPI,
    AttachmentDownloadAPI
)
from django.conf.urls.static import static
from django.conf import settings

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', IndexView.as_view(), name='index'),
    path('health/', HeartCheckView.as_view(), name='health_check'),
    path('robots.txt', Robots_txtView.as_view(), name='robots_txt'),
    path('sitemap.xml', Sitemap_xmlView.as_view(), name='sitemap_xml'),
    
    # Tempmail API (Async CBVs)
    path('api/email/', TempEmailAPI.as_view(), name='temp_email_api'),
    path('api/messages/', MessageListAPI.as_view(), name='message_list_api'),
    path('api/messages/<int:message_id>/', MessageDetailAPI.as_view(), name='api-message-detail'),
    path('api/messages/<int:message_id>/download/', MessageDownloadAPI.as_view(), name='api-message-download'),
    path('api/messages/<int:message_id>/attachments/<str:attachment_id>/download/', AttachmentDownloadAPI.as_view(), name='api-attachment-download'),
]

urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
