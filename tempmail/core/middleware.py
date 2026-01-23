import os
import logging
import inspect
import aiofiles
import mimetypes
from django.conf import settings
from django.utils._os import safe_join
from django.utils.http import http_date
from email.utils import parsedate_to_datetime
from django.http import StreamingHttpResponse, HttpResponseNotModified
from asgiref.sync import iscoroutinefunction, markcoroutinefunction

logger = logging.getLogger("django.request")

# STREAMING_CLASSES = {"StreamingHttpResponse"}
# SUSPECT_PATH_PREFIXES = ("/service-worker.js", "/.well-known/")

# class ResponseDiagnosticsMiddleware:
#     # ESSENCIAL: Informe ao Django que este middleware suporta async
#     async_capable = True
#     sync_capable = False 

#     def __init__(self, get_response):
#         self.get_response = get_response
#         # Se o próximo handler for async, marcamos esta instância como coroutine
#         if iscoroutinefunction(self.get_response):
#             markcoroutinefunction(self)

#     async def __call__(self, request):
#         # Em um middleware async_capable no ASGI, get_response DEVE ser awaitado
#         response = await self.get_response(request)

#         # Coleta dados (sem alteração na sua lógica de diagnóstico)
#         cls_name = getattr(response, "__class__", type(response)).__name__
#         status = getattr(response, "status_code", "<?>")
        
#         # WhiteNoise costuma retornar respostas que o Django precisa adaptar
#         # No Django 6, acessamos headers assim:
#         headers = getattr(response, "headers", {})
#         content_type = headers.get("Content-Type")
#         x_frame = headers.get("X-Frame-Options")
#         is_streaming = cls_name in STREAMING_CLASSES

#         logger.debug(
#             f"[diag] URL={request.path} | RespClass={cls_name} | Status={status} "
#             f"| Streaming={is_streaming} | CT={content_type}"
#         )

#         if any(request.path.startswith(p) for p in SUSPECT_PATH_PREFIXES):
#             frame = inspect.currentframe()
#             outer = inspect.getouterframes(frame, 3)
#             stack_hint = " > ".join(
#                 f"{f.function}@{f.filename.split('/')[-1]}:{f.lineno}" for f in outer[:4]
#             )
#             logger.debug(f"[diag] SuspectPath={request.path} | StackHint={stack_hint}")

#         return response
    
class AsyncStaticMiddleware:
    async_capable = True
    sync_capable = False

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    async def __call__(self, request):
        if not request.path.startswith(settings.STATIC_URL):
            return await self.get_response(request)

        rel_path = request.path[len(settings.STATIC_URL):].lstrip('/')
        try:
            full_path = safe_join(settings.STATIC_ROOT, rel_path)
        except ValueError:
            return await self.get_response(request)

        # 1. Lógica de Gzip (Igual WhiteNoise)
        accept_encoding = request.headers.get("Accept-Encoding", "")
        compressed_path = full_path + ".gz"
        serve_path = full_path
        is_compressed = False

        if "gzip" in accept_encoding and os.path.exists(compressed_path):
            serve_path = compressed_path
            is_compressed = True
            logger.debug(f"✅ [static] Gzip encontrado para {rel_path}")
        else:
            # Esse log vai te dizer se o arquivo .gz realmente existe onde o Django procura
            if "gzip" in accept_encoding:
                logger.debug(f"❌ [static] Gzip ausente no disco: {compressed_path}")

        if os.path.exists(serve_path) and os.path.isfile(serve_path):
            stat = os.stat(serve_path)
            
            # 2. Lógica de Cache (304) - Crucial para performance
            if_modified_since = request.headers.get("If-Modified-Since")
            if if_modified_since and int(stat.st_mtime) <= self.parse_http_date(if_modified_since):
                return HttpResponseNotModified()

            # 3. Resposta Assíncrona Nativa (Resolve o Warning)
            # Criamos um gerador assíncrono para ler o arquivo
            async def file_iterator(file_path, chunk_size=65536):
                async with aiofiles.open(file_path, mode='rb') as f:
                    while chunk := await f.read(chunk_size):
                        yield chunk

            response = StreamingHttpResponse(file_iterator(serve_path))
            
            # 4. Cabeçalhos de Eficiência
            content_type, _ = mimetypes.guess_type(full_path)
            response["Content-Type"] = content_type or "application/octet-stream"
            if is_compressed:
                response["Content-Encoding"] = "gzip"
            
            response["Cache-Control"] = "public, max-age=31536000, immutable"
            response["Last-Modified"] = http_date(stat.st_mtime)
            return response

        return await self.get_response(request)

    def parse_http_date(self, date_str):
        try:
            return int(parsedate_to_datetime(date_str).timestamp())
        except: return 0