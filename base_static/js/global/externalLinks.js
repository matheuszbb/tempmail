/**
 * External Links Manager - Global
 * Sistema global de proteção para links externos
 * Pode ser usado em qualquer parte do sistema
 * 
 * Uso:
 * 1. Incluir este script no base.html ou em qualquer template
 * 2. Adicionar atributo data-external-link="URL" em qualquer elemento
 * 3. O script automaticamente adiciona proteção e modal de confirmação
 * 
 * Exemplos:
 * <a data-external-link="https://google.com">Google</a>
 * <span data-external-link="gmail.com">Gmail</span>
 * <div data-external-link="https://example.com">Clique aqui</div>
 */

(function() {
    'use strict';

    class ExternalLinksManager {
        constructor(options = {}) {
            this.options = {
                // Seletor para encontrar elementos com links externos
                selector: '[data-external-link]',
                
                // ID do modal de confirmação
                modalId: 'linkConfirmModal',
                
                // Classes CSS para elementos clicáveis
                clickableClasses: [
                    'cursor-pointer',
                    'text-brand-orange',
                    'hover:text-brand-dark',
                    'transition-colors',
                    'underline',
                    'decoration-dotted'
                ],
                
                // Auto-inicializar ao carregar
                autoInit: true,
                
                // Callback antes de abrir o modal
                beforeOpen: null,
                
                // Callback após confirmar
                afterConfirm: null,
                
                ...options
            };

            this.elements = {
                modal: null,
                targetDisplay: null,
                confirmBtn: null,
                warningDiv: null
            };

            if (this.options.autoInit) {
                this.init();
            }
        }

        /**
         * Inicializa o gerenciador de links
         */
        init() {
            // Aguarda DOM estar pronto
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', () => this.setup());
            } else {
                this.setup();
            }
        }

        /**
         * Configuração inicial
         */
        setup() {
            // Localiza elementos do modal
            this.locateModalElements();

            // Configura links existentes
            this.setupLinks();

            // Observa mudanças no DOM (para conteúdo dinâmico/HTMX)
            this.observeDOMChanges();

            // Listener para HTMX
            this.setupHTMXListeners();
        }

        /**
         * Localiza elementos do modal no DOM
         */
        locateModalElements() {
            this.elements.modal = document.getElementById(this.options.modalId);
            this.elements.targetDisplay = document.getElementById('targetLinkDisplay');
            this.elements.confirmBtn = document.getElementById('confirmLinkBtn');
            this.elements.warningDiv = document.getElementById('hiddenUrlWarning');
        }

        /**
         * Configura todos os links externos na página
         */
        setupLinks(container = document) {
            const elements = container.querySelectorAll(this.options.selector);
            
            elements.forEach(element => {
                const url = element.getAttribute('data-external-link');
                if (!url) return;

                // Previne duplicação de listeners
                if (element.hasAttribute('data-external-ready')) return;
                element.setAttribute('data-external-ready', 'true');

                // Adiciona classes de estilo
                this.options.clickableClasses.forEach(className => {
                    element.classList.add(className);
                });

                // Adiciona evento de clique
                element.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    
                    const fullUrl = this.normalizeUrl(url);
                    this.openModal(fullUrl);
                });

                // Adiciona título (tooltip)
                if (!element.hasAttribute('title')) {
                    element.setAttribute('title', `Clique para abrir: ${this.normalizeUrl(url)}`);
                }
            });
        }

        /**
         * Normaliza URL (adiciona https:// se necessário)
         */
        normalizeUrl(url) {
            if (!url) return '';
            
            url = url.trim();
            
            // Se já tem protocolo, retorna como está
            if (url.startsWith('http://') || url.startsWith('https://')) {
                return url;
            }
            
            // Se começa com //, adiciona https:
            if (url.startsWith('//')) {
                return 'https:' + url;
            }
            
            // Adiciona https:// por padrão
            return 'https://' + url;
        }

        /**
         * Abre o modal de confirmação
         */
        openModal(url, isHidden = false) {
            // Callback antes de abrir
            if (typeof this.options.beforeOpen === 'function') {
                this.options.beforeOpen(url);
            }

            // Se modal não existe, abre diretamente
            if (!this.elements.modal) {
                window.open(url, '_blank', 'noopener,noreferrer');
                return;
            }

            // Atualiza display da URL
            if (this.elements.targetDisplay) {
                this.elements.targetDisplay.textContent = url;
            }

            // Configura botão de confirmação
            if (this.elements.confirmBtn) {
                this.elements.confirmBtn.href = url;
                this.elements.confirmBtn.target = '_blank';
                this.elements.confirmBtn.rel = 'noopener noreferrer';

                // Remove listeners anteriores
                const newBtn = this.elements.confirmBtn.cloneNode(true);
                this.elements.confirmBtn.parentNode.replaceChild(newBtn, this.elements.confirmBtn);
                this.elements.confirmBtn = newBtn;

                // Adiciona novo listener
                this.elements.confirmBtn.addEventListener('click', () => {
                    if (typeof this.options.afterConfirm === 'function') {
                        this.options.afterConfirm(url);
                    }
                });
            }

            // Mostra/oculta aviso de URL oculta
            if (this.elements.warningDiv) {
                if (isHidden) {
                    this.elements.warningDiv.classList.remove('hidden');
                } else {
                    this.elements.warningDiv.classList.add('hidden');
                }
            }

            // Abre o modal
            this.elements.modal.showModal();
        }

        /**
         * Fecha o modal
         */
        closeModal() {
            if (this.elements.modal) {
                this.elements.modal.close();
            }
        }

        /**
         * Observa mudanças no DOM para adicionar listeners em conteúdo dinâmico
         */
        observeDOMChanges() {
            const observer = new MutationObserver((mutations) => {
                mutations.forEach((mutation) => {
                    if (mutation.addedNodes.length) {
                        mutation.addedNodes.forEach((node) => {
                            if (node.nodeType === 1) { // Element node
                                this.setupLinks(node);
                            }
                        });
                    }
                });
            });

            observer.observe(document.body, {
                childList: true,
                subtree: true
            });
        }

        /**
         * Configura listeners para HTMX
         */
        setupHTMXListeners() {
            // Quando HTMX carrega novo conteúdo
            document.body.addEventListener('htmx:afterSwap', (event) => {
                this.setupLinks(event.detail.target);
            });

            // Quando HTMX processa conteúdo
            document.body.addEventListener('htmx:afterSettle', (event) => {
                this.setupLinks(event.detail.target);
            });
        }

        /**
         * Força reconfiguração de todos os links
         */
        refresh() {
            // Remove marcadores de "ready"
            document.querySelectorAll('[data-external-ready]').forEach(el => {
                el.removeAttribute('data-external-ready');
            });
            
            // Reconfigura tudo
            this.setupLinks();
        }

        /**
         * Adiciona um link externo programaticamente
         */
        addLink(element, url) {
            if (!element) return;
            
            element.setAttribute('data-external-link', url);
            this.setupLinks(element.parentNode);
        }

        /**
         * Remove proteção de um elemento
         */
        removeLink(element) {
            if (!element) return;
            
            element.removeAttribute('data-external-link');
            element.removeAttribute('data-external-ready');
            
            this.options.clickableClasses.forEach(className => {
                element.classList.remove(className);
            });
        }
    }

    // Cria instância global
    window.ExternalLinksManager = ExternalLinksManager;

    // Inicializa automaticamente uma instância padrão
    window.externalLinks = new ExternalLinksManager({
        autoInit: true
    });

    // Atalhos globais (para compatibilidade)
    window.openExternalLink = (url) => window.externalLinks.openModal(url);
    window.closeExternalLinkModal = () => window.externalLinks.closeModal();
    window.refreshExternalLinks = () => window.externalLinks.refresh();

})();