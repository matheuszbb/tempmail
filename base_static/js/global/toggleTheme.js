function toggleTheme() {
    const html = document.documentElement;

    // Desativa transições para evitar glitch visual de 1s
    html.classList.add('no-transitions');

    // Alterna a classe dark
    const isDark = html.classList.toggle('dark');

    // Sincroniza com DaisyUI
    html.setAttribute('data-theme', isDark ? 'dark' : 'light');

    // Salva a preferência
    localStorage.setItem('theme', isDark ? 'dark' : 'light');

    // Força o reflow para garantir que a mudança de classe foi aplicada sem transições
    window.getComputedStyle(html).opacity;

    // Remove a trava de transições no próximo frame
    requestAnimationFrame(() => {
        html.classList.remove('no-transitions');
    });
}

