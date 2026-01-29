function openClearCacheModal() {
    document.getElementById('clearCacheModal').showModal();
}

function closeClearCacheModal() {
    document.getElementById('clearCacheModal').close();
}

async function confirmClearCache() {
    try {
        const response = await fetch('/clear-domain-cache/', {
            method: 'POST',
            headers: {
                'X-CSRFToken': getCsrfToken()
            }
        });
        
        const data = await response.json();
        
        if (data.success) {
            Toast.success(gettext(data.message));
            closeClearCacheModal();
        } else {
            Toast.error(data.error || gettext('Erro ao limpar cache'));
        }
    } catch (error) {
        Toast.error(gettext('Erro de conexão'));
    }
}

function getCsrfToken() {
    const name = 'csrftoken';
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}