function copyToClipboard(clickedBtn) {
    const targetId = clickedBtn.getAttribute('data-copy-target');
    const targetElement = document.getElementById(targetId);

    if (!targetElement) return;

    const textToCopy = targetElement.value || targetElement.innerText;

    navigator.clipboard.writeText(textToCopy).then(() => {
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
    });
}