/**
 * Receipt Scanner - Frontend Application
 * Handles file upload, OCR processing, review/edit, and Excel export.
 */

// ─── State ───────────────────────────────────────────────────────────────────
// ─── Batch System ────────────────────────────────────────────────────────────
const MAX_BATCH_SIZE = 50;

/** Load batches from localStorage, migrating old single-batch format if needed */
function _loadBatches() {
    try {
        const saved = localStorage.getItem('scannerBatches');
        if (saved) return JSON.parse(saved);
        // Migrate from old flat batchReceiptIds format
        const oldIds = localStorage.getItem('batchReceiptIds');
        if (oldIds) {
            const ids = JSON.parse(oldIds);
            if (ids.length > 0) {
                const batch = { id: 'batch_' + Date.now(), name: 'Batch 1', receiptIds: ids, created: new Date().toISOString().split('T')[0] };
                localStorage.removeItem('batchReceiptIds');
                return [batch];
            }
            localStorage.removeItem('batchReceiptIds');
        }
    } catch (e) { /* corrupted — start fresh */ }
    return [];
}

const state = {
    currentTab: 'scan',
    batches: _loadBatches(),
    activeBatchId: localStorage.getItem('activeBatchId') || null,
    currentReceiptData: null,
    editingProduct: null,
    catalogCache: {},               // code → { name, unit, category }
    isProcessing: false,            // prevent double uploads
    progressInterval: null,         // track progress simulation
    isDirty: false,                 // unsaved edits exist
    confirmed: false,               // receipt has been confirmed
};

// Compatibility: code using state.batchReceiptIds auto-redirects to active batch
Object.defineProperty(state, 'batchReceiptIds', {
    get() {
        const batch = getActiveBatch();
        return batch ? batch.receiptIds : [];
    },
    set(val) {
        const batch = getActiveBatch();
        if (batch) batch.receiptIds = val;
    },
});

/** Get the currently active batch, auto-selecting first if needed */
function getActiveBatch() {
    if (state.activeBatchId) {
        const found = state.batches.find(b => b.id === state.activeBatchId);
        if (found) return found;
    }
    if (state.batches.length > 0) {
        state.activeBatchId = state.batches[0].id;
        return state.batches[0];
    }
    return null;
}

/** Create a new named batch and make it active */
function createNewBatch(name) {
    const batch = {
        id: 'batch_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
        name: name || `Batch ${state.batches.length + 1}`,
        receiptIds: [],
        created: new Date().toISOString().split('T')[0],
    };
    state.batches.push(batch);
    state.activeBatchId = batch.id;
    saveBatchState();
    updateBatchBar();
    return batch;
}

/** Add a receipt to a specific batch by batch ID */
function addToBatch(batchId, receiptId) {
    const batch = state.batches.find(b => b.id === batchId);
    if (!batch) return false;
    if (batch.receiptIds.includes(receiptId)) return true;
    if (batch.receiptIds.length >= MAX_BATCH_SIZE) {
        showToast(`Batch "${batch.name}" is full (max ${MAX_BATCH_SIZE}).`, 'warning');
        return false;
    }
    batch.receiptIds.push(receiptId);
    saveBatchState();
    updateBatchBar();
    return true;
}

/** Find which batch a receipt belongs to (any batch) */
function findReceiptBatch(receiptId) {
    return state.batches.find(b => b.receiptIds.includes(receiptId)) || null;
}

/** Persist all batches + active batch ID to localStorage */
function saveBatchState() {
    try {
        localStorage.setItem('scannerBatches', JSON.stringify(state.batches));
        localStorage.setItem('activeBatchId', state.activeBatchId || '');
    } catch (e) {
        // localStorage full — warn user so they know batch data won't persist
        if (typeof showToast === 'function') {
            showToast('Storage is full. Batch data may not be saved. Export or delete old batches.', 'warning');
        }
    }
}

// ─── Catalog Cache ───────────────────────────────────────────────────────────
async function loadCatalogCache() {
    try {
        const res = await fetch('/api/products');
        const data = await res.json();
        state.catalogCache = {};
        if (data.products) {
            data.products.forEach(p => {
                if (!p.product_code) return;
                state.catalogCache[p.product_code.toUpperCase()] = {
                    name: p.product_name,
                    unit: p.unit || 'Piece',
                    category: p.category || '',
                };
            });
        }
    } catch (e) { /* silent */ }
}
loadCatalogCache();   // load on startup

// ─── Dashboard Stats ─────────────────────────────────────────────────────────
const perfState = {
    processingTimes: [],     // array of ms values for avg speed calculation
};

async function loadDashboardStats() {
    try {
        const res = await fetch('/api/dashboard');
        const data = await res.json();

        const todayScans = data.today?.receipts_count ?? 0;
        const totalProducts = data.total_products ?? 0;

        const elToday = $('#statTodayScans');
        const elProducts = $('#statTotalProducts');
        const elSpeed = $('#statAvgSpeed');

        if (elToday) animateCounter(elToday, todayScans);
        if (elProducts) animateCounter(elProducts, totalProducts);

        // Average speed from recent processing times
        if (elSpeed) {
            if (perfState.processingTimes.length > 0) {
                const avg = perfState.processingTimes.reduce((a, b) => a + b, 0) / perfState.processingTimes.length;
                elSpeed.textContent = avg < 1000 ? `${Math.round(avg)}ms` : `${(avg / 1000).toFixed(1)}s`;
            } else {
                elSpeed.textContent = '—';
            }
        }

        // Azure usage stats (show pill only when Azure is configured)
        const usage = data.ocr_engine?.usage;
        const azurePill = $('#azureUsagePill');
        const azureUsageEl = $('#statAzureUsage');
        if (azurePill && azureUsageEl && usage) {
            const todayPages = usage.today?.pages_used ?? usage.today?.pages ?? 0;
            const dailyLimit = usage.today?.pages_limit ?? usage.today?.daily_limit ?? 50;
            const monthlyUsed = usage.this_month?.pages_used ?? 0;
            const monthlyLimit = usage.this_month?.pages_limit ?? 500;
            const monthlyPct = Math.round((monthlyUsed / monthlyLimit) * 100);
            const dailyPct = Math.round((todayPages / dailyLimit) * 100);

            azureUsageEl.textContent = `${monthlyUsed}/${monthlyLimit}`;
            azurePill.style.display = 'flex';

            // Color warning based on monthly usage (most important for free tier)
            const banner = $('#azureWarningBanner');
            const bannerText = $('#azureWarningText');
            if (monthlyPct >= 100) {
                azurePill.style.color = 'var(--danger, #e53e3e)';
                azurePill.title = `🚫 Azure FREE TIER EXHAUSTED! ${monthlyUsed}/${monthlyLimit} pages this month. Using local OCR only.`;
                if (banner && bannerText) {
                    banner.style.display = 'block';
                    banner.className = 'azure-warning-banner azure-danger';
                    bannerText.textContent = `🚫 Azure free tier exhausted (${monthlyUsed}/${monthlyLimit} pages). All scans now use local OCR — no charges will occur.`;
                }
            } else if (monthlyPct >= 80) {
                azurePill.style.color = 'var(--warning, #d69e2e)';
                azurePill.title = `⚠ Azure free tier ${monthlyPct}% used: ${monthlyUsed}/${monthlyLimit} pages this month`;
                if (banner && bannerText) {
                    banner.style.display = 'block';
                    banner.className = 'azure-warning-banner';
                    bannerText.textContent = `⚠ Azure free tier ${monthlyPct}% used (${monthlyUsed}/${monthlyLimit} pages). ${monthlyLimit - monthlyUsed} pages remaining this month.`;
                }
            } else if (dailyPct >= 90) {
                azurePill.style.color = 'var(--warning, #d69e2e)';
                azurePill.title = `⚠ Daily limit almost reached: ${todayPages}/${dailyLimit} pages today`;
                if (banner) banner.style.display = 'none';
            } else {
                azurePill.style.color = '';
                azurePill.title = `Azure: ${monthlyUsed}/${monthlyLimit} pages this month (click for details)`;
                if (banner) banner.style.display = 'none';
            }
        }
    } catch (e) { /* silent */ }
}

/** Smoothly animate a counter from current value to target */
function animateCounter(el, target) {
    const current = parseInt(el.textContent) || 0;
    if (current === target) return;
    const duration = 400;
    const start = performance.now();
    const step = (now) => {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
        el.textContent = Math.round(current + (target - current) * eased);
        if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
}

// Load stats on page load and periodically (pause when tab is hidden)
loadDashboardStats();
let _dashboardTimer = setInterval(loadDashboardStats, 30000);
document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
        clearInterval(_dashboardTimer);
        _dashboardTimer = null;
    } else {
        loadDashboardStats();                          // refresh immediately on re-focus
        if (_dashboardTimer) clearInterval(_dashboardTimer);  // prevent duplicate timers
        _dashboardTimer = setInterval(loadDashboardStats, 30000);
    }
});

// ─── DOM Elements ────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ─── Initialize Lucide Icons ─────────────────────────────────────────────────
if (typeof lucide !== 'undefined') lucide.createIcons();

// ─── Sliding Nav Indicator ───────────────────────────────────────────────────
function updateNavIndicator() {
    const activeBtn = $('.nav-btn.active');
    const indicator = $('#navIndicator');
    if (!activeBtn || !indicator) return;
    indicator.style.width = activeBtn.offsetWidth + 'px';
    indicator.style.left = activeBtn.offsetLeft + 'px';
}

// Set indicator on load (after fonts settle)
window.addEventListener('load', () => {
    setTimeout(updateNavIndicator, 50);
    // Re-init icons in case some loaded late
    if (typeof lucide !== 'undefined') lucide.createIcons();
});
window.addEventListener('resize', updateNavIndicator);

// ─── Tab Navigation ──────────────────────────────────────────────────────────
$$('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        state.currentTab = tab;

        $$('.nav-btn').forEach(b => {
            b.classList.remove('active');
            b.setAttribute('aria-selected', 'false');
        });
        btn.classList.add('active');
        btn.setAttribute('aria-selected', 'true');
        updateNavIndicator();

        $$('.tab-content').forEach(t => t.classList.remove('active'));
        $(`#tab-${tab}`).classList.add('active');

        // Scroll to top on tab switch
        window.scrollTo({ top: 0, behavior: 'instant' });

        // Update URL hash (without triggering hashchange)
        history.replaceState(null, '', `#${tab}`);

        // The quick-stats bar lives on the 'scan' tab, so keep the dashboard
        // timer running there.  Only pause it on the data-heavy tabs (receipts,
        // catalog, train) where the stats are not visible and the extra requests
        // would be wasted.
        if (tab === 'scan' || tab === 'dashboard') {
            if (!_dashboardTimer) {
                loadDashboardStats();
                _dashboardTimer = setInterval(loadDashboardStats, 30000);
            }
        } else {
            if (_dashboardTimer) {
                clearInterval(_dashboardTimer);
                _dashboardTimer = null;
            }
        }

        // Load data for the tab
        if (tab === 'receipts') loadReceipts();
        if (tab === 'catalog') {
            // Clear stale search so results match the displayed data
            const searchInput = $('#catalogSearch');
            if (searchInput) searchInput.value = '';
            loadCatalog();
        }
        if (tab === 'train') loadTrainingTab();
    });
});

// ─── URL Hash Routing ────────────────────────────────────────────────────────
// On page load, restore tab from URL hash
(function restoreTabFromHash() {
    const hash = location.hash.replace('#', '');
    const validTabs = ['scan', 'receipts', 'catalog', 'train'];
    if (hash && validTabs.includes(hash)) {
        const btn = document.querySelector(`.nav-btn[data-tab="${hash}"]`);
        if (btn) setTimeout(() => btn.click(), 100);
    }
})();
// Listen for browser back/forward
window.addEventListener('hashchange', () => {
    const hash = location.hash.replace('#', '');
    const btn = document.querySelector(`.nav-btn[data-tab="${hash}"]`);
    if (btn && !btn.classList.contains('active')) btn.click();
});

// ─── File Upload & Drag/Drop ─────────────────────────────────────────────────
const dropZone = $('#dropZone');
const fileInput = $('#fileInput');

// The browseBtn is a <label for="fileInput">, so it natively opens
// the file dialog — no JS needed for that.
// Prevent the dropZone click from ALSO triggering the dialog
// when the user clicks the label/button.
$('#browseBtn').addEventListener('click', (e) => {
    e.stopPropagation();
});

if ($('#openCameraBtn')) {
    $('#openCameraBtn').addEventListener('click', (e) => {
        e.stopPropagation();
    });
}

dropZone.addEventListener('click', (e) => {
    // Only trigger if the click was NOT on the browse label / file input / camera button
    if (e.target.closest('#browseBtn') || e.target.closest('#openCameraBtn') || e.target.id === 'fileInput') return;
    fileInput.click();
});

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('drag-over');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const files = e.dataTransfer.files;
    if (files.length > 1) {
        processFiles(Array.from(files));
    } else if (files.length === 1) {
        processFile(files[0]);
    }
});

fileInput.addEventListener('change', () => {
    // Remove capture attribute (set by camera fallback) so gallery button works next time
    fileInput.removeAttribute('capture');
    if (fileInput.files.length > 1) {
        processFiles(Array.from(fileInput.files));
    } else if (fileInput.files.length === 1) {
        processFile(fileInput.files[0]);
    }
    // Reset so same files can be re-selected
    fileInput.value = '';
});

// ─── Clipboard Paste Support ─────────────────────────────────────────────────
document.addEventListener('paste', (e) => {
    // Don't intercept paste when focus is on a text input
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) return;
    // Only on scan tab
    if (state.currentTab !== 'scan') return;
    if (state.isProcessing) return;

    const items = e.clipboardData?.items;
    if (!items) return;

    for (const item of items) {
        if (item.type.startsWith('image/')) {
            e.preventDefault();
            const blob = item.getAsFile();
            if (blob) {
                // Briefly flash the paste indicator
                dropZone.classList.add('paste-active');
                setTimeout(() => dropZone.classList.remove('paste-active'), 600);
                showToast('Image pasted from clipboard!', 'success');
                const file = new File([blob], `pasted_receipt_${Date.now()}.png`, { type: blob.type });
                processFile(file);
            }
            return;
        }
    }
});

// ─── Keyboard Shortcuts ──────────────────────────────────────────────────────
document.addEventListener('keydown', (e) => {
    // Don't intercept when focus is on input/textarea
    const active = document.activeElement;
    if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable)) return;
    // Don't fire shortcuts behind modals or camera overlay
    if ($('#modalOverlay')?.style.display === 'flex') return;
    if ($('#cameraOverlay')?.style.display === 'flex') return;
    // Close shortcuts modal on Escape
    if (e.key === 'Escape' && $('#shortcutsModal')?.classList.contains('visible')) {
        $('#shortcutsModal').classList.remove('visible');
        return;
    }

    // ? = Show shortcuts help
    if (e.key === '?' || (e.shiftKey && e.key === '/')) {
        e.preventDefault();
        $('#shortcutsModal')?.classList.toggle('visible');
        return;
    }

    // D = Toggle dark mode
    if ((e.key === 'd' || e.key === 'D') && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        toggleTheme();
        return;
    }

    // Ctrl/Cmd+V is handled by paste event above
    // Tab navigation: 1, 2, 3
    if (e.key === '1' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        $$('.nav-btn')[0]?.click();
    } else if (e.key === '2' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        $$('.nav-btn')[1]?.click();
    } else if (e.key === '3' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        $$('.nav-btn')[2]?.click();
    } else if (e.key === '4' && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        $$('.nav-btn')[3]?.click();
    }
    // N = New scan (reset to upload)
    else if (e.key === 'n' || e.key === 'N') {
        if (state.currentTab === 'scan' && $('#resultsContainer').style.display !== 'none') {
            e.preventDefault();
            $('#scanAgainBtn')?.click();
        }
    }
    // C = Camera (open camera if available)
    else if (e.key === 'c' || e.key === 'C') {
        if (state.currentTab === 'scan' && cameraState.hasCamera && !state.isProcessing) {
            e.preventDefault();
            openCamera();
        }
    }
});

// ─── Multi-File Batch Upload ─────────────────────────────────────────────────

/**
 * Process multiple receipt images in sequence with batch progress UI.
 * Each file is compressed, uploaded individually, and results are accumulated.
 * After all files are processed, a summary is shown with options to view receipts.
 */
async function processFiles(files) {
    if (state.isProcessing) {
        showToast('Already processing. Please wait…', 'warning');
        return;
    }

    // Filter to valid image files
    const validTypes = ['image/jpeg', 'image/png', 'image/bmp', 'image/tiff', 'image/webp'];
    const validFiles = files.filter(f =>
        validTypes.includes(f.type) || f.name.match(/\.(jpg|jpeg|png|bmp|tiff|webp)$/i)
    );

    if (validFiles.length === 0) {
        showToast('No valid image files selected.', 'error');
        return;
    }

    if (validFiles.length > 10) {
        showToast('Maximum 10 images per batch. Please select fewer files.', 'warning');
        return;
    }

    // Show batch upload overlay
    state.isProcessing = true;
    const overlay = $('#batchUploadOverlay');
    const title = $('#batchUploadTitle');
    const subtitle = $('#batchUploadSubtitle');
    const fill = $('#batchProgressFill');
    const status = $('#batchUploadStatus');
    const pctLabel = $('#batchProgressPct');
    const progressLabel = $('#batchProgressLabel');
    const resultsDiv = $('#batchUploadResults');
    const actionsDiv = $('#batchUploadActions');
    const iconDiv = $('#batchUploadIcon');
    const summaryDiv = $('#batchUploadSummary');

    overlay.style.display = 'flex';
    actionsDiv.style.display = 'none';
    summaryDiv.style.display = 'none';
    resultsDiv.innerHTML = '';
    iconDiv.className = 'batch-upload-icon';
    iconDiv.innerHTML = `<svg class="batch-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>`;
    fill.className = 'batch-progress-fill';
    title.textContent = `Processing ${validFiles.length} Receipt${validFiles.length > 1 ? 's' : ''}…`;
    subtitle.textContent = 'Scanning your receipts with OCR';
    progressLabel.textContent = 'Scanning';
    fill.style.width = '0%';
    pctLabel.textContent = '0%';
    status.textContent = `0 / ${validFiles.length}`;

    const batchResults = [];
    let succeeded = 0;
    let totalItemsFound = 0;

    for (let i = 0; i < validFiles.length; i++) {
        const file = validFiles[i];
        const pct = Math.round(((i) / validFiles.length) * 100);
        fill.style.width = pct + '%';
        pctLabel.textContent = pct + '%';
        status.textContent = `${i + 1} / ${validFiles.length} — ${file.name}`;

        // Add a pending row with animated icon
        const row = document.createElement('div');
        row.className = 'batch-upload-row pending';
        row.style.animationDelay = `${i * 0.05}s`;
        row.innerHTML = `
            <span class="batch-upload-row-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="animation:batchSpin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg></span>
            <span class="batch-upload-filename">${escHtml(file.name)}</span>
            <span class="batch-upload-row-status">Processing<span class="batch-pending-dots"><span></span><span></span><span></span></span></span>`;
        resultsDiv.appendChild(row);
        resultsDiv.scrollTop = resultsDiv.scrollHeight;

        try {
            // Compress
            const optimizedFile = await compressImage(file);
            const formData = new FormData();
            formData.append('file', optimizedFile);

            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 180000);

            const res = await fetch('/api/receipts/scan', {
                method: 'POST',
                body: formData,
                signal: controller.signal,
            });
            clearTimeout(timeoutId);

            const data = await res.json();

            if (res.ok && data.success !== false) {
                const itemCount = data.receipt_data?.items?.length || 0;
                const dbId = data.receipt_data?.db_id;
                totalItemsFound += itemCount;
                row.className = 'batch-upload-row success';
                row.querySelector('.batch-upload-row-icon').innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
                row.querySelector('.batch-upload-row-status').textContent = `${itemCount} item${itemCount !== 1 ? 's' : ''}`;
                batchResults.push({ file, data, success: true, dbId });
                succeeded++;

                // Auto-add to active batch if there is one
                if (dbId) {
                    const batch = getActiveBatch();
                    if (batch && !batch.receiptIds.includes(dbId) && batch.receiptIds.length < MAX_BATCH_SIZE) {
                        batch.receiptIds.push(dbId);
                        saveBatchState();
                    }
                }
            } else {
                const errMsg = data.detail || data.errors?.[0] || 'Processing failed';
                row.className = 'batch-upload-row failed';
                row.querySelector('.batch-upload-row-icon').innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
                row.querySelector('.batch-upload-row-status').textContent = errMsg;
                batchResults.push({ file, data, success: false, error: errMsg });
            }
        } catch (err) {
            const errMsg = err.name === 'AbortError' ? 'Timeout' : (err.message || 'Error');
            row.className = 'batch-upload-row failed';
            row.querySelector('.batch-upload-row-icon').innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
            row.querySelector('.batch-upload-row-status').textContent = errMsg;
            batchResults.push({ file, success: false, error: errMsg });
        }

        // Update progress after each file
        const donePct = Math.round(((i + 1) / validFiles.length) * 100);
        fill.style.width = donePct + '%';
        pctLabel.textContent = donePct + '%';
    }

    // Complete
    fill.style.width = '100%';
    fill.classList.add('complete');
    pctLabel.textContent = '100%';
    progressLabel.textContent = 'Complete';

    const failed = validFiles.length - succeeded;

    // Update icon to success/warning state
    if (failed === 0) {
        iconDiv.classList.add('complete');
        iconDiv.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`;
        title.textContent = 'All Receipts Processed!';
        subtitle.textContent = `Successfully scanned ${succeeded} receipt${succeeded !== 1 ? 's' : ''}`;
    } else {
        iconDiv.classList.add('has-errors');
        iconDiv.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`;
        title.textContent = `Batch Complete — ${succeeded} ✓, ${failed} failed`;
        subtitle.textContent = `${failed} receipt${failed !== 1 ? 's' : ''} could not be processed`;
    }

    // Show summary stats
    $('#batchSuccessCount').textContent = succeeded;
    $('#batchTotalItems').textContent = totalItemsFound;
    if (failed > 0) {
        $('#batchFailedCount').textContent = failed;
        $('#batchFailedStat').style.display = '';
    } else {
        $('#batchFailedStat').style.display = 'none';
    }
    summaryDiv.style.display = 'flex';

    status.textContent = `${succeeded} / ${validFiles.length} succeeded`;
    actionsDiv.style.display = 'flex';

    // Save batch state (receipts were added to active batch above)
    saveBatchState();
    updateBatchBar();
    loadDashboardStats();

    state.isProcessing = false;

    // Track processing time for avg speed
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// Batch upload overlay buttons
if ($('#batchUploadDoneBtn')) {
    $('#batchUploadDoneBtn').addEventListener('click', () => {
        $('#batchUploadOverlay').style.display = 'none';
        // Switch to receipts tab
        $$('.nav-btn')[1]?.click();
    });
}
if ($('#batchUploadScanMoreBtn')) {
    $('#batchUploadScanMoreBtn').addEventListener('click', () => {
        $('#batchUploadOverlay').style.display = 'none';
        resetScanUI();
    });
}

// ─── Process Receipt ─────────────────────────────────────────────────────────

/**
 * Compress image client-side before upload for faster transfer and processing.
 * Targets max 1800px on longest side (matches server's IMAGE_MAX_DIMENSION).
 * Returns a Blob (JPEG) if compression was useful, or the original file.
 */
function compressImage(file, maxDim = 1800, quality = 0.88) {
    return new Promise((resolve) => {
        // Skip non-image or small files (< 500KB)
        if (!file.type.startsWith('image/') || file.size < 512 * 1024) {
            resolve(file);
            return;
        }

        const img = new Image();
        const url = URL.createObjectURL(file);

        img.onload = () => {
            URL.revokeObjectURL(url);

            let { width, height } = img;

            // Only resize if larger than maxDim
            if (width <= maxDim && height <= maxDim) {
                resolve(file);
                return;
            }

            const ratio = Math.min(maxDim / width, maxDim / height);
            width = Math.round(width * ratio);
            height = Math.round(height * ratio);

            const canvas = document.createElement('canvas');
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext('2d');

            // Use high-quality downscaling
            ctx.imageSmoothingEnabled = true;
            ctx.imageSmoothingQuality = 'high';
            ctx.drawImage(img, 0, 0, width, height);

            canvas.toBlob((blob) => {
                if (blob && blob.size < file.size) {
                    // Use compressed version — rename extension to .jpg since content is JPEG
                    const jpgName = file.name.replace(/\.[^.]+$/, '.jpg');
                    const compressed = new File([blob], jpgName, { type: 'image/jpeg' });
                    resolve(compressed);
                } else {
                    resolve(file);
                }
            }, 'image/jpeg', quality);
        };

        img.onerror = () => {
            URL.revokeObjectURL(url);
            resolve(file);
        };

        img.src = url;
    });
}

/**
 * Client-side image quality check — detects blur and darkness BEFORE upload.
 * Uses Laplacian variance (sharpness) and mean luminance (brightness) on a
 * downscaled version of the image for speed (~5ms on mobile).
 *
 * Returns an array of warning strings (empty = all good).
 */
function checkImageQuality(file) {
    return new Promise((resolve) => {
        if (!file.type.startsWith('image/')) {
            resolve([]);
            return;
        }

        const img = new Image();
        const url = URL.createObjectURL(file);

        img.onload = () => {
            URL.revokeObjectURL(url);
            const issues = [];

            try {
                // Downscale to 200px max for fast analysis
                const maxDim = 200;
                let { width, height } = img;
                const ratio = Math.min(maxDim / width, maxDim / height, 1);
                width = Math.round(width * ratio);
                height = Math.round(height * ratio);

                const canvas = document.createElement('canvas');
                canvas.width = width;
                canvas.height = height;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0, width, height);
                const imageData = ctx.getImageData(0, 0, width, height);
                const data = imageData.data;

                // ── Brightness check (mean luminance) ──
                let lumSum = 0;
                const pixelCount = width * height;
                if (pixelCount === 0) {
                    resolve(issues);
                    return;
                }
                for (let i = 0; i < data.length; i += 4) {
                    lumSum += data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114;
                }
                const meanLum = lumSum / pixelCount;

                if (meanLum < 50) {
                    issues.push('Image is very dark — try better lighting');
                } else if (meanLum < 80) {
                    issues.push('Image is dark — results may be affected');
                } else if (meanLum > 240) {
                    issues.push('Image is overexposed — try reducing brightness');
                }

                // ── Blur check (Laplacian variance approximation) ──
                // Compute a simple edge-detection (Sobel-like) variance as blur proxy
                const gray = new Float32Array(pixelCount);
                for (let i = 0; i < pixelCount; i++) {
                    const idx = i * 4;
                    gray[i] = data[idx] * 0.299 + data[idx + 1] * 0.587 + data[idx + 2] * 0.114;
                }

                let lapSum = 0;
                let lapSq = 0;
                let lapCount = 0;
                for (let y = 1; y < height - 1; y++) {
                    for (let x = 1; x < width - 1; x++) {
                        const idx = y * width + x;
                        // Laplacian kernel: center*4 - top - bottom - left - right
                        const lap = 4 * gray[idx] - gray[idx - width] - gray[idx + width] - gray[idx - 1] - gray[idx + 1];
                        lapSum += lap;
                        lapSq += lap * lap;
                        lapCount++;
                    }
                }
                const lapVar = lapCount > 0 ? (lapSq / lapCount) - Math.pow(lapSum / lapCount, 2) : 0;

                if (lapVar < 100) {
                    issues.push('Image appears blurry — try holding steadier');
                } else if (lapVar < 300) {
                    issues.push('Image is slightly blurry');
                }

                // ── Contrast check ──
                let lumSq = 0;
                for (let i = 0; i < pixelCount; i++) {
                    lumSq += gray[i] * gray[i];
                }
                const lumStd = Math.sqrt(lumSq / pixelCount - meanLum * meanLum);
                if (lumStd < 25) {
                    issues.push('Low contrast — text may be hard to read');
                }

            } catch (e) {
                // Quality check failed — don't block the upload
                console.warn('Quality check failed:', e);
            }

            resolve(issues);
        };

        img.onerror = () => {
            URL.revokeObjectURL(url);
            resolve([]);
        };

        img.src = url;
    });
}

async function processFile(file) {
    // Prevent double upload
    if (state.isProcessing) {
        showToast('Already processing a receipt. Please wait...', 'warning');
        return;
    }

    // Validate
    const validTypes = ['image/jpeg', 'image/png', 'image/bmp', 'image/tiff', 'image/webp'];
    if (!validTypes.includes(file.type) && !file.name.match(/\.(jpg|jpeg|png|bmp|tiff|webp)$/i)) {
        showToast('Unsupported file type. Use JPG, PNG, BMP, TIFF, or WebP.', 'error');
        return;
    }

    if (file.size > 20 * 1024 * 1024) {
        showToast('File too large. Maximum size is 20MB.', 'error');
        return;
    }

    // ── CLIENT-SIDE QUALITY CHECK ──
    // Detect blurry/dark images BEFORE uploading to save time and bandwidth.
    // Uses Laplacian variance (blur) and mean luminance (darkness).
    const qualityIssues = await checkImageQuality(file);
    if (qualityIssues.length > 0) {
        const issueText = qualityIssues.join(' • ');
        showToast(`⚠ Image quality: ${issueText}. Results may be inaccurate.`, 'warning');
    }

    // Show processing
    state.isProcessing = true;
    state._uploadStartTime = performance.now();  // Track processing time
    dropZone.style.display = 'none';
    $('#processing').style.display = 'block';
    $('#resultsContainer').style.display = 'none';
    simulateProgress();

    // Compress image client-side for faster upload & processing
    let optimizedFile = await compressImage(file);

    // NOTE: Server-side preprocessing (OpenCV) handles enhancement much better
    // than client-side Canvas filters. Skipping client-side enhancement avoids
    // degrading handwriting and eliminates the need for auto-retry.

    // Upload
    const formData = new FormData();
    formData.append('file', optimizedFile);

    try {
        // Use AbortController for request timeout (180s max) and manual cancel
        const controller = new AbortController();
        state._abortController = controller;
        const timeoutId = setTimeout(() => controller.abort(), 180000);

        const res = await fetch('/api/receipts/scan', {
            method: 'POST',
            body: formData,
            signal: controller.signal,
        });

        clearTimeout(timeoutId);

        const data = await res.json();

        if (!res.ok) {
            throw new Error(data.detail || 'Processing failed.');
        }

        // Show results
        displayResults(data, file);

    } catch (err) {
        const message = err.name === 'AbortError'
            ? 'Scan cancelled.'
            : (err.message || 'Error processing receipt.');
        showToast(message, err.name === 'AbortError' ? 'info' : 'error');
        clearProgressInterval();
        state.isProcessing = false;
        state._abortController = null;
        if (err.name !== 'AbortError') {
            showToast('💡 Tip: Try better lighting or a clearer photo', 'info');
        }
        resetScanUI();
    }
}

// Cancel scan button
if ($('#cancelScanBtn')) {
    $('#cancelScanBtn').addEventListener('click', () => {
        if (state._abortController) {
            state._abortController.abort();
            state._abortController = null;
        }
    });
}

function simulateProgress() {
    const fill = $('#progressFill');
    const status = $('#processingStatus');
    const tips = $('#processingTips');
    const tipText = $('#tipText');
    const stages = [
        { pct: 15, text: 'Enhancing image...', tip: null },
        { pct: 35, text: 'Extracting text...', tip: '💡 For best results, use good lighting and flat surfaces' },
        { pct: 60, text: 'Reading handwriting...', tip: '💡 Clear, dark ink on white paper gives the best accuracy' },
        { pct: 80, text: 'Structuring data...', tip: '💡 You can edit any field in the results table' },
        { pct: 92, text: 'Finalizing...', tip: null },
    ];

    let i = 0;
    clearProgressInterval();
    state.progressInterval = setInterval(() => {
        if (i < stages.length) {
            fill.style.width = stages[i].pct + '%';
            status.textContent = stages[i].text;
            // Show contextual tip
            if (tips && tipText && stages[i].tip) {
                tipText.textContent = stages[i].tip;
                tips.style.display = 'block';
            }
            i++;
        } else {
            clearInterval(state.progressInterval);
            state.progressInterval = null;
        }
    }, 400);
}

function clearProgressInterval() {
    if (state.progressInterval) {
        clearInterval(state.progressInterval);
        state.progressInterval = null;
    }
}

function completeProgress() {
    clearProgressInterval();
    const fill = $('#progressFill');
    const status = $('#processingStatus');
    fill.style.width = '100%';
    status.textContent = 'Complete!';
}

// ─── Display Results ─────────────────────────────────────────────────────────
function displayResults(data, file) {
    completeProgress();
    state.isProcessing = false;
    state.isDirty = false;
    state.confirmed = false;

    // Reset confirm button for fresh result
    $('#confirmBtn').innerHTML = '<i data-lucide="check" style="width:15px;height:15px"></i> Confirm &amp; Save';
    $('#confirmBtn').classList.remove('btn-confirmed');
    $('#exportExcelBtn').style.display = 'none';

    // Clear previous raw OCR
    $('#rawOcrOutput').textContent = '';

    // Always switch view (even on error/empty)
    // Show results immediately (no artificial delay)
    $('#processing').style.display = 'none';
    $('#resultsContainer').style.display = 'block';
    // Hide tips
    const tips = $('#processingTips');
    if (tips) tips.style.display = 'none';

    // Haptic feedback on mobile (if supported)
    if (navigator.vibrate) navigator.vibrate(50);

    state.currentReceiptData = data;
    state._removedItemIds = [];  // Reset tracked deletions for new receipt

    // Show processing time
    const serverTimeMs = data.metadata?.total_time_ms;
    const clientElapsedMs = state._uploadStartTime ? Math.round(performance.now() - state._uploadStartTime) : null;
    const displayMs = serverTimeMs || clientElapsedMs;
    const ptEl = $('#processingTime');
    const ptVal = $('#processTimeValue');
    if (ptEl && ptVal && displayMs) {
        ptVal.textContent = displayMs < 1000 ? `${displayMs}ms` : `${(displayMs / 1000).toFixed(1)}s`;
        ptEl.style.display = 'inline-flex';
        // Track for avg speed
        perfState.processingTimes.push(displayMs);
        if (perfState.processingTimes.length > 20) perfState.processingTimes.shift();
    } else if (ptEl) {
        ptEl.style.display = 'none';
    }

    // ── Processing Pipeline Breakdown ──
    // Show timing for each stage so users can see what's slow
    let existingBreakdown = document.querySelector('.pipeline-breakdown');
    if (existingBreakdown) existingBreakdown.remove();

    const preprocessMs = data.metadata?.preprocessing?.processing_time_ms;
    const ocrMs = data.metadata?.ocr_time_ms;
    const parseMs = data.metadata?.parse_time_ms;
    const stages = data.metadata?.preprocessing?.stages || [];
    const engineUsed = data.metadata?.engine_used || 'local';
    const receiptType = data.metadata?.receipt_type || 'unknown';
    const qualityScore = data.metadata?.preprocessing?.quality?.score;

    if (preprocessMs || ocrMs || parseMs) {
        const breakdownDiv = document.createElement('div');
        breakdownDiv.className = 'pipeline-breakdown';
        
        let html = '<div class="breakdown-header"><i data-lucide="activity" style="width:13px;height:13px"></i> Pipeline</div><div class="breakdown-stages">';
        
        if (preprocessMs != null) {
            html += `<span class="breakdown-stage" title="Stages: ${stages.join(', ') || 'N/A'}"><span class="stage-label">Preprocess</span><span class="stage-time">${preprocessMs}ms</span></span>`;
        }
        if (ocrMs != null) {
            html += `<span class="breakdown-stage" title="Engine: ${engineUsed}"><span class="stage-label">OCR</span><span class="stage-time">${ocrMs}ms</span></span>`;
        }
        if (parseMs != null) {
            html += `<span class="breakdown-stage"><span class="stage-label">Parse</span><span class="stage-time">${parseMs}ms</span></span>`;
        }
        
        html += '</div>';
        
        // Meta badges
        html += '<div class="breakdown-meta">';
        if (receiptType !== 'unknown') {
            html += `<span class="meta-badge">${receiptType === 'structured' ? '📊' : '✍️'} ${receiptType}</span>`;
        }
        if (qualityScore != null) {
            const qClass = qualityScore >= 60 ? 'good' : qualityScore >= 30 ? 'fair' : 'poor';
            html += `<span class="meta-badge quality-${qClass}">Quality: ${Math.round(qualityScore)}/100</span>`;
        }
        if (stages.includes('document_scan')) {
            html += `<span class="meta-badge">📱 Doc scanned</span>`;
        }
        if (stages.includes('rotation_180')) {
            html += `<span class="meta-badge">↻ Auto-rotated</span>`;
        }
        if (stages.includes('deskew')) {
            html += `<span class="meta-badge">📐 Deskewed</span>`;
        }
        if (stages.includes('white_balance')) {
            html += `<span class="meta-badge">🎨 WB corrected</span>`;
        }
        html += '</div>';
        
        breakdownDiv.innerHTML = html;
        ptEl?.parentNode?.insertBefore(breakdownDiv, ptEl.nextSibling);
    }

    // Show Azure engine usage info after scan
    const strategy = data.metadata?.strategy || '';
    const azurePagesUsed = data.metadata?.azure_pages_used || 0;
    const usageReason = data.metadata?.reason || '';
    let existingAzureInfo = document.querySelector('.azure-scan-info');
    if (existingAzureInfo) existingAzureInfo.remove();

    if (strategy.includes('usage-limited') || strategy.includes('blocked')) {
        const infoDiv = document.createElement('div');
        infoDiv.className = 'azure-scan-info info-blocked';
        infoDiv.innerHTML = `⚠️ <strong>Local OCR used</strong> — Azure free tier limit reached. No charges incurred.`;
        ptEl?.parentNode?.insertBefore(infoDiv, ptEl.nextSibling);
    } else if (azurePagesUsed > 0) {
        const infoDiv = document.createElement('div');
        infoDiv.className = 'azure-scan-info info-azure';
        infoDiv.innerHTML = `☁️ <strong>Azure OCR used</strong> — ${azurePagesUsed} free page(s) consumed`;
        ptEl?.parentNode?.insertBefore(infoDiv, ptEl.nextSibling);
    } else if (strategy.includes('local') || strategy === 'local-only') {
        const infoDiv = document.createElement('div');
        infoDiv.className = 'azure-scan-info info-local';
        infoDiv.innerHTML = `💻 <strong>Local OCR used</strong> — high confidence, Azure page saved`;
        ptEl?.parentNode?.insertBefore(infoDiv, ptEl.nextSibling);
    }

    // Refresh dashboard stats (new receipt scanned)
    loadDashboardStats();

    // Show image preview — prefer server-side image when available
    // (it's the exact file stored in the uploads dir, not the compressed client version)
    const serverImage = data.receipt_data?.image_path;
    if (serverImage) {
        const filename = serverImage.split(/[\/\\]/).pop();
        $('#receiptImage').src = '/uploads/' + encodeURIComponent(filename);
    } else if (file) {
        const reader = new FileReader();
        reader.onload = (e) => { $('#receiptImage').src = e.target.result; };
        reader.readAsDataURL(file);
    }

    // Check for backend failure flag
    if (data.success === false) {
        const errMsg = (data.errors && data.errors.length > 0) ? data.errors.join(' | ') : 'Processing failed.';
        showToast(errMsg, 'error');
    }

    const receiptData = data.receipt_data;
    if (!receiptData || !receiptData.items) {
        showToast('No items found on receipt. Try a clearer image with good lighting.', 'warning');
        $('#itemsBody').innerHTML = `<tr><td colspan="8" class="placeholder">
            <div style="display:flex;flex-direction:column;align-items:center;gap:0.5rem">
                <i data-lucide="search-x" style="width:32px;height:32px;color:var(--text-muted)"></i>
                <strong>No items detected</strong>
                <span style="font-size:0.8rem">Try re-scanning with better lighting, less blur, or a flatter surface</span>
            </div>
        </td></tr>`;
        $('#avgConfidence').textContent = '';
        $('#avgConfidence').className = 'confidence-badge';
        $('#warnings').style.display = 'none';
        return;
    }

    if (receiptData.items.length === 0) {
        showToast('No recognizable products found. You can add items manually.', 'warning');
        $('#itemsBody').innerHTML = `<tr><td colspan="8" class="placeholder">
            <div style="display:flex;flex-direction:column;align-items:center;gap:0.5rem">
                <i data-lucide="plus-circle" style="width:28px;height:28px;color:var(--primary)"></i>
                <strong>No products recognized</strong>
                <span style="font-size:0.8rem">Use "+ Add Row" below to enter items manually</span>
            </div>
        </td></tr>`;
    }

    // Confidence badge
    const avgConf = receiptData.avg_confidence || 0;
    const badge = $('#avgConfidence');
    badge.textContent = `${(avgConf * 100).toFixed(1)}% avg`;
    badge.className = 'confidence-badge ' + getConfClass(avgConf);

    // Populate table
    populateItemsTable(receiptData.items);

    // ── Bill Total Verification Panel ─────────────────────────────────
    displayTotalVerification(data);

    // ── Math / Price Verification Panel ───────────────────────────────
    displayMathVerification(data);

    // Show warnings
    if (data.errors && data.errors.length > 0) {
        $('#warnings').style.display = 'block';
        $('#warningText').textContent = data.errors.join(' | ');
    } else if (receiptData.needs_review) {
        $('#warnings').style.display = 'block';
        $('#warningText').textContent = 'Some items have low confidence. Please review highlighted rows.';
    } else {
        $('#warnings').style.display = 'none';
    }

    // Raw OCR
    if (data.metadata && data.metadata.raw_ocr) {
        $('#rawOcrOutput').textContent = JSON.stringify(data.metadata.raw_ocr, null, 2);
    }

    // Re-init icons after DOM update
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ─── Bill Total Verification Display ──────────────────────────────────────────
function displayTotalVerification(data) {
    const panel = $('#totalVerificationPanel');
    if (!panel) return;

    // Get verification data from receipt_data or metadata
    const verification = data.receipt_data?.total_verification
                      || data.metadata?.total_verification;

    if (!verification) {
        panel.style.display = 'none';
        return;
    }

    const ocrTotal = verification.ocr_total ?? verification.total_qty_ocr;
    const computedTotal = verification.computed_total ?? verification.total_qty_computed;
    const isMatch = verification.total_qty_match;
    const status = verification.verification_status || verification.verification_method || 'unknown';
    const totalLineText = verification.total_line_text;
    const confidence = verification.confidence ?? verification.total_line_confidence;

    // Always show if we have computed total (even without OCR total)
    panel.style.display = 'block';

    const iconEl = $('#totalVerifyIcon');
    const titleEl = $('#totalVerifyTitle');
    const badgeEl = $('#totalVerifyBadge');
    const computedEl = $('#totalComputed');
    const ocrEl = $('#totalOcr');
    const matchIcon = $('#totalMatchIcon');
    const detailEl = $('#totalVerifyDetail');

    // Set computed total
    computedEl.textContent = computedTotal != null ? computedTotal : '—';

    // Set OCR total
    ocrEl.textContent = ocrTotal != null ? ocrTotal : 'Not found';

    if (ocrTotal != null && isMatch) {
        // ✅ VERIFIED — totals match
        panel.className = 'total-verification-panel total-verified';
        iconEl.textContent = '✅';
        titleEl.textContent = 'Bill Total Verified';
        badgeEl.textContent = 'MATCH';
        badgeEl.className = 'total-verification-badge badge-match';
        matchIcon.textContent = '=';
        matchIcon.className = 'total-match-yes';
        detailEl.style.display = 'none';
    } else if (ocrTotal != null && !isMatch) {
        // ⚠️ MISMATCH
        const diff = Math.abs(ocrTotal - (computedTotal || 0));
        panel.className = 'total-verification-panel total-mismatch';
        iconEl.textContent = '⚠️';
        titleEl.textContent = 'Bill Total Mismatch';
        badgeEl.textContent = `DIFF: ${diff}`;
        badgeEl.className = 'total-verification-badge badge-mismatch';
        matchIcon.textContent = '≠';
        matchIcon.className = 'total-match-no';
        detailEl.style.display = 'block';
        detailEl.innerHTML = `
            <span class="total-detail-text">
                Receipt shows <strong>${ocrTotal}</strong> but items sum to <strong>${computedTotal}</strong>.
                Please review the quantities above.
            </span>
        `;
    } else {
        // ℹ️ No total line found on receipt
        panel.className = 'total-verification-panel total-no-total';
        iconEl.textContent = 'ℹ️';
        titleEl.textContent = 'Bill Total';
        badgeEl.textContent = 'NO TOTAL LINE';
        badgeEl.className = 'total-verification-badge badge-no-total';
        matchIcon.textContent = '—';
        matchIcon.className = '';
        ocrEl.textContent = 'Not on receipt';
        detailEl.style.display = 'block';
        detailEl.innerHTML = `
            <span class="total-detail-text">
                No "Total" line detected on the receipt. Computed qty sum: <strong>${computedTotal}</strong>
            </span>
        `;
    }

    // Show total line text if captured
    if (totalLineText && detailEl.style.display !== 'block') {
        detailEl.style.display = 'block';
        detailEl.innerHTML = `<span class="total-detail-text">Read from receipt: "${escHtml(totalLineText)}"</span>`;
    }
}

// ─── Math / Price Verification Display ────────────────────────────────────────
function displayMathVerification(data) {
    const panel = $('#mathVerificationPanel');
    if (!panel) return;

    const math = data.receipt_data?.math_verification
              || data.metadata?.math_verification;

    if (!math || !math.has_prices) {
        panel.style.display = 'none';
        return;
    }

    panel.style.display = 'block';

    const iconEl = $('#mathVerifyIcon');
    const titleEl = $('#mathVerifyTitle');
    const badgeEl = $('#mathVerifyBadge');
    const computedEl = $('#mathGrandComputed');
    const ocrEl = $('#mathGrandOcr');
    const matchIcon = $('#mathGrandMatchIcon');
    const summaryEl = $('#mathSummary');
    const detailEl = $('#mathVerifyDetail');

    const lineChecks = math.line_checks || [];
    const allOk = math.all_line_math_ok;
    const computed = math.computed_grand_total;
    const ocrGrand = math.ocr_grand_total;
    const grandMatch = math.grand_total_match;
    const mismatches = math.catalog_mismatches || [];

    computedEl.textContent = computed != null ? '₹' + computed.toFixed(2) : '—';
    ocrEl.textContent = ocrGrand != null ? '₹' + ocrGrand.toFixed(2) : 'Not found';

    const linesOkCount = lineChecks.filter(c => c.math_ok).length;
    const linesTotal = lineChecks.length;

    if (allOk && (grandMatch || ocrGrand == null)) {
        panel.className = 'total-verification-panel total-verified';
        iconEl.textContent = '✅';
        titleEl.textContent = 'Price & Math Verified';
        badgeEl.textContent = `ALL ${linesTotal} OK`;
        badgeEl.className = 'total-verification-badge badge-match';
        matchIcon.textContent = '=';
        matchIcon.className = 'total-match-yes';
    } else {
        panel.className = 'total-verification-panel total-mismatch';
        iconEl.textContent = '⚠️';
        titleEl.textContent = 'Math Issues Found';
        const failCount = linesTotal - linesOkCount;
        badgeEl.textContent = `${failCount} ERROR${failCount > 1 ? 'S' : ''}`;
        badgeEl.className = 'total-verification-badge badge-mismatch';
        matchIcon.textContent = grandMatch ? '=' : '≠';
        matchIcon.className = grandMatch ? 'total-match-yes' : 'total-match-no';
    }

    // Summary line
    summaryEl.style.display = 'block';
    let summaryHtml = `<div class="math-summary-row">Line math: <strong>${linesOkCount}/${linesTotal}</strong> correct`;
    if (ocrGrand != null) {
        summaryHtml += ` · Grand total: ${grandMatch ? '✅ Match' : '❌ Mismatch'}`;
    }
    if (mismatches.length > 0) {
        summaryHtml += ` · <span style="color:var(--warning)">⚠ ${mismatches.length} catalog price mismatch${mismatches.length > 1 ? 'es' : ''}</span>`;
    }
    summaryHtml += '</div>';
    summaryEl.innerHTML = summaryHtml;

    // Detail: show failing lines and catalog mismatches
    const failLines = lineChecks.filter(c => !c.math_ok);
    if (failLines.length > 0 || mismatches.length > 0) {
        detailEl.style.display = 'block';
        let html = '';
        if (failLines.length > 0) {
            html += '<div class="math-detail-section"><strong>Math errors:</strong><ul>';
            failLines.forEach(c => {
                html += `<li><code>${escHtml(c.code)}</code>: ${c.qty} × ₹${c.rate} = ₹${c.amount_expected.toFixed(2)} (receipt shows ₹${c.amount_ocr.toFixed(2)})</li>`;
            });
            html += '</ul></div>';
        }
        if (mismatches.length > 0) {
            html += '<div class="math-detail-section"><strong>Catalog price differences:</strong><ul>';
            mismatches.forEach(m => {
                html += `<li><code>${escHtml(m.code)}</code>: Receipt ₹${m.ocr_price} vs Catalog ₹${m.catalog_price}</li>`;
            });
            html += '</ul></div>';
        }
        detailEl.innerHTML = html;
    } else {
        detailEl.style.display = 'none';
    }
}

function populateItemsTable(items) {
    const tbody = $('#itemsBody');
    tbody.innerHTML = '';

    // Get math verification line checks for per-row math status
    const mathData = state.currentReceiptData?.receipt_data?.math_verification
                  || state.currentReceiptData?.metadata?.math_verification;
    const lineChecks = mathData?.line_checks || [];

    items.forEach((item, idx) => {
        const confClass = getConfClass(item.confidence);
        const rowClass = item.confidence < 0.85 ? 'row-low-confidence' : '';
        const rate = item.unit_price || 0;
        const amount = item.line_total || 0;
        const hasPrice = rate > 0;

        // Find matching line check for math status (prefer index-based match
        // to handle duplicate product codes correctly)
        let mathOk = null;
        if (lineChecks.length > 0) {
            const lc = lineChecks[idx] || lineChecks.find(c => c.code === item.code);
            if (lc) mathOk = lc.math_ok;
        }

        const mathCell = mathOk === true ? '<span class="math-ok" title="Qty × Rate = Amount ✓">✅</span>'
                       : mathOk === false ? '<span class="math-fail" title="Math mismatch ✗">❌</span>'
                       : '<span class="math-na" title="No price data">—</span>';

        const tr = document.createElement('tr');
        tr.className = rowClass;
        tr.innerHTML = `
            <td><input class="editable" value="${escHtml(item.code)}" data-idx="${idx}" data-field="code"></td>
            <td><input class="editable" value="${escHtml(item.product)}" data-idx="${idx}" data-field="product"></td>
            <td><input class="editable" type="number" step="1" min="1" max="9999" value="${Math.max(1, Math.round(item.quantity || 0) || 1)}" data-idx="${idx}" data-field="quantity"></td>
            <td class="price-cell">${hasPrice ? '₹' + rate.toFixed(2) : '—'}</td>
            <td class="price-cell">${hasPrice ? '₹' + amount.toFixed(2) : '—'}</td>
            <td class="conf-cell ${confClass}">${(item.confidence * 100).toFixed(1)}%</td>
            <td class="math-cell">${mathCell}</td>
            <td><button class="btn btn-sm btn-ghost" onclick="removeRow(${idx})" title="Remove row" style="padding:0.25rem 0.4rem;color:var(--danger)"><i data-lucide="trash-2" style="width:14px;height:14px"></i></button></td>
        `;
        tbody.appendChild(tr);
    });

    // Mark edits as dirty for unsaved-changes tracking
    tbody.querySelectorAll('.editable').forEach(inp => {
        inp.addEventListener('input', () => { state.isDirty = true; });
    });

    // Prevent scroll wheel from changing quantity when scrolling the page
    tbody.querySelectorAll('input[type="number"]').forEach(inp => {
        inp.addEventListener('wheel', (e) => {
            if (document.activeElement === inp) {
                e.preventDefault();   // block value change
                inp.blur();
            }
        }, { passive: false });  // must be non-passive to allow preventDefault
        // Block decimal point/comma entry
        inp.addEventListener('keydown', (e) => {
            if (e.key === '.' || e.key === ',') e.preventDefault();
        });
        // Floor to integer on change (handles paste)
        inp.addEventListener('change', () => {
            inp.value = Math.max(1, Math.min(9999, Math.round(parseFloat(inp.value) || 1)));
        });
    });

    // Re-init Lucide icons for dynamically added buttons
    if (typeof lucide !== 'undefined') lucide.createIcons();

    // Remove auto-filled styling when user manually edits the product name
    tbody.querySelectorAll('input[data-field="product"]').forEach(inp => {
        inp.addEventListener('input', () => {
            inp.classList.remove('auto-filled');
        });
    });

    // Auto-populate product name when a known code is entered/changed/cleared
    tbody.querySelectorAll('input[data-field="code"]').forEach(inp => {
        let lastAutoName = '';   // track what WE filled in, so manual edits aren't overwritten

        // Initialise: if this row already has a catalog match, record it
        const initCode = inp.value.trim().toUpperCase();
        const initCached = state.catalogCache[initCode];
        if (initCached) lastAutoName = initCached.name;

        const handleCodeChange = () => {
            const code = inp.value.trim().toUpperCase();
            const idx = inp.dataset.idx;
            const nameInput = tbody.querySelector(`input[data-idx="${idx}"][data-field="product"]`);
            if (!nameInput) return;

            const currentName = nameInput.value.trim();
            const cached = state.catalogCache[code];

            if (cached) {
                // Only overwrite if: name is empty, OR name still matches our last auto-fill
                // This respects manual edits while still being dynamic
                if (!currentName || currentName === lastAutoName) {
                    nameInput.value = cached.name;
                    nameInput.classList.add('auto-filled');
                    lastAutoName = cached.name;
                }
                // Update backing data
                if (state.currentReceiptData?.receipt_data?.items[idx]) {
                    const item = state.currentReceiptData.receipt_data.items[idx];
                    item.code = code;
                    if (!currentName || currentName === lastAutoName) {
                        item.product = cached.name;
                    }
                }
            } else {
                // Code not in catalog → clear auto-filled name, leave manual names alone
                if (currentName === lastAutoName) {
                    nameInput.value = '';
                    lastAutoName = '';
                }
                nameInput.classList.remove('auto-filled');
                // Update backing data
                if (state.currentReceiptData?.receipt_data?.items[idx]) {
                    const item = state.currentReceiptData.receipt_data.items[idx];
                    item.code = code;
                    if (item.product === lastAutoName) {
                        item.product = '';
                    }
                }
            }
        };

        // Fire on every keystroke AND on blur/paste for full coverage
        inp.addEventListener('input', handleCodeChange);
        inp.addEventListener('change', handleCodeChange);
    });
}

function getConfClass(conf) {
    if (conf >= 0.90) return 'confidence-high conf-high';
    if (conf >= 0.80) return 'confidence-medium conf-medium';
    return 'confidence-low conf-low';
}

// ─── Row Actions ─────────────────────────────────────────────────────────────
window.removeRow = function(idx) {
    if (state.currentReceiptData && state.currentReceiptData.receipt_data) {
        const items = state.currentReceiptData.receipt_data.items;
        if (items.length <= 1) {
            showToast('Cannot remove the last item. Use "Scan Again" to start over.', 'warning');
            return;
        }
        // Track removed item IDs for deletion on confirm
        const removedItem = items[idx];
        if (removedItem && removedItem.id) {
            if (!state._removedItemIds) state._removedItemIds = [];
            state._removedItemIds.push(removedItem.id);
        }
        // Sync current DOM input values to backing data BEFORE splicing,
        // so that user edits (e.g. changed quantity) are reflected in
        // the math recalculation after row removal.
        document.querySelectorAll('#itemsBody .editable').forEach(inp => {
            const i = parseInt(inp.dataset.idx);
            const field = inp.dataset.field;
            const items = state.currentReceiptData.receipt_data.items;
            if (items[i]) {
                items[i][field] = field === 'quantity' ? (parseFloat(inp.value) || 0) : inp.value;
            }
        });
        // Also recalculate line_total for items whose quantity was edited
        state.currentReceiptData.receipt_data.items.forEach(item => {
            if ((item.unit_price || 0) > 0) {
                item.line_total = Math.round(item.quantity * item.unit_price * 100) / 100;
            }
        });

        state.currentReceiptData.receipt_data.items.splice(idx, 1);
        state.isDirty = true;
        populateItemsTable(state.currentReceiptData.receipt_data.items);
        // Refresh math verification panel after row removal
        const mathData = state.currentReceiptData.receipt_data.math_verification;
        if (mathData) {
            // Rebuild line_checks from remaining items to avoid stale-index bugs
            // when multiple rows are removed in sequence.
            const remaining = state.currentReceiptData.receipt_data.items;
            if (mathData.line_checks && remaining.length > 0) {
                // Rebuild line_checks from scratch using remaining items
                mathData.line_checks = remaining.map(item => {
                    const rate = item.unit_price || 0;
                    const qty = item.quantity || 0;
                    const amt = item.line_total || 0;
                    const expected = Math.round(qty * rate * 100) / 100;
                    return {
                        code: item.code || '',
                        qty: qty,
                        rate: rate,
                        amount_ocr: amt,
                        amount_expected: expected,
                        math_ok: amt > 0 ? Math.abs(amt - expected) < 0.01 : true,
                    };
                });
                // Recalculate computed grand total
                mathData.computed_grand_total = mathData.line_checks.reduce(
                    (sum, lc) => sum + (lc.amount_expected || 0), 0
                );
                mathData.computed_grand_total = Math.round(mathData.computed_grand_total * 100) / 100;
                if (mathData.ocr_grand_total != null) {
                    mathData.grand_total_match = Math.abs(mathData.ocr_grand_total - mathData.computed_grand_total) < 0.01;
                }
                mathData.all_line_math_ok = mathData.line_checks.every(lc => lc.math_ok);
            }
            // displayMathVerification expects the full receipt response
            // (it accesses .receipt_data.math_verification internally)
            displayMathVerification(state.currentReceiptData);
        }
    }
};

$('#addRowBtn').addEventListener('click', () => {
    // Ensure receipt data structure exists even without a prior scan
    if (!state.currentReceiptData) {
        state.currentReceiptData = {
            success: true,
            receipt_data: { items: [], total_items: 0, processing_status: 'manual', avg_confidence: 1.0 },
        };
    }
    if (!state.currentReceiptData.receipt_data) {
        state.currentReceiptData.receipt_data = { items: [], total_items: 0, processing_status: 'manual', avg_confidence: 1.0 };
    }
    if (!state.currentReceiptData.receipt_data.items) {
        state.currentReceiptData.receipt_data.items = [];
    }

    state.currentReceiptData.receipt_data.items.push({
        code: '',
        product: '',
        quantity: 1,
        confidence: 1.0,
        unit: 'Piece',
        unit_price: 0,
        line_total: 0,
        match_type: 'manual',
        needs_review: true,
    });
    state.isDirty = true;
    populateItemsTable(state.currentReceiptData.receipt_data.items);

    // Focus the new row's code input for immediate typing
    setTimeout(() => {
        const lastCode = document.querySelector('#itemsBody tr:last-child input[data-field="code"]');
        if (lastCode) lastCode.focus();
    }, 50);
});

// ─── Confirm & Save ──────────────────────────────────────────────────────────
$('#confirmBtn').addEventListener('click', async () => {
    if (!state.currentReceiptData) return;
    if (state.confirmed) {
        showToast('Receipt already confirmed. Use "Scan Again" for a new receipt.', 'info');
        return;
    }

    const confirmBtn = $('#confirmBtn');
    if (confirmBtn.disabled) return;
    // Disable immediately to prevent double-click race
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Saving...';

    // Read edited values from inputs
    const inputs = $$('#itemsBody .editable');
    const items = state.currentReceiptData.receipt_data.items;

    inputs.forEach(input => {
        const idx = parseInt(input.dataset.idx);
        const field = input.dataset.field;
        if (items[idx]) {
            items[idx][field] = field === 'quantity' ? parseFloat(input.value) || 0 : input.value;
        }
    });

    // Recalculate line_total when quantity was edited
    items.forEach(item => {
        const rate = item.unit_price || 0;
        if (rate > 0) {
            item.line_total = Math.round(item.quantity * rate * 100) / 100;
        }
    });

    // Validate: check for empty codes or zero/negative quantities
    const problems = [];
    items.forEach((item, i) => {
        if (!item.code || !item.code.trim()) problems.push(`Row ${i + 1}: Product code is empty`);
        if (!item.quantity || item.quantity <= 0) problems.push(`Row ${i + 1}: Quantity must be at least 1`);
    });
    if (items.length === 0) problems.push('No items to save. Add at least one item.');
    if (problems.length > 0) {
        showToast(problems[0], 'warning');
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = '<i data-lucide="check" style="width:15px;height:15px"></i> Confirm &amp; Save';
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }

    // Persist edits to database if we have a db_id
    const dbId = state.currentReceiptData.receipt_data.db_id;
    let _saveFailures = 0;
    if (dbId) {
        try {
            // Delete items that were removed by the user
            if (state._removedItemIds && state._removedItemIds.length > 0) {
                for (const removedId of state._removedItemIds) {
                    try {
                        const dr = await fetch(`/api/receipts/items/${removedId}`, { method: 'DELETE' });
                        if (!dr.ok) _saveFailures++;
                    } catch (delErr) {
                        _saveFailures++;
                        console.warn('Failed to delete removed item:', delErr);
                    }
                }
                state._removedItemIds = [];
            }
            // Update existing / add new items
            for (const item of items) {
                try {
                    if (item.id) {
                        // Update existing item
                        const r = await fetch(`/api/receipts/items/${item.id}`, {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                product_code: item.code,
                                product_name: item.product,
                                quantity: item.quantity,
                                unit_price: item.unit_price || 0,
                                line_total: item.line_total || 0,
                            }),
                        });
                        if (!r.ok) _saveFailures++;
                    } else {
                        // Add new manually-added item
                        const res = await fetch(`/api/receipts/${dbId}/items`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                product_code: item.code,
                                product_name: item.product,
                                quantity: item.quantity,
                                unit_price: item.unit_price || 0,
                                line_total: item.line_total || 0,
                            }),
                        });
                        if (!res.ok) { _saveFailures++; continue; }
                        const result = await safeJson(res);
                        if (result.item_id) item.id = result.item_id;
                    }
                } catch (itemErr) {
                    _saveFailures++;
                    console.warn('Failed to persist item:', itemErr);
                }
            }
        } catch (err) {
            console.warn('Failed to persist edits to DB:', err);
            _saveFailures = items.length;
        } finally {
            confirmBtn.disabled = false;
        }
    } else {
        // No db_id — receipt was not saved to the server yet.
        // Mark as a warning so the user knows data is local-only.
        if (items.length > 0) {
            showToast('Receipt data is local only — it was not saved to the server.', 'info');
        }
        confirmBtn.disabled = false;
    }

    if (_saveFailures === 0) {
        state.isDirty = false;
        state.confirmed = true;
        confirmBtn.innerHTML = '<i data-lucide="check-circle" style="width:15px;height:15px"></i> Confirmed';
        confirmBtn.classList.add('btn-confirmed');
        showToast('Receipt confirmed and saved!', 'success');
        // Only show Export and post-confirm panel when save fully succeeded
        $('#exportExcelBtn').style.display = 'inline-flex';
        showPostConfirmPanel();
    } else {
        // Keep dirty so user can retry saving — do NOT show Export / post-confirm
        state.isDirty = true;
        state.confirmed = false;
        confirmBtn.disabled = false;
        confirmBtn.classList.remove('btn-confirmed');
        confirmBtn.innerHTML = '<i data-lucide="check" style="width:15px;height:15px"></i> Retry Save';
        showToast(`${_saveFailures} item(s) failed to save. Click Confirm to retry.`, 'warning');
    }
    if (typeof lucide !== 'undefined') lucide.createIcons();
});

// ─── Export Single Receipt Excel ──────────────────────────────────────────────
$('#exportExcelBtn').addEventListener('click', async () => {
    const dbId = state.currentReceiptData?.receipt_data?.db_id;
    if (!dbId) {
        showToast('Receipt not saved. Please confirm first.', 'warning');
        return;
    }

    const btn = $('#exportExcelBtn');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    try {
        const res = await fetch('/api/export/excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receipt_ids: [dbId] }),
        });
        const data = await safeJson(res);

        if (res.ok && data.download_url) {
            window.open(data.download_url, '_blank');
            showToast('Excel report downloaded!', 'success');
        } else {
            throw new Error(data.detail || 'Export failed.');
        }
    } catch (err) {
        showToast(err.message || 'Export failed.', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="file-spreadsheet" style="width:15px;height:15px"></i> Download Excel';
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
});

// ─── Scan Again ──────────────────────────────────────────────────────────────
$('#scanAgainBtn').addEventListener('click', () => {
    if (state.isDirty && !state.confirmed) {
        if (!confirm('You have unsaved changes. Discard and scan a new receipt?')) return;
    }
    resetScanUI();
});

// Post-confirm: "Scan Next Receipt" button
if ($('#scanNextBtn')) {
    $('#scanNextBtn').addEventListener('click', () => resetScanUI());
}

// Post-confirm: "Download Excel" button
if ($('#postConfirmExportBtn')) {
    $('#postConfirmExportBtn').addEventListener('click', () => {
        if ($('#exportExcelBtn')) $('#exportExcelBtn').click();
    });
}

// Post-confirm: "Add to batch" button
if ($('#postConfirmAddBtn')) {
    $('#postConfirmAddBtn').addEventListener('click', () => {
        const dbId = state.currentReceiptData?.receipt_data?.db_id;
        if (!dbId) return;
        const select = $('#postConfirmBatchSelect');
        const batchId = select?.value;
        if (!batchId) {
            showToast('Select a batch or create a new one first.', 'warning');
            return;
        }
        if (addToBatch(batchId, dbId)) {
            const batch = state.batches.find(b => b.id === batchId);
            const status = $('#postConfirmBatchStatus');
            if (status) {
                status.textContent = `✓ Added to "${batch?.name}"`;
                status.style.display = 'inline';
            }
            showToast(`Added to "${batch?.name}"!`, 'success');
            $('#postConfirmAddBtn').disabled = true;
            updateBatchBar();
        }
    });
}

// Post-confirm: "New Batch" button (in post-confirm panel)
if ($('#postConfirmNewBatchBtn')) {
    $('#postConfirmNewBatchBtn').addEventListener('click', () => {
        const name = prompt('Enter batch name:', `Batch ${state.batches.length + 1}`);
        if (name === null) return;
        createNewBatch(name.trim() || undefined);
        populateBatchSelect($('#postConfirmBatchSelect'));
        $('#postConfirmBatchSelect').value = state.activeBatchId;
        // Re-enable Add button for newly created batch
        const addBtn = $('#postConfirmAddBtn');
        if (addBtn) addBtn.disabled = false;
        const status = $('#postConfirmBatchStatus');
        if (status) { status.textContent = ''; status.style.display = 'none'; }
        showToast(`Batch "${getActiveBatch()?.name}" created!`, 'success');
    });
}

/** Show the post-confirm panel with batch selection + scan-next */
function showPostConfirmPanel() {
    const panel = $('#postConfirmPanel');
    const actionBtns = $('#actionButtons');
    if (!panel) return;

    // Populate batch selector
    populateBatchSelect($('#postConfirmBatchSelect'));

    // Show receipt ID
    const receiptId = state.currentReceiptData?.receipt_data?.receipt_id;
    const idEl = $('#postConfirmReceiptId');
    if (idEl) idEl.textContent = receiptId || '';

    // Check if already in a batch
    const dbId = state.currentReceiptData?.receipt_data?.db_id;
    const existingBatch = dbId ? findReceiptBatch(dbId) : null;
    const status = $('#postConfirmBatchStatus');
    const addBtn = $('#postConfirmAddBtn');
    if (existingBatch) {
        if (status) { status.textContent = `✓ In "${existingBatch.name}"`; status.style.display = 'inline'; }
        if (addBtn) addBtn.disabled = true;
    } else {
        if (status) { status.textContent = ''; status.style.display = 'none'; }
        if (addBtn) addBtn.disabled = false;
    }

    // Hide action buttons row, show post-confirm panel
    if (actionBtns) actionBtns.style.display = 'none';
    panel.style.display = 'block';
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

/** Populate a <select> with all batches */
function populateBatchSelect(select) {
    if (!select) return;
    select.innerHTML = '';
    if (state.batches.length === 0) {
        const opt = document.createElement('option');
        opt.value = '';
        opt.textContent = 'No batches yet — create one →';
        opt.disabled = true;
        select.appendChild(opt);
        return;
    }
    state.batches.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = `${b.name} (${b.receiptIds.length})`;
        select.appendChild(opt);
    });
    if (state.activeBatchId) select.value = state.activeBatchId;
}

function resetScanUI() {
    dropZone.style.display = 'block';
    $('#processing').style.display = 'none';
    $('#resultsContainer').style.display = 'none';
    fileInput.value = '';
    $('#progressFill').style.width = '0%';
    $('#receiptImage').src = '';
    $('#exportExcelBtn').style.display = 'none';
    state.currentReceiptData = null;
    state.isDirty = false;
    state.confirmed = false;
    state.isProcessing = false;
    state._abortController = null;
    state._removedItemIds = [];
    clearProgressInterval();
    // Reset confirm button
    const confirmBtn = $('#confirmBtn');
    confirmBtn.disabled = false;
    confirmBtn.innerHTML = '<i data-lucide="check" style="width:15px;height:15px"></i> Confirm &amp; Save';
    confirmBtn.classList.remove('btn-confirmed');
    // Hide post-confirm panel, restore action buttons
    const postPanel = $('#postConfirmPanel');
    if (postPanel) postPanel.style.display = 'none';
    const actionBtns = $('#actionButtons');
    if (actionBtns) actionBtns.style.display = 'flex';
    // Reset warnings
    $('#warnings').style.display = 'none';
    // BUG FIX: Remove dynamically injected pipeline/azure info nodes.
    // displayResults() inserts .pipeline-breakdown and .azure-scan-info divs
    // next to the processing-time element on every scan.  Without removal they
    // accumulate and stack on top of each other across multiple scans.
    document.querySelector('.pipeline-breakdown')?.remove();
    document.querySelector('.azure-scan-info')?.remove();
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ─── Batch Management ────────────────────────────────────────────────────────
function updateBatchBar() {
    const bar = $('#batchBar');
    const select = $('#batchSelect');
    if (!bar) return;

    if (state.batches.length === 0) {
        bar.style.display = 'none';
        return;
    }

    bar.style.display = 'flex';
    // Populate batch selector dropdown
    if (select) {
        const prevVal = select.value;
        select.innerHTML = '';
        state.batches.forEach(b => {
            const opt = document.createElement('option');
            opt.value = b.id;
            opt.textContent = `${b.name} (${b.receiptIds.length})`;
            select.appendChild(opt);
        });
        if (state.activeBatchId) select.value = state.activeBatchId;
        else if (prevVal) select.value = prevVal;
    }
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// Switch active batch via dropdown
if ($('#batchSelect')) {
    $('#batchSelect').addEventListener('change', (e) => {
        state.activeBatchId = e.target.value;
        saveBatchState();
        showToast(`Switched to "${getActiveBatch()?.name}"`, 'info');
        // Refresh receipts tab if visible
        if (document.querySelector('.nav-btn.active')?.dataset.tab === 'receipts') loadReceipts();
    });
}

// Delete current batch
if ($('#deleteBatchBtn')) {
    $('#deleteBatchBtn').addEventListener('click', () => {
        const batch = getActiveBatch();
        if (!batch) return;
        const msg = batch.receiptIds.length > 0
            ? `Delete batch "${batch.name}" with ${batch.receiptIds.length} receipt(s)? Receipts stay in history.`
            : `Delete empty batch "${batch.name}"?`;
        if (!confirm(msg)) return;
        state.batches = state.batches.filter(b => b.id !== batch.id);
        state.activeBatchId = state.batches.length > 0 ? state.batches[0].id : null;
        saveBatchState();
        updateBatchBar();
        $('#batchDetailPanel').style.display = 'none';
        showToast(`Batch "${batch.name}" deleted.`, 'info');
        if (document.querySelector('.nav-btn.active')?.dataset.tab === 'receipts') loadReceipts();
    });
}

// Create new batch
if ($('#newBatchBtn')) {
    $('#newBatchBtn').addEventListener('click', () => {
        const name = prompt('Enter batch name:', `Batch ${state.batches.length + 1}`);
        if (name === null) return;
        createNewBatch(name.trim() || undefined);
        showToast(`Batch "${getActiveBatch()?.name}" created!`, 'success');
    });
}

// Rename batch — inline edit
if ($('#editBatchNameBtn')) {
    $('#editBatchNameBtn').addEventListener('click', () => {
        const batch = getActiveBatch();
        if (!batch) return;
        const nameInput = $('#batchNameInput');
        const select = $('#batchSelect');
        const editBtn = $('#editBatchNameBtn');
        if (!nameInput) return;
        nameInput.value = batch.name;
        nameInput.style.display = 'block';
        if (select) select.style.display = 'none';
        if (editBtn) editBtn.style.display = 'none';
        nameInput.focus();
        nameInput.select();
    });
}

// Save batch name on blur/Enter
if ($('#batchNameInput')) {
    const nameInput = $('#batchNameInput');
    const saveName = () => {
        const batch = getActiveBatch();
        if (!batch) return;
        const newName = nameInput.value.trim();
        if (newName && newName !== batch.name) {
            batch.name = newName;
            saveBatchState();
            showToast(`Batch renamed to "${newName}"`, 'success');
        }
        nameInput.style.display = 'none';
        if ($('#batchSelect')) $('#batchSelect').style.display = '';
        if ($('#editBatchNameBtn')) $('#editBatchNameBtn').style.display = '';
        updateBatchBar();
    };
    nameInput.addEventListener('blur', saveName);
    nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); nameInput.blur(); }
        if (e.key === 'Escape') {
            nameInput.style.display = 'none';
            if ($('#batchSelect')) $('#batchSelect').style.display = '';
            if ($('#editBatchNameBtn')) $('#editBatchNameBtn').style.display = '';
        }
    });
}

// View batch receipts
$('#viewBatchBtn').addEventListener('click', async () => {
    const panel = $('#batchDetailPanel');
    const body = $('#batchDetailBody');

    // Toggle panel
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        return;
    }

    if (state.batchReceiptIds.length === 0) {
        showToast('No receipts in batch.', 'warning');
        return;
    }

    panel.style.display = 'block';
    // Show active batch name in panel header
    const _batchName = getActiveBatch()?.name || 'Batch';
    const _headerH3 = panel.querySelector('.batch-detail-header h3');
    if (_headerH3) _headerH3.innerHTML = `<i data-lucide="layers" style="width:18px;height:18px"></i> ${escHtml(_batchName)}`;
    body.innerHTML = '<p class="placeholder">Loading batch receipts...</p>';

    try {
        // Fetch details for all batch receipt IDs in parallel
        const results = await Promise.all(
            state.batchReceiptIds.map(id =>
                fetch(`/api/receipts/${id}`).then(r => r.ok ? r.json() : null).catch(() => null)
            )
        );

        const receipts = results.filter(Boolean);
        if (receipts.length === 0) {
            body.innerHTML = '<p class="placeholder">No receipt data found.</p>';
            return;
        }

        body.innerHTML = receipts.map((r, idx) => {
            const items = r.items || [];
            const itemRows = items.length > 0
                ? items.map(it =>
                    `<tr><td>${escHtml(it.product_code)}</td><td>${escHtml(it.product_name)}</td><td>${it.quantity}</td></tr>`
                  ).join('')
                : '<tr><td colspan="3" style="text-align:center;color:#999">No items</td></tr>';

            return `
                <div class="batch-receipt-card">
                    <div class="batch-receipt-header">
                        <div class="batch-receipt-title">
                            <span class="batch-receipt-number">#${idx + 1}</span>
                            <strong>${escHtml(r.receipt_number || 'N/A')}</strong>
                        </div>
                        <div class="batch-receipt-meta">
                            <span>${r.scan_date || ''} ${r.scan_time || ''}</span>
                            <span class="badge">${items.length} item${items.length !== 1 ? 's' : ''}</span>
                        </div>
                    </div>
                    <table class="data-table batch-receipt-table">
                        <thead><tr><th>Code</th><th>Product</th><th>Qty</th></tr></thead>
                        <tbody>${itemRows}</tbody>
                    </table>
                    <div class="batch-receipt-actions">
                        <button class="btn btn-sm btn-accent" onclick="exportReceipt(${r.id})">Export Excel</button>
                        <button class="btn btn-sm btn-danger" onclick="removeBatchReceipt(${r.id})">Remove from Batch</button>
                    </div>
                </div>
            `;
        }).join('');

        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (err) {
        body.innerHTML = '<p class="placeholder">Failed to load batch details.</p>';
    }
});

// Close batch detail panel
$('#closeBatchPanel').addEventListener('click', () => {
    $('#batchDetailPanel').style.display = 'none';
});

// Remove a single receipt from the batch
window.removeBatchReceipt = function(id) {
    state.batchReceiptIds = state.batchReceiptIds.filter(rid => rid !== id);
    saveBatchState();
    updateBatchBar();
    // Refresh the panel if it's open
    if ($('#batchDetailPanel').style.display !== 'none') {
        if (state.batchReceiptIds.length > 0) {
            // Close first so the toggle in viewBatchBtn re-opens with fresh data
            $('#batchDetailPanel').style.display = 'none';
            $('#viewBatchBtn').click();
        } else {
            $('#batchDetailPanel').style.display = 'none';
        }
    }
    showToast('Receipt removed from batch.', 'info');
};

$('#batchExportBtn').addEventListener('click', async () => {
    if (state.batchReceiptIds.length === 0) {
        showToast('No receipts in batch.', 'warning');
        return;
    }

    const btn = $('#batchExportBtn');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    try {
        const res = await fetch('/api/export/excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receipt_ids: state.batchReceiptIds }),
        });
        const data = await safeJson(res);

        if (res.ok && data.download_url) {
            window.open(data.download_url, '_blank');
            showToast(`Excel report with ${state.batchReceiptIds.length} receipts downloaded!`, 'success');
        } else {
            throw new Error(data.detail || 'Batch export failed.');
        }
    } catch (err) {
        showToast(err.message || 'Batch export failed.', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="file-spreadsheet" style="width:14px;height:14px"></i> Export';
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
});

// ─── Receipts Tab ────────────────────────────────────────────────────────────

/** Generate receipt card HTML with batch toggle support and checkbox */
function renderReceiptCard(r) {
    const inBatch = state.batchReceiptIds.includes(r.id);
    const isChecked = (state.selectedReceiptIds || []).includes(r.id);
    const total = r.bill_total || 0;
    const conf = r.ocr_confidence_avg || 0;
    const confPct = (conf * 100).toFixed(0);
    const confClass = conf >= 0.9 ? 'badge-conf-high' : conf >= 0.7 ? 'badge-conf-mid' : 'badge-conf-low';
    const storeName = r.store_name ? escHtml(r.store_name) : '';
    return `
        <div class="receipt-card ${isChecked ? 'selected' : ''}" data-receipt-id="${r.id}">
            <input type="checkbox" class="receipt-checkbox" ${isChecked ? 'checked' : ''} onchange="toggleReceiptSelect(${r.id}, this)" aria-label="Select receipt">
            <div class="receipt-info">
                <h4>${escHtml(r.receipt_number)}</h4>
                <p>${escHtml(r.scan_date)} ${escHtml(r.scan_time || '')} · ${r.total_items || 0} items · ${escHtml(r.processing_status || 'N/A')}</p>
                <div class="receipt-meta-badges">
                    ${total > 0 ? `<span class="receipt-meta-badge badge-amount">₹${total.toLocaleString()}</span>` : ''}
                    ${conf > 0 ? `<span class="receipt-meta-badge ${confClass}" title="OCR confidence">${confPct}%</span>` : ''}
                    ${storeName ? `<span class="receipt-meta-badge">${storeName}</span>` : ''}
                    ${r.quality_grade ? `<span class="receipt-meta-badge">Grade ${r.quality_grade}</span>` : ''}
                </div>
            </div>
            <div class="receipt-actions">
                <button class="btn btn-sm btn-primary" onclick="viewReceipt(${r.id})" aria-label="View receipt ${escAttr(r.receipt_number)}">View</button>
                <button class="btn btn-sm btn-accent" onclick="exportReceipt(${r.id})" aria-label="Export receipt ${escAttr(r.receipt_number)} as Excel">Excel</button>
                <button class="btn btn-sm ${inBatch ? 'btn-in-batch' : 'btn-ghost'}" onclick="toggleBatchReceipt(${r.id}, this)" aria-label="${inBatch ? 'Remove from batch' : 'Add to batch'}">
                    ${inBatch ? '✓ In Batch' : '+ Batch'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteReceipt(${r.id})" aria-label="Delete receipt ${escAttr(r.receipt_number)}">Delete</button>
            </div>
        </div>
    `;
}

/* ── Multi-Select State ─────────────────────────────────────────────────── */
state.selectedReceiptIds = [];
state._allLoadedReceipts = [];
state._receiptsOffset = 0;
state._receiptsHasMore = false;

window.toggleReceiptSelect = function(id, cb) {
    if (cb.checked) {
        if (!state.selectedReceiptIds.includes(id)) state.selectedReceiptIds.push(id);
    } else {
        state.selectedReceiptIds = state.selectedReceiptIds.filter(x => x !== id);
    }
    // Update card selected class
    const card = cb.closest('.receipt-card');
    if (card) card.classList.toggle('selected', cb.checked);
    updateBulkBar();
};

function updateBulkBar() {
    const bar = $('#receiptBulkBar');
    const count = state.selectedReceiptIds.length;
    if (count > 0) {
        bar.style.display = 'flex';
        $('#receiptSelectedCount').textContent = count;
        // Update select-all checkbox state
        const allCbs = document.querySelectorAll('.receipt-checkbox');
        const allChecked = allCbs.length > 0 && [...allCbs].every(cb => cb.checked);
        $('#receiptSelectAll').checked = allChecked;
        $('#receiptSelectAll').indeterminate = !allChecked && count > 0;
    } else {
        bar.style.display = 'none';
        $('#receiptSelectAll').checked = false;
        $('#receiptSelectAll').indeterminate = false;
    }
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

$('#receiptSelectAll').addEventListener('change', function() {
    const cbs = document.querySelectorAll('.receipt-checkbox');
    if (this.checked) {
        cbs.forEach(cb => {
            cb.checked = true;
            const id = parseInt(cb.closest('.receipt-card').dataset.receiptId);
            if (!state.selectedReceiptIds.includes(id)) state.selectedReceiptIds.push(id);
            cb.closest('.receipt-card').classList.add('selected');
        });
    } else {
        cbs.forEach(cb => {
            cb.checked = false;
            cb.closest('.receipt-card').classList.remove('selected');
        });
        state.selectedReceiptIds = [];
    }
    updateBulkBar();
});

/* ── Bulk Actions ───────────────────────────────────────────────────────── */
$('#bulkDeleteBtn').addEventListener('click', () => {
    const count = state.selectedReceiptIds.length;
    if (count === 0) return;
    showDeleteConfirm(
        `Delete ${count} receipt${count > 1 ? 's' : ''}?`,
        `This will permanently remove ${count} receipt${count > 1 ? 's' : ''} and all their items. This cannot be undone.`,
        async () => {
            showToast(`Deleting ${count} receipts…`, 'info');
            let ok = 0, fail = 0;
            for (const id of [...state.selectedReceiptIds]) {
                try {
                    const res = await fetch(`/api/receipts/${id}`, { method: 'DELETE' });
                    if (res.ok) { ok++; state.batches.forEach(b => { b.receiptIds = b.receiptIds.filter(rid => rid !== id); }); }
                    else fail++;
                } catch { fail++; }
            }
            state.selectedReceiptIds = [];
            saveBatchState();
            updateBatchBar();
            updateBulkBar();
            showToast(`Deleted ${ok} receipt${ok !== 1 ? 's' : ''}${fail ? `, ${fail} failed` : ''}.`, fail ? 'warning' : 'success');
            loadReceipts();
        }
    );
});

$('#bulkExportBtn').addEventListener('click', async () => {
    if (state.selectedReceiptIds.length === 0) return;
    const btn = $('#bulkExportBtn');
    btn.disabled = true;
    try {
        const res = await fetch('/api/export/excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receipt_ids: state.selectedReceiptIds }),
        });
        const data = await safeJson(res);
        if (res.ok && data.download_url) {
            window.open(data.download_url, '_blank');
            showToast(`Exported ${state.selectedReceiptIds.length} receipts!`, 'success');
        } else { throw new Error(data.detail || 'Export failed.'); }
    } catch (err) { showToast(err.message, 'error'); }
    finally { btn.disabled = false; }
});

$('#bulkBatchBtn').addEventListener('click', () => {
    let added = 0;
    state.selectedReceiptIds.forEach(id => {
        if (!state.batchReceiptIds.includes(id)) {
            state.batchReceiptIds.push(id);
            added++;
        }
    });
    saveBatchState();
    updateBatchBar();
    showToast(`Added ${added} receipt${added !== 1 ? 's' : ''} to batch.`, 'success');
    // Re-render to update batch button states
    rerenderReceiptCards();
});

function rerenderReceiptCards() {
    const list = $('#receiptsList');
    const sorted = applySortFilter(state._allLoadedReceipts);
    list.innerHTML = sorted.map(r => renderReceiptCard(r)).join('');
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

/* ── Styled Delete Confirmation ─────────────────────────────────────────── */
function showDeleteConfirm(title, message, onConfirm) {
    $('#modalTitle').textContent = '';
    $('#modalMessage').innerHTML = `
        <div class="confirm-delete-body">
            <div class="confirm-delete-icon">🗑️</div>
            <div class="confirm-delete-title">${escHtml(title)}</div>
            <div class="confirm-delete-msg">${escHtml(message)}</div>
            <div class="confirm-delete-actions">
                <button class="btn-confirm-cancel" id="confirmCancelBtn">Cancel</button>
                <button class="btn-confirm-delete" id="confirmDeleteBtn">Delete</button>
            </div>
        </div>
    `;
    $('#modalConfirm').style.display = 'none';
    $('#modalCancel').style.display = 'none';
    $('#modalOverlay').style.display = 'flex';

    const close = () => {
        $('#modalOverlay').style.display = 'none';
        $('#modalConfirm').style.display = '';
        $('#modalCancel').style.display = '';
        $('#modalCancel').textContent = 'Cancel';
    };

    $('#confirmCancelBtn').addEventListener('click', close, { once: true });
    $('#confirmDeleteBtn').addEventListener('click', () => { close(); onConfirm(); }, { once: true });
    // Click outside to cancel
    const overlayClose = (e) => { if (e.target === $('#modalOverlay')) { close(); $('#modalOverlay').removeEventListener('click', overlayClose); } };
    $('#modalOverlay').addEventListener('click', overlayClose);
}

/* ── Search & Sort ──────────────────────────────────────────────────────── */
function applySortFilter(receipts) {
    let filtered = receipts;
    const q = ($('#receiptSearch')?.value || '').trim().toLowerCase();
    if (q) {
        filtered = filtered.filter(r =>
            (r.receipt_number || '').toLowerCase().includes(q) ||
            (r.store_name || '').toLowerCase().includes(q) ||
            (r.scan_date || '').includes(q) ||
            String(r.bill_total || 0).includes(q)
        );
    }
    const sort = $('#receiptSortBy')?.value || 'newest';
    const sorted = [...filtered];
    switch (sort) {
        case 'newest': sorted.sort((a, b) => (b.id || 0) - (a.id || 0)); break;
        case 'oldest': sorted.sort((a, b) => (a.id || 0) - (b.id || 0)); break;
        case 'amount-desc': sorted.sort((a, b) => (b.bill_total || 0) - (a.bill_total || 0)); break;
        case 'amount-asc': sorted.sort((a, b) => (a.bill_total || 0) - (b.bill_total || 0)); break;
        case 'items-desc': sorted.sort((a, b) => (b.total_items || 0) - (a.total_items || 0)); break;
        case 'confidence': sorted.sort((a, b) => (a.ocr_confidence_avg || 0) - (b.ocr_confidence_avg || 0)); break;
    }
    return sorted;
}

// Debounced search
let _searchTimer = null;
$('#receiptSearch').addEventListener('input', () => {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => rerenderReceiptCards(), 250);
});
$('#receiptSortBy').addEventListener('change', () => rerenderReceiptCards());

async function loadReceipts(limit = 20) {
    const list = $('#receiptsList');
    // Skeleton loading state
    list.innerHTML = Array.from({ length: 4 }, () =>
        '<div class="skeleton skeleton-card"></div>'
    ).join('');

    state.selectedReceiptIds = [];
    updateBulkBar();

    try {
        const res = await fetch(`/api/receipts?limit=${limit}`);
        const data = await res.json();

        if (!data.receipts || data.receipts.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📄</div>
                    <div class="empty-state-title">No receipts yet</div>
                    <div class="empty-state-msg">Scan your first receipt to see it here.</div>
                    <button class="btn btn-primary btn-sm" onclick="$$('.nav-btn')[0]?.click()">
                        <i data-lucide="scan" style="width:14px;height:14px"></i> Scan Now
                    </button>
                </div>`;
            if (typeof lucide !== 'undefined') lucide.createIcons();
            state._allLoadedReceipts = [];
            return;
        }

        state._allLoadedReceipts = data.receipts;
        state._receiptsOffset = data.receipts.length;
        state._receiptsHasMore = data.receipts.length >= limit;

        const sorted = applySortFilter(data.receipts);
        list.innerHTML = sorted.map(r => renderReceiptCard(r)).join('');
        if (typeof lucide !== 'undefined') lucide.createIcons();

        // Show/hide load more
        const loadMoreDiv = $('#receiptsLoadMore');
        if (loadMoreDiv) loadMoreDiv.style.display = state._receiptsHasMore ? 'block' : 'none';

    } catch (err) {
        list.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">⚠️</div>
                <div class="empty-state-title">Failed to load receipts</div>
                <div class="empty-state-msg">Check your connection and try again.</div>
                <button class="btn btn-primary btn-sm" onclick="loadReceipts()">
                    <i data-lucide="refresh-cw" style="width:14px;height:14px"></i> Retry
                </button>
            </div>`;
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
}

// Load More button
if ($('#loadMoreBtn')) {
    $('#loadMoreBtn').addEventListener('click', async () => {
        const btn = $('#loadMoreBtn');
        btn.disabled = true;
        btn.textContent = 'Loading…';
        try {
            const res = await fetch(`/api/receipts?limit=20&offset=${state._receiptsOffset}`);
            const data = await res.json();
            if (data.receipts && data.receipts.length > 0) {
                state._allLoadedReceipts.push(...data.receipts);
                state._receiptsOffset += data.receipts.length;
                state._receiptsHasMore = data.receipts.length >= 20;
                rerenderReceiptCards();
            } else {
                state._receiptsHasMore = false;
            }
            const loadMoreDiv = $('#receiptsLoadMore');
            if (loadMoreDiv) loadMoreDiv.style.display = state._receiptsHasMore ? 'block' : 'none';
        } catch { showToast('Failed to load more receipts.', 'error'); }
        finally { btn.disabled = false; btn.textContent = 'Load More…'; }
    });
}

/* ── Receipt Image Lightbox ─────────────────────────────────────────────── */
window._openReceiptLightbox = function(src) {
    // Prevent duplicate
    const existing = document.querySelector('.receipt-lightbox');
    if (existing) existing.remove();

    let scale = 1;
    const MIN_SCALE = 0.5, MAX_SCALE = 5;
    let lastDist = 0;

    const overlay = document.createElement('div');
    overlay.className = 'receipt-lightbox';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Receipt image full view');
    overlay.innerHTML = `
        <button class="receipt-lightbox-close" title="Close" aria-label="Close image viewer">✕</button>
        <img src="${src}" alt="Receipt full view" draggable="false" style="transform:scale(1)">
        <div class="zoom-controls" aria-label="Zoom controls">
            <button class="zoom-btn" data-zoom="out" aria-label="Zoom out">−</button>
            <span class="zoom-level" aria-live="polite">100%</span>
            <button class="zoom-btn" data-zoom="in" aria-label="Zoom in">+</button>
            <button class="zoom-btn" data-zoom="reset" aria-label="Reset zoom">↺</button>
        </div>
        <span class="receipt-lightbox-hint">Scroll to zoom · Pinch to zoom · Click outside to close</span>
    `;

    const img = overlay.querySelector('img');
    const zoomLabel = overlay.querySelector('.zoom-level');

    const updateZoom = () => {
        img.style.transform = `scale(${scale})`;
        zoomLabel.textContent = `${Math.round(scale * 100)}%`;
    };

    // Mouse wheel zoom
    overlay.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -0.15 : 0.15;
        scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale + delta));
        updateZoom();
    }, { passive: false });

    // Pinch-to-zoom (touch)
    overlay.addEventListener('touchstart', (e) => {
        if (e.touches.length === 2) {
            e.preventDefault();
            lastDist = Math.hypot(
                e.touches[0].clientX - e.touches[1].clientX,
                e.touches[0].clientY - e.touches[1].clientY
            );
        }
    }, { passive: false });
    overlay.addEventListener('touchmove', (e) => {
        if (e.touches.length === 2) {
            e.preventDefault();
            const dist = Math.hypot(
                e.touches[0].clientX - e.touches[1].clientX,
                e.touches[0].clientY - e.touches[1].clientY
            );
            if (lastDist > 0) {
                const delta = (dist - lastDist) * 0.005;
                scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale + delta));
                updateZoom();
            }
            lastDist = dist;
        }
    }, { passive: false });
    overlay.addEventListener('touchend', () => { lastDist = 0; });

    // Zoom button controls
    overlay.querySelectorAll('.zoom-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const action = btn.dataset.zoom;
            if (action === 'in') scale = Math.min(MAX_SCALE, scale + 0.25);
            else if (action === 'out') scale = Math.max(MIN_SCALE, scale - 0.25);
            else if (action === 'reset') scale = 1;
            updateZoom();
        });
    });

    const close = () => {
        overlay.style.opacity = '0';
        overlay.style.transition = 'opacity 0.2s ease';
        setTimeout(() => overlay.remove(), 200);
        document.removeEventListener('keydown', escHandler);
        document.body.classList.remove('focus-trap-active');
    };
    const escHandler = (e) => { if (e.key === 'Escape') close(); };

    overlay.addEventListener('click', (e) => {
        if (e.target === overlay || e.target.classList.contains('receipt-lightbox-close'))
            close();
    });
    overlay.querySelector('.receipt-lightbox-close').addEventListener('click', close);
    document.addEventListener('keydown', escHandler);

    document.body.appendChild(overlay);
    document.body.classList.add('focus-trap-active');
    overlay.querySelector('.receipt-lightbox-close').focus();
};

window.viewReceipt = async function(id) {
    // Show loading in the modal immediately
    $('#modalTitle').textContent = 'Loading...';
    $('#modalMessage').innerHTML = '<p class="placeholder">Fetching receipt data...</p>';
    $('#modalConfirm').style.display = 'none';
    $('#modalCancel').textContent = 'Close';
    $('#modalOverlay').style.display = 'flex';
    try {
        const res = await fetch(`/api/receipts/${id}`);
        if (!res.ok) {
            $('#modalOverlay').style.display = 'none';
            showToast('Receipt not found or failed to load.', 'error');
            return;
        }
        const data = await res.json();

        // Build a formatted view in the modal
        const items = data.items || [];
        const hasPrices = items.some(it => (it.unit_price || 0) > 0 || (it.line_total || 0) > 0);
        const billTotal = data.bill_total || 0;
        const totalItems = data.total_items || items.length;
        const totalQty = items.reduce((s, it) => s + (it.quantity || 0), 0);
        const computedTotal = items.reduce((s, it) => s + (it.line_total || 0), 0);
        const avgConf = data.ocr_confidence_avg || 0;

        // Build item rows — with inline editing support
        const itemRows = items.length > 0
            ? items.map((it, idx) => {
                const unitPrice = it.unit_price || 0;
                const lineTotal = it.line_total || 0;
                const conf = it.ocr_confidence || 0;
                const confClass = conf >= 0.9 ? 'conf-high' : conf >= 0.7 ? 'conf-mid' : 'conf-low';
                return `<tr data-item-id="${it.id}" role="row">
                    <td class="receipt-detail-num">${idx + 1}</td>
                    <td class="editable-cell" data-field="product_code" data-original="${escAttr(it.product_code || '')}" tabindex="0" role="gridcell" aria-label="Product code: ${escAttr(it.product_code || '')}. Click to edit."><span class="receipt-detail-code">${escHtml(it.product_code || '')}</span></td>
                    <td class="editable-cell" data-field="product_name" data-original="${escAttr(it.product_name || '')}" tabindex="0" role="gridcell" aria-label="Product name: ${escAttr(it.product_name || '')}. Click to edit.">${escHtml(it.product_name || '')}</td>
                    <td class="editable-cell receipt-detail-num" data-field="quantity" data-original="${it.quantity || 0}" tabindex="0" role="gridcell" aria-label="Quantity: ${it.quantity || 0}. Click to edit.">${it.quantity || 0}</td>
                    ${hasPrices ? `<td class="editable-cell receipt-detail-num" data-field="unit_price" data-original="${unitPrice}" tabindex="0" role="gridcell" aria-label="Unit price: ${unitPrice}. Click to edit.">${unitPrice ? unitPrice.toLocaleString() : '—'}</td>` : ''}
                    ${hasPrices ? `<td class="receipt-detail-num receipt-detail-amount">${lineTotal ? lineTotal.toLocaleString() : '—'}</td>` : ''}
                    <td><span class="receipt-conf-badge ${confClass}" title="OCR confidence">${(conf * 100).toFixed(0)}%</span></td>
                    <td class="receipt-detail-num"><button class="btn btn-sm btn-danger" onclick="window._deleteReceiptItem(${it.id}, ${id})" title="Delete item" aria-label="Delete item ${escAttr(it.product_name || '')}" style="padding:0.15rem 0.4rem;font-size:0.68rem">✕</button></td>
                </tr>`;
              }).join('')
            : `<tr><td colspan="${hasPrices ? 8 : 6}" style="text-align:center;color:var(--text-muted);padding:1.5rem">No items found</td></tr>`;

        // Summary stats row
        const summaryCards = `
            <div class="receipt-detail-stats">
                <div class="receipt-stat-card">
                    <span class="receipt-stat-value">${totalItems}</span>
                    <span class="receipt-stat-label">Items</span>
                </div>
                <div class="receipt-stat-card">
                    <span class="receipt-stat-value">${totalQty}</span>
                    <span class="receipt-stat-label">Total Qty</span>
                </div>
                ${hasPrices ? `<div class="receipt-stat-card receipt-stat-highlight">
                    <span class="receipt-stat-value">₹${(billTotal || computedTotal).toLocaleString()}</span>
                    <span class="receipt-stat-label">Grand Total</span>
                </div>` : ''}
                <div class="receipt-stat-card">
                    <span class="receipt-stat-value">${(avgConf * 100).toFixed(0)}%</span>
                    <span class="receipt-stat-label">Confidence</span>
                </div>
                ${data.quality_grade ? `<div class="receipt-stat-card">
                    <span class="receipt-stat-value receipt-grade-${data.quality_grade}">${data.quality_grade}</span>
                    <span class="receipt-stat-label">Quality</span>
                </div>` : ''}
            </div>
        `;

        // Math verification section
        let mathSection = '';
        if (hasPrices && items.length > 0) {
            const allLineCorrect = items.every(it => {
                if (!it.unit_price || !it.line_total) return true;
                const expected = it.unit_price * (it.quantity || 0);
                return Math.abs(expected - it.line_total) < 0.5;
            });
            const billMatch = billTotal > 0 && Math.abs(billTotal - computedTotal) < 0.5;

            mathSection = `
                <div class="receipt-detail-math">
                    <h4>Math Verification</h4>
                    <div class="receipt-math-checks">
                        <div class="receipt-math-row">
                            <span>${allLineCorrect ? '✅' : '⚠️'} Line Totals (Qty × Rate)</span>
                            <span class="${allLineCorrect ? 'math-pass' : 'math-warn'}">${allLineCorrect ? 'All Correct' : 'Mismatch Found'}</span>
                        </div>
                        ${billTotal > 0 ? `<div class="receipt-math-row">
                            <span>${billMatch ? '✅' : '⚠️'} Grand Total</span>
                            <span class="${billMatch ? 'math-pass' : 'math-warn'}">OCR: ₹${billTotal.toLocaleString()} | Computed: ₹${computedTotal.toLocaleString()}</span>
                        </div>` : ''}
                    </div>
                </div>
            `;
        }

        // Metadata section
        const metaItems = [];
        if (data.receipt_date) metaItems.push(`<span class="receipt-meta-tag">📅 ${escHtml(data.receipt_date)}</span>`);
        if (data.store_name) metaItems.push(`<span class="receipt-meta-tag">🏪 ${escHtml(data.store_name)}</span>`);
        if (data.quality_score) metaItems.push(`<span class="receipt-meta-tag">⭐ Score: ${data.quality_score}/100</span>`);
        const metaSection = metaItems.length > 0 ? `<div class="receipt-detail-meta">${metaItems.join('')}</div>` : '';

        const html = `
            <div class="receipt-detail-view">
                <div class="receipt-detail-header">
                    <p class="receipt-detail-id">${escHtml(data.receipt_number || 'N/A')}</p>
                    <p class="receipt-detail-date">${escHtml(data.scan_date || '')} ${escHtml(data.scan_time || '')}</p>
                    <span class="receipt-detail-status status-${(data.processing_status || 'pending').toLowerCase()}">${escHtml(data.processing_status || 'N/A')}</span>
                </div>
                ${metaSection}
                ${data.image_path ? `
                <div class="receipt-detail-image" onclick="window._openReceiptLightbox('/uploads/${escHtml(data.image_path.split(/[\/\\]/).pop())}')">
                    <img src="/uploads/${escHtml(data.image_path.split(/[\/\\]/).pop())}" alt="Receipt image">
                </div>
                ` : ''}
                ${summaryCards}
                <div class="receipt-detail-table-wrap">
                    <table class="receipt-detail-table" role="grid" aria-label="Receipt items">
                        <thead><tr>
                            <th>#</th><th>Code</th><th>Product</th><th>Qty</th>
                            ${hasPrices ? '<th>Rate</th><th>Amount</th>' : ''}
                            <th>Conf</th><th></th>
                        </tr></thead>
                        <tbody>${itemRows}</tbody>
                        ${hasPrices ? `<tfoot><tr>
                            <td colspan="${hasPrices ? 5 : 3}" class="receipt-detail-total-label">Grand Total</td>
                            <td class="receipt-detail-total-value">₹${(billTotal || computedTotal).toLocaleString()}</td>
                            <td colspan="2"></td>
                        </tr></tfoot>` : ''}
                    </table>
                </div>
                <div class="receipt-edit-toolbar">
                    <button class="btn btn-ghost btn-sm" onclick="window._addReceiptItem(${id})" aria-label="Add new item to receipt">
                        <i data-lucide="plus" style="width:14px;height:14px" aria-hidden="true"></i> Add Item
                    </button>
                </div>
                ${mathSection}
            </div>
        `;

        $('#modalTitle').textContent = `Receipt #${data.receipt_number || id}`;
        $('#modalMessage').innerHTML = html;

        // Clean up any previous close handler's event listener
        if (_activeModalClose) {
            $('#modalCancel').removeEventListener('click', _activeModalClose);
            _activeModalClose = null;
        }

        // Close handler (shared with Escape key via _activeModalClose)
        const closeModal = () => {
            $('#modalOverlay').style.display = 'none';
            $('#modalConfirm').style.display = '';
            $('#modalCancel').textContent = 'Cancel';
            $('#modalCancel').removeEventListener('click', closeModal);
            $('#modalOverlay').removeEventListener('click', overlayClose);
            _activeModalClose = null;
        };
        const overlayClose = (e) => { if (e.target === $('#modalOverlay')) closeModal(); };
        $('#modalCancel').addEventListener('click', closeModal);
        $('#modalOverlay').addEventListener('click', overlayClose);
        _activeModalClose = closeModal;

    } catch (err) {
        showToast('Failed to load receipt.', 'error');
    }
};

window.exportReceipt = async function(id) {
    try {
        const res = await fetch('/api/export/excel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ receipt_ids: [id] }),
        });
        const data = await safeJson(res);
        if (res.ok && data.download_url) {
            window.open(data.download_url, '_blank');
            showToast('Excel downloaded!', 'success');
        } else {
            throw new Error(data.detail || 'Export failed.');
        }
    } catch (err) {
        showToast(err.message || 'Export failed.', 'error');
    }
};

window.toggleBatchReceipt = function(id, btn) {
    if (state.batchReceiptIds.includes(id)) {
        // Remove from batch
        state.batchReceiptIds = state.batchReceiptIds.filter(rid => rid !== id);
        saveBatchState();
        updateBatchBar();
        if (btn) {
            btn.className = 'btn btn-sm btn-ghost';
            btn.textContent = '+ Batch';
        }
        showToast('Removed from batch.', 'info');
    } else {
        // Enforce batch size limit
        if (state.batchReceiptIds.length >= MAX_BATCH_SIZE) {
            showToast(`Batch is full (max ${MAX_BATCH_SIZE} receipts). Export or clear the batch first.`, 'warning');
            return;
        }
        // Add to batch
        state.batchReceiptIds.push(id);
        saveBatchState();
        updateBatchBar();
        if (btn) {
            btn.className = 'btn btn-sm btn-in-batch';
            btn.textContent = '✓ In Batch';
        }
        showToast('Added to batch!', 'success');
    }
};

window.deleteReceipt = async function(id) {
    showDeleteConfirm(
        'Delete this receipt?',
        'This will permanently remove the receipt and all its items. This cannot be undone.',
        async () => {
            // Disable all delete buttons to prevent double-click
            const btns = document.querySelectorAll('.receipt-actions .btn-danger');
            btns.forEach(b => { b.disabled = true; b.textContent = '...'; });
            try {
                const res = await fetch(`/api/receipts/${id}`, { method: 'DELETE' });
                if (!res.ok) {
                    const data = await res.json().catch(() => ({}));
                    throw new Error(data.detail || 'Delete failed.');
                }
                // Remove from all batches if present
                state.batches.forEach(b => { b.receiptIds = b.receiptIds.filter(rid => rid !== id); });
                state.selectedReceiptIds = state.selectedReceiptIds.filter(x => x !== id);
                saveBatchState();
                updateBatchBar();
                updateBulkBar();
                showToast('Receipt deleted.', 'success');
                loadReceipts();
            } catch (err) {
                showToast(err.message || 'Delete failed.', 'error');
                loadReceipts();
            }
        }
    );
};

// Date filter
$('#filterDateBtn').addEventListener('click', async () => {
    const date = $('#dateFilter').value;
    if (!date) {
        showToast('Please select a date.', 'warning');
        return;
    }

    const list = $('#receiptsList');
    list.innerHTML = Array.from({ length: 3 }, () => '<div class="skeleton skeleton-card"></div>').join('');

    state.selectedReceiptIds = [];
    updateBulkBar();

    try {
        const res = await fetch(`/api/receipts/date/${date}`);
        const data = await res.json();

        if (!data.receipts || data.receipts.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📅</div>
                    <div class="empty-state-title">No receipts for ${escHtml(date)}</div>
                    <div class="empty-state-msg">Try selecting a different date or scan a new receipt.</div>
                </div>`;
            state._allLoadedReceipts = [];
            return;
        }

        state._allLoadedReceipts = data.receipts;
        const sorted = applySortFilter(data.receipts);
        list.innerHTML = sorted.map(r => renderReceiptCard(r)).join('');
        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (err) {
        list.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">⚠️</div>
                <div class="empty-state-title">Failed to load receipts</div>
                <div class="empty-state-msg">Check your connection and try again.</div>
                <button class="btn btn-primary btn-sm" onclick="loadReceipts()">Retry</button>
            </div>`;
    }
});

// Show All (clear date filter and load ALL receipts)
$('#showAllBtn').addEventListener('click', () => {
    $('#dateFilter').value = '';
    $('#receiptSearch').value = '';
    loadReceipts(100);  // Load up to 100 receipts
});

// Daily report
$('#dailyReportBtn').addEventListener('click', async () => {
    const date = $('#dateFilter').value || undefined;
    const btn = $('#dailyReportBtn');
    btn.disabled = true;
    btn.textContent = 'Generating...';
    try {
        const url = date ? `/api/export/daily?date=${date}` : '/api/export/daily';
        const res = await fetch(url);
        const data = await safeJson(res);

        if (res.ok && data.download_url) {
            window.open(data.download_url, '_blank');
            showToast('Daily report downloaded!', 'success');
        } else {
            throw new Error(data.detail || 'Report generation failed.');
        }
    } catch (err) {
        showToast(err.message || 'Report generation failed.', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-lucide="file-bar-chart" style="width:14px;height:14px"></i> Daily Report';
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
});

// ─── Catalog Tab ─────────────────────────────────────────────────────────────
async function loadCatalog() {
    const tbody = $('#catalogBody');
    // Skeleton loading state
    tbody.innerHTML = Array.from({ length: 5 }, () =>
        '<tr><td colspan="5"><div class="skeleton skeleton-row"></div></td></tr>'
    ).join('');

    try {
        const res = await fetch('/api/products');
        const data = await res.json();

        if (!data.products || data.products.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="placeholder">No products. Add your first product!</td></tr>';
            return;
        }

        tbody.innerHTML = data.products.map(p => `
            <tr>
                <td><strong>${escHtml(p.product_code)}</strong></td>
                <td>${escHtml(p.product_name)}</td>
                <td>${escHtml(p.category || '-')}</td>
                <td>${escHtml(p.unit || 'Piece')}</td>
                <td>
                    <button class="btn btn-sm btn-outline" onclick="editProduct('${escAttr(p.product_code)}')" aria-label="Edit product ${escAttr(p.product_code)}">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteProduct('${escAttr(p.product_code)}')" aria-label="Delete product ${escAttr(p.product_code)}">Delete</button>
                </td>
            </tr>
        `).join('');

    } catch (err) {
        tbody.innerHTML = '<tr><td colspan="5" class="placeholder">Failed to load catalog.</td></tr>';
    }
}

// Search (debounced)
let _catalogSearchTimer = null;
$('#catalogSearch').addEventListener('input', async (e) => {
    const q = e.target.value.trim();
    if (q.length === 0) {
        clearTimeout(_catalogSearchTimer);
        loadCatalog();
        return;
    }
    // Debounce: wait 250ms after last keystroke
    clearTimeout(_catalogSearchTimer);
    _catalogSearchTimer = setTimeout(() => searchCatalog(q), 250);
});

async function searchCatalog(q) {
    if (q.length < 1) return;

    try {
        const res = await fetch(`/api/products/search?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        const tbody = $('#catalogBody');

        if (!data.products || data.products.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" class="placeholder">No matches for "${escHtml(q)}"</td></tr>`;
            return;
        }

        tbody.innerHTML = data.products.map(p => `
            <tr>
                <td><strong>${escHtml(p.product_code)}</strong></td>
                <td>${escHtml(p.product_name)}</td>
                <td>${escHtml(p.category || '-')}</td>
                <td>${escHtml(p.unit || 'Piece')}</td>
                <td>
                    <button class="btn btn-sm btn-outline" onclick="editProduct('${escAttr(p.product_code)}')" aria-label="Edit product ${escAttr(p.product_code)}">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteProduct('${escAttr(p.product_code)}')" aria-label="Delete product ${escAttr(p.product_code)}">Delete</button>
                </td>
            </tr>
        `).join('');
    } catch (err) { /* ignore */ }
}

// Add / Edit Product
$('#addProductBtn').addEventListener('click', () => {
    state.editingProduct = null;
    $('#formTitle').textContent = 'Add New Product';
    $('#prodCode').value = '';
    $('#prodCode').disabled = false;
    $('#prodName').value = '';
    $('#prodCategory').value = '';
    $('#prodUnit').value = 'Piece';
    $('#productForm').style.display = 'block';
    setTimeout(() => $('#prodCode').focus(), 50);
});

// Auto-uppercase product code as user types
$('#prodCode').addEventListener('input', (e) => {
    e.target.value = e.target.value.toUpperCase();
});

$('#cancelProductBtn').addEventListener('click', () => {
    $('#productForm').style.display = 'none';
});

// Enter key to save product form
['#prodCode', '#prodName', '#prodCategory'].forEach(sel => {
    $(sel).addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            $('#saveProductBtn').click();
        }
    });
});

// Centralized modal close (cleans up any active close handlers)
let _activeModalClose = null;
function closeActiveModal() {
    if (_activeModalClose) {
        _activeModalClose();
    } else {
        $('#modalOverlay').style.display = 'none';
    }
}

// Escape key to cancel product form or close modal
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        // Camera overlays take priority (handled by their own listeners)
        if ($('#cameraOverlay')?.style.display !== 'none') return;
        if ($('#trainCameraOverlay')?.style.display === 'flex') return;

        if ($('#productForm')?.style.display !== 'none') {
            $('#productForm').style.display = 'none';
        } else if ($('#modalOverlay')?.style.display !== 'none') {
            closeActiveModal();
        }
    }
});

$('#saveProductBtn').addEventListener('click', async () => {
    const code = $('#prodCode').value.trim().toUpperCase();
    const name = $('#prodName').value.trim();
    const category = $('#prodCategory').value.trim();
    const unit = $('#prodUnit').value;

    if (!code || !name) {
        showToast('Product code and name are required.', 'warning');
        return;
    }

    const saveBtn = $('#saveProductBtn');
    if (saveBtn.disabled) return;
    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving...';

    try {
        if (state.editingProduct) {
            // Update
            const res = await fetch(`/api/products/${state.editingProduct}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ product_name: name, category, unit }),
            });
            if (!res.ok) {
                const data = await safeJson(res);
                throw new Error(data.detail || 'Failed to update product.');
            }
            showToast('Product updated!', 'success');
        } else {
            // Create
            const res = await fetch('/api/products', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ product_code: code, product_name: name, category, unit }),
            });
            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Failed to add product.');
            }
            showToast('Product added!', 'success');
        }

        $('#productForm').style.display = 'none';
        loadCatalog();
        loadCatalogCache();

    } catch (err) {
        showToast(err.message, 'error');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save';
    }
});

window.editProduct = async function(code) {
    try {
        const res = await fetch(`/api/products/${code}`);
        if (!res.ok) {
            showToast('Product not found.', 'error');
            return;
        }
        const p = await res.json();

        state.editingProduct = code;
        $('#formTitle').textContent = `Edit Product: ${code}`;
        $('#prodCode').value = p.product_code;
        $('#prodCode').disabled = true;
        $('#prodName').value = p.product_name;
        $('#prodCategory').value = p.category || '';
        $('#prodUnit').value = p.unit || 'Piece';
        $('#productForm').style.display = 'block';
        setTimeout(() => $('#prodName').focus(), 50);

    } catch (err) {
        showToast('Failed to load product.', 'error');
    }
};

window.deleteProduct = async function(code) {
    showDeleteConfirm(
        `Delete product "${code}"?`,
        'This product will be permanently removed from the catalog. This cannot be undone.',
        async () => {
            const btns = document.querySelectorAll('#catalogTable .btn-danger');
            btns.forEach(b => { b.disabled = true; b.textContent = '...'; });
            try {
                const res = await fetch(`/api/products/${code}`, { method: 'DELETE' });
                if (!res.ok) {
                    const d = await safeJson(res);
                    throw new Error(d.detail || 'Delete failed.');
                }
                showToast('Product deleted.', 'success');
                loadCatalog();
                loadCatalogCache();
            } catch (err) {
                showToast(err.message || 'Delete failed.', 'error');
                loadCatalog();
            }
        }
    );
};

// Export catalog CSV
$('#exportCatalogBtn').addEventListener('click', () => {
    window.open('/api/products/export/csv', '_blank');
});

// ─── CSV Import ──────────────────────────────────────────────────────────────
$('#importCatalogBtn')?.addEventListener('click', () => {
    const area = $('#csvImportArea');
    area.classList.toggle('visible');
    if (area.classList.contains('visible')) {
        setTimeout(() => $('#csvBrowseBtn')?.focus(), 100);
    }
});

$('#csvBrowseBtn')?.addEventListener('click', () => {
    $('#csvFileInput')?.click();
});

$('#csvFileInput')?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (file) uploadCsvFile(file);
    e.target.value = '';
});

// Drag & drop on CSV import area
(function() {
    const area = $('#csvImportArea');
    if (!area) return;
    area.addEventListener('dragover', (e) => { e.preventDefault(); area.classList.add('dragover'); });
    area.addEventListener('dragleave', () => area.classList.remove('dragover'));
    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('dragover');
        const file = e.dataTransfer?.files?.[0];
        if (file && file.name.toLowerCase().endsWith('.csv')) {
            uploadCsvFile(file);
        } else {
            showToast('Please drop a .csv file.', 'warning');
        }
    });
})();

async function uploadCsvFile(file) {
    if (!file.name.toLowerCase().endsWith('.csv')) {
        showToast('Only CSV files are accepted.', 'warning');
        return;
    }
    if (file.size > 1024 * 1024) {
        showToast('CSV file too large (max 1MB).', 'warning');
        return;
    }

    const btn = $('#csvBrowseBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Importing…'; }

    try {
        const formData = new FormData();
        formData.append('file', file);
        const res = await fetch('/api/products/import/csv', { method: 'POST', body: formData });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Import failed.');

        const added = data.added || 0;
        const updated = data.updated || 0;
        const skipped = data.skipped || 0;
        showToast(`CSV imported! ${added} added, ${updated} updated, ${skipped} skipped.`, 'success');
        $('#csvImportArea')?.classList.remove('visible');
        loadCatalog();
        loadCatalogCache();
    } catch (err) {
        showToast(err.message || 'CSV import failed.', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Choose CSV File'; }
    }
}

// ─── Inline Editing in Receipt Detail ────────────────────────────────────────
// Delegate click on editable cells within the modal
document.addEventListener('click', (e) => {
    const cell = e.target.closest('.editable-cell');
    if (!cell || cell.querySelector('.inline-edit-input')) return;

    const field = cell.dataset.field;
    const original = cell.dataset.original || '';
    const isNum = field === 'quantity' || field === 'unit_price';

    // Replace cell content with input
    const input = document.createElement('input');
    input.type = isNum ? 'number' : 'text';
    input.className = 'inline-edit-input' + (isNum ? ' edit-num' : '');
    input.value = original;
    if (isNum) { input.min = '0'; input.step = field === 'unit_price' ? '0.01' : '1'; }
    input.setAttribute('aria-label', `Edit ${field.replace('_', ' ')}`);

    cell.innerHTML = '';
    cell.appendChild(input);
    input.focus();
    input.select();

    const save = async () => {
        const newVal = input.value.trim();
        if (newVal === original) {
            // No change, restore
            restoreCell(cell, field, original);
            return;
        }
        const row = cell.closest('tr');
        const itemId = row?.dataset.itemId;
        if (!itemId) { restoreCell(cell, field, original); return; }

        row.classList.add('receipt-row-saving');
        try {
            // Build update payload from the row's current data
            const payload = {};
            row.querySelectorAll('.editable-cell').forEach(c => {
                const f = c.dataset.field;
                const inp = c.querySelector('.inline-edit-input');
                const val = inp ? inp.value.trim() : c.dataset.original;
                if (f === 'quantity' || f === 'unit_price') payload[f] = parseFloat(val) || 0;
                else payload[f] = val;
            });
            // Compute line_total
            if (payload.unit_price !== undefined && payload.quantity !== undefined) {
                payload.line_total = payload.unit_price * payload.quantity;
            }

            const res = await fetch(`/api/receipts/items/${itemId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!res.ok) {
                const d = await safeJson(res);
                throw new Error(d.detail || 'Save failed.');
            }

            // Update original value and restore display
            cell.dataset.original = newVal;
            restoreCell(cell, field, newVal);
            row.classList.remove('receipt-row-saving');
            row.classList.add('receipt-row-saved');
            setTimeout(() => row.classList.remove('receipt-row-saved'), 600);
            showToast('Item updated.', 'success');

            // Update amount column if price or qty changed
            if (field === 'quantity' || field === 'unit_price') {
                const qtyCell = row.querySelector('[data-field="quantity"]');
                const priceCell = row.querySelector('[data-field="unit_price"]');
                const qty = parseFloat(qtyCell?.dataset.original || 0);
                const price = parseFloat(priceCell?.dataset.original || 0);
                const amountTd = row.querySelector('.receipt-detail-amount');
                if (amountTd) {
                    const lt = qty * price;
                    amountTd.textContent = lt ? lt.toLocaleString() : '—';
                }
            }
        } catch (err) {
            row.classList.remove('receipt-row-saving');
            row.classList.add('receipt-row-error');
            setTimeout(() => row.classList.remove('receipt-row-error'), 600);
            showToast(err.message || 'Failed to update item.', 'error');
            restoreCell(cell, field, original);
        }
    };

    input.addEventListener('blur', save, { once: true });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { e.preventDefault(); input.removeEventListener('blur', save); restoreCell(cell, field, original); }
        // Tab to next editable cell
        if (e.key === 'Tab') {
            e.preventDefault();
            input.blur();
            const allCells = [...document.querySelectorAll('.editable-cell')];
            const idx = allCells.indexOf(cell);
            const next = allCells[e.shiftKey ? idx - 1 : idx + 1];
            if (next) setTimeout(() => next.click(), 50);
        }
    });
});

// Also allow Enter key on editable cells to start editing
document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target.classList?.contains('editable-cell') && !e.target.querySelector('.inline-edit-input')) {
        e.target.click();
    }
});

function restoreCell(cell, field, value) {
    if (field === 'product_code') {
        cell.innerHTML = `<span class="receipt-detail-code">${escHtml(value)}</span>`;
    } else if (field === 'quantity' || field === 'unit_price') {
        const num = parseFloat(value) || 0;
        cell.textContent = num ? num.toLocaleString() : '—';
    } else {
        cell.textContent = value;
    }
}

// Delete a single receipt item
window._deleteReceiptItem = function(itemId, receiptId) {
    showDeleteConfirm(
        'Delete this item?',
        'This item will be permanently removed from the receipt.',
        async () => {
            try {
                const res = await fetch(`/api/receipts/items/${itemId}`, { method: 'DELETE' });
                if (!res.ok) throw new Error('Failed to delete item.');
                showToast('Item deleted.', 'success');
                // Refresh the receipt detail view
                window.viewReceipt(receiptId);
            } catch (err) {
                showToast(err.message || 'Delete failed.', 'error');
            }
        }
    );
};

// Add a new item to a receipt
window._addReceiptItem = async function(receiptId) {
    try {
        const res = await fetch(`/api/receipts/${receiptId}/items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ product_code: 'NEW', product_name: 'New Item', quantity: 1, unit_price: 0, line_total: 0 }),
        });
        if (!res.ok) throw new Error('Failed to add item.');
        showToast('Item added — click cells to edit.', 'success');
        window.viewReceipt(receiptId);
    } catch (err) {
        showToast(err.message || 'Failed to add item.', 'error');
    }
};

// ─── Focus Trap ──────────────────────────────────────────────────────────────
function trapFocus(container) {
    const focusableSelectors = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    const focusableEls = container.querySelectorAll(focusableSelectors);
    if (focusableEls.length === 0) return null;

    const first = focusableEls[0];
    const last = focusableEls[focusableEls.length - 1];

    const handler = (e) => {
        if (e.key !== 'Tab') return;
        if (e.shiftKey) {
            if (document.activeElement === first) { e.preventDefault(); last.focus(); }
        } else {
            if (document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
    };
    container.addEventListener('keydown', handler);
    // Auto-focus first focusable element
    first.focus();
    return handler;
}

// Apply focus trap when modal opens
const _origModalDisplay = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'style');
(function observeModal() {
    const overlay = document.getElementById('modalOverlay');
    if (!overlay) return;
    let _focusTrapHandler = null;
    const observer = new MutationObserver(() => {
        const modal = document.getElementById('modalContent') || overlay.querySelector('.modal');
        if (overlay.style.display === 'flex' || overlay.style.display === 'block') {
            document.body.classList.add('focus-trap-active');
            if (modal) {
                // Small delay for DOM to settle
                setTimeout(() => { _focusTrapHandler = trapFocus(modal); }, 50);
            }
        } else {
            document.body.classList.remove('focus-trap-active');
            if (modal && _focusTrapHandler) {
                modal.removeEventListener('keydown', _focusTrapHandler);
                _focusTrapHandler = null;
            }
        }
    });
    observer.observe(overlay, { attributes: true, attributeFilter: ['style'] });
})();

// ─── Training Onboarding ────────────────────────────────────────────────────
(function initTrainOnboarding() {
    const dismissed = localStorage.getItem('trainOnboardingDismissed');
    const guide = document.getElementById('trainOnboarding');
    if (dismissed === 'true' && guide) {
        guide.style.display = 'none';
    }
    document.getElementById('dismissOnboarding')?.addEventListener('click', () => {
        const g = document.getElementById('trainOnboarding');
        if (g) {
            g.style.transition = 'opacity 0.3s, max-height 0.3s';
            g.style.opacity = '0';
            g.style.maxHeight = '0';
            g.style.overflow = 'hidden';
            g.style.marginBottom = '0';
            g.style.padding = '0';
            setTimeout(() => { g.style.display = 'none'; }, 300);
        }
        localStorage.setItem('trainOnboardingDismissed', 'true');
    });
})();

// ─── Azure Usage Modal ───────────────────────────────────────────────────────
async function showAzureUsageModal() {
    const modal = $('#azureUsageModal');
    if (!modal) return;
    modal.style.display = 'flex';

    try {
        const resp = await fetch('/api/ocr/usage');
        const data = await resp.json();
        const usage = data.usage;
        if (!usage) return;

        const dailyUsed = usage.today?.pages_used ?? 0;
        const dailyLimit = usage.today?.pages_limit ?? 50;
        const dailyPct = Math.min(100, Math.round((dailyUsed / dailyLimit) * 100));

        const monthlyUsed = usage.this_month?.pages_used ?? 0;
        const monthlyLimit = usage.this_month?.pages_limit ?? 500;
        const monthlyPct = Math.min(100, Math.round((monthlyUsed / monthlyLimit) * 100));

        const freeRemaining = usage.cost?.free_tier_remaining ?? (500 - monthlyUsed);
        const isWithinFree = usage.cost?.is_within_free_tier !== false;

        // Daily bar
        const dailyBar = $('#azureDailyBar');
        dailyBar.style.width = dailyPct + '%';
        dailyBar.className = 'azure-meter-fill' + (dailyPct >= 90 ? ' danger' : dailyPct >= 70 ? ' warn' : '');
        $('#azureDailyText').textContent = `${dailyUsed} / ${dailyLimit} pages`;

        // Monthly bar
        const monthlyBar = $('#azureMonthlyBar');
        monthlyBar.style.width = monthlyPct + '%';
        monthlyBar.className = 'azure-meter-fill' + (monthlyPct >= 100 ? ' danger' : monthlyPct >= 80 ? ' warn' : '');
        $('#azureMonthlyText').textContent = `${monthlyUsed} / ${monthlyLimit} pages`;

        // Tier badge
        const tierBadge = $('#azureTierStatus');
        const tierDetail = $('#azureTierDetail');
        if (!isWithinFree || monthlyPct >= 100) {
            tierBadge.className = 'azure-tier-badge tier-cutoff';
            tierBadge.textContent = '🚫 FREE TIER EXHAUSTED';
            tierDetail.textContent = 'Azure OCR is disabled. All scans use local OCR (free). Resets next month.';
        } else if (monthlyPct >= 80) {
            tierBadge.className = 'azure-tier-badge tier-cutoff';
            tierBadge.textContent = `⚠ ${freeRemaining} pages left`;
            tierDetail.textContent = `You have ${freeRemaining} free pages remaining. Azure will auto-disable when exhausted — no charges.`;
        } else {
            tierBadge.className = 'azure-tier-badge tier-free';
            tierBadge.textContent = '✅ FREE TIER';
            tierDetail.textContent = `You have ${freeRemaining} free pages remaining this month. No charges will apply.`;
        }

        // Pacing info
        const pacingEl = $('#azurePacingInfo');
        const pacing = data.pacing;
        if (pacingEl && pacing) {
            const rate = pacing.sustainable_daily_rate ?? 0;
            const daysLeft = pacing.days_left_in_month ?? 0;
            pacingEl.innerHTML = `📊 <strong>Budget pacing:</strong> ~${rate} pages/day sustainable · ${daysLeft} days left in month`;
        }
    } catch (e) {
        console.warn('Failed to load Azure usage:', e);
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ─── Toast Notifications ─────────────────────────────────────────────────────
function showToast(message, type = 'info') {
    const container = $('#toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
    toast.innerHTML = `<span class="toast-icon">${icons[type] || 'ℹ'}</span><span>${escHtml(message)}</span><span class="toast-close">✕</span>`;
    container.appendChild(toast);

    // Limit max visible toasts to 5 — remove oldest if exceeded
    const toasts = container.querySelectorAll('.toast');
    if (toasts.length > 5) {
        toasts[0].remove();
    }

    const dismiss = () => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(120%)';
        toast.style.transition = 'all 0.35s cubic-bezier(0.4, 0, 0.2, 1)';
        setTimeout(() => toast.remove(), 350);
    };

    // Click anywhere on toast to dismiss
    toast.addEventListener('click', dismiss);
    toast.style.cursor = 'pointer';

    // Auto-dismiss after 3.5s
    setTimeout(dismiss, 3500);
}

// ─── Utilities ───────────────────────────────────────────────────────────────
function escHtml(str) {
    if (str == null) return '';
    const div = document.createElement('div');
    div.textContent = String(str);
    return div.innerHTML;
}
/** Escape a string for safe use inside HTML attribute values (both single and double quoted). */
function escAttr(str) {
    return escHtml(str).replace(/'/g, '&#39;').replace(/"/g, '&quot;');
}
/** Safely parse JSON from a fetch response. Returns {} on failure. */
async function safeJson(res) {
    try {
        return await res.json();
    } catch {
        return {};
    }
}

// ─── Dark Mode Toggle ───────────────────────────────────────────────────────
function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    // Update toggle icon
    const icon = document.querySelector('#themeToggle i');
    if (icon) {
        icon.setAttribute('data-lucide', next === 'dark' ? 'sun' : 'moon');
        if (typeof lucide !== 'undefined') lucide.createIcons();
    }
}
// Apply saved theme on load
(function() {
    const saved = localStorage.getItem('theme');
    if (saved === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else if (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        // Follow system preference if no manual override
        document.documentElement.setAttribute('data-theme', 'dark');
    }
    // Update icon after DOM ready
    document.addEventListener('DOMContentLoaded', () => {
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        const icon = document.querySelector('#themeToggle i');
        if (icon) {
            icon.setAttribute('data-lucide', isDark ? 'sun' : 'moon');
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
        document.getElementById('themeToggle')?.addEventListener('click', toggleTheme);
    });
})();

// ─── Global Error Boundary ──────────────────────────────────────────────────
window.addEventListener('error', (e) => {
    console.error('Uncaught error:', e.error);
    if (typeof showToast === 'function') {
        showToast('Something went wrong. Please try again.', 'error');
    }
});
window.addEventListener('unhandledrejection', (e) => {
    console.error('Unhandled promise rejection:', e.reason);
    if (typeof showToast === 'function') {
        // Don't toast on AbortError (user-initiated cancel)
        if (e.reason?.name === 'AbortError') return;
        showToast('A background operation failed. Please retry.', 'error');
    }
});

// ─── Image Zoom ──────────────────────────────────────────────────────────────
document.addEventListener('click', (e) => {
    const frame = e.target.closest('.image-frame');
    if (!frame) return;
    frame.classList.toggle('zoomed');
});

// ─── Camera Scanner ─────────────────────────────────────────────────────────
const cameraState = {
    stream: null,
    facingMode: 'environment',  // 'environment' = back camera, 'user' = front
    flashOn: false,
    flashMode: 'off',           // 'off' | 'on' | 'auto'
    autoFlashInterval: null,    // interval ID for auto-flash brightness polling
    torchSupported: false,      // whether device supports torch
    track: null,
    hasCamera: false,
};

// Detect camera availability
async function detectCamera() {
    try {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            // No camera API — fallback: camera button triggers file input with capture
            const cameraBtn = $('#openCameraBtn');
            if (cameraBtn) {
                cameraBtn.removeEventListener('click', openCamera);
                cameraBtn.addEventListener('click', () => {
                    fileInput.setAttribute('capture', 'environment');
                    fileInput.click();
                });
            }
            return;
        }
        const devices = await navigator.mediaDevices.enumerateDevices();
        const videoDevices = devices.filter(d => d.kind === 'videoinput');
        if (videoDevices.length > 0) {
            cameraState.hasCamera = true;
            if (videoDevices.length < 2 && $('#cameraSwitchBtn')) {
                $('#cameraSwitchBtn').style.display = 'none';
            }
        } else {
            // No camera hardware — fallback to file input with capture
            const cameraBtn = $('#openCameraBtn');
            if (cameraBtn) {
                cameraBtn.removeEventListener('click', openCamera);
                cameraBtn.addEventListener('click', () => {
                    fileInput.setAttribute('capture', 'environment');
                    fileInput.click();
                });
            }
        }
    } catch (e) {
        // Camera detection failed — button stays visible, fallback to file input
    }
}

// Open camera overlay
async function openCamera() {
    const overlay = $('#cameraOverlay');
    const video = $('#cameraVideo');
    if (!overlay || !video) return;

    // Reset preview
    $('#cameraPreview').style.display = 'none';

    try {
        // Request camera with optimal settings for document scanning
        const constraints = {
            video: {
                facingMode: cameraState.facingMode,
                width: { ideal: 1920 },
                height: { ideal: 1080 },
                // Focus hints for documents
                focusMode: { ideal: 'continuous' },
            },
            audio: false,
        };

        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        cameraState.stream = stream;
        cameraState.track = stream.getVideoTracks()[0];
        video.srcObject = stream;

        // Show overlay
        overlay.style.display = 'flex';
        document.body.style.overflow = 'hidden';

        // Detect torch capability
        try {
            const caps = cameraState.track.getCapabilities();
            cameraState.torchSupported = !!(caps && caps.torch);
        } catch (_) {
            cameraState.torchSupported = false;
        }

        // Check flash capability & apply flash mode
        updateFlashUI();
        applyFlashMode();

        // Re-init icons
        if (typeof lucide !== 'undefined') lucide.createIcons();

    } catch (err) {
        if (err.name === 'NotAllowedError') {
            showToast('Camera access denied. Please allow camera permissions.', 'error');
        } else if (err.name === 'NotFoundError') {
            showToast('No camera found on this device.', 'error');
        } else {
            showToast('Could not open camera. Try uploading an image instead.', 'error');
        }
        console.warn('Camera error:', err);
    }
}

// Close camera and stop stream
function closeCamera() {
    const overlay = $('#cameraOverlay');
    const video = $('#cameraVideo');

    // Stop auto-flash polling
    if (cameraState.autoFlashInterval) {
        clearInterval(cameraState.autoFlashInterval);
        cameraState.autoFlashInterval = null;
    }

    if (cameraState.stream) {
        cameraState.stream.getTracks().forEach(t => t.stop());
        cameraState.stream = null;
        cameraState.track = null;
    }

    if (video) video.srcObject = null;
    if (overlay) overlay.style.display = 'none';
    document.body.style.overflow = '';
    cameraState.flashOn = false;
    cameraState.torchSupported = false;
}

// Measure ambient brightness from a video element (0-255 scale)
function measureBrightness(videoEl) {
    const c = document.createElement('canvas');
    const sz = 64; // tiny sample for speed
    c.width = sz;
    c.height = sz;
    const cx = c.getContext('2d');
    cx.drawImage(videoEl, 0, 0, sz, sz);
    const d = cx.getImageData(0, 0, sz, sz).data;
    let sum = 0;
    for (let i = 0; i < d.length; i += 16) { // sample every 4th pixel
        sum += d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114;
    }
    return sum / (d.length / 16);
}

// Screen flash animation overlay
function showScreenFlash() {
    const overlay = $('#cameraOverlay');
    if (!overlay) return;
    let flash = overlay.querySelector('.screen-flash');
    if (!flash) {
        flash = document.createElement('div');
        flash.className = 'screen-flash';
        overlay.appendChild(flash);
    }
    flash.classList.remove('flash-animate');
    // Force reflow
    void flash.offsetWidth;
    flash.classList.add('flash-animate');
    setTimeout(() => flash.classList.remove('flash-animate'), 400);
}

// Capture photo from video stream — applies scanner-style enhancement
async function capturePhoto() {
    const video = $('#cameraVideo');
    const canvas = $('#cameraCanvas');
    if (!video || !canvas) return;

    // Flash-on-capture: if auto mode, briefly turn on torch for capture
    let flashFiredForCapture = false;
    if (cameraState.flashMode === 'auto' && cameraState.torchSupported && !cameraState.flashOn) {
        const brightness = measureBrightness(video);
        if (brightness < 80) {
            try {
                await cameraState.track.applyConstraints({ advanced: [{ torch: true }] });
                flashFiredForCapture = true;
                // Wait briefly for torch to stabilize and illuminate scene
                await new Promise(r => setTimeout(r, 300));
            } catch (_) {}
        }
    }

    // Screen flash effect for visual feedback
    showScreenFlash();

    // Set canvas to video's native resolution for maximum quality
    const vw = video.videoWidth;
    const vh = video.videoHeight;
    canvas.width = vw;
    canvas.height = vh;

    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, vw, vh);

    // Turn off capture flash if we fired it
    if (flashFiredForCapture) {
        try {
            await cameraState.track.applyConstraints({ advanced: [{ torch: false }] });
        } catch (_) {}
    }

    // ── Apply scanner-style enhancement on capture ──
    // Auto-levels + sharpen for crisp, vibrant result (like CamScanner)
    const imageData = ctx.getImageData(0, 0, vw, vh);
    const data = imageData.data;

    // Quick auto-levels (contrast stretch)
    const sample = [];
    for (let i = 0; i < data.length; i += 16) {  // Sample every 4th pixel
        sample.push(Math.round(data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114));
    }
    sample.sort((a, b) => a - b);
    const pLow = sample[Math.floor(sample.length * 0.02)];
    const pHigh = sample[Math.floor(sample.length * 0.98)];
    const range = pHigh - pLow;

    if (range > 20 && range < 240) {
        const scale = 255 / range;
        for (let i = 0; i < data.length; i += 4) {
            data[i] = Math.min(255, Math.max(0, (data[i] - pLow) * scale));
            data[i + 1] = Math.min(255, Math.max(0, (data[i + 1] - pLow) * scale));
            data[i + 2] = Math.min(255, Math.max(0, (data[i + 2] - pLow) * scale));
        }
    }
    ctx.putImageData(imageData, 0, 0);

    // Show preview
    const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
    $('#capturedImage').src = dataUrl;
    $('#cameraPreview').style.display = 'flex';

    // Flash animation on capture button
    const captureBtn = $('#cameraCaptureBtn');
    if (captureBtn) {
        captureBtn.classList.add('capturing');
        setTimeout(() => captureBtn.classList.remove('capturing'), 350);
    }

    // Re-init icons
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// Use the captured photo — enhance, convert to File and process
function useCapturedPhoto() {
    const canvas = $('#cameraCanvas');
    if (!canvas) return;

    canvas.toBlob(async (blob) => {
        if (!blob) {
            showToast('Failed to process captured image.', 'error');
            return;
        }
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const file = new File([blob], `receipt_scan_${timestamp}.jpg`, { type: 'image/jpeg' });

        // NOTE: Do NOT call enhanceImageForOCR here — the image was already
        // auto-leveled (contrast stretch) during capturePhoto().
        // The server-side OpenCV pipeline applies further enhancement;
        // adding another client-side pass would over-saturate ink and
        // over-sharpen handwriting, reducing OCR accuracy.

        // Close camera and process the file
        closeCamera();
        processFile(file);
    }, 'image/jpeg', 0.92);
}

// Retake — go back to live viewfinder
function retakePhoto() {
    $('#cameraPreview').style.display = 'none';
}

// Toggle flash mode: off → on → auto (3-state cycle like CamScanner)
async function toggleFlash() {
    if (!cameraState.track) return;

    if (!cameraState.torchSupported) {
        showToast('Flash is not available on this camera.', 'info');
        return;
    }

    // Cycle: off → on → auto → off
    const modes = ['off', 'on', 'auto'];
    const idx = modes.indexOf(cameraState.flashMode);
    cameraState.flashMode = modes[(idx + 1) % modes.length];

    await applyFlashMode();
    updateFlashUI();

    // Show toast for mode change
    const labels = { off: '⚡ Flash Off', on: '⚡ Flash On', auto: '⚡ Auto Flash' };
    showToast(labels[cameraState.flashMode], 'info');
}

// Apply the current flash mode — turn torch on/off or start auto-polling
async function applyFlashMode() {
    // Clear any existing auto-flash polling
    if (cameraState.autoFlashInterval) {
        clearInterval(cameraState.autoFlashInterval);
        cameraState.autoFlashInterval = null;
    }

    if (!cameraState.track || !cameraState.torchSupported) return;

    try {
        if (cameraState.flashMode === 'on') {
            cameraState.flashOn = true;
            await cameraState.track.applyConstraints({ advanced: [{ torch: true }] });
        } else if (cameraState.flashMode === 'off') {
            cameraState.flashOn = false;
            await cameraState.track.applyConstraints({ advanced: [{ torch: false }] });
        } else if (cameraState.flashMode === 'auto') {
            // Auto-flash: poll brightness and toggle torch based on ambient light
            startAutoFlashPolling();
        }
    } catch (err) {
        console.warn('Flash mode apply error:', err);
    }

    updateFlashUI();
}

// Auto-flash: periodically check video brightness and toggle torch
function startAutoFlashPolling() {
    const video = $('#cameraVideo');
    if (!video) return;

    const DARK_THRESHOLD = 70;   // below this → turn torch ON
    const BRIGHT_THRESHOLD = 100; // above this → turn torch OFF (hysteresis)

    const poll = async () => {
        if (!cameraState.track || cameraState.flashMode !== 'auto') return;
        try {
            const brightness = measureBrightness(video);
            const shouldBeOn = brightness < DARK_THRESHOLD;
            const shouldBeOff = brightness > BRIGHT_THRESHOLD;

            if (shouldBeOn && !cameraState.flashOn) {
                cameraState.flashOn = true;
                await cameraState.track.applyConstraints({ advanced: [{ torch: true }] });
                updateFlashUI();
            } else if (shouldBeOff && cameraState.flashOn) {
                cameraState.flashOn = false;
                await cameraState.track.applyConstraints({ advanced: [{ torch: false }] });
                updateFlashUI();
            }
        } catch (_) {}
    };

    // Poll every 1.5 seconds — not too aggressive
    poll();
    cameraState.autoFlashInterval = setInterval(poll, 1500);
}

function updateFlashUI() {
    const flashBtn = $('#cameraFlashBtn');
    const flashIcon = $('#flashIcon');
    if (!flashBtn || !flashIcon) return;

    // Remove all flash state classes
    flashBtn.classList.remove('flash-on', 'flash-auto');

    if (cameraState.flashMode === 'on') {
        flashBtn.classList.add('flash-on');
        flashIcon.setAttribute('data-lucide', 'zap');
    } else if (cameraState.flashMode === 'auto') {
        flashBtn.classList.add('flash-auto');
        flashIcon.setAttribute('data-lucide', 'zap');
    } else {
        flashIcon.setAttribute('data-lucide', 'zap-off');
    }

    // Update label badge
    let badge = flashBtn.querySelector('.flash-mode-label');
    if (!badge) {
        badge = document.createElement('span');
        badge.className = 'flash-mode-label';
        flashBtn.appendChild(badge);
    }
    badge.textContent = cameraState.flashMode === 'auto' ? 'A' : '';
    badge.style.display = cameraState.flashMode === 'auto' ? 'flex' : 'none';

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// Switch between front and back camera
async function switchCamera() {
    cameraState.facingMode = cameraState.facingMode === 'environment' ? 'user' : 'environment';
    // Stop auto-flash polling
    if (cameraState.autoFlashInterval) {
        clearInterval(cameraState.autoFlashInterval);
        cameraState.autoFlashInterval = null;
    }
    // Close current stream and reopen
    if (cameraState.stream) {
        cameraState.stream.getTracks().forEach(t => t.stop());
        cameraState.stream = null;
        cameraState.track = null;
    }
    cameraState.flashOn = false;
    // Keep flash mode preference across camera switch
    await openCamera();
}

// Camera event listeners
if ($('#openCameraBtn'))   $('#openCameraBtn').addEventListener('click', openCamera);
if ($('#cameraCloseBtn'))  $('#cameraCloseBtn').addEventListener('click', closeCamera);
if ($('#cameraCaptureBtn'))$('#cameraCaptureBtn').addEventListener('click', capturePhoto);
if ($('#useCaptureBtn'))   $('#useCaptureBtn').addEventListener('click', useCapturedPhoto);
if ($('#retakeBtn'))       $('#retakeBtn').addEventListener('click', retakePhoto);
if ($('#cameraFlashBtn'))  $('#cameraFlashBtn').addEventListener('click', toggleFlash);
if ($('#cameraSwitchBtn')) $('#cameraSwitchBtn').addEventListener('click', switchCamera);

// Gallery button in camera overlay opens file picker
if ($('#cameraGalleryBtn')) {
    $('#cameraGalleryBtn').addEventListener('click', () => {
        closeCamera();
        fileInput.click();
    });
}

// Close camera on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('#cameraOverlay').style.display !== 'none') {
        if ($('#cameraPreview').style.display !== 'none') {
            retakePhoto();
        } else {
            closeCamera();
        }
    }
});

// Detect camera on startup
detectCamera();

// ─── Initial Load ────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    // Set today's date in the filter (visual hint, no auto-filter)
    const today = new Date().toISOString().split('T')[0];
    $('#dateFilter').value = today;
    $('#dateFilter').max = today;  // Prevent selecting future dates

    // Load dashboard stats
    loadDashboardStats();

    // Restore batch bar from persisted state & prune deleted receipts
    updateBatchBar();
    const _activeBatch = getActiveBatch();
    if (_activeBatch && _activeBatch.receiptIds.length > 0) {
        Promise.all(
            _activeBatch.receiptIds.map(id =>
                fetch(`/api/receipts/${id}`).then(r => r.ok ? id : null).catch(() => null)
            )
        ).then(results => {
            const valid = results.filter(Boolean);
            if (valid.length !== _activeBatch.receiptIds.length) {
                _activeBatch.receiptIds = valid;
                saveBatchState();
                updateBatchBar();
            }
        });
    }

    // Set up online/offline indicator
    window.addEventListener('online', () => showToast('Back online!', 'success'));
    window.addEventListener('offline', () => showToast('You are offline. Some features may not work.', 'warning'));

    // Warn before closing if there are unsaved edits
    window.addEventListener('beforeunload', (e) => {
        if (state.isDirty && !state.confirmed) {
            e.preventDefault();
            e.returnValue = '';
        }
    });
});

// ═══════════════════════════════════════════════════════════════════════════════
// TRAINING TAB — Upload labeled receipts, benchmark, optimize, apply profiles
// ═══════════════════════════════════════════════════════════════════════════════

const trainState = {
    selectedFiles: [],       // Files selected for upload
    currentFileIndex: -1,    // Index of file being labeled
    isTraining: false,       // Whether a long operation is running
};

// ─── Load Training Tab ───────────────────────────────────────────────────────

async function loadTrainingTab() {
    await Promise.all([
        loadTrainingStatus(),
        loadTrainingSamples(),
        loadCurrentParams(),
        loadSavedProfiles(),
    ]);
}

// ─── Training Status Dashboard ───────────────────────────────────────────────

async function loadTrainingStatus() {
    try {
        const res = await fetch('/api/training/status');
        const data = await res.json();

        const sampleCount = $('#trainSampleCount');
        const benchmarkCount = $('#trainBenchmarkCount');
        const profileCount = $('#trainProfileCount');
        const templateCount = $('#trainTemplateCount');

        if (sampleCount) sampleCount.textContent = data.training_samples ?? 0;
        if (benchmarkCount) benchmarkCount.textContent = data.benchmark_results ?? 0;
        if (profileCount) profileCount.textContent = (data.available_profiles ?? []).length;
        if (templateCount) templateCount.textContent = (data.available_templates ?? []).length;
    } catch (e) {
        console.warn('Failed to load training status:', e);
    }
}

// ─── Training Samples List ───────────────────────────────────────────────────

async function loadTrainingSamples() {
    const tbody = $('#trainSamplesBody');
    if (!tbody) return;

    try {
        const res = await fetch('/api/training/samples');
        const data = await res.json();
        const samples = data.samples || [];

        if (samples.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No training samples yet. Upload labeled receipts above.</td></tr>';
            return;
        }

        tbody.innerHTML = samples.map(s => {
            const items = s.ground_truth?.items || s.items || [];
            const totalQty = items.reduce((sum, i) => sum + (i.quantity || 0), 0);
            const added = s.timestamp ? new Date(s.timestamp).toLocaleDateString() : '—';
            return `<tr>
                <td><code style="font-size:0.78rem">${escHtml(s.receipt_id || s.id || '—')}</code></td>
                <td>${items.length}</td>
                <td>${totalQty}</td>
                <td>${added}</td>
                <td>
                    <button class="btn btn-ghost btn-sm" onclick="deleteTrainingSample('${escAttr(s.receipt_id || s.id)}')" title="Delete sample">
                        <i data-lucide="trash-2" style="width:14px;height:14px"></i>
                    </button>
                </td>
            </tr>`;
        }).join('');

        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty-state">Failed to load training samples.</td></tr>';
    }
}

async function deleteTrainingSample(receiptId) {
    showDeleteConfirm(
        `Delete training sample "${receiptId}"?`,
        'This sample and its ground truth data will be permanently removed.',
        async () => {
            try {
                const res = await fetch(`/api/training/samples/${encodeURIComponent(receiptId)}`, { method: 'DELETE' });
                if (res.ok) {
                    showToast(`Sample "${receiptId}" deleted.`, 'success');
                    loadTrainingSamples();
                    loadTrainingStatus();
                } else {
                    const data = await res.json();
                    showToast(data.detail || 'Failed to delete sample.', 'error');
                }
            } catch (e) {
                showToast('Error deleting sample.', 'error');
            }
        }
    );
}

// ─── Upload Training Data ────────────────────────────────────────────────────

function initTrainingUpload() {
    const dropZone = $('#trainDropZone');
    const fileInput = $('#trainFileInput');
    if (!dropZone || !fileInput) return;

    // Drag & drop
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('drag-over');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        handleTrainingFiles(Array.from(e.dataTransfer.files));
    });

    // Click to browse — skip if clicking camera/browse buttons
    dropZone.addEventListener('click', (e) => {
        if (e.target.closest('.train-browse-link')) return;
        if (e.target.closest('#trainCameraBtn')) return;
        if (e.target.closest('#trainBrowseBtn')) return;
        fileInput.click();
    });

    // Prevent buttons from bubbling to dropZone click
    $('#trainCameraBtn')?.addEventListener('click', (e) => {
        e.stopPropagation();
        openTrainCamera();
    });
    $('#trainBrowseBtn')?.addEventListener('click', (e) => {
        e.stopPropagation();
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            handleTrainingFiles(Array.from(fileInput.files));
        }
        fileInput.value = '';
    });

    // Ground truth form buttons
    $('#trainAddItemBtn')?.addEventListener('click', () => addGtRow());
    $('#trainSubmitBtn')?.addEventListener('click', submitTrainingSample);
    $('#trainCancelBtn')?.addEventListener('click', resetTrainingUpload);
    $('#trainClearFileBtn')?.addEventListener('click', resetTrainingUpload);
}

function handleTrainingFiles(files) {
    const validTypes = ['image/jpeg', 'image/png', 'image/bmp', 'image/tiff', 'image/webp'];
    const validFiles = files.filter(f =>
        validTypes.includes(f.type) || f.name.match(/\.(jpg|jpeg|png|bmp|tiff|webp)$/i)
    );

    if (validFiles.length === 0) {
        showToast('No valid image files selected.', 'error');
        return;
    }

    if (validFiles.length === 1) {
        // Single file: show ground truth form
        showGroundTruthForm(validFiles[0]);
    } else {
        // Multiple files: show batch mode
        showBatchTrainingMode(validFiles);
    }
}

function showGroundTruthForm(file) {
    trainState.selectedFiles = [file];
    trainState.currentFileIndex = 0;

    const form = $('#trainGtForm');
    const dropArea = $('#trainDropZone');
    const batchArea = $('#trainBatchArea');

    // Show form, hide upload area
    if (dropArea) dropArea.style.display = 'none';
    if (batchArea) batchArea.style.display = 'none';
    if (form) form.style.display = 'block';

    // Set preview image
    const img = $('#trainPreviewImg');
    if (img) {
        const url = URL.createObjectURL(file);
        img.src = url;
        img.onload = () => URL.revokeObjectURL(url);
    }

    // Set file name
    const nameEl = $('#trainFileName');
    if (nameEl) nameEl.textContent = file.name;

    // Initialize with 2 empty rows
    const itemsContainer = $('#trainGtItems');
    if (itemsContainer) {
        itemsContainer.innerHTML = '';
        addGtRow();
        addGtRow();
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function addGtRow() {
    const container = $('#trainGtItems');
    if (!container) return;

    const row = document.createElement('div');
    row.className = 'train-gt-row';
    row.innerHTML = `
        <input type="text" placeholder="Product Code (e.g. ABC)" class="gt-code" maxlength="10">
        <input type="number" placeholder="Quantity" class="gt-qty" min="0.01" step="0.01">
        <button class="train-gt-remove-btn" title="Remove" onclick="this.parentElement.remove()">
            <i data-lucide="x" style="width:14px;height:14px"></i>
        </button>
    `;
    container.appendChild(row);
    if (typeof lucide !== 'undefined') lucide.createIcons();

    // Focus the code input
    row.querySelector('.gt-code')?.focus();
}

async function submitTrainingSample() {
    if (trainState.isTraining) return;

    const file = trainState.selectedFiles[trainState.currentFileIndex];
    if (!file) {
        showToast('No file selected.', 'error');
        return;
    }

    // Collect ground truth items
    const rows = $$('#trainGtItems .train-gt-row');
    const items = [];
    for (const row of rows) {
        const code = row.querySelector('.gt-code')?.value?.trim();
        const qty = parseFloat(row.querySelector('.gt-qty')?.value);
        if (code && !isNaN(qty) && qty > 0) {
            items.push({ code, quantity: qty });
        }
    }

    if (items.length === 0) {
        showToast('Add at least one item with product code and quantity.', 'warning');
        return;
    }

    // Build form data
    const formData = new FormData();
    formData.append('file', file);
    formData.append('ground_truth', JSON.stringify({ items }));

    trainState.isTraining = true;
    const submitBtn = $('#trainSubmitBtn');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="train-spinner"></span> Uploading…';
    }

    try {
        const res = await fetch('/api/training/upload', {
            method: 'POST',
            body: formData,
        });
        const data = await res.json();

        if (res.ok) {
            showToast(`Training sample uploaded: ${data.sample?.receipt_id || 'success'}`, 'success');
            resetTrainingUpload();
            loadTrainingSamples();
            loadTrainingStatus();
        } else {
            showToast(data.detail || 'Upload failed.', 'error');
        }
    } catch (e) {
        showToast('Network error during upload.', 'error');
    } finally {
        trainState.isTraining = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i data-lucide="upload" style="width:15px;height:15px"></i> Upload Training Sample';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    }
}

function resetTrainingUpload() {
    trainState.selectedFiles = [];
    trainState.currentFileIndex = -1;

    const form = $('#trainGtForm');
    const dropArea = $('#trainDropZone');
    const batchArea = $('#trainBatchArea');

    if (form) form.style.display = 'none';
    if (batchArea) batchArea.style.display = 'none';
    if (dropArea) dropArea.style.display = 'block';
}

// ─── Batch Training Mode ─────────────────────────────────────────────────────

function showBatchTrainingMode(files) {
    trainState.selectedFiles = files;
    trainState.currentFileIndex = 0;

    const dropArea = $('#trainDropZone');
    const batchArea = $('#trainBatchArea');
    const form = $('#trainGtForm');

    if (dropArea) dropArea.style.display = 'none';
    if (form) form.style.display = 'none';
    if (batchArea) batchArea.style.display = 'block';

    const countEl = $('#trainBatchFileCount');
    if (countEl) countEl.textContent = files.length;

    // Render file list
    const listEl = $('#trainBatchList');
    if (listEl) {
        listEl.innerHTML = files.map((f, i) => `
            <div class="train-batch-item" id="trainBatchItem_${i}">
                <span class="batch-item-name">${escHtml(f.name)}</span>
                <span class="batch-item-status">Waiting…</span>
            </div>
        `).join('');
    }

    // Show the first file's ground truth form
    showGroundTruthFormForBatch(0);
}

function showGroundTruthFormForBatch(index) {
    if (index >= trainState.selectedFiles.length) {
        // All done
        showToast(`Batch complete! ${trainState.selectedFiles.length} files processed.`, 'success');
        resetTrainingUpload();
        loadTrainingSamples();
        loadTrainingStatus();
        return;
    }

    trainState.currentFileIndex = index;
    const file = trainState.selectedFiles[index];
    const form = $('#trainGtForm');
    if (form) form.style.display = 'block';

    // Update preview
    const img = $('#trainPreviewImg');
    if (img) {
        const url = URL.createObjectURL(file);
        img.src = url;
        img.onload = () => URL.revokeObjectURL(url);
    }

    const nameEl = $('#trainFileName');
    if (nameEl) nameEl.textContent = `[${index + 1}/${trainState.selectedFiles.length}] ${file.name}`;

    // Reset items
    const itemsContainer = $('#trainGtItems');
    if (itemsContainer) {
        itemsContainer.innerHTML = '';
        addGtRow();
        addGtRow();
    }

    // Update batch item status
    const item = $(`#trainBatchItem_${index}`);
    if (item) {
        item.classList.add('pending');
        item.querySelector('.batch-item-status').textContent = '✏️ Labeling…';
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// Override submit for batch mode
const _origSubmit = submitTrainingSample;
submitTrainingSample = async function() {
    if (trainState.selectedFiles.length <= 1) {
        return _origSubmit();
    }

    // Batch mode: submit current, then show next
    if (trainState.isTraining) return;

    const index = trainState.currentFileIndex;
    const file = trainState.selectedFiles[index];
    if (!file) return;

    const rows = $$('#trainGtItems .train-gt-row');
    const items = [];
    for (const row of rows) {
        const code = row.querySelector('.gt-code')?.value?.trim();
        const qty = parseFloat(row.querySelector('.gt-qty')?.value);
        if (code && !isNaN(qty) && qty > 0) {
            items.push({ code, quantity: qty });
        }
    }

    if (items.length === 0) {
        showToast('Add at least one item with product code and quantity.', 'warning');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);
    formData.append('ground_truth', JSON.stringify({ items }));

    trainState.isTraining = true;
    const submitBtn = $('#trainSubmitBtn');
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span class="train-spinner"></span> Uploading…';
    }

    try {
        const res = await fetch('/api/training/upload', { method: 'POST', body: formData });
        const data = await res.json();
        const itemEl = $(`#trainBatchItem_${index}`);

        if (res.ok) {
            if (itemEl) {
                itemEl.classList.remove('pending');
                itemEl.classList.add('success');
                itemEl.querySelector('.batch-item-status').textContent = '✅ Uploaded';
            }
        } else {
            if (itemEl) {
                itemEl.classList.remove('pending');
                itemEl.classList.add('failed');
                itemEl.querySelector('.batch-item-status').textContent = '❌ ' + (data.detail || 'Failed');
            }
        }
    } catch (e) {
        const itemEl = $(`#trainBatchItem_${index}`);
        if (itemEl) {
            itemEl.classList.remove('pending');
            itemEl.classList.add('failed');
            itemEl.querySelector('.batch-item-status').textContent = '❌ Network error';
        }
    } finally {
        trainState.isTraining = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.innerHTML = '<i data-lucide="upload" style="width:15px;height:15px"></i> Upload Training Sample';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    }

    // Move to next file
    showGroundTruthFormForBatch(index + 1);
};

// ─── Benchmark ───────────────────────────────────────────────────────────────

async function runBenchmark() {
    if (trainState.isTraining) return;

    const verbose = $('#benchmarkVerbose')?.checked ?? true;
    const btn = $('#runBenchmarkBtn');

    trainState.isTraining = true;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="train-spinner"></span> Running Benchmark…';
    }

    try {
        const res = await fetch(`/api/training/benchmark?verbose=${verbose}`, { method: 'POST' });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.detail || 'Benchmark failed.', 'error');
            return;
        }

        displayBenchmarkResults(data);
        showToast('Benchmark complete!', 'success');
        loadTrainingStatus();
    } catch (e) {
        showToast('Network error running benchmark.', 'error');
    } finally {
        trainState.isTraining = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i data-lucide="play" style="width:15px;height:15px"></i> Run Benchmark';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    }
}

function displayBenchmarkResults(data) {
    const container = $('#benchmarkResults');
    const metricsGrid = $('#benchmarkMetrics');
    const detailsContainer = $('#benchmarkDetails');
    const detailsBody = $('#benchmarkDetailsBody');

    if (!container || !metricsGrid) return;
    container.style.display = 'block';

    // Main metrics
    const metrics = data.metrics || data;
    const metricItems = [
        { label: 'Precision', value: metrics.precision, format: 'pct' },
        { label: 'Recall', value: metrics.recall, format: 'pct' },
        { label: 'F1 Score', value: metrics.f1_score, format: 'pct' },
        { label: 'Code Accuracy', value: metrics.code_accuracy, format: 'pct' },
        { label: 'Qty Accuracy', value: metrics.qty_accuracy, format: 'pct' },
        { label: 'Samples', value: metrics.total_samples || data.total_samples, format: 'num' },
    ];

    metricsGrid.innerHTML = metricItems.map(m => {
        const val = m.value;
        let displayVal, colorClass;
        if (m.format === 'pct' && val !== undefined) {
            displayVal = (val * 100).toFixed(1) + '%';
            colorClass = val >= 0.8 ? 'good' : val >= 0.5 ? 'ok' : 'poor';
        } else {
            displayVal = val ?? '—';
            colorClass = '';
        }
        return `<div class="train-metric-card">
            <div class="train-metric-value ${colorClass}">${displayVal}</div>
            <div class="train-metric-label">${m.label}</div>
        </div>`;
    }).join('');

    // Per-image details
    const details = data.details || data.per_image || [];
    if (details.length > 0 && detailsContainer && detailsBody) {
        detailsContainer.style.display = 'block';
        detailsBody.innerHTML = details.map(d => {
            const matched = d.matched_codes ?? d.matched ?? 0;
            const missing = d.missing_codes ?? d.missing ?? 0;
            const extra = d.extra_codes ?? d.extra ?? 0;
            const qtyMatch = d.qty_matches !== undefined
                ? `${d.qty_matches}/${d.qty_total || (matched + missing)}`
                : '—';
            return `<tr>
                <td><code style="font-size:0.75rem">${escHtml(d.receipt_id || '—')}</code></td>
                <td style="color:var(--accent)">${matched}</td>
                <td style="color:var(--danger)">${missing}</td>
                <td style="color:var(--warning)">${extra}</td>
                <td>${qtyMatch}</td>
            </tr>`;
        }).join('');
    } else if (detailsContainer) {
        detailsContainer.style.display = 'none';
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ─── Optimization ────────────────────────────────────────────────────────────

async function runOptimization() {
    if (trainState.isTraining) return;

    const strategy = $('#optimizeStrategy')?.value || 'smart';
    const metric = $('#optimizeMetric')?.value || 'f1_score';
    const maxRounds = parseInt($('#optimizeRounds')?.value) || 3;

    const btn = $('#runOptimizeBtn');
    trainState.isTraining = true;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="train-spinner"></span> Optimizing… This may take a while';
    }

    try {
        const res = await fetch('/api/training/optimize', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                strategy,
                metric,
                max_rounds: maxRounds,
                quick: true,
            }),
        });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.detail || 'Optimization failed.', 'error');
            return;
        }

        displayOptimizationResults(data);
        showToast('Optimization complete!', 'success');
        loadTrainingStatus();
        loadSavedProfiles();
        loadCurrentParams();
    } catch (e) {
        showToast('Network error during optimization.', 'error');
    } finally {
        trainState.isTraining = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i data-lucide="wand-2" style="width:15px;height:15px"></i> Start Optimization';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    }
}

function displayOptimizationResults(data) {
    const container = $('#optimizeResults');
    const summaryEl = $('#optimizeSummary');
    const paramsEl = $('#optimizeParams');
    const applyBtn = $('#applyProfileBtn');

    if (!container) return;
    container.style.display = 'block';

    // Summary badges
    if (summaryEl) {
        const baseMetrics = data.baseline_metrics || data.baseline || {};
        const optMetrics = data.optimized_metrics || data.best_score || {};
        const improvement = data.improvement || {};

        let summaryHtml = '';
        const metricLabels = {
            f1_score: 'F1 Score',
            precision: 'Precision',
            recall: 'Recall',
            code_accuracy: 'Code Accuracy',
            qty_accuracy: 'Qty Accuracy',
        };

        for (const [key, label] of Object.entries(metricLabels)) {
            const before = baseMetrics[key];
            const after = optMetrics[key];
            if (before !== undefined && after !== undefined) {
                const diff = ((after - before) * 100).toFixed(1);
                const improved = after > before;
                summaryHtml += `<span class="train-optimize-badge ${improved ? 'improved' : 'unchanged'}">
                    ${label}: ${(before * 100).toFixed(1)}% → ${(after * 100).toFixed(1)}%
                    ${improved ? `(+${diff}%)` : ''}
                </span>`;
            }
        }

        summaryEl.innerHTML = summaryHtml || '<span class="train-optimize-badge unchanged">Optimization complete</span>';
    }

    // Best params
    if (paramsEl && data.best_params) {
        let paramsHtml = '<div class="card-label" style="font-size:0.78rem;margin-bottom:0.5rem">' +
            '<i data-lucide="settings-2" style="width:13px;height:13px"></i> Optimized Parameters</div>';
        paramsHtml += '<div class="train-params-grid">';
        for (const [key, val] of Object.entries(data.best_params)) {
            paramsHtml += `<div class="train-param-card">
                <span class="train-param-name">${escHtml(key)}</span>
                <span class="train-param-value">${typeof val === 'number' ? val.toFixed(3) : escHtml(String(val))}</span>
            </div>`;
        }
        paramsHtml += '</div>';
        paramsEl.innerHTML = paramsHtml;
    }

    // Apply button
    if (applyBtn && data.best_params) {
        applyBtn.style.display = 'inline-flex';
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ─── Apply Profile ───────────────────────────────────────────────────────────

async function applyOptimizedProfile(profileName = 'optimized') {
    try {
        const res = await fetch('/api/training/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ profile_name: profileName }),
        });
        const data = await res.json();

        if (res.ok) {
            showToast(`Profile "${profileName}" applied! ${data.changes || 0} parameters updated.`, 'success');
            loadCurrentParams();
        } else {
            showToast(data.detail || 'Failed to apply profile.', 'error');
        }
    } catch (e) {
        showToast('Error applying profile.', 'error');
    }
}

// ─── Current Parameters ──────────────────────────────────────────────────────

async function loadCurrentParams() {
    const grid = $('#currentParamsGrid');
    if (!grid) return;

    try {
        const res = await fetch('/api/training/params');
        const data = await res.json();

        if (Object.keys(data).length === 0) {
            grid.innerHTML = '<p class="empty-state">No parameters available.</p>';
            return;
        }

        grid.innerHTML = Object.entries(data).map(([key, val]) => {
            const displayVal = typeof val === 'number'
                ? (Number.isInteger(val) ? val : val.toFixed(3))
                : String(val);
            return `<div class="train-param-card">
                <span class="train-param-name">${escHtml(key)}</span>
                <span class="train-param-value">${escHtml(displayVal)}</span>
            </div>`;
        }).join('');
    } catch (e) {
        grid.innerHTML = '<p class="empty-state">Failed to load parameters.</p>';
    }
}

// ─── Saved Profiles ──────────────────────────────────────────────────────────

async function loadSavedProfiles() {
    const container = $('#savedProfilesList');
    if (!container) return;

    try {
        const res = await fetch('/api/training/profiles');
        const data = await res.json();
        const profiles = data.profiles || [];

        if (profiles.length === 0) {
            container.innerHTML = '<p class="empty-state">No saved profiles yet. Run optimization first.</p>';
            return;
        }

        container.innerHTML = profiles.map(p => {
            const name = p.name || 'unknown';
            const strategy = p.data?.strategy || '—';
            const timestamp = p.data?.timestamp
                ? new Date(p.data.timestamp).toLocaleString()
                : '—';
            const metrics = p.data?.metrics || {};
            const f1 = metrics.f1_score !== undefined
                ? (metrics.f1_score * 100).toFixed(1) + '% F1'
                : '';

            return `<div class="train-profile-card">
                <div class="train-profile-info">
                    <span class="train-profile-name">${escHtml(name)}</span>
                    <span class="train-profile-meta">${strategy} · ${timestamp} ${f1 ? '· ' + f1 : ''}</span>
                </div>
                <button class="btn btn-primary btn-sm" onclick="applyOptimizedProfile('${escAttr(name)}')">
                    <i data-lucide="check-circle" style="width:14px;height:14px"></i>
                    Apply
                </button>
            </div>`;
        }).join('');

        if (typeof lucide !== 'undefined') lucide.createIcons();
    } catch (e) {
        container.innerHTML = '<p class="empty-state">Failed to load profiles.</p>';
    }
}

// ─── Template Learning ───────────────────────────────────────────────────────

async function learnReceiptTemplate() {
    if (trainState.isTraining) return;

    const btn = $('#learnTemplateBtn');
    trainState.isTraining = true;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="train-spinner"></span> Learning template…';
    }

    try {
        const res = await fetch('/api/training/learn-template?template_id=default', { method: 'POST' });
        const data = await res.json();

        if (!res.ok) {
            showToast(data.detail || 'Template learning failed.', 'error');
            return;
        }

        const resultEl = $('#templateResult');
        if (resultEl) {
            resultEl.style.display = 'block';
            const tmpl = data.template || {};
            resultEl.innerHTML = `
                <div class="card-label" style="font-size:0.78rem;margin-bottom:0.4rem">
                    <i data-lucide="check-circle" style="width:13px;height:13px;color:var(--accent)"></i>
                    Template learned successfully
                </div>
                <div style="font-size:0.78rem;color:var(--text-secondary)">
                    ID: <strong>${escHtml(tmpl.template_id || 'default')}</strong> ·
                    Samples analyzed: <strong>${tmpl.sample_count ?? '—'}</strong> ·
                    Regions: <strong>${tmpl.regions?.length ?? '—'}</strong>
                </div>
            `;
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }

        showToast('Receipt template learned!', 'success');
        loadTrainingStatus();
    } catch (e) {
        showToast('Error learning template.', 'error');
    } finally {
        trainState.isTraining = false;
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i data-lucide="brain" style="width:14px;height:14px"></i> Learn Receipt Template';
            if (typeof lucide !== 'undefined') lucide.createIcons();
        }
    }
}

// ─── Training Camera ─────────────────────────────────────────────────────────

const trainCameraState = {
    stream: null,
    track: null,
    facingMode: 'environment',
    flashOn: false,
    flashMode: 'off',           // 'off' | 'on' | 'auto'
    autoFlashInterval: null,
    torchSupported: false,
};

async function openTrainCamera() {
    const overlay = $('#trainCameraOverlay');
    const video = $('#trainCameraVideo');
    if (!overlay || !video) return;

    // Reset preview
    $('#trainCameraPreview').style.display = 'none';

    try {
        const constraints = {
            video: {
                facingMode: trainCameraState.facingMode,
                width: { ideal: 1920 },
                height: { ideal: 1080 },
                focusMode: { ideal: 'continuous' },
            },
            audio: false,
        };

        const stream = await navigator.mediaDevices.getUserMedia(constraints);
        trainCameraState.stream = stream;
        trainCameraState.track = stream.getVideoTracks()[0];
        video.srcObject = stream;

        overlay.style.display = 'flex';
        document.body.style.overflow = 'hidden';

        // Detect torch capability
        try {
            const caps = trainCameraState.track.getCapabilities();
            trainCameraState.torchSupported = !!(caps && caps.torch);
        } catch (_) {
            trainCameraState.torchSupported = false;
        }

        updateTrainFlashUI();
        applyTrainFlashMode();
        if (typeof lucide !== 'undefined') lucide.createIcons();

    } catch (err) {
        if (err.name === 'NotAllowedError') {
            showToast('Camera access denied. Please allow camera permissions.', 'error');
        } else if (err.name === 'NotFoundError') {
            showToast('No camera found on this device.', 'error');
        } else {
            showToast('Could not open camera. Try uploading an image instead.', 'error');
        }
        console.warn('Train camera error:', err);
    }
}

function closeTrainCamera() {
    const overlay = $('#trainCameraOverlay');
    const video = $('#trainCameraVideo');

    // Stop auto-flash polling
    if (trainCameraState.autoFlashInterval) {
        clearInterval(trainCameraState.autoFlashInterval);
        trainCameraState.autoFlashInterval = null;
    }

    if (trainCameraState.stream) {
        trainCameraState.stream.getTracks().forEach(t => t.stop());
        trainCameraState.stream = null;
        trainCameraState.track = null;
    }

    if (video) video.srcObject = null;
    if (overlay) overlay.style.display = 'none';
    document.body.style.overflow = '';
    trainCameraState.flashOn = false;
    trainCameraState.torchSupported = false;
}

function captureTrainPhoto() {
    const video = $('#trainCameraVideo');
    const canvas = $('#trainCameraCanvas');
    if (!video || !canvas) return;

    const vw = video.videoWidth;
    const vh = video.videoHeight;
    canvas.width = vw;
    canvas.height = vh;

    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, vw, vh);

    const dataUrl = canvas.toDataURL('image/jpeg', 0.92);
    $('#trainCapturedImage').src = dataUrl;
    $('#trainCameraPreview').style.display = 'flex';

    const captureBtn = $('#trainCameraCaptureBtn');
    if (captureBtn) {
        captureBtn.classList.add('capturing');
        setTimeout(() => captureBtn.classList.remove('capturing'), 350);
    }

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function useTrainCapturedPhoto() {
    const canvas = $('#trainCameraCanvas');
    if (!canvas) return;

    canvas.toBlob((blob) => {
        if (!blob) {
            showToast('Failed to process captured image.', 'error');
            return;
        }
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
        const file = new File([blob], `train_receipt_${timestamp}.jpg`, { type: 'image/jpeg' });

        closeTrainCamera();
        // Route captured photo to the ground truth form instead of scan
        handleTrainingFiles([file]);
    }, 'image/jpeg', 0.92);
}

function retakeTrainPhoto() {
    $('#trainCameraPreview').style.display = 'none';
}

async function toggleTrainFlash() {
    if (!trainCameraState.track) return;

    if (!trainCameraState.torchSupported) {
        showToast('Flash is not available on this camera.', 'info');
        return;
    }

    // Cycle: off → on → auto → off
    const modes = ['off', 'on', 'auto'];
    const idx = modes.indexOf(trainCameraState.flashMode);
    trainCameraState.flashMode = modes[(idx + 1) % modes.length];

    await applyTrainFlashMode();
    updateTrainFlashUI();

    const labels = { off: '⚡ Flash Off', on: '⚡ Flash On', auto: '⚡ Auto Flash' };
    showToast(labels[trainCameraState.flashMode], 'info');
}

async function applyTrainFlashMode() {
    if (trainCameraState.autoFlashInterval) {
        clearInterval(trainCameraState.autoFlashInterval);
        trainCameraState.autoFlashInterval = null;
    }

    if (!trainCameraState.track || !trainCameraState.torchSupported) return;

    try {
        if (trainCameraState.flashMode === 'on') {
            trainCameraState.flashOn = true;
            await trainCameraState.track.applyConstraints({ advanced: [{ torch: true }] });
        } else if (trainCameraState.flashMode === 'off') {
            trainCameraState.flashOn = false;
            await trainCameraState.track.applyConstraints({ advanced: [{ torch: false }] });
        } else if (trainCameraState.flashMode === 'auto') {
            startTrainAutoFlashPolling();
        }
    } catch (err) {
        console.warn('Train flash mode apply error:', err);
    }

    updateTrainFlashUI();
}

function startTrainAutoFlashPolling() {
    const video = $('#trainCameraVideo');
    if (!video) return;

    const DARK_THRESHOLD = 70;
    const BRIGHT_THRESHOLD = 100;

    const poll = async () => {
        if (!trainCameraState.track || trainCameraState.flashMode !== 'auto') return;
        try {
            const brightness = measureBrightness(video);
            const shouldBeOn = brightness < DARK_THRESHOLD;
            const shouldBeOff = brightness > BRIGHT_THRESHOLD;

            if (shouldBeOn && !trainCameraState.flashOn) {
                trainCameraState.flashOn = true;
                await trainCameraState.track.applyConstraints({ advanced: [{ torch: true }] });
                updateTrainFlashUI();
            } else if (shouldBeOff && trainCameraState.flashOn) {
                trainCameraState.flashOn = false;
                await trainCameraState.track.applyConstraints({ advanced: [{ torch: false }] });
                updateTrainFlashUI();
            }
        } catch (_) {}
    };

    poll();
    trainCameraState.autoFlashInterval = setInterval(poll, 1500);
}

function updateTrainFlashUI() {
    const flashBtn = $('#trainCameraFlashBtn');
    const flashIcon = $('#trainFlashIcon');
    if (!flashBtn || !flashIcon) return;

    flashBtn.classList.remove('flash-on', 'flash-auto');

    if (trainCameraState.flashMode === 'on') {
        flashBtn.classList.add('flash-on');
        flashIcon.setAttribute('data-lucide', 'zap');
    } else if (trainCameraState.flashMode === 'auto') {
        flashBtn.classList.add('flash-auto');
        flashIcon.setAttribute('data-lucide', 'zap');
    } else {
        flashIcon.setAttribute('data-lucide', 'zap-off');
    }

    let badge = flashBtn.querySelector('.flash-mode-label');
    if (!badge) {
        badge = document.createElement('span');
        badge.className = 'flash-mode-label';
        flashBtn.appendChild(badge);
    }
    badge.textContent = trainCameraState.flashMode === 'auto' ? 'A' : '';
    badge.style.display = trainCameraState.flashMode === 'auto' ? 'flex' : 'none';

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

async function switchTrainCamera() {
    trainCameraState.facingMode = trainCameraState.facingMode === 'environment' ? 'user' : 'environment';
    if (trainCameraState.autoFlashInterval) {
        clearInterval(trainCameraState.autoFlashInterval);
        trainCameraState.autoFlashInterval = null;
    }
    if (trainCameraState.stream) {
        trainCameraState.stream.getTracks().forEach(t => t.stop());
        trainCameraState.stream = null;
        trainCameraState.track = null;
    }
    trainCameraState.flashOn = false;
    await openTrainCamera();
}

// Close training camera on Escape
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('#trainCameraOverlay')?.style.display === 'flex') {
        if ($('#trainCameraPreview')?.style.display !== 'none') {
            retakeTrainPhoto();
        } else {
            closeTrainCamera();
        }
    }
});

// ─── Training Event Listeners ────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    // Initialize upload area
    initTrainingUpload();

    // Dashboard buttons
    $('#refreshTrainStatusBtn')?.addEventListener('click', loadTrainingStatus);
    $('#refreshSamplesBtn')?.addEventListener('click', loadTrainingSamples);
    $('#refreshParamsBtn')?.addEventListener('click', loadCurrentParams);

    // Benchmark
    $('#runBenchmarkBtn')?.addEventListener('click', runBenchmark);

    // Optimize
    $('#runOptimizeBtn')?.addEventListener('click', runOptimization);
    $('#applyProfileBtn')?.addEventListener('click', () => applyOptimizedProfile('optimized'));

    // Template learning
    $('#learnTemplateBtn')?.addEventListener('click', learnReceiptTemplate);

    // Training camera buttons
    $('#trainCameraCloseBtn')?.addEventListener('click', closeTrainCamera);
    $('#trainCameraCaptureBtn')?.addEventListener('click', captureTrainPhoto);
    $('#trainUseCaptureBtn')?.addEventListener('click', useTrainCapturedPhoto);
    $('#trainRetakeBtn')?.addEventListener('click', retakeTrainPhoto);
    $('#trainCameraFlashBtn')?.addEventListener('click', toggleTrainFlash);
    $('#trainCameraSwitchBtn')?.addEventListener('click', switchTrainCamera);
    $('#trainCameraGalleryBtn')?.addEventListener('click', () => {
        closeTrainCamera();
        $('#trainFileInput')?.click();
    });
});
