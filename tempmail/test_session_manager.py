#!/usr/bin/env python
"""
Script de teste para verificar o funcionamento do SMTPLabsSessionManager.
Testa se a sessão é compartilhada entre múltiplas instâncias do client.
"""
import asyncio
import sys
import os
import django

# Configurar Django
sys.path.insert(0, 'core')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from core.services.smtplabs_client import SMTPLabsClient, SMTPLabsSessionManager


async def test_shared_session():
    """Testa se múltiplas instâncias compartilham a mesma sessão"""
    print("=" * 60)
    print("Teste: Verificar sessão compartilhada")
    print("=" * 60)
    
    # Criar múltiplas instâncias do client
    client1 = SMTPLabsClient()
    client2 = SMTPLabsClient()
    client3 = SMTPLabsClient()
    
    print("\n✓ Criadas 3 instâncias de SMTPLabsClient")
    
    # Obter sessões
    session1 = await client1._get_session()
    session2 = await client2._get_session()
    session3 = await client3._get_session()
    
    print(f"\nSessão 1 ID: {id(session1)}")
    print(f"Sessão 2 ID: {id(session2)}")
    print(f"Sessão 3 ID: {id(session3)}")
    
    # Verificar se todas são a mesma instância
    if session1 is session2 is session3:
        print("\n✅ SUCESSO: Todas as instâncias compartilham a MESMA sessão!")
        print("   Isso significa que connection pooling está funcionando.")
    else:
        print("\n❌ FALHA: Sessões diferentes foram criadas!")
        return False
    
    # Verificar se a sessão está aberta
    if not session1.closed:
        print("✓ Sessão está aberta e pronta para uso")
    else:
        print("❌ Sessão está fechada!")
        return False
    
    # Testar fechamento
    print("\n" + "=" * 60)
    print("Teste: Fechar sessão compartilhada")
    print("=" * 60)
    
    await SMTPLabsSessionManager.close_session()
    print("✓ close_session() chamado")
    
    if session1.closed:
        print("✅ SUCESSO: Sessão foi fechada corretamente!")
    else:
        print("❌ FALHA: Sessão ainda está aberta!")
        return False
    
    # Testar recriação de sessão
    print("\n" + "=" * 60)
    print("Teste: Recriar sessão após fechamento")
    print("=" * 60)
    
    client4 = SMTPLabsClient()
    session4 = await client4._get_session()
    
    if not session4.closed:
        print("✅ SUCESSO: Nova sessão criada após fechamento!")
        print(f"   Nova sessão ID: {id(session4)}")
    else:
        print("❌ FALHA: Nova sessão está fechada!")
        return False
    
    # Cleanup final
    await SMTPLabsSessionManager.close_session()
    print("\n✓ Cleanup final executado")
    
    return True


async def main():
    print("\n🧪 Iniciando testes do SMTPLabsSessionManager\n")
    
    try:
        success = await test_shared_session()
        
        print("\n" + "=" * 60)
        if success:
            print("🎉 TODOS OS TESTES PASSARAM!")
            print("=" * 60)
            print("\n✅ A implementação está correta:")
            print("   • Sessão compartilhada entre todas as instâncias")
            print("   • Fechamento correto da sessão")
            print("   • Recriação de sessão funciona")
            print("\n💡 Benefícios:")
            print("   • Connection pooling ativo")
            print("   • Melhor performance")
            print("   • Sem warnings de sessões não fechadas")
        else:
            print("❌ ALGUNS TESTES FALHARAM")
            print("=" * 60)
            sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERRO DURANTE OS TESTES: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
