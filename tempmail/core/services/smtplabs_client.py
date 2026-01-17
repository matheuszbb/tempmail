"""
Cliente Python Assíncrono para interagir com a API SMTP.dev usando aiohttp
"""
import aiohttp
import logging
import asyncio
from typing import Optional, Dict, List, Any
from django.conf import settings


logger = logging.getLogger(__name__)


class SMTPLabsAPIError(Exception):
    """Exceção customizada para erros da API SMTP.dev"""
    pass


class SMTPLabsClient:
    """Cliente assíncrono para interagir com a API SMTP.dev usando aiohttp"""
    
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.SMTPLABS_API_KEY
        self.base_url = base_url or settings.SMTPLABS_BASE_URL
        self.headers = {
            'X-API-KEY': self.api_key,
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
    
    async def _make_request(
        self, 
        method: str, 
        endpoint: str, 
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        max_retries: int = 3,
        raw_response: bool = False
    ) -> Any:
        """
        Faz requisição assíncrona para a API com retry automático para rate limiting
        """
        url = f"{self.base_url}{endpoint}"
        
        async with aiohttp.ClientSession(headers=self.headers) as session:
            for attempt in range(max_retries):
                try:
                    async with session.request(
                        method=method,
                        url=url,
                        json=data,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        
                        # Rate limiting - retry com backoff exponencial
                        if response.status == 429:
                            wait_time = 2 ** attempt
                            logger.warning(f"Rate limit atingido. Aguardando {wait_time}s...")
                            await asyncio.sleep(wait_time)
                            continue
                        
                        # Sucesso (200-204)
                        if 200 <= response.status <= 204:
                            if response.status == 204:
                                return {}
                            if raw_response:
                                return await response.read()
                            return await response.json()
                        
                        # Erros
                        response_text = await response.text()
                        error_msg = f"API Error {response.status}: {response_text}"
                        logger.error(error_msg)
                        raise SMTPLabsAPIError(error_msg)
                        
                except aiohttp.ClientError as e:
                    logger.error(f"Request failed: {str(e)}")
                    if attempt == max_retries - 1:
                        raise SMTPLabsAPIError(f"Request failed after {max_retries} attempts: {str(e)}")
                    await asyncio.sleep(1)
            
            raise SMTPLabsAPIError("Max retries exceeded")
    
    # ==================== DOMAINS ====================
    
    async def get_domains(self, is_active: bool = True, page: int = 1) -> Dict[str, Any]:
        params = {'isActive': str(is_active).lower(), 'page': page}
        return await self._make_request('GET', '/domains', params=params)
    
    async def create_domain(self, domain: str, is_active: bool = True) -> Dict[str, Any]:
        data = {'domain': domain, 'isActive': is_active}
        return await self._make_request('POST', '/domains', data=data)
    
    async def get_domain(self, domain_id: str) -> Dict[str, Any]:
        return await self._make_request('GET', f'/domains/{domain_id}')
    
    async def delete_domain(self, domain_id: str) -> None:
        await self._make_request('DELETE', f'/domains/{domain_id}')
    
    # ==================== ACCOUNTS ====================
    
    async def get_accounts(
        self, 
        address: Optional[str] = None,
        is_active: bool = True,
        page: int = 1
    ) -> Dict[str, Any]:
        params = {'isActive': str(is_active).lower(), 'page': page}
        if address:
            params['address'] = address
        return await self._make_request('GET', '/accounts', params=params)
    
    async def create_account(self, address: str, password: str) -> Dict[str, Any]:
        data = {'address': address, 'password': password}
        return await self._make_request('POST', '/accounts', data=data)
    
    async def get_account(self, account_id: str) -> Dict[str, Any]:
        return await self._make_request('GET', f'/accounts/{account_id}')
    
    async def delete_account(self, account_id: str) -> None:
        await self._make_request('DELETE', f'/accounts/{account_id}')
    
    # ==================== MAILBOXES ====================
    
    async def get_mailboxes(self, account_id: str, page: int = 1) -> Dict[str, Any]:
        params = {'page': page}
        return await self._make_request('GET', f'/accounts/{account_id}/mailboxes', params=params)
    
    async def create_mailbox(self, account_id: str, path: str) -> Dict[str, Any]:
        data = {'path': path}
        return await self._make_request('POST', f'/accounts/{account_id}/mailboxes', data=data)
    
    async def get_mailbox(self, account_id: str, mailbox_id: str) -> Dict[str, Any]:
        return await self._make_request('GET', f'/accounts/{account_id}/mailboxes/{mailbox_id}')
    
    # ==================== MESSAGES ====================
    
    async def get_messages(
        self,
        account_id: str,
        mailbox_id: str,
        page: int = 1
    ) -> Dict[str, Any]:
        params = {'page': page}
        return await self._make_request(
            'GET',
            f'/accounts/{account_id}/mailboxes/{mailbox_id}/messages',
            params=params
        )
    
    async def get_message(
        self,
        account_id: str,
        mailbox_id: str,
        message_id: str
    ) -> Dict[str, Any]:
        return await self._make_request(
            'GET',
            f'/accounts/{account_id}/mailboxes/{mailbox_id}/messages/{message_id}'
        )
    
    async def get_attachment_content(
        self,
        account_id: str,
        mailbox_id: str,
        message_id: str,
        attachment_id: str
    ) -> bytes:
        """Busca o conteúdo bruto de um anexo"""
        return await self._make_request(
            'GET',
            f'/accounts/{account_id}/mailboxes/{mailbox_id}/messages/{message_id}/attachment/{attachment_id}',
            raw_response=True
        )
    
    async def delete_message(
        self,
        account_id: str,
        mailbox_id: str,
        message_id: str
    ) -> None:
        await self._make_request(
            'DELETE',
            f'/accounts/{account_id}/mailboxes/{mailbox_id}/messages/{message_id}'
        )
    
    async def get_message_source(
        self,
        account_id: str,
        mailbox_id: str,
        message_id: str
    ) -> str:
        response = await self._make_request(
            'GET',
            f'/accounts/{account_id}/mailboxes/{mailbox_id}/messages/{message_id}/source'
        )
        # Logar para debugar em produção se necessário
        if isinstance(response, dict):
            logger.info(f"API Source Response Keys: {list(response.keys())}")
            # Tentar várias chaves comuns em APIs de email
            source = response.get('source') or response.get('data') or response.get('raw') or response.get('body')
            if source:
                return source
        
        # Se for uma string direta (raro via .json() mas possível se a API retornar "string")
        if isinstance(response, str):
            return response
            
        return ""
    
    # ==================== HELPER METHODS ====================
    
    async def get_inbox_mailbox(self, account_id: str) -> Optional[Dict[str, Any]]:
        try:
            mailboxes_response = await self.get_mailboxes(account_id)
            mailboxes = mailboxes_response if isinstance(mailboxes_response, list) else mailboxes_response.get('member', [])
            
            for mailbox in mailboxes:
                if mailbox.get('path', '').upper() == 'INBOX':
                    return mailbox
            
            return None
        except SMTPLabsAPIError as e:
            logger.error(f"Erro ao buscar INBOX: {str(e)}")
            return None
    
    async def get_all_inbox_messages(self, account_id: str) -> List[Dict[str, Any]]:
        inbox = await self.get_inbox_mailbox(account_id)
        if not inbox:
            logger.warning(f"INBOX não encontrada para conta {account_id}")
            return []
        
        mailbox_id = inbox.get('id')
        all_messages = []
        page = 1
        
        while True:
            try:
                response = await self.get_messages(account_id, mailbox_id, page=page)
                messages = response if isinstance(response, list) else response.get('member', [])
                
                if not messages:
                    break
                
                all_messages.extend(messages)
                
                # Se resposta for lista, não há metadados de paginação (assumimos página única)
                if isinstance(response, list):
                    break

                total_items = response.get('totalItems', 0)
                if len(all_messages) >= total_items:
                    break
                
                page += 1
            except SMTPLabsAPIError as e:
                logger.error(f"Erro ao buscar mensagens página {page}: {str(e)}")
                break
        
        return all_messages
