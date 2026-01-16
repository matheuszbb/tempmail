// No seu arquivo JS
function scrollToTop(el) {
    // Se passar o elemento, ele sobe o pai com scroll. 
    // Se não passar nada, sobe a página inteira.
    const target = el ? el.closest('.overflow-y-auto') : window;
    target.scrollTo({
        top: 0,
        behavior: 'smooth'
    });
}