function toggleTheme() {
    const html = document.documentElement;
    
    // Alterna a classe dark
    const isDark = html.classList.toggle('dark');
    
    // Sincroniza com DaisyUI (importante para componentes do framework)
    html.setAttribute('data-theme', isDark ? 'dark' : 'light');
    
    // Salva a preferência
    localStorage.setItem('theme', isDark ? 'dark' : 'light');
}

