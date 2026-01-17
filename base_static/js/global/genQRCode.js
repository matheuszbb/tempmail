/**
 * Gera um QR Code dentro de um container específico
 * @param {string} data - O texto/URL que será codificado no QR Code
 * @param {HTMLElement} container - O elemento DOM onde o QR será renderizado
 */
function generateQRCode(data, container) {
    if (!container) return;

    // Limpa o container antes de gerar novo QR
    container.innerHTML = "";

    const logoComplexa = `data:image/svg+xml;base64,${btoa(`
        <svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 650'>
            <path fill='#ea580c' d='M48 64C21.5 64 0 85.5 0 112c0 15.1 7.1 29.3 19.2 38.4L236.8 313.6c11.4 8.5 27 8.5 38.4 0L492.8 150.4c12.1-9.1 19.2-23.3 19.2-38.4c0-26.5-21.5-48-48-48H48zM0 176V384c0 35.3 28.7 64 64 64H448c35.3 0 64-28.7 64-64V176L294.4 339.2c-22.8 17.1-54.1 17.1-76.8 0L0 176z'/>
            
            <text x='256' y='560' font-family='Verdana, sans-serif' font-weight='900' font-size='80' text-anchor='middle'>
                <tspan fill='#ea580c'>EMAIL</tspan><tspan fill='#111827'>RUSH</tspan>
            </text>
        </svg>
    `)}`;

    const qrCode = new QRCodeStyling({
        width: 220,
        height: 220,
        type: "svg",
        data: data || "",
        image: logoComplexa,
        dotsOptions: {
            color: "#ea580c",
            type: "rounded"
        },
        backgroundOptions: {
            color: "transparent",
        },
        imageOptions: {
            crossOrigin: "anonymous",
            margin: 12,
            imageSize: 0.5
        },
        cornersSquareOptions: {
            color: "#ea580c",
            type: "extra-rounded"
        }
    });

    qrCode.append(container);
}

// ============================================
// SISTEMA DE POPOVER GLOBAL REUTILIZÁVEL
// ============================================

const QRPopover = {
    activeTrigger: null,
    popoverElement: null,
    containerElement: null,

    /**
     * Inicializa o sistema de popover
     * @param {string} popoverId - ID do elemento popover (padrão: 'qrContent')
     * @param {string} qrContainerId - ID do container do QR dentro do popover (padrão: 'qrImage')
     */
    init(popoverId = 'qrContent', qrContainerId = 'qrImage') {
        this.popoverElement = document.getElementById(popoverId);
        this.containerElement = document.getElementById(qrContainerId);

        if (!this.popoverElement || !this.containerElement) {
            console.warn('QRPopover: Elementos não encontrados');
            return;
        }

        // Listener global para fechar ao clicar fora
        document.addEventListener('click', (e) => {
            if (!this.popoverElement.classList.contains('hidden') &&
                !this.popoverElement.contains(e.target) &&
                !e.target.closest('[data-qr-trigger]')) {
                this.close();
            }
        });

        // Listener para reposicionar ao redimensionar/scroll
        window.addEventListener('resize', () => this.position());
        window.addEventListener('scroll', () => this.position());
    },

    /**
     * Posiciona o popover relativo ao botão ativo
     */
    position() {
        if (!this.popoverElement || !this.activeTrigger ||
            this.popoverElement.classList.contains('hidden')) return;

        const rect = this.activeTrigger.getBoundingClientRect();
        const popoverWidth = 256;
        const gap = 12;

        let top = rect.bottom + window.scrollY + gap;
        let left = rect.left + window.scrollX + (rect.width / 2) - (popoverWidth / 2);

        // Ajusta se sair da tela
        if (left < 10) left = 10;
        if (left + popoverWidth > window.innerWidth - 10) {
            left = window.innerWidth - popoverWidth - 10;
        }

        this.popoverElement.style.position = 'absolute';
        this.popoverElement.style.top = `${top}px`;
        this.popoverElement.style.left = `${left}px`;
    },

    /**
     * Abre o popover
     * @param {HTMLElement} trigger - O botão que acionou o popover
     * @param {string} data - Os dados para gerar o QR Code
     */
    open(trigger, data) {
        this.activeTrigger = trigger;
        this.popoverElement.dataset.activeBtn = trigger.id;

        // Gera o QR Code
        generateQRCode(data, this.containerElement);

        // Mostra e posiciona
        this.popoverElement.classList.remove('hidden');
        this.position();
    },

    /**
     * Fecha o popover
     */
    close() {
        if (this.popoverElement) {
            this.popoverElement.classList.add('hidden');
            this.activeTrigger = null;
        }
    },

    /**
     * Alterna o estado do popover
     * @param {HTMLElement} trigger - O botão que acionou
     * @param {string} data - Os dados para o QR Code
     */
    toggle(trigger, data) {
        const isOpen = !this.popoverElement.classList.contains('hidden');
        const isSameTrigger = this.popoverElement.dataset.activeBtn === trigger.id;

        if (isOpen && isSameTrigger) {
            this.close();
        } else {
            this.open(trigger, data);
        }
    }
};

/**
 * Função global para ser chamada pelos botões
 * Usa data attributes para configuração:
 * - data-qr-trigger: Marca o botão como trigger
 * - data-qr-source: ID do elemento de onde pegar o texto (opcional, usa value ou textContent)
 * - data-qr-text: Texto direto para gerar o QR (alternativa ao data-qr-source)
 */
function toggleQR(event) {
    event.preventDefault();
    event.stopPropagation();

    const trigger = event.currentTarget;

    // Obtém o texto para gerar o QR Code
    let qrData = '';

    // Prioridade 1: data-qr-text (texto direto)
    if (trigger.dataset.qrText) {
        qrData = trigger.dataset.qrText;
    }
    // Prioridade 2: data-qr-source (ID de um elemento)
    else if (trigger.dataset.qrSource) {
        const sourceElement = document.getElementById(trigger.dataset.qrSource);
        if (sourceElement) {
            qrData = sourceElement.value || sourceElement.textContent || '';
        }
    }
    // Fallback: tenta pegar de 'emailDisplay' (compatibilidade)
    else {
        const fallbackElement = document.getElementById('emailDisplay');
        if (fallbackElement) {
            qrData = fallbackElement.value || fallbackElement.textContent || '';
        }
    }

    QRPopover.toggle(trigger, qrData.trim());
}

// Inicializa quando o DOM estiver pronto
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => QRPopover.init());
} else {
    QRPopover.init();
}