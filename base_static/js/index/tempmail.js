/**
 * Tempmail - Integração Frontend-Backend
 * Gerencia a obtenção de e-mails, polling de mensagens e exibição.
 */

class TempMailApp {
    constructor() {
        this.currentEmail = null;
        this.pollingInterval = null;
        this.isRefreshing = false;
        this.isResetting = false; // Flag para bloquear polling durante reset/edição
        this.sessionTimer = null; // Timer da sessão
        this.sessionSecondsRemaining = 0;
        this.popoverActiveMobile = false; // Estado para controle de toque no mobile

        this.elements = {
            emailDisplay: document.getElementById('emailDisplay'),
            emailSkeleton: document.getElementById('emailSkeleton'),
            emailList: document.getElementById('email-list'),
            emptyState: document.getElementById('empty-state'),
            viewList: document.getElementById('view-list'),
            viewContent: document.getElementById('view-content'),
            msgAvatar: document.getElementById('msg-avatar'),
            msgNome: document.getElementById('msg-nome'),
            msgEmail: document.getElementById('msg-email'),
            msgData: document.getElementById('msg-data'),
            msgAssunto: document.getElementById('msg-assunto'),
            msgCorpo: document.getElementById('msg-corpo'),
            editModal: document.getElementById('editModal'),
            modalInputUser: document.getElementById('modalInputUser'),
            modalDomainLabel: document.getElementById('modalDomainLabel'),
            // Novos elementos para scroll logic
            messageScrollArea: document.getElementById('message-scroll-area'),
            headerTitle: document.getElementById('header-title'),
            emailContainer: document.getElementById('email-container'), // Container pai da lista
            // Elementos de Segurança e Anexos
            linkConfirmModal: document.getElementById('linkConfirmModal'),
            targetLinkDisplay: document.getElementById('targetLinkDisplay'),
            confirmLinkBtn: document.getElementById('confirmLinkBtn'),
            hiddenUrlWarning: document.getElementById('hiddenUrlWarning'),
            attachmentsSection: document.getElementById('attachments-section'),
            attachmentsList: document.getElementById('attachments-list'),
            attachmentsCount: document.getElementById('attachments-count'),
            downloadConfirmModal: document.getElementById('downloadConfirmModal'),
            downloadFileNameDisplay: document.getElementById('downloadFileNameDisplay'),
            confirmDownloadBtn: document.getElementById('confirmDownloadBtn'),
            btnHeaderAttachments: document.getElementById('btn-header-attachments'),
            headerAttachmentsBadge: document.getElementById('header-attachments-badge'),
            sessionCountdown: document.getElementById('session-countdown')
        };

        this.init();
    }

    async init() {
        this.setupScrollBehavior();
        await this.loadEmail();
        this.startPolling();
    }

    setupScrollBehavior() {
        if (this.elements.messageScrollArea && this.elements.headerTitle) {
            this.elements.messageScrollArea.addEventListener('scroll', () => {
                const headerTitle = this.elements.headerTitle;
                if (this.elements.messageScrollArea.scrollTop > 150) {
                    headerTitle.innerHTML = '<i class="fa-solid fa-chevron-up text-[12px] sm:text-[14px]"></i> <span class="hidden xs:inline">Voltar ao Topo</span><span class="xs:hidden">Topo</span>';
                    headerTitle.classList.remove('cursor-default', 'pointer-events-none');
                    headerTitle.classList.add('cursor-pointer', 'bg-orange-700/90', 'px-3', 'py-1', 'rounded-full');
                } else {
                    headerTitle.innerHTML = '<span class="hidden xs:inline">Leitura da Mensagem</span><span class="xs:hidden">Mensagem</span>';
                    headerTitle.classList.add('cursor-default', 'pointer-events-none');
                    headerTitle.classList.remove('cursor-pointer', 'bg-orange-700/40', 'bg-orange-700/90', 'px-3', 'py-1', 'rounded-full');
                }
            });
        }
    }

    scrollToTop() {
        if (this.elements.messageScrollArea) {
            this.elements.messageScrollArea.scrollTo({
                top: 0,
                behavior: 'smooth'
            });
        }
    }

    /**
     * Carrega o email da sessão ou cria um novo
     */
    async loadEmail() {
        this.showSkeleton();
        const { success, data } = await this._apiCall('/api/email/');

        if (success && data && data.success) {
            this.currentEmail = data.email;
            if (this.elements.emailDisplay) {
                this.elements.emailDisplay.value = data.email;
                this.hideSkeleton();
                if (window.updateQRCode) window.updateQRCode(data.email);
            }

            if (data.expires_in !== undefined) {
                this.startSessionCountdown(data.expires_in);
            }
        } else {
            // Falha silenciosa ou amigável
            this.hideSkeleton();
            if (data && data.error) Toast.error(data.error);
        }
    }

    showSkeleton() {
        if (this.elements.emailSkeleton) this.elements.emailSkeleton.classList.remove('hidden');
        if (this.elements.emailDisplay) this.elements.emailDisplay.classList.add('hidden');
    }

    hideSkeleton() {
        if (this.elements.emailSkeleton) this.elements.emailSkeleton.classList.add('hidden');
        if (this.elements.emailDisplay) this.elements.emailDisplay.classList.remove('hidden');
    }

    startPolling() {
        this.stopPolling();
        // Polling de 10 segundos
        this.pollingInterval = setInterval(() => this.refreshMessages(), 10000);
    }

    stopPolling() {
        if (this.pollingInterval) clearInterval(this.pollingInterval);
    }

    /**
     * Busca novas mensagens do servidor
     */
    async refreshMessages() {
        // PAUSA INTELIGENTE:
        // 1. Se estiver resetando (isResetting)
        // 2. Se a aba estiver oculta (document.hidden)
        // 3. Se estiver LENDO uma mensagem (view-list oculto)
        const isReadingMessage = this.elements.viewList && this.elements.viewList.classList.contains('hidden');

        if (this.isRefreshing || this.isResetting || document.hidden || isReadingMessage) {
            return;
        }

        this.isRefreshing = true;

        try {
            const response = await fetch('/api/messages/', {
                headers: {
                    'X-CSRFToken': this.getCsrfToken()
                }
            });
            const data = await response.json();

            if (data.success) {
                this.renderMessages(data.messages);
            } else if (data.error === 'Sessão não encontrada' || response.status === 400) {
                if (!this.isResetting) {
                    console.warn('Sessão perdida. Tentando recuperar...');
                    await this.loadEmail();
                }
            }
        } catch (error) {
            console.error('Erro ao atualizar mensagens:', error);
        } finally {
            this.isRefreshing = false;
        }
    }

    /**
     * Renderiza a lista de mensagens usando a estrutura avançada com Popovers e Eventos
     */
    renderMessages(messages) {
        if (!this.elements.emailList) return;

        // Limpa a lista atual
        this.elements.emailList.innerHTML = '';

        if (!messages || messages.length === 0) {
            if (this.elements.emptyState) this.elements.emptyState.classList.remove('hidden');
            if (this.elements.emptyState) this.elements.emptyState.style.display = 'flex';
            return;
        }

        if (this.elements.emptyState) this.elements.emptyState.classList.add('hidden');
        if (this.elements.emptyState) this.elements.emptyState.style.display = 'none';

        messages.forEach(msg => {
            const newEmail = document.createElement('div');

            // Dados para display
            const nomeCompleto = this.escapeHtml(msg.from_name || msg.from_address);
            const emailCompleto = this.escapeHtml(msg.from_address);
            const assuntoCompleto = this.escapeHtml(msg.subject || '(Sem assunto)');

            // Estilos condicionais para lido/não lido
            const isReadClass = msg.is_read ? 'opacity-60 bg-gray-50 dark:bg-gray-800/40' : 'bg-white dark:bg-dark-card font-semibold border-l-4 border-l-brand-orange';
            const textClass = msg.is_read ? 'text-gray-600 dark:text-gray-400 font-normal' : 'text-gray-900 dark:text-white font-bold';
            const subjectClass = msg.is_read ? 'text-gray-600 dark:text-gray-400 font-normal' : 'text-gray-900 dark:text-white font-bold';

            newEmail.className = `px-4 py-4 hover:bg-gray-50 dark:hover:bg-gray-800/60 transition-all cursor-pointer group relative hover:z-50 border-b border-gray-100 dark:border-gray-700 ${isReadClass}`;
            newEmail.dataset.messageId = msg.id;

            newEmail.innerHTML = `
                <div class="flex items-center gap-3 w-full">
                    <div class="flex-none w-[35%] min-w-0 relative">
                        <p class="${textClass} text-[11px] sm:text-sm truncate leading-tight group-hover:underline decoration-gray-900 dark:decoration-white underline-offset-2">
                            ${nomeCompleto}
                        </p>
                        <p class="font-bold text-[9px] sm:text-xs text-gray-800 dark:text-gray-100 truncate group-hover:underline decoration-gray-600 dark:decoration-gray-300 underline-offset-2">
                            ${emailCompleto}
                        </p>

                        <div class="js-popover absolute invisible opacity-0 transition-all duration-200 bg-white dark:bg-gray-800 text-gray-900 dark:text-white p-3 rounded-lg shadow-2xl left-0 whitespace-nowrap pointer-events-none border border-gray-200 dark:border-gray-700 min-w-[280px] z-[100]">
                            <div class="flex flex-col gap-2">
                                <div class="flex flex-col">
                                    <span class="text-[12px] font-bold text-orange-600 dark:text-orange-400 uppercase tracking-tighter">Remetente:</span>
                                    <span class="text-xs font-bold">${nomeCompleto}</span>
                                    <span class="text-[13px] font-bold text-gray-900 dark:text-gray-200 break-all whitespace-normal leading-tight">
                                        ${emailCompleto}
                                    </span>
                                </div>
                                <div class="h-[1px] bg-gray-200 dark:bg-gray-700 w-full my-1"></div>
                                <div class="flex flex-col">
                                    <span class="text-[12px] font-bold text-orange-600 dark:text-orange-400 uppercase tracking-tighter">Assunto:</span>
                                    <span class="text-[13px] font-bold leading-relaxed text-gray-900 dark:text-gray-200 whitespace-normal max-w-[260px] break-words">
                                        ${assuntoCompleto}
                                    </span>
                                </div>
                            </div>
                            <div class="js-arrow absolute left-4 w-2 h-2 bg-white dark:bg-gray-800 rotate-45 border-gray-200 dark:border-gray-700"></div>
                        </div>
                    </div>

                    <div class="flex-1 min-w-0">
                        <p class="text-[12px] sm:text-sm font-bold text-gray-900 dark:text-white truncate">
                            ${assuntoCompleto}
                        </p>
                    </div>

                    <div class="flex-none text-right text-gray-300 group-hover:text-orange-600 transition-colors">
                        <i class="fa-solid fa-chevron-right text-[10px]"></i>
                    </div>
                </div>
            `;

            // --- LÓGICA DE POSICIONAMENTO POPOVER ---
            const updatePopoverPosition = () => {
                const popover = newEmail.querySelector('.js-popover');
                const arrow = newEmail.querySelector('.js-arrow');
                const container = this.elements.emailContainer || document.body;

                if (!popover || !arrow) return;

                popover.style.transition = 'none';
                popover.classList.remove('invisible', 'opacity-0');

                const rect = newEmail.getBoundingClientRect();
                const containerRect = container.getBoundingClientRect();
                const popoverHeight = popover.offsetHeight;
                const spaceBelow = containerRect.bottom - rect.bottom;

                if (spaceBelow < (popoverHeight + 15)) {
                    popover.style.top = 'auto';
                    popover.style.bottom = '120%';
                    arrow.style.top = 'auto';
                    arrow.style.bottom = '-5px';
                    arrow.className = "js-arrow absolute left-4 w-2 h-2 bg-white dark:bg-gray-800 rotate-45 border-r border-b border-gray-200 dark:border-gray-700";
                } else {
                    popover.style.bottom = 'auto';
                    popover.style.top = '120%';
                    arrow.style.bottom = 'auto';
                    arrow.style.top = '-5px';
                    arrow.className = "js-arrow absolute left-4 w-2 h-2 bg-white dark:bg-gray-800 rotate-45 border-l border-t border-gray-200 dark:border-gray-700";
                }

                // Force reflow
                popover.offsetHeight;
                popover.style.transition = '';
            };

            const hidePopover = () => {
                const popover = newEmail.querySelector('.js-popover');
                if (popover) popover.classList.add('invisible', 'opacity-0');
                this.popoverActiveMobile = false;
            };

            // --- EVENTOS DESKTOP ---
            newEmail.addEventListener('mouseenter', updatePopoverPosition);
            newEmail.addEventListener('mouseleave', hidePopover);

            // --- EVENTOS MOBILE (TOQUE LONGO) ---
            let touchTimer;

            newEmail.addEventListener('touchstart', (e) => {
                this.popoverActiveMobile = false;
                touchTimer = setTimeout(() => {
                    this.popoverActiveMobile = true;
                    updatePopoverPosition();
                    if (window.navigator.vibrate) window.navigator.vibrate(50);
                }, 600);
            }, { passive: true });

            newEmail.addEventListener('touchend', (e) => {
                clearTimeout(touchTimer);
                if (this.popoverActiveMobile) {
                    setTimeout(() => hidePopover(), 3000);
                    // Prevent default click if it was a long press
                    e.preventDefault();
                }
            });

            newEmail.addEventListener('touchmove', () => {
                clearTimeout(touchTimer);
            }, { passive: true });

            // --- EVENTO DE CLIQUE (ABRIR EMAIL) ---
            newEmail.addEventListener('click', (e) => {
                if (!this.popoverActiveMobile) {
                    this.viewMessage(msg.id);
                }
            });

            this.elements.emailList.appendChild(newEmail);
        });
    }

    /**
     * Abre uma mensagem específica
     */
    async viewMessage(messageId) {
        this.currentMessageId = messageId;
        try {
            const response = await fetch(`/api/messages/${messageId}/`, {
                headers: {
                    'X-CSRFToken': this.getCsrfToken()
                }
            });
            const data = await response.json();

            if (data.success) {
                const msg = data.message;

                // ATUALIZAÇÃO VISUAL IMEDIATA: Marcar como lido na lista DOM
                const listItem = document.querySelector(`div[data-message-id="${messageId}"]`);
                if (listItem) {
                    listItem.classList.remove('bg-white', 'dark:bg-dark-card', 'font-semibold', 'border-l-4', 'border-l-brand-orange');
                    listItem.classList.add('opacity-60', 'bg-gray-50', 'dark:bg-gray-800/40');

                    // Atualizar textos internos para cinza/normal
                    const textElements = listItem.querySelectorAll('p.font-bold, p.text-gray-900, p.dark\\:text-white');
                    textElements.forEach(el => {
                        el.classList.remove('font-bold', 'text-gray-900', 'dark:text-white', 'text-gray-800');
                        el.classList.add('font-normal', 'text-gray-600', 'dark:text-gray-400');
                    });
                }

                const dados = {
                    nome: msg.from_name || msg.from_address || 'Sem nome',
                    email: msg.from_address,
                    assunto: msg.subject || '(Sem assunto)',
                    corpo: msg.text || '(Mensagem sem conteúdo)',
                    html: msg.html,
                    data_recebimento: msg.received_at
                };

                // Lógica de UI (baseada na solicitação do user "openEmail")
                if (!this.elements.viewList || !this.elements.viewContent) {
                    console.error("ERRO: Containers de view não encontrados");
                    return;
                }

                // Troca as telas
                this.elements.viewList.classList.add('hidden');
                this.elements.viewContent.classList.remove('hidden');

                // Preenche os dados
                if (this.elements.msgNome) this.elements.msgNome.textContent = dados.nome;
                if (this.elements.msgEmail) this.elements.msgEmail.textContent = dados.email;
                if (this.elements.msgAssunto) this.elements.msgAssunto.textContent = dados.assunto;

                if (this.elements.msgCorpo) {
                    if (dados.html) {
                        // SANITIZAÇÃO CRÍTICA (Antra-XSS): 
                        // Impede execução de JS malicioso dentro do e-mail
                        this.elements.msgCorpo.innerHTML = DOMPurify.sanitize(dados.html);
                    } else {
                        this.elements.msgCorpo.textContent = dados.corpo;
                    }
                }

                // Reset do scroll
                if (this.elements.messageScrollArea) this.elements.messageScrollArea.scrollTop = 0;

                // Avatar
                if (this.elements.msgAvatar) {
                    this.elements.msgAvatar.textContent = dados.nome.charAt(0).toUpperCase();
                }

                // Data
                if (this.elements.msgData && dados.data_recebimento) {
                    const date = new Date(dados.data_recebimento);
                    this.elements.msgData.textContent = date.toLocaleString('pt-BR');
                }

                // PROCESSAMENTO DE SEGURANÇA: Links
                this.processLinks();

                // EXIBIÇÃO DE ANEXOS
                this.renderAttachments(msg.attachments, messageId);
            }
        } catch (error) {
            console.error('Erro ao abrir mensagem:', error);
            Toast.error('Não foi possível abrir a mensagem.');
        }
    }

    /**
     * Processa links dentro do corpo da mensagem para segurança
     */
    processLinks() {
        if (!this.elements.msgCorpo) return;

        const links = this.elements.msgCorpo.querySelectorAll('a');
        links.forEach(link => {
            const href = link.getAttribute('href');
            if (!href || href.startsWith('#')) return;

            const visibleText = link.textContent.trim();
            const isHiddenUrl = this.detectHiddenUrl(visibleText, href);

            // Estilizar links para indicar que são clicáveis/especiais
            link.classList.add('text-brand-orange', 'underline', 'hover:text-brand-dark');

            if (isHiddenUrl) {
                link.classList.add('bg-red-50', 'dark:bg-red-900/20', 'px-1', 'rounded', 'border', 'border-red-200', 'dark:border-red-800');
                link.title = "⚠️ Aviso: O texto do link diverge do destino real";
            }

            // Interceptar clique
            link.addEventListener('click', (e) => {
                e.preventDefault();
                this.showLinkConfirmModal(href, isHiddenUrl);
            });
        });
    }

    /**
     * Detecta se o texto visível parece uma URL que diverge do href real
     */
    detectHiddenUrl(text, href) {
        // Se o texto não se parece com uma URL (não tem dot nem slash), ignora
        if (!text.includes('.') && !text.includes('/')) return false;

        try {
            // Tenta normalizar para comparar domínios
            const hrefUrl = new URL(href);

            // Regex simples para capturar algo que pareça domínio no texto
            const domainRegex = /([a-z0-9|-]+\.)+[a-z]{2,}/i;
            const match = text.match(domainRegex);

            if (match) {
                const textDomain = match[0].toLowerCase();
                const actualDomain = hrefUrl.hostname.toLowerCase();

                // Se o domínio no texto for diferente do domínio real -> Alerta
                if (textDomain !== actualDomain && !actualDomain.endsWith('.' + textDomain)) {
                    return true;
                }
            }
        } catch (e) {
            // Se falhar no parse da URL, faz comparação de string bruta se houver divergência óbvia
            if (text.startsWith('http') && !href.startsWith(text.substring(0, 15))) {
                return true;
            }
        }
        return false;
    }

    /**
     * Mostra o modal de confirmação de link
     */
    showLinkConfirmModal(url, isHidden) {
        if (!this.elements.linkConfirmModal) return;

        if (this.elements.targetLinkDisplay) this.elements.targetLinkDisplay.textContent = url;
        if (this.elements.confirmLinkBtn) this.elements.confirmLinkBtn.href = url;

        if (this.elements.hiddenUrlWarning) {
            if (isHidden) {
                this.elements.hiddenUrlWarning.classList.remove('hidden');
            } else {
                this.elements.hiddenUrlWarning.classList.add('hidden');
            }
        }

        this.elements.linkConfirmModal.showModal();
    }

    /**
     * Renderiza a lista de anexos
     */
    renderAttachments(attachments, messageId) {
        if (!this.elements.attachmentsSection || !this.elements.attachmentsList) return;

        if (!attachments || attachments.length === 0) {
            this.elements.attachmentsSection.classList.add('hidden');
            return;
        }

        this.elements.attachmentsSection.classList.remove('hidden');
        this.elements.attachmentsList.innerHTML = '';
        if (this.elements.attachmentsCount) {
            this.elements.attachmentsCount.textContent = attachments.length;
        }

        attachments.forEach(att => {
            const item = document.createElement('div');
            item.className = 'flex items-center justify-between p-3 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg group hover:border-primary transition-colors';

            const icon = this.getAttachmentIcon(att.contentType);
            const size = this.formatSize(att.size);

            item.innerHTML = `
                <div class="flex items-center space-x-3">
                    <div class="p-2 bg-gray-100 dark:bg-gray-700 rounded text-gray-500">
                        ${icon}
                    </div>
                    <div>
                        <p class="text-sm font-medium text-gray-900 dark:text-gray-100 truncate max-w-[150px]" title="${att.filename}">
                            ${att.filename}
                        </p>
                        <p class="text-xs text-gray-500">${size}</p>
                    </div>
                </div>
                <button onclick="window.app.showDownloadModal({type: 'attachment', messageId: ${messageId}, attachmentId: '${att.id}', filename: '${att.filename.replace(/'/g, "\\'")}'})" 
                        class="btn btn-ghost btn-sm text-primary">
                    <i class="fas fa-download mr-1"></i> Baixar
                </button>
            `;
            this.elements.attachmentsList.appendChild(item);
        });

        // ATUALIZAÇÃO DO HEADER: Mostrar botão de anexo no topo se houver anexos
        this.showHeaderAttachmentBtn(attachments.length);
    }

    /**
     * Mostra ou oculta o botão de atalho de anexos no header
     */
    showHeaderAttachmentBtn(count) {
        if (!this.elements.btnHeaderAttachments || !this.elements.headerAttachmentsBadge) return;

        if (count > 0) {
            this.elements.btnHeaderAttachments.classList.remove('hidden');
            this.elements.btnHeaderAttachments.classList.add('flex');
            this.elements.headerAttachmentsBadge.textContent = count;
        } else {
            this.elements.btnHeaderAttachments.classList.add('hidden');
            this.elements.btnHeaderAttachments.classList.remove('flex');
        }
    }

    /**
     * Rola a visualização até a seção de anexos
     */
    scrollToAttachments() {
        if (this.elements.attachmentsSection && this.elements.messageScrollArea) {
            // Pequeno delay para garantir que a renderização do DOM está ok
            setTimeout(() => {
                this.elements.attachmentsSection.scrollIntoView({
                    behavior: 'smooth',
                    block: 'start'
                });
            }, 50);
        }
    }

    /**
     * Exibe o modal de confirmação de download
     */
    showDownloadModal(options) {
        if (!this.elements.downloadConfirmModal || !this.elements.downloadFileNameDisplay || !this.elements.confirmDownloadBtn) return;

        this.elements.downloadFileNameDisplay.textContent = options.filename;

        // Configurar o botão de confirmação
        this.elements.confirmDownloadBtn.onclick = () => {
            this.elements.downloadConfirmModal.close();
            if (options.type === 'attachment') {
                this.downloadAttachment(options.messageId, options.attachmentId, options.filename);
            } else if (options.type === 'message') {
                this.executeDownloadMessage();
            }
        };

        this.elements.downloadConfirmModal.showModal();
    }

    async downloadAttachment(messageId, attachmentId, filename) {
        if (!messageId || !attachmentId) return;

        Toast.info('Iniciando download do anexo...', 2000);

        try {
            const response = await fetch(`/api/messages/${messageId}/attachments/${attachmentId}/download/`, {
                headers: {
                    'X-CSRFToken': this.getCsrfToken()
                }
            });

            if (!response.ok) {
                const errorData = await response.text();
                throw new Error(errorData || 'Erro ao baixar anexo');
            }

            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            Toast.success('Download concluído!');
        } catch (error) {
            console.error('Download error:', error);
            Toast.error(`Erro no download: ${error.message}`);
        }
    }

    getAttachmentIcon(type) {
        if (!type) return '<i class="fa-solid fa-file"></i>';
        const t = type.toLowerCase();
        if (t.includes('pdf')) return '<i class="fa-solid fa-file-pdf text-red-500"></i>';
        if (t.includes('image') || t.includes('png') || t.includes('jpg')) return '<i class="fa-solid fa-file-image text-blue-500"></i>';
        if (t.includes('zip') || t.includes('rar')) return '<i class="fa-solid fa-file-zipper text-yellow-600"></i>';
        if (t.includes('doc') || t.includes('word')) return '<i class="fa-solid fa-file-word text-blue-600"></i>';
        if (t.includes('xls') || t.includes('sheet')) return '<i class="fa-solid fa-file-excel text-green-600"></i>';
        return '<i class="fa-solid fa-file"></i>';
    }

    formatSize(bytes) {
        if (!bytes) return "0 B";
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
    }

    /**
     * Inicia o processo de download da mensagem (com confirmação)
     */
    async downloadMessage() {
        if (!this.currentMessageId) {
            Toast.warning('Abra uma mensagem primeiro para baixar.');
            return;
        }

        // Primeiro mostramos o modal de confirmação
        this.showDownloadModal({
            type: 'message',
            filename: `mensagem_${this.currentMessageId}.eml`
        });
    }

    /**
     * Executa o download real da mensagem após confirmação
     */
    async executeDownloadMessage() {
        try {
            Toast.info('Preparando download...', 2000);

            const response = await fetch(`/api/messages/${this.currentMessageId}/download/`, {
                headers: {
                    'X-CSRFToken': this.getCsrfToken()
                }
            });

            if (!response.ok) {
                const errorText = await response.text();
                throw new Error(errorText || `Erro ${response.status}`);
            }

            const blob = await response.blob();
            if (blob.size === 0) {
                throw new Error('Arquivo vazio retornado pelo servidor.');
            }

            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;

            // Tentar extrair filename do Content-Disposition
            const cd = response.headers.get('Content-Disposition');
            let filename = `mensagem_${this.currentMessageId}.eml`;
            if (cd && cd.includes('filename=')) {
                filename = cd.split('filename=')[1].split(';')[0].replace(/["']/g, '').trim();
            }

            a.download = filename;
            document.body.appendChild(a);
            a.click();

            setTimeout(() => {
                window.URL.revokeObjectURL(url);
                document.body.removeChild(a);
            }, 100);

            Toast.success('Download pronto!');
        } catch (error) {
            console.error('Download failed:', error);
            Toast.error(`Erro ao baixar: ${error.message}`);
        }
    }


    async resetSession() {
        const modal = document.getElementById('confirmResetModal');
        if (modal) {
            modal.showModal();
        } else {
            // Fallback se o modal não existir por algum motivo
            if (confirm('Deseja gerar um novo e-mail? Todas as mensagens atuais serão perdidas.')) {
                this.confirmResetEmail();
            }
        }
    }

    async confirmResetEmail() {
        const modal = document.getElementById('confirmResetModal');
        if (modal) modal.close();

        if (this.isResetting) return;
        this.isResetting = true;
        this.stopPolling(); // <--- CRITICAL: Stop polling explicitly

        // Bloqueio visual e Feedback (User Request)
        Toast.warning('Deletando e-mail atual, por favor aguarde...', 3000);

        // Limpar display IMEDIATAMENTE antes do fetch
        if (this.elements.emailDisplay) this.elements.emailDisplay.value = '';
        this.backToList(); // <--- Voltar para a lista se estiver lendo uma mensagem
        this.showSkeleton();

        this.showSkeleton();

        const { success, data, status } = await this._apiCall('/api/email/', {
            method: 'POST',
            headers: { 'X-CSRFToken': this.getCsrfToken() }
        });

        if (success && data && data.success) {
            this.currentEmail = data.email;
            if (this.elements.emailDisplay) this.elements.emailDisplay.value = data.email;
            this.clearMessageList();
            if (window.generateQRCode && data.email) window.generateQRCode(data.email);

            Toast.success('Novo e-mail gerado com sucesso!');
            if (data.expires_in !== undefined) this.startSessionCountdown(data.expires_in);
        } else {
            const errorMsg = data?.error || (status === 403 ? 'Sessão expirada. Recarregue a página.' : 'Erro ao resetar email.');
            Toast.error(errorMsg);
        }

        this.hideSkeleton();
        this.isResetting = false;
        this.startPolling();
    }

    async syncInbox(btn) {
        if (this.isSyncing) return;
        this.isSyncing = true;

        // Encontrar o ícone dentro do botão
        const icon = btn.querySelector('i');
        if (icon) {
            icon.classList.add('fa-spin', 'text-brand-orange', 'dark:text-orange-400');
        }

        try {
            await this.refreshMessages();
            Toast.success('Caixa de entrada atualizada!');
            // Manter girando e laranja por pelo menos 2 segundos
            await new Promise(resolve => setTimeout(resolve, 2000));
        } catch (error) {
            console.error('Erro na sincronização manual:', error);
            Toast.error('Erro ao atualizar mensagens.');
        } finally {
            if (icon) {
                icon.classList.remove('fa-spin', 'text-brand-orange', 'dark:text-orange-400');
            }
            this.isSyncing = false;
        }
    }

    // ==================== MODAL EDIT LOGIC ====================

    openEditModal() {
        if (!this.elements.editModal) return;

        // Extrair domínio do email atual
        if (this.currentEmail && this.currentEmail.includes('@')) {
            const domain = this.currentEmail.split('@')[1];
            if (this.elements.modalDomainLabel) {
                this.elements.modalDomainLabel.textContent = `@${domain}`;
            }
        }

        const modalContent = this.elements.editModal.querySelector('div > div');
        this.elements.editModal.classList.remove('hidden');
        setTimeout(() => {
            this.elements.editModal.classList.add('opacity-100');
            if (modalContent) {
                modalContent.classList.remove('scale-95');
                modalContent.classList.add('scale-100');
            }
        }, 10);
    }

    closeEditModal() {
        if (!this.elements.editModal) return;

        const modalContent = this.elements.editModal.querySelector('div > div');
        this.elements.editModal.classList.remove('opacity-100');
        if (modalContent) {
            modalContent.classList.remove('scale-100');
            modalContent.classList.add('scale-95');
        }

        setTimeout(() => {
            this.elements.editModal.classList.add('hidden');
        }, 300);
    }

    async saveEditModal() {
        const usernameInput = this.elements.modalInputUser;
        if (!usernameInput) return;

        const username = usernameInput.value.trim();
        if (!username) {
            Toast.warning('Por favor, digite um nome de usuário.');
            return;
        }

        const domain = this.elements.modalDomainLabel.textContent.replace('@', '');
        const fullEmail = `${username}@${domain}`;

        if (fullEmail === this.currentEmail) {
            Toast.info('Você já está usando este endereço de e-mail.');
            this.closeEditModal();
            return;
        }

        if (this.isResetting) return;
        this.isResetting = true;
        this.stopPolling();

        const { success, data, status } = await this._apiCall('/api/email/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': this.getCsrfToken()
            },
            body: JSON.stringify({ email: fullEmail })
        });

        if (success && data && data.success) {
            this.currentEmail = data.email;
            if (this.elements.emailDisplay) this.elements.emailDisplay.value = data.email;
            this.clearMessageList();
            if (window.generateQRCode && data.email) window.generateQRCode(data.email);

            Toast.success('E-mail alterado com sucesso!');
            if (data.expires_in !== undefined) this.startSessionCountdown(data.expires_in);
        } else {
            const errorMsg = data?.error || (status === 403 ? 'Sessão inválida ou expirada.' : 'Erro ao alterar email.');
            Toast.error(errorMsg);
        }

        this.hideSkeleton();
        this.isResetting = false;
        this.startPolling();
    }

    // Helper para limpar lista
    clearMessageList() {
        if (this.elements.emailList) {
            this.elements.emailList.innerHTML = '';
        }
        if (this.elements.emptyState) {
            this.elements.emptyState.classList.remove('hidden');
            this.elements.emptyState.style.display = 'flex';
        }
    }

    escapeHtml(text) {
        if (!text) return "";
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    showError(msg) {
        console.error(msg);
        // Opcional: implementar um toast
    }

    backToList() {
        if (this.elements.viewContent) this.elements.viewContent.classList.add('hidden');
        if (this.elements.viewList) this.elements.viewList.classList.remove('hidden');
    }

    getCsrfToken() {
        const name = 'csrftoken';
        let cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            const cookies = document.cookie.split(';');
            for (let i = 0; i < cookies.length; i++) {
                const cookie = cookies[i].trim();
                // Does this cookie string begin with the name we want?
                if (cookie.substring(0, name.length + 1) === (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    /**
     * Inicia o contador decrescente da sessão
     */
    startSessionCountdown(seconds) {
        if (this.sessionTimer) clearInterval(this.sessionTimer);

        this.sessionSecondsRemaining = seconds;
        this.updateCountdownUI();

        this.sessionTimer = setInterval(() => {
            this.sessionSecondsRemaining--;

            if (this.sessionSecondsRemaining <= 0) {
                clearInterval(this.sessionTimer);
                this.updateCountdownUI();
                return;
            }

            this.updateCountdownUI();
        }, 1000);
    }

    updateCountdownUI() {
        if (!this.elements.sessionCountdown) return;

        if (this.sessionSecondsRemaining <= 0) {
            this.elements.sessionCountdown.textContent = 'Expirado';
            this.elements.sessionCountdown.classList.add('text-red-500');
            return;
        }

        const minutes = Math.floor(this.sessionSecondsRemaining / 60);
        const seconds = this.sessionSecondsRemaining % 60;

        // Formato: 59:03 ou 5 minutos
        if (minutes > 0) {
            this.elements.sessionCountdown.textContent = `${minutes} min e ${seconds.toString().padStart(2, '0')}s`;
        } else {
            this.elements.sessionCountdown.textContent = `${seconds} segundos`;
        }
    }

    /**
     * Helper para requisições seguras e silenciosas (Security Hardening)
     */
    async _apiCall(url, options = {}) {
        try {
            const response = await fetch(url, options);

            // Verificamos se a resposta é do tipo JSON antes de tentar o parse
            const contentType = response.headers.get('content-type');
            let data = null;

            if (contentType && contentType.includes('application/json')) {
                data = await response.json();
            }

            if (!response.ok) {
                // Em caso de erro, não logamos o objeto de erro inteiro no console (Segurança)
                return { success: false, status: response.status, data };
            }

            return { success: true, status: response.status, data };
        } catch (error) {
            // Log silencioso apenas para depuração interna se necessário
            return { success: false, error: 'connection_error' };
        }
    }
}

// Inicialização Global
document.addEventListener('DOMContentLoaded', () => {
    window.app = new TempMailApp();
    // Alias para compatibilidade se necessário
    window.tempMailApp = window.app;
});

// Funções globais chamadas pelo HTML
window.syncInbox = (btn) => window.app.syncInbox(btn);
window.resetSession = () => window.app.resetSession();
window.confirmResetEmail = () => window.app.confirmResetEmail();
window.backToList = () => window.app.backToList();
window.openEditModal = () => window.app.openEditModal();
window.closeEditModal = () => window.app.closeEditModal();
window.saveEditModal = () => window.app.saveEditModal();
window.scrollToTop = () => window.app.scrollToTop();
window.scrollToAttachments = () => window.app.scrollToAttachments();
window.downloadMessage = () => window.app.downloadMessage();
window.downloadAttachment = (mId, aId, fn) => window.app.downloadAttachment(mId, aId, fn);
