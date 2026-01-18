document.addEventListener('DOMContentLoaded', function() {
    const contactForm = document.querySelector('#contact-form');
    if (contactForm) {
        contactForm.addEventListener('submit', function(e) {
            e.preventDefault(); // Prevenir o comportamento padrão (GET na URL)

            // Simular validação básica
            const nome = contactForm.querySelector('#nome').value.trim();
            const email = contactForm.querySelector('#email').value.trim();
            const assunto = contactForm.querySelector('#assunto').value;
            const mensagem = contactForm.querySelector('#mensagem').value.trim();

            // Mostrar toast de sucesso
            Toast.success('Mensagem enviada com sucesso! Responderemos em breve.');

            // Limpar formulário
            contactForm.reset();
        });
    }
});