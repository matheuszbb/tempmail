/**
 * Sistema de Notificações Toast utilizando DaisyUI e Tailwind CSS
 */
const Toast = {
    container: null,

    init() {
        this.container = document.getElementById('toast-container');
    },

    /**
     * Mostra uma notificação profissional estilo DaisyUI
     */
    show(message, type = 'info', duration = 6000) {
        if (!this.container) this.init();
        if (!this.container) return;

        // Classes de cores do DaisyUI/Tailwind
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

        // Elemento Toast Individual
        const toastItem = document.createElement('div');
        // Adicionando largura máxima e permitindo que o flex se ajuste
        toastItem.className = 'pointer-events-auto transition-all duration-300 ease-in-out translate-x-full opacity-0 w-full max-w-[320px] sm:max-w-sm ml-auto';

        toastItem.innerHTML = `
            <div class="alert ${alertClass} shadow-xl border-none text-white font-bold flex items-start gap-3 relative overflow-hidden pr-10 py-4">
                <i class="fa-solid ${icon} text-xl mt-0.5 flex-shrink-0"></i>
                <span class="text-sm whitespace-normal break-words flex-1 leading-tight">${message}</span>
                
                <!-- Barra de progresso com Tailwind + estilo inline para animação -->
                <!-- Reduzimos 100ms da animação para garantir que ela termine visualmente antes do fechamento -->
                <div class="absolute bottom-0 left-0 h-1 bg-white/40 w-full origin-left" 
                     style="animation: toast-progress-anim ${duration - 100}ms linear forwards"></div>
                
                <!-- Botão de fechar -->
                <button class="absolute top-2 right-2 opacity-50 hover:opacity-100 transition-opacity p-1" onclick="this.closest('.pointer-events-auto').remove()">
                    <i class="fa-solid fa-xmark text-sm"></i>
                </button>
            </div>
        `;

        this.container.appendChild(toastItem);

        // Disparar animações de entrada do Tailwind
        requestAnimationFrame(() => {
            toastItem.classList.remove('translate-x-full', 'opacity-0');
            toastItem.classList.add('translate-x-0', 'opacity-100');
        });

        // Auto-remover com animação de saída
        setTimeout(() => {
            toastItem.classList.remove('translate-x-0', 'opacity-100');
            toastItem.classList.add('translate-x-full', 'opacity-0');
            setTimeout(() => toastItem.remove(), 310);
        }, duration);
    },

    success(msg, dur) { this.show(msg, 'success', dur); },
    error(msg, dur) { this.show(msg, 'error', dur); },
    info(msg, dur) { this.show(msg, 'info', dur); },
    warning(msg, dur) { this.show(msg, 'warning', dur); }
};

document.addEventListener('DOMContentLoaded', () => Toast.init());
window.Toast = Toast;
