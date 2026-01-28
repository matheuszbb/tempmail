function copyToClipboard(clickedBtn) {
    const targetId = clickedBtn.getAttribute('data-copy-target');
    const targetElement = document.getElementById(targetId);

    if (!targetElement) return;

    const textToCopy = targetElement.value || targetElement.innerText;

    // Função para mostrar feedback visual de sucesso
    const showSuccess = () => {
        // 1. Salva o estado original
        const originalHTML = clickedBtn.innerHTML;
        const originalClasses = clickedBtn.className;
        
        const hasText = clickedBtn.querySelector('span') !== null;

        // 2. Classes de estilo para remover
        const styleClasses = [
            'bg-white', 'bg-gray-100', 'dark:bg-[#1e293b]', 'dark:bg-gray-700',
            'hover:bg-gray-200', 'dark:hover:bg-gray-600',
            'text-gray-600', 'dark:text-gray-300', 'text-gray-700', 'dark:text-gray-200',
            'border-gray-200', 'dark:border-gray-700', 'shadow-sm'
        ];
        
        clickedBtn.classList.remove(...styleClasses);

        // 3. Aplica o visual de sucesso (Fundo verde fixo)
        clickedBtn.classList.add('bg-green-600', 'text-white', 'border-transparent');

        // 4. Atualiza o conteúdo (Sem a animação bounce)
        if (hasText) {
            clickedBtn.innerHTML = `
                <i class="fa-solid fa-check text-xl lg:text-lg mb-1 lg:mb-0 lg:mr-2"></i>
                <span class="text-xs lg:text-sm font-bold">Copiado!</span>
            `;
        } else {
            clickedBtn.innerHTML = '<i class="fa-solid fa-check text-lg transition-transform"></i>';
        }

        clickedBtn.style.pointerEvents = 'none';

        // 5. Restaura tudo após 2 segundos
        setTimeout(() => {
            clickedBtn.className = originalClasses;
            clickedBtn.innerHTML = originalHTML;
            clickedBtn.style.pointerEvents = 'auto';
        }, 2000);
    };

    // Tentar usar a API moderna do Clipboard primeiro
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(textToCopy)
            .then(showSuccess)
            .catch(() => {
                // Fallback para método tradicional se a API moderna falhar
                fallbackCopy(textToCopy, showSuccess);
            });
    } else {
        // Fallback direto se a API não estiver disponível (comum em HTTP ou mobile antigo)
        fallbackCopy(textToCopy, showSuccess);
    }
}

// Método fallback para copiar usando document.execCommand (compatível com mobile)
function fallbackCopy(text, onSuccess) {
    // Criar elemento de input temporário
    const tempInput = document.createElement('textarea');
    tempInput.value = text;
    tempInput.style.position = 'fixed';
    tempInput.style.top = '0';
    tempInput.style.left = '-9999px';
    tempInput.style.opacity = '0';
    tempInput.setAttribute('readonly', '');
    document.body.appendChild(tempInput);

    // Selecionar o texto
    if (navigator.userAgent.match(/ipad|ipod|iphone/i)) {
        // iOS requer abordagem especial
        tempInput.contentEditable = 'true';
        tempInput.readOnly = false;
        const range = document.createRange();
        range.selectNodeContents(tempInput);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        tempInput.setSelectionRange(0, 999999);
    } else {
        tempInput.select();
        tempInput.setSelectionRange(0, 99999);
    }

    // Executar comando de cópia
    let success = false;
    try {
        // eslint-disable-next-line deprecation/deprecation
        // Fallback necessário para compatibilidade com mobile/HTTP
        success = document.execCommand('copy');
    } catch (err) {
        // Silencioso
    }

    // Remover elemento temporário
    document.body.removeChild(tempInput);

    // Chamar callback de sucesso se funcionou
    if (success && onSuccess) {
        onSuccess();
    }
}