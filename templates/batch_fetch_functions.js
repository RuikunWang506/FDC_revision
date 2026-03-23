// Batch Fetch Functions for Master Image Viewer

let batchTaskId = null;
let batchTaskStartTime = null;
let batchTaskInterval = null;

function openBatchFetchModal() {
    document.getElementById('batchFetchModal').style.display = 'flex';
    document.getElementById('skuListInput').focus();
}

function closeBatchFetchModal() {
    document.getElementById('batchFetchModal').style.display = 'none';
    document.getElementById('skuListInput').value = '';
}

function openBatchStatusModal() {
    document.getElementById('batchStatusModal').style.display = 'flex';
    batchTaskStartTime = Date.now();
}

function closeBatchStatusModal() {
    document.getElementById('batchStatusModal').style.display = 'none';
    if (batchTaskInterval) {
        clearInterval(batchTaskInterval);
        batchTaskInterval = null;
    }
}

function updateElapsedTime() {
    if (!batchTaskStartTime) return;
    const elapsed = Math.floor((Date.now() - batchTaskStartTime) / 1000);
    const minutes = Math.floor(elapsed / 60);
    const seconds = elapsed % 60;
    document.getElementById('elapsedTime').textContent = `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

function updateBatchProgress(status) {
    updateElapsedTime();
    
    const total = status.total || 0;
    const completed = status.completed || 0;
    const percent = total > 0 ? (completed / total) * 100 : 0;
    
    document.getElementById('progressBar').style.width = percent + '%';
    document.getElementById('progressCount').textContent = `${completed}/${total}`;
    
    const resultsList = document.getElementById('batchResultsList');
    let html = '';
    
    if (status.results && Array.isArray(status.results)) {
        status.results.forEach(result => {
            const statusColor = result.status === 'completed' ? '#1cc88a' : result.status === 'failed' ? '#dc3545' : '#f8b500';
            const statusIcon = result.status === 'completed' ? 'check-circle' : result.status === 'failed' ? 'exclamation-circle' : 'spinner fa-spin';
            html += `
                <div style="display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0; border-bottom: 1px solid #e5e7eb;">
                    <i class="fas fa-${statusIcon}" style="color: ${statusColor}; font-size: 1rem;"></i>
                    <div style="flex: 1;">
                        <div style="font-weight: 600; color: #1f2937;">${result.sku}</div>
                        <div style="font-size: 0.85rem; color: #666;">${result.count || 0} images</div>
                    </div>
                    <div style="font-size: 0.85rem; color: ${statusColor};">${result.status || 'pending'}</div>
                </div>
            `;
        });
    }
    
    resultsList.innerHTML = html;
}

async function submitBatchFetch() {
    const input = document.getElementById('skuListInput').value.trim();
    if (!input) {
        alert('Please enter at least one SKU');
        return;
    }
    
    const skus = input.split(/[,\n\t]+/)
        .map(s => s.trim().toUpperCase())
        .filter(s => s.length > 0);
    
    if (skus.length === 0) {
        alert('No valid SKUs found');
        return;
    }
    
    closeBatchFetchModal();
    openBatchStatusModal();
    
    try {
        const response = await fetch('/api/batch_fetch_china_ftp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ skus: skus })
        });
        
        if (!response.ok) {
            alert('Failed to start batch fetch');
            closeBatchStatusModal();
            return;
        }
        
        const data = await response.json();
        batchTaskId = data.task_id;
        
        if (batchTaskInterval) clearInterval(batchTaskInterval);
        batchTaskInterval = setInterval(async () => {
            try {
                const statusResponse = await fetch(`/api/batch_fetch_status?task_id=${batchTaskId}`);
                if (!statusResponse.ok) return;
                const status = await statusResponse.json();
                
                updateBatchProgress(status);
                
                if (status.status === 'completed') {
                    clearInterval(batchTaskInterval);
                    batchTaskInterval = null;
                }
            } catch (err) {
                console.error('Error polling batch status:', err);
            }
        }, 1000);
        
    } catch (error) {
        alert('Failed to initiate batch fetch: ' + error.message);
        closeBatchStatusModal();
    }
}
