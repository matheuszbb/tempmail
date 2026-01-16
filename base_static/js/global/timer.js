document.addEventListener("DOMContentLoaded", () => {
    // Busca todos os elementos que devem ser timers
    const timers = document.querySelectorAll('.js-timer');

    timers.forEach(timerElement => {
        // Pega o valor inicial do atributo 'data-time', ou assume 10 como padrão
        let timeLeft = parseInt(timerElement.getAttribute('data-time')) || 10;
        const resetValue = timeLeft; // Salva o valor inicial para o reset

        const interval = setInterval(() => {
            timeLeft--;
            
            if (timeLeft < 0) {
                timeLeft = resetValue; // Reseta para o valor inicial individual
            }

            // Atualiza apenas o texto deste elemento específico
            timerElement.innerText = timeLeft + 's';
        }, 1000);
    });
});