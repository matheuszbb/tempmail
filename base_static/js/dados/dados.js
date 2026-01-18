document.addEventListener('DOMContentLoaded', function() {
    // Selecionamos todos os links que possuem a classe tab-link
    const tabLinks = document.querySelectorAll('.tab-link');

    /**
     * Função principal para alternar a identidade visual
     * Remove as classes de todas e aplica o laranja (brand-orange) apenas na ativa
     */
    function updateActiveVisual(activeElement) {
        if (!activeElement) return;

        tabLinks.forEach(tab => {
            // Reset total: remove laranja e garante cores neutras/hover
            tab.classList.remove('!bg-brand-orange', '!text-white', '!border-brand-orange');
            tab.classList.add('text-gray-600', 'dark:text-gray-400', 'hover:bg-gray-200', 'dark:hover:bg-gray-700');
        });

        // Aplica o estado Ativo (Quem manda é o seu brand-orange)
        activeElement.classList.add('!bg-brand-orange', '!text-white', '!border-brand-orange');
        // Remove os estados neutros para não haver conflito de cor
        activeElement.classList.remove('text-gray-600', 'dark:text-gray-400', 'hover:bg-gray-200', 'dark:hover:bg-gray-700');
    }

    // 1. Ouvinte de Clique Manual (Para resposta instantânea na UI)
    tabLinks.forEach(link => {
        link.addEventListener('click', function() {
            updateActiveVisual(this);
        });
    });

    // 2. Sincronização com HTMX (Garante que a aba certa fique laranja após o carregamento)
    // Usamos 'htmx:afterOnLoad' para garantir que o conteúdo já foi processado
    document.body.addEventListener('htmx:afterOnLoad', function(evt) {
        // Verifica se o alvo da atualização foi o conteúdo dos sites
        if (evt.detail.target.id === 'sites-content' || evt.detail.target.id === 'dashboard-content') {
            try {
                // Extrai o filtro da URL da requisição feita pelo HTMX
                const path = evt.detail.requestConfig.path;
                const url = new URL(path, window.location.origin);
                const filterParam = url.searchParams.get('filter') || 'all';

                // Localiza a aba correspondente ao filtro aplicado
                const targetTab = document.querySelector(`.tab-link[data-filter="${filterParam}"]`);
                if (targetTab) {
                    updateActiveVisual(targetTab);
                }
            } catch (e) {
                console.debug('Aguardando parâmetro de filtro...');
            }
        }
    });
});