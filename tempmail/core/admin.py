"""
Django admin configuration for tempmail models
"""
from django.contrib import admin
from .models import Domain, EmailAccount, Message


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ('domain', 'is_active', 'created_at', 'updated_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('domain', 'smtp_id')
    readonly_fields = ('smtp_id', 'created_at', 'updated_at')
    ordering = ('-is_active', 'domain')


@admin.register(EmailAccount)
class EmailAccountAdmin(admin.ModelAdmin):
    list_display = ('address', 'domain', 'is_available', 'last_used_at', 'created_at')
    list_filter = ('is_available', 'domain', 'created_at')
    search_fields = ('address', 'smtp_id')
    readonly_fields = ('smtp_id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    
    fieldsets = (
        ('Informações Básicas', {
            'fields': ('smtp_id', 'address', 'password', 'domain')
        }),
        ('Controle de Uso', {
            'fields': ('is_available', 'last_used_at')
        }),
        ('Metadados', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ('subject', 'from_address', 'account', 'received_at', 'is_read', 'has_attachments')
    list_filter = ('is_read', 'is_flagged', 'has_attachments', 'received_at')
    search_fields = ('subject', 'from_address', 'from_name', 'text')
    readonly_fields = ('smtp_id', 'created_at', 'updated_at')
    ordering = ('-received_at',)
    date_hierarchy = 'received_at'
    
    fieldsets = (
        ('Informações Básicas', {
            'fields': ('smtp_id', 'account', 'received_at')
        }),
        ('Remetente', {
            'fields': ('from_address', 'from_name')
        }),
        ('Destinatários', {
            'fields': ('to_addresses', 'cc_addresses', 'bcc_addresses'),
            'classes': ('collapse',)
        }),
        ('Conteúdo', {
            'fields': ('subject', 'text', 'html')
        }),
        ('Anexos', {
            'fields': ('has_attachments', 'attachments'),
            'classes': ('collapse',)
        }),
        ('Status', {
            'fields': ('is_read', 'is_flagged')
        }),
        ('Metadados', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
