const Toast = {
    container: null,

    init() {
        this.container = document.getElementById('toast-container');
    },

    show(message, type = 'info', duration = 6000) {
        if (!this.container) this.init();
        if (!this.container) return;

        const alertTypes = {
            success: 'alert-success',
            error: 'alert-error',
            info: 'alert-info',
            warning: 'alert-warning'
        };

        const icons = {
            success: 'fa-circle-check',
            error: 'fa-circle-xmark',
            info: 'fa-circle-info',
            warning: 'fa-triangle-exclamation'
        };

        const alertClass = alertTypes[type] || 'alert-info';
        const icon = icons[type] || 'fa-circle-info';

        const toastItem = document.createElement('div');
        
        // Estado inicial de entrada: deslocado para direita e invisível
        toastItem.className = 'pointer-events-auto transition-all duration-300 ease-in-out translate-x-full opacity-0 w-full max-w-[320px] sm:max-w-sm ml-auto';

        toastItem.innerHTML = `
            <div class="alert ${alertClass} shadow-xl border-none text-white font-bold flex items-start gap-3 relative overflow-hidden pr-10 py-4">
                <i class="fa-solid ${icon} text-xl mt-0.5 flex-shrink-0"></i>
                <span class="text-sm whitespace-normal break-words flex-1 leading-tight">${message}</span>
                
                <div id="progress-${Date.now()}" 
                     class="absolute bottom-0 left-0 h-1 bg-white/40 w-full origin-left transform scale-x-100 transition-transform ease-linear" 
                     style="transition-duration: ${duration}ms;">
                </div>
                
                <button class="absolute top-2 right-2 opacity-50 hover:opacity-100 transition-opacity p-1" onclick="this.closest('.pointer-events-auto').remove()">
                    <i class="fa-solid fa-xmark text-sm"></i>
                </button>
            </div>
        `;

        this.container.appendChild(toastItem);

        // Animações
        requestAnimationFrame(() => {
            // 1. Animação de Entrada do Toast
            toastItem.classList.remove('translate-x-full', 'opacity-0');
            toastItem.classList.add('translate-x-0', 'opacity-100');
        
            // 2. Animação da Barra de Progresso
            const progressBar = toastItem.querySelector('div[id^="progress-"]');
            if (progressBar) {
                // O "pulo do gato": um pequeno delay para o navegador processar a escala inicial
                setTimeout(() => {
                    progressBar.classList.remove('scale-x-100');
                    progressBar.classList.add('scale-x-0');
                }, 10); 
            }
        });

        // Auto-remover
        setTimeout(() => {
            // Se o usuário já fechou manualmente, o elemento pode não existir mais
            if (toastItem.parentElement) {
                toastItem.classList.remove('translate-x-0', 'opacity-100');
                toastItem.classList.add('translate-x-full', 'opacity-0');
                setTimeout(() => {
                    if (toastItem.parentElement) toastItem.remove();
                }, 300); // Tempo da transição de saída
            }
        }, duration);
    },

    success(msg, dur) { this.show(msg, 'success', dur); },
    error(msg, dur) { this.show(msg, 'error', dur); },
    info(msg, dur) { this.show(msg, 'info', dur); },
    warning(msg, dur) { this.show(msg, 'warning', dur); }
};

document.addEventListener('DOMContentLoaded', () => Toast.init());
window.Toast = Toast;