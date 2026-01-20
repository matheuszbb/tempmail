document.addEventListener('DOMContentLoaded', function() {
    /**
     * Função principal para alternar a identidade visual das abas
     * Remove as classes de todas e aplica o laranja (brand-orange) apenas na ativa
     */
    function updateActiveVisual(activeElement) {
        if (!activeElement) return;

        const tabLinks = document.querySelectorAll('.tab-link');
        
        tabLinks.forEach(tab => {
            // Reset total: remove laranja e garante cores neutras/hover
            tab.classList.remove('!bg-brand-orange', '!text-white', '!border-brand-orange');
            tab.classList.add('text-gray-600', 'dark:text-gray-400', 'hover:bg-gray-200', 'dark:hover:bg-gray-700');
        });

        // Aplica o estado Ativo (brand-orange)
        activeElement.classList.add('!bg-brand-orange', '!text-white', '!border-brand-orange');
        // Remove os estados neutros para não haver conflito de cor
        activeElement.classList.remove('text-gray-600', 'dark:text-gray-400', 'hover:bg-gray-200', 'dark:hover:bg-gray-700');
    }

    /**
     * Inicializa o estado correto das abas baseado no filtro atual
     */
    function initializeTabState() {
        // Pega o filtro da URL atual
        const urlParams = new URLSearchParams(window.location.search);
        const currentFilter = urlParams.get('filter') || 'all';
        
        // Encontra a aba correspondente
        const activeTab = document.querySelector(`.tab-link[data-filter="${currentFilter}"]`);
        
        if (activeTab) {
            updateActiveVisual(activeTab);
        } else {
            // Se não encontrar, marca a primeira (Top 100) por padrão
            const defaultTab = document.querySelector('.tab-link[data-filter="all"]');
            if (defaultTab) {
                updateActiveVisual(defaultTab);
            }
        }
    }

    // 1. Inicializa o estado correto ao carregar a página
    initializeTabState();

    // 2. Ouvinte de Clique Manual (Para resposta instantânea na UI)
    document.addEventListener('click', function(e) {
        const tabLink = e.target.closest('.tab-link');
        if (tabLink) {
            updateActiveVisual(tabLink);
        }
    });

    // 3. Sincronização com HTMX após carregamento de conteúdo
    document.body.addEventListener('htmx:afterOnLoad', function(evt) {
        // Verifica se é uma atualização do dashboard ou dos sites
        if (evt.detail.target.id === 'sites-content' || evt.detail.target.id === 'dashboard-content') {
            try {
                const path = evt.detail.requestConfig.path;
                const url = new URL(path, window.location.origin);
                
                // Tenta pegar o filtro da URL da requisição
                const filterParam = url.searchParams.get('filter');
    
                // SÓ ATUALIZA se houver um parâmetro de filtro na requisição
                if (filterParam) {
                    const targetTab = document.querySelector(`.tab-link[data-filter="${filterParam}"]`);
                    if (targetTab) {
                        updateActiveVisual(targetTab);
                    }
                } else if (evt.detail.target.id === 'dashboard-content') {
                    // Se atualizou o dashboard todo sem filtro específico, re-inicializa as abas
                    setTimeout(initializeTabState, 100);
                }
            } catch (e) {
                console.debug('Requisição processada:', e.message);
            }
        }
    });

    // 4. Garante que após swap do HTMX, o estado visual seja preservado
    document.body.addEventListener('htmx:afterSwap', function(evt) {
        if (evt.detail.target.id === 'dashboard-content') {
            // Re-inicializa após swap completo do dashboard
            setTimeout(initializeTabState, 50);
        }
    });
});