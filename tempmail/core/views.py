from django.views import View
from django.contrib import messages
from datetime import datetime, timedelta
from django.utils.safestring import mark_safe
from django.core.exceptions import PermissionDenied
from django.template.loader import render_to_string
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import HttpResponse, Http404, HttpResponseForbidden, JsonResponse

class HeartCheckView(View):
    def get(self, request):
        return JsonResponse({"status": "OK"}, status=200)

class Robots_txtView(View):
    def get(self, request):
        robots_txt_content = f"""\
        
User-Agent: *
Allow: /
Sitemap: {request.build_absolute_uri('/sitemap.xml')}
"""
        return HttpResponse(robots_txt_content, content_type="text/plain", status=200)

class Sitemap_xmlView(View):
    def get(self, request):
        site_url = request.build_absolute_uri('/')[:-1]  # Remove a última barra se houver
        sitemap_xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url>
    <loc>{site_url}</loc>
</url>
</urlset>
"""
        return HttpResponse(sitemap_xml_content, content_type="application/xml", status=200)
    
class IndexView(View):
    def get(self, request):
        return render(request, 'core/index.html')