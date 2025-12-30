// SOSenki Mini App Client Logic

// ---------------------------------------------------------------------------
// Translations
// ---------------------------------------------------------------------------
let __translations = null;

/**
 * Load translations from backend API with ETag-based caching
 * @returns {Promise<Object>} Translations dictionary
 */
async function loadTranslations() {
    if (__translations) return __translations;
    
    try {
        // Load translations from static JSON file (no backend endpoint needed)
        const response = await fetch('/mini-app/translations.json');
        
        if (!response.ok) {
            console.error('Failed to load translations:', response.status);
            __translations = {};
            return __translations;
        }
        
        __translations = await response.json();
        console.debug('Loaded translations from static file');
        
    } catch (error) {
        console.error('Error loading translations:', error);
        __translations = {};
    }
    return __translations;
}

/**
 * Get translation for a key with optional placeholder substitution.
 * Falls back to key if translation not found.
 * @param {string} key - Flat translation key (e.g., "btn_close", "label_loading", "err_invalid_number")
 * @param {Object} params - Placeholder values for string formatting
 * @returns {string} Translated string
 */
function t(key, params = {}) {
    if (!__translations) {
        console.warn('Translations not loaded yet, using key as fallback:', key);
        return key;
    }
    
    // Access flat namespace directly
    let value = __translations[key];
    if (value === undefined) {
        console.warn('Translation key not found:', key);
        return key;
    }
    
    // Replace placeholders like {user_name} with provided values
    if (params && typeof value === 'string') {
        for (const [paramKey, paramValue] of Object.entries(params)) {
            value = value.replace(new RegExp(`\\{${paramKey}\\}`, 'g'), paramValue);
        }
    }
    
    return value;
}

/**
 * Apply translations to all DOM elements with data-i18n attributes.
 * Call this after any template is rendered to the DOM.
 */
function applyTranslations() {
    // Handle data-i18n (text content)
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        const translated = t(key);
        el.textContent = translated;
    });
    
    // Handle data-i18n-html (innerHTML for elements with HTML content like <code>)
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
        const key = el.getAttribute('data-i18n-html');
        const translated = t(key);
        el.innerHTML = translated;
    });
}

// Initialize Telegram WebApp
const tg = window.Telegram.WebApp;

// Expand WebApp to full height
tg.expand();

// Ready the WebApp
tg.ready();

// Get app container
const appContainer = document.getElementById('app');

// ---------------------------------------------------------------------------
// Telegram Native BackButton Management
// ---------------------------------------------------------------------------
let __currentBackHandler = null;

/**
 * Show Telegram's native back button with a custom handler
 * @param {Function} handler - Callback to execute when back button is pressed
 */
function showBackButton(handler) {
    // Remove previous handler if exists
    if (__currentBackHandler) {
        tg.BackButton.offClick(__currentBackHandler);
    }
    
    __currentBackHandler = handler;
    tg.BackButton.onClick(__currentBackHandler);
    tg.BackButton.show();
}

/**
 * Hide Telegram's native back button
 */
function hideBackButton() {
    if (__currentBackHandler) {
        tg.BackButton.offClick(__currentBackHandler);
        __currentBackHandler = null;
    }
    tg.BackButton.hide();
}

// ---------------------------------------------------------------------------
// Debug Console (temporary)
// ---------------------------------------------------------------------------
const debugConsole = document.getElementById('debug-console');
const debugConsoleOutput = document.getElementById('debug-console-output');

function addDebugLog(message) {
    if (!debugConsole || !debugConsoleOutput) return;
    
    debugConsole.style.display = 'block';
    const logDiv = document.createElement('div');
    logDiv.style.marginBottom = '4px';
    logDiv.style.wordBreak = 'break-all';
    logDiv.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
    debugConsoleOutput.appendChild(logDiv);
    debugConsoleOutput.scrollTop = debugConsoleOutput.scrollHeight;
}

// Override console.log and console.error
const originalLog = console.log;
const originalError = console.error;
console.log = function(...args) {
    originalLog.apply(console, args);
    addDebugLog(args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '));
};
console.error = function(...args) {
    originalError.apply(console, args);
    addDebugLog('ERROR: ' + args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '));
};

// ---------------------------------------------------------------------------
// Unified App Context (authenticated vs represented user)
// ---------------------------------------------------------------------------
let __appContext = null; // memoized context
let __selectedUserId = null; // admin-selected user ID (takes precedence)
let __authenticatedUserInfo = null; // Keep authenticated user info constant (name, isAdmin)
let __staticUrls = null; // Static URLs from /init (stakeholder_url, photo_gallery_url)
let __currentAccountId = null; // Current account ID for endpoint calls
let __usersList = null; // Users list for admin dropdown (fetched once in /init)
const __navStack = []; // Navigation stack: [{page, params}]

/**
 * Get selected user ID from URL hash parameter
 * @returns {number|null} User ID from hash or null
 */
function getSelectedUserIdFromUrl() {
    const hash = window.location.hash;
    const match = hash.match(/selected_user=(\d+)/);
    return match ? parseInt(match[1], 10) : null;
}

/**
 * Set selected user ID in URL hash parameter
 * @param {number} userId - User ID to store in hash
 */
function setSelectedUserIdInUrl(userId) {
    // Preserve existing tgWebAppData in hash, add/update selected_user parameter
    const hash = window.location.hash;
    const parts = hash.split('&');
    
    // Remove existing selected_user parameter
    const filtered = parts.filter(p => !p.includes('selected_user='));
    
    // Add new selected_user parameter
    filtered.push(`selected_user=${userId}`);
    
    window.location.hash = filtered.join('&');
}

async function getAppContext(selectedUserId = null) {
    // If selectedUserId provided, always refresh context
    if (selectedUserId !== null) {
        __selectedUserId = selectedUserId;
        __appContext = null;
    }
    
    // On first load, check URL hash for persisted selection
    if (__selectedUserId === null && __appContext === null) {
        const urlSelectedUserId = getSelectedUserIdFromUrl();
        if (urlSelectedUserId !== null) {
            __selectedUserId = urlSelectedUserId;
        }
    }
    
    if (__appContext) return __appContext;
    const initData = getInitData();
    if (!initData) {
        console.error('[context] Missing initData');
        return null;
    }
    try {
        // For admin context switching, call /user-context endpoint
        // Note: This endpoint requires selected_user_id and is admin-only
        if (__selectedUserId === null) {
            console.error('[context] No selected user ID for context switch');
            return null;
        }
        
        const url = `/api/mini-app/user-context?selected_user_id=${__selectedUserId}`;
        
        const resp = await fetchWithTmaAuth(url, initData);
        if (!resp.ok) {
            console.error('[context] user-context failed', resp.status, resp.statusText);
            return null;
        }
        const data = await resp.json();
        
        // Parse response from UserContextResponse schema
        const roles = data.roles || [];
        const isOwner = roles.includes('owner');
        const isStakeholder = roles.includes('stakeholder');
        
        // Extract and store account_id for endpoint calls
        __currentAccountId = data.account_id || null;
        
        __appContext = {
            isRepresenting: false, // Admin switching, not representing
            isAdministrator: __authenticatedUserInfo?.is_administrator || false,
            authenticatedUserId: data.user_id,
            roles,
            isOwner,
            isStakeholder,
            stakeholderUrl: __staticUrls?.stakeholder_url || null,
            representativeOf: null,
            greetingName: __authenticatedUserInfo?.name || 'User',
            photo_gallery_url: __staticUrls?.photo_gallery_url || null
        };
        console.log('[getAppContext] Context retrieved:', __appContext);
        console.log('[getAppContext] Current account ID:', __currentAccountId);
        return __appContext;
    } catch (e) {
        console.error('[context] Exception', e);
        return null;
    }
}

function refreshAppContext() { __appContext = null; }

/**
 * Extract initData - works on both Desktop and iOS
 * On iOS, Telegram may pass initData in URL hash instead of WebApp property
 */
function getInitData() {
    // First try standard WebApp property (works on desktop)
    if (tg.initData && tg.initData.trim()) {
        return tg.initData;
    }
    
    // Fallback: Try to extract from URL hash (iOS)
    // On iOS, the hash looks like: #tgWebAppData=query_id%3D...%26user%3D...
    const hash = window.location.hash;
    const match = hash.match(/tgWebAppData=([^&]*)/);
    if (match && match[1]) {
        try {
            const encodedData = match[1];
            const decodedData = decodeURIComponent(encodedData);
            return decodedData;
        } catch (e) {
            return null;
        }
    }
    return null;
}

/**
 * Fetch helper using Telegram Mini Apps recommended transport.
 * Sends POST with Authorization: "tma <initData>" and falls back to GET with X header.
 */
async function fetchWithTmaAuth(url, initData) {
    return fetch(url, {
        method: 'POST',
        headers: {
            'Authorization': 'tma ' + initData,
        },
    });
}

// Global error handler
window.addEventListener('error', (event) => {
    console.error('Uncaught error:', event.error);
    renderError(
        'An unexpected error occurred',
        event.error
    );
});

// Unhandled promise rejection handler
window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled rejection:', event.reason);
    renderError(
        'An unexpected error occurred',
        event.reason instanceof Error ? event.reason : new Error(String(event.reason))
    );
});

/**
 * Render welcome screen for registered user
 */
function renderWelcomeScreen(data) {
    const template = document.getElementById('welcome-template');
    const content = template.content.cloneNode(true);
    
    // Set user name
    const userNameSpan = content.getElementById('user-name');
    userNameSpan.textContent = data.name || 'User';
    
    // Handle Invest menu item based on investor role from user_context
    const investItem = content.getElementById('invest-item');
    const isInvestor = data.user_context && data.user_context.roles && data.user_context.roles.includes('investor');
    if (!isInvestor) {
        investItem.classList.add('disabled');
        investItem.disabled = true;
    }
    
    // Add click handlers to menu items
    const menuItems = content.querySelectorAll('.menu-item');
    menuItems.forEach(item => {
        item.addEventListener('click', () => {
            if (!item.classList.contains('disabled')) {
                handleMenuAction(item.dataset.action);
            }
        });
    });
    
    // Clear and render
    appContainer.innerHTML = '';
    appContainer.appendChild(content);
    
    // Apply translations to rendered template
    applyTranslations();
}

/**
 * Render access denied screen for non-registered user
 */
function renderAccessDenied(data) {
    const template = document.getElementById('access-denied-template');
    const content = template.content.cloneNode(true);
    
    // Clear and render
    appContainer.innerHTML = '';
    appContainer.appendChild(content);
    
    // Apply translations to rendered template
    applyTranslations();
}

/**
 * Render error screen
 */
function renderError(message, error = null) {
    const template = document.getElementById('error-template');
    const content = template.content.cloneNode(true);
    
    // Set error message
    const errorMessageEl = content.getElementById('error-message');
    errorMessageEl.textContent = message || 'Please try again later.';
    
    // Collect comprehensive debug info
    const debugInfo = {
        timestamp: new Date().toISOString(),
        userAgent: navigator.userAgent,
        platform: navigator.platform,
        url: window.location.href,
        urlHash: window.location.hash ? window.location.hash.substring(0, 200) : 'empty',
        message: message,
        error: error ? {
            message: error.message,
            stack: error.stack,
            name: error.name
        } : null,
        telegramWebApp: {
            ready: tg.isExpanded,
            version: tg.version || 'unknown',
            platform: tg.platform || 'unknown',
            initData: tg.initData ? `✓ present (${tg.initData.length} chars)` : '✗ missing',
            initDataUnsafe: tg.initDataUnsafe ? JSON.stringify(tg.initDataUnsafe).substring(0, 200) : 'null'
        },
        network: {
            online: navigator.onLine,
            connectionType: navigator.connection?.effectiveType || 'unknown'
        },
        browserCapabilities: {
            fetchAvailable: typeof fetch !== 'undefined',
            headersAvailable: typeof Headers !== 'undefined',
            corsSupported: true
        }
    };
    
    // Display debug info
    const debugInfoEl = content.getElementById('debug-info');
    debugInfoEl.textContent = JSON.stringify(debugInfo, null, 2);
    
    // Store debug info globally for copy function
    window.currentDebugInfo = JSON.stringify(debugInfo, null, 2);
    
    // Log to console
    console.error('Mini App Error:', debugInfo);
    
    // Clear and render
    appContainer.innerHTML = '';
    appContainer.appendChild(content);
    
    // Apply translations to rendered template
    applyTranslations();
}

/**
 * Copy debug info to clipboard
 */
function copyDebugInfo() {
    if (!window.currentDebugInfo) {
        alert(t('empty_data'));
        return;
    }
    
    navigator.clipboard.writeText(window.currentDebugInfo).then(() => {
        alert(t('msg_copied'));
    }).catch(err => {
        console.error('Failed to copy:', err);
        alert(t('err_copy_failed'));
    });
}

/**
 * Render user status badges from roles array
 * @param {Array<string>} roles - Array of role strings (e.g., ["investor", "owner"])
 */
function renderUserStatuses(roles) {
    const container = document.getElementById('statuses-container');
    if (!container) {
        console.warn('Status container not found');
        return;
    }
    
    // Clear existing badges
    container.innerHTML = '';
    
    // Skip rendering if no roles (should not happen - always minimum ["member"])
    if (!roles || roles.length === 0) {
        return;
    }
    
    // Filter out "member" role - never show it as badge (all users have at least one role)
    const displayRoles = roles.filter(role => role.toLowerCase() !== 'member');
    
    // Create badge elements for each role
    displayRoles.forEach(role => {
        const badge = document.createElement('span');
        badge.className = 'badge';
        // Capitalize first letter of each word
        badge.textContent = role.charAt(0).toUpperCase() + role.slice(1).toLowerCase();
        container.appendChild(badge);
    });
}

/**
 * Format date to locale string (e.g., "19 нояб. 2025")
 */
function formatDate(dateString) {
    const date = new Date(dateString);
    return date.toLocaleDateString('ru-RU', {
        year: 'numeric',
        month: 'short',
        day: 'numeric'
    });
}

/**
 * Format currency amount with locale-specific formatting
 * Single unified function for all currency displays throughout the app
 * 
 * @param {number} amount - Amount to format
 * @param {Object} options - Formatting options
 * @param {boolean} options.includeSymbol - Whether to include currency symbol (default true)
 * @param {string} options.symbolPosition - Symbol position: 'after' or 'before' (default 'after')
 * @param {number} options.minimumDecimals - Minimum decimal places (default 0)
 * @param {number} options.maximumDecimals - Maximum decimal places (default 0)
 * @param {string} options.locale - Locale code (default 'ru-RU')
 * @param {string} options.currency - Currency code (default 'RUB')
 * @returns {string} Formatted currency string (e.g., "4 658 480 ₽")
 * 
 * @example
 * formatCurrency(1234.56)                    // "1 235 ₽"
 * formatCurrency(1234.56, { includeSymbol: false })  // "1 235"
 * formatCurrency(1234.56, { symbolPosition: 'before' })  // "₽ 1 235"
 */
function formatCurrency(amount, options = {}) {
    const {
        includeSymbol = true,
        symbolPosition = 'after',  // Match backend format: "amount symbol"
        minimumDecimals = 0,       // Always integers (match backend requirement)
        maximumDecimals = 0,
        locale = 'ru-RU',
        currency = 'RUB'
    } = options;

    // Format the number only
    const formatted = new Intl.NumberFormat(locale, {
        minimumFractionDigits: minimumDecimals,
        maximumFractionDigits: maximumDecimals
    }).format(amount);

    if (!includeSymbol) return formatted;

    // Currency symbols for common currencies
    const symbols = {
        'RUB': '₽',
        'USD': '$',
        'EUR': '€',
        'GBP': '£'
    };
    const symbol = symbols[currency] || currency;

    return symbolPosition === 'after' 
        ? `${formatted} ${symbol}`
        : `${symbol} ${formatted}`;
}

/**
 * Load and render transactions from backend
 * @param {string} containerId - ID of container to render transactions into
 * @param {string} scope - Scope for filtering ('personal' or 'all'). Defaults to 'personal'
 */
async function loadTransactions(containerId = 'transactions-list', scope = 'personal') {
    try {
        const initData = getInitData();
        
        if (!initData) {
            return;
        }

        if (!__currentAccountId) {
            console.warn('Account ID not available, cannot load transactions');
            return;
        }
        
        // Build URL with account_id parameter
        let url = `/api/mini-app/transactions?account_id=${__currentAccountId}&scope=${scope}`;
        
        const response = await fetchWithTmaAuth(url, initData);

        if (!response.ok) {
            return;
        }
        
        const data = await response.json();
        
        // Render transactions
        if (data.transactions && Array.isArray(data.transactions)) {
            renderTransactionsList(data.transactions, containerId);
        }
        
    } catch (error) {
        // Error silently handled
    }
}

/**
 * Render transactions list into specified container
 * @param {Array} transactions - Array of transaction objects
 * @param {string} containerId - ID of container to render into
 */
function renderTransactionsList(transactions, containerId = 'transactions-list') {
    const container = document.getElementById(containerId);
    
    if (!container) {
        console.warn(`Transaction list container '${containerId}' not found`);
        return;
    }
    
    container.innerHTML = '';
    
    if (!transactions || transactions.length === 0) {
        container.innerHTML = `<div class="transaction-empty">${t('empty_transactions')}</div>`;
        return;
    }
    
    transactions.forEach(transaction => {
        const transactionItem = document.createElement('div');
        transactionItem.className = 'transaction-item';
        
        // Header row: date and accounts with amount
        const headerDiv = document.createElement('div');
        headerDiv.className = 'transaction-item-header';
        
        const leftDiv = document.createElement('div');
        leftDiv.className = 'transaction-item-left';
        
        const dateEl = document.createElement('div');
        dateEl.className = 'transaction-item-date';
        dateEl.textContent = formatDate(transaction.date);
        
        const accountEl = document.createElement('div');
        accountEl.className = 'transaction-item-account';
        
        // Make accounts clickable for administrators only (check authenticated user, not context)
        if (__authenticatedUserInfo?.is_administrator) {
            const fromLink = document.createElement('a');
            fromLink.href = '#';
            fromLink.className = 'account-link';
            fromLink.textContent = transaction.from_ac_name;
            fromLink.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                navigateToAccountDetails(transaction.from_account_id, transaction.from_ac_name);
            };
            
            const arrowSpan = document.createElement('span');
            arrowSpan.textContent = ' → ';
            
            const toLink = document.createElement('a');
            toLink.href = '#';
            toLink.className = 'account-link';
            toLink.textContent = transaction.to_ac_name;
            toLink.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                navigateToAccountDetails(transaction.to_account_id, transaction.to_ac_name);
            };
            
            accountEl.appendChild(fromLink);
            accountEl.appendChild(arrowSpan);
            accountEl.appendChild(toLink);
        } else {
            // Plain text for non-administrators
            accountEl.textContent = `${transaction.from_ac_name} → ${transaction.to_ac_name}`;
        }
        
        leftDiv.appendChild(dateEl);
        leftDiv.appendChild(accountEl);
        
        const amountEl = document.createElement('div');
        amountEl.className = 'transaction-item-amount';
        amountEl.textContent = formatCurrency(transaction.amount);
        
        headerDiv.appendChild(leftDiv);
        headerDiv.appendChild(amountEl);
        
        transactionItem.appendChild(headerDiv);
        
        // Description line (if exists)
        if (transaction.description) {
            const descriptionEl = document.createElement('div');
            descriptionEl.className = 'transaction-item-description';
            descriptionEl.textContent = transaction.description;
            transactionItem.appendChild(descriptionEl);
        }
        
        container.appendChild(transactionItem);
    });
}

/**
 * Load and render bills from backend
 * @param {string} containerId - ID of container to render bills into
 */
async function loadBills(containerId = 'bills-list') {
    try {
        const initData = getInitData();
        
        if (!initData) {
            return;
        }

        if (!__currentAccountId) {
            console.warn('Account ID not available, cannot load bills');
            return;
        }
        
        // Build URL with account_id parameter
        let url = `/api/mini-app/bills?account_id=${__currentAccountId}`;
        
        const response = await fetchWithTmaAuth(url, initData);

        if (!response.ok) {
            return;
        }
        
        const data = await response.json();
        
        // Render bills
        if (data.bills && Array.isArray(data.bills)) {
            renderBills(data.bills, containerId);
        }
        
    } catch (error) {
        // Error silently handled
    }
}

/**
 * Render bills list into specified container (all bill types), grouped by service periods
 * @param {Array} bills - Array of bill objects
 * @param {string} containerId - ID of container to render into
 */
function renderBills(bills, containerId = 'bills-list') {
    const container = document.getElementById(containerId);
    
    if (!container) {
        console.warn(`Bills container '${containerId}' not found`);
        return;
    }
    
    container.innerHTML = '';
    
    if (!bills || bills.length === 0) {
        container.innerHTML = `<div class="bill-empty">${t('empty_bills_list')}</div>`;
        return;
    }
    
    // Group bills by service period (period_start_date + period_end_date)
    const groupedBills = bills.reduce((groups, bill) => {
        const periodKey = `${bill.period_start_date}_${bill.period_end_date}`;
        if (!groups[periodKey]) {
            groups[periodKey] = {
                period_start_date: bill.period_start_date,
                period_end_date: bill.period_end_date,
                bills: []
            };
        }
        groups[periodKey].bills.push(bill);
        return groups;
    }, {});
    
    // Sort periods by start date (newest first)
    const sortedPeriods = Object.values(groupedBills).sort((a, b) => 
        new Date(b.period_start_date) - new Date(a.period_start_date)
    );
    
    // Render each period and its bills
    sortedPeriods.forEach(periodGroup => {
        // Create period section header
        const periodSection = document.createElement('div');
        periodSection.className = 'bills-period-section';
        
        const periodHeader = document.createElement('div');
        periodHeader.className = 'bills-period-header';
        periodHeader.textContent = `${formatDate(periodGroup.period_start_date)} - ${formatDate(periodGroup.period_end_date)}`;
        periodSection.appendChild(periodHeader);
        
        // Create bills container for this period
        const billsContainer = document.createElement('div');
        billsContainer.className = 'bills-period-bills';
        
        // Render bills for this period
        periodGroup.bills.forEach(bill => {
            const billItem = document.createElement('div');
            billItem.className = 'bill-item';
            
            // Line 1: Bill type, Property, Amount (no period dates since they're in the header)
            const typePropertyRowDiv = document.createElement('div');
            typePropertyRowDiv.className = 'bill-item-type-property-row';
            
            // Bill type badge (left)
            const badgeEl = document.createElement('span');
            badgeEl.className = `bill-type-badge bill-type-${bill.bill_type.toLowerCase()}`;
            badgeEl.textContent = formatBillType(bill.bill_type);
            typePropertyRowDiv.appendChild(badgeEl);
            
            // Property info (center)
            const propertyLeftDiv = document.createElement('div');
            propertyLeftDiv.className = 'bill-item-property-center';
            
            if (bill.property_name || bill.property_type || bill.comment) {
                const propertyEl = document.createElement('div');
                propertyEl.className = 'bill-item-property';
                
                if (bill.property_name) {
                    propertyEl.textContent = bill.property_name;
                } else if (bill.comment) {
                    propertyEl.textContent = bill.comment;
                }
                
                propertyLeftDiv.appendChild(propertyEl);
            }
            
            typePropertyRowDiv.appendChild(propertyLeftDiv);
            
            // Amount (right)
            const amountEl = document.createElement('div');
            amountEl.className = 'bill-item-amount';
            amountEl.textContent = formatCurrency(bill.bill_amount);
            
            typePropertyRowDiv.appendChild(amountEl);
            billItem.appendChild(typePropertyRowDiv);
            
            // Readings row (only for ELECTRICITY bills with end_reading)
            if (bill.bill_type && bill.bill_type.toUpperCase() === 'ELECTRICITY' && bill.end_reading !== null) {
                const readingsDiv = document.createElement('div');
                readingsDiv.className = 'bill-item-readings';
                
                const readingsText = document.createElement('span');
                readingsText.className = 'readings-range';
                
                // Display start → end if both exist, otherwise just end
                if (bill.start_reading !== null) {
                    readingsText.textContent = `${formatCurrency(bill.start_reading, { includeSymbol: false })} → ${formatCurrency(bill.end_reading, { includeSymbol: false })}`;
                } else {
                    readingsText.textContent = `${formatCurrency(bill.end_reading, { includeSymbol: false })} ${t('label_unit_kwh')}`;
                }
                
                readingsDiv.appendChild(readingsText);
                
                // Add consumption badge if available
                if (bill.consumption !== null) {
                    const consumptionBadge = document.createElement('span');
                    consumptionBadge.className = 'consumption-badge';
                    consumptionBadge.textContent = `${formatCurrency(bill.consumption, { includeSymbol: false })} ${t('label_unit_kwh')}`;
                    readingsDiv.appendChild(consumptionBadge);
                }
                
                billItem.appendChild(readingsDiv);
            }
            
            billsContainer.appendChild(billItem);
        });
        
        // Calculate and display period total
        const periodTotal = periodGroup.bills.reduce((sum, bill) => sum + bill.bill_amount, 0);
        const periodSumDiv = document.createElement('div');
        periodSumDiv.className = 'bills-period-sum';
        periodSumDiv.textContent = `${t('label_total')}: ${formatCurrency(periodTotal)}`;
        billsContainer.appendChild(periodSumDiv);
        
        periodSection.appendChild(billsContainer);
        container.appendChild(periodSection);
    });
}

/**
 * Format bill type for display
 * @param {string} billType - Bill type (electricity, shared_electricity, conservation, main or uppercase variants)
 * @returns {string} Formatted display string
 */
function formatBillType(billType) {
    const normalized = billType.toUpperCase();
    const typeMap = {
        'ELECTRICITY': t('label_bill_electricity'),
        'SHARED_ELECTRICITY': t('label_bill_shared_electricity'),
        'CONSERVATION': t('label_bill_conservation'),
        'MAIN': t('label_bill_main')
    };
    return typeMap[normalized] || billType;
}

/**
 * Render stakeholder link for owners only
 * @param {string|null} url - Stakeholder shares URL from backend (null if not owner or not configured)
 * @param {boolean} isOwner - Whether the user is an owner (from roles array)
 * @param {boolean} isStakeholder - Whether the owner has signed the stakeholder contract (from roles array)
 */
function renderStakeholderLink(url, isOwner = false, isStakeholder = false) {
    console.log('[renderStakeholderLink] url:', url, 'isOwner:', isOwner, 'isStakeholder:', isStakeholder);
    const container = document.getElementById('stakeholder-link-container');
    if (!container) {
        console.warn('Stakeholder link container not found');
        return;
    }
    
    // Clear existing content
    container.innerHTML = '';
    
    // Only render if user is owner
    if (!isOwner) {
        return;
    }
    
    // Create section for stakeholder status and link
    const section = document.createElement('div');
    section.className = 'stakeholder-section';
    
    // Create status element showing Signed or Not Signed
    const statusDiv = document.createElement('div');
    statusDiv.className = 'stakeholder-status';
    
    if (isStakeholder) {
        // Signed owner
        statusDiv.classList.add('signed');
        statusDiv.textContent = t('status_signed');
    } else {
        // Unsigned owner
        statusDiv.classList.add('not-signed');
        statusDiv.textContent = t('status_not_signed');
    }
    
    section.appendChild(statusDiv);
    
    // Create link element if URL is provided
    if (url && url.trim() !== '') {
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.className = 'stakeholder-link';
        link.textContent = t('label_view_stakeholder_shares');
        section.appendChild(link);
    }
    
    container.appendChild(section);
}

/**
 * Render representative info (if user represents someone)
 * @param {Object|null} representativeOf - Representative info object with user_id, name, telegram_id
 */
function renderRepresentativeInfo(representativeOf) {
    const container = document.getElementById('representative-info-container');
    if (!container) {
        console.warn('Representative info container not found');
        return;
    }
    
    // Clear existing content
    container.innerHTML = '';
    
    // Only render if representative_of data exists
    if (!representativeOf || !representativeOf.name) {
        // Hide the container when there's no representative info
        container.style.display = 'none';
        return;
    }
    
    // Show the container when there is representative info
    container.style.display = 'flex';
    
    // Create one-liner: "Represents [Name]"
    const representsText = document.createElement('div');
    representsText.className = 'representative-info-text';
    representsText.textContent = t('label_represents', { name: representativeOf.name });
    
    container.appendChild(representsText);
}

/**
 * Render admin user selector dropdown
 * @param {boolean} isAdministrator - Whether authenticated user is admin
 * @param {number} currentUserId - ID of current target user (for default selection)
 * @param {Array|null} users - Array of user objects from /init response (admin only)
 */
function renderAdminUserSelector(isAdministrator, currentUserId, users = null) {
    const container = document.getElementById('representative-info-container');
    if (!container) {
        console.warn('Representative info container not found');
        return;
    }
    
    // Clear existing content
    container.innerHTML = '';
    
    // Only render for administrators
    if (!isAdministrator) {
        container.style.display = 'none';
        return;
    }
    
    // Use provided users or fall back to cached list
    const usersList = users || __usersList;
    
    if (!usersList || usersList.length === 0) {
        console.warn('No users available for admin selector');
        container.style.display = 'none';
        return;
    }
    
    // Show container
    container.style.display = 'flex';
    
    // Create dropdown wrapper
    const selectorDiv = document.createElement('div');
    selectorDiv.className = 'admin-user-selector';
    
    // Create label
    const label = document.createElement('label');
    label.textContent = t('label_view_as');
    label.setAttribute('for', 'admin-user-select');
    selectorDiv.appendChild(label);
    
    // Create select element
    const select = document.createElement('select');
    select.id = 'admin-user-select';
    
    // Add options
    usersList.forEach(user => {
        const option = document.createElement('option');
        option.value = user.user_id;
        option.textContent = user.name;
        
        // Pre-select current user (admin-selected or target from /init)
        const selectedId = __selectedUserId !== null ? __selectedUserId : currentUserId;
        if (user.user_id === selectedId) {
            option.selected = true;
        }
        
        select.appendChild(option);
    });
    
    // Add change handler
    select.addEventListener('change', async (event) => {
        const selectedUserId = parseInt(event.target.value, 10);
        
        // Update global selected user ID and persist in URL
        __selectedUserId = selectedUserId;
        setSelectedUserIdInUrl(selectedUserId);
        refreshAppContext();
        
        // Get updated context for selected user
        const updatedContext = await getAppContext(selectedUserId);
        
        if (updatedContext) {
            // Reload all datasets with the new context
            await reloadAllDatasets(updatedContext);
        }
    });
    
    selectorDiv.appendChild(select);
    container.appendChild(selectorDiv);
}

/**
 * Handle menu item click
 */
async function handleMenuAction(action) {
    if (action === 'enjoy') {
        try {
            // Get photo gallery URL from cached app context (available from /init)
            if (__appContext && __appContext.photo_gallery_url) {
                tg.openLink(__appContext.photo_gallery_url);
            } else {
                tg.showAlert(t('err_gallery_not_configured'));
            }
        } catch (error) {
            console.error('Error opening gallery:', error);
            tg.showAlert(t('err_gallery'));
        }
        return;
    }
    
    // Show Telegram alert for other features (placeholder)
    tg.showAlert(t('err_feature_coming_soon', { action: action }));
}

/**
 * Reload all datasets (statuses, stakeholder link, properties, balance, transactions, bills)
 * Used when context changes (admin switch or representing user change)
 * @param {Object} context - App context object with all user info
 */
async function reloadAllDatasets(context) {
    if (!context) return;
    
    // Render static data first (no backend calls needed)
    renderUserStatuses(context.roles);
    renderStakeholderLink(context.stakeholderUrl, context.isOwner, context.isStakeholder);
    
    // Reload dynamic data from backend
    if (context.isOwner) {
        await loadProperties();
    } else {
        // Hide properties if not owner
        const propsContainer = document.getElementById('properties-container');
        if (propsContainer) {
            propsContainer.classList.remove('visible');
        }
    }
    
    // Load balance for all users (owners and staff)
    await loadBalance();
    
    // Load transactions and bills
    await loadTransactions('transactions-list', 'personal');
    await loadBills('bills-list');
}

// ---------------------------------------------------------------------------
// Stack-Based Navigation
// ---------------------------------------------------------------------------

/**
 * Navigate to a page, pushing it onto the navigation stack
 * @param {string} page - Page identifier ('welcome', 'transactions', 'accounts', 'account-details')
 * @param {Object} params - Page-specific parameters (e.g., {accountId, accountName} for account-details)
 */
function navigateTo(page, params = {}) {
    // Push to stack
    __navStack.push({ page, params });
    
    // Render the page
    renderPage(page, params);
}

/**
 * Render a page without modifying the stack (used by goBack)
 * @param {string} page - Page identifier
 * @param {Object} params - Page-specific parameters
 */
function renderPage(page, params = {}) {
    if (page === 'welcome') {
        // Welcome is special - just reload to get fresh state
        hideBackButton();
        location.reload();
        return;
    }
    
    const template = document.getElementById(`${page}-template`);
    if (!template) {
        console.error(`Template ${page}-template not found`);
        return;
    }
    
    const content = template.content.cloneNode(true);
    
    // Page-specific setup before appending to DOM
    if (page === 'account-details' && params.accountName) {
        const nameEl = content.getElementById('account-details-name');
        if (nameEl) {
            nameEl.textContent = params.accountName;
        }
    }
    
    appContainer.innerHTML = '';
    appContainer.appendChild(content);
    applyTranslations();
    
    // Show back button (all non-welcome pages have back)
    showBackButton(goBack);
    
    // Page-specific data loading
    if (page === 'transactions') {
        loadTransactions('transactions-list', 'all');
    } else if (page === 'accounts') {
        loadAccounts('accounts-list');
    } else if (page === 'account-details' && params.accountId) {
        loadAccountDetails(params.accountId);
    }
}

/**
 * Go back to previous page in navigation stack
 */
function goBack() {
    // Pop current page
    __navStack.pop();
    
    if (__navStack.length === 0) {
        // Stack empty, go to welcome
        hideBackButton();
        location.reload();
        return;
    }
    
    // Get previous page and render it (don't push again)
    const prev = __navStack[__navStack.length - 1];
    renderPage(prev.page, prev.params);
}

/**
 * Navigate to transactions page
 */
function navigateToTransactions(event) {
    event.preventDefault();
    navigateTo('transactions');
}

/**
 * Navigate to account details page
 * @param {number} accountId - Account ID to show details for
 * @param {string} accountName - Account name for header
 */
function navigateToAccountDetails(accountId, accountName) {
    navigateTo('account-details', { accountId, accountName });
}

/**
 * Load all data for account details page
 * @param {number} accountId - Account ID to load
 */
async function loadAccountDetails(accountId) {
    try {
        const initData = getInitData();
        if (!initData) return;
        
        // Load balance
        const balanceUrl = `/api/mini-app/account?account_id=${accountId}`;
        const balanceResp = await fetchWithTmaAuth(balanceUrl, initData);
        if (balanceResp.ok) {
            const balanceData = await balanceResp.json();
            if (balanceData.balance !== undefined) {
                renderBalance(
                    balanceData.balance,
                    balanceData.invert_for_display || false,
                    balanceData.suggested_payment,
                    'account-balance-container'
                );
            }
        }
        
        // Load transactions for this account
        const transUrl = `/api/mini-app/transactions?account_id=${accountId}&scope=personal`;
        const transResp = await fetchWithTmaAuth(transUrl, initData);
        if (transResp.ok) {
            const transData = await transResp.json();
            if (transData.transactions && Array.isArray(transData.transactions)) {
                renderTransactionsList(transData.transactions, 'account-transactions-list');
            }
        }
        
        // Load bills for this account
        const billsUrl = `/api/mini-app/bills?account_id=${accountId}`;
        const billsResp = await fetchWithTmaAuth(billsUrl, initData);
        if (billsResp.ok) {
            const billsData = await billsResp.json();
            if (billsData.bills && Array.isArray(billsData.bills)) {
                renderBills(billsData.bills, 'account-bills-list');
            }
        }
    } catch (error) {
        console.error('Error loading account details:', error);
    }
}

/**
 * Initialize Mini App
 */
/**
 * Load and render balance from backend
 */
async function loadBalance() {
    try {
        const initData = getInitData();
        
        if (!initData) {
            return;
        }

        if (!__currentAccountId) {
            console.warn('Account ID not available, cannot load balance');
            return;
        }
        
        // Build URL with account_id parameter
        let url = `/api/mini-app/account?account_id=${__currentAccountId}`;
        
        const response = await fetchWithTmaAuth(url, initData);

        if (!response.ok) {
            return;
        }
        
        const data = await response.json();
        
        if (data.balance !== undefined) {
            renderBalance(
                data.balance,
                data.invert_for_display || false,
                data.suggested_payment
            );
        }
        
    } catch (error) {
        // Error silently handled
    }
}

/**
 * Render balance into the balance-container
 * @param {number} balance - Balance amount (raw value)
 * @param {boolean} invert - Whether to invert the display value (for OWNER accounts)
 * @param {number|null} suggestedPayment - Suggested payment amount to show when owning debt
 * @param {string} containerId - Optional container ID (defaults to 'balance-container')
 */
function renderBalance(
    balance,
    invert = false,
    suggestedPayment = null,
    containerId = 'balance-container'
) {
    const container = document.getElementById(containerId);
    
    if (!container) {
        console.warn('Balance container not found:', containerId);
        return;
    }
    
    // Show the balance section
    container.classList.add('visible');
    
    // Apply inversion for display (OWNER accounts show inverted balance)
    const displayBalance = invert ? -balance : balance;
    
    // Find the balance-value element and update it
    const balanceValue = container.querySelector('.balance-value') || document.getElementById(containerId.replace('-container', '-value'));
    if (balanceValue) {
        const formatted = formatCurrency(Math.abs(displayBalance));
        const balanceClass = displayBalance >= 0 ? 'positive' : 'negative';
        balanceValue.className = `balance-value ${balanceClass}`;
        balanceValue.textContent = `${displayBalance >= 0 ? '+' : '-'}${formatted}`;
    }

    const suggestionEl = container.querySelector('.balance-suggestion');
    if (suggestionEl) {
        const shouldShowSuggestion = invert && balance > 0 && suggestedPayment;
        if (shouldShowSuggestion) {
            suggestionEl.textContent = t('hint_suggested_amount', {
                amount: formatCurrency(suggestedPayment),
            });
            suggestionEl.style.display = 'block';
        } else {
            suggestionEl.style.display = 'none';
        }
    }
}

/**
 * Navigate to accounts page
 */
function navigateToAccounts(event) {
    event.preventDefault();
    navigateTo('accounts');
}

/**
 * Load and render accounts from backend
 * @param {string} containerId - ID of container to render accounts into
 */
function loadAccounts(containerId = 'accounts-list') {
    try {
        const initData = getInitData();
        
        if (!initData) {
            return;
        }

        // Fetch accounts from backend (all users, info available to any owner)
        const url = `/api/mini-app/accounts`;
        
        fetchWithTmaAuth(url, initData)
            .then(response => {
                if (!response.ok) {
                    return response.text().then(errorText => {
                        throw new Error(`HTTP ${response.status}`);
                    });
                }
                
                return response.json();
            })
            .then(data => {
                renderAccountsPage(data.accounts || [], containerId);
            })
            .catch(error => {
                // Error silently handled
            });
        
    } catch (error) {
        // Error silently handled
    }
}

/**
 * Render accounts page with list of accounts and their balances
 * @param {Array} accounts - Array of account items {account_name, account_type, balance}
 * @param {string} containerId - ID of container to render into
 */
function renderAccountsPage(accounts, containerId = 'accounts-list') {
    const container = document.getElementById(containerId);
    
    if (!container) {
        return;
    }
    
    container.innerHTML = '';
    
    if (!accounts || accounts.length === 0) {
        container.innerHTML = `<div class="account-empty">${t('empty_accounts')}</div>`;
        return;
    }
    
    // Filter out accounts of users who represent others (have representative_id set)
    const filteredAccounts = accounts.filter(acc => !acc.representative_id);
    
    if (filteredAccounts.length === 0) {
        container.innerHTML = `<div class="account-empty">${t('empty_accounts')}</div>`;
        return;
    }
    
    // Sort: Owners first (biggest debt), then Organization, then Staff
    const sorted = [...filteredAccounts].sort((a, b) => {
        // Define type priority: owner (0), organization (1), staff (2)
        const typePriority = { 'owner': 0, 'organization': 1, 'staff': 2 };
        const aPriority = typePriority[a.account_type] ?? 3;
        const bPriority = typePriority[b.account_type] ?? 3;
        
        // First, sort by type priority
        if (aPriority !== bPriority) {
            return aPriority - bPriority;
        }
        
        // Within same type, for owners sort by biggest debt (most positive balance after inversion)
        // For others, sort by balance descending (most positive first)
        if (a.account_type === 'owner') {
            // For owners: invert_for_display means we show -balance, so sort by raw balance descending (most positive = most debt when displayed)
            // return b.balance - a.balance;
            return a.account_name.localeCompare(b.account_name);
        }
        
        // For organization and staff, sort by balance descending
        return b.balance - a.balance;
    });
    
    sorted.forEach((item, index) => {
        const row = document.createElement('div');
        
        // Determine if account is clickable based on access rules
        const isAdministrator = __authenticatedUserInfo?.is_administrator || false;
        const isOwnAccount = item.account_id === __currentAccountId;
        const isOrganization = item.account_type === 'organization';
        const isStaff = item.account_type === 'staff';
        const isClickable = isAdministrator || isOwnAccount || isOrganization || isStaff;
        
        row.className = isClickable ? 'account-row clickable' : 'account-row disabled';
        
        // Make row clickable to navigate to account details (if allowed)
        if (isClickable) {
            row.onclick = () => {
                navigateToAccountDetails(item.account_id, item.account_name);
            };
        }
        
        // Account info (icon + name)
        const infoDiv = document.createElement('div');
        infoDiv.className = 'account-row-info';
        
        // Type icon based on account type (organization, owner, staff)
        const iconEl = document.createElement('span');
        iconEl.className = 'account-row-icon';
        if (item.account_type === 'organization') {
            iconEl.textContent = '🏦';
        } else if (item.account_type === 'staff') {
            iconEl.textContent = '👷';
        } else {
            iconEl.textContent = '👤';
        }
        infoDiv.appendChild(iconEl);
        
        const nameEl = document.createElement('div');
        nameEl.className = 'account-row-name';
        nameEl.textContent = item.account_name;
        infoDiv.appendChild(nameEl);
        
        row.appendChild(infoDiv);
        
        // Balance amount (with color coding and inversion for owner accounts)
        const displayBalance = item.invert_for_display ? -item.balance : item.balance;
        const amountEl = document.createElement('div');
        const formatted = formatCurrency(Math.abs(displayBalance));
        const balanceClass = displayBalance >= 0 ? 'positive' : 'negative';
        amountEl.className = `account-row-amount ${balanceClass}`;
        amountEl.textContent = `${displayBalance >= 0 ? '+' : '-'}${formatted}`;
        
        row.appendChild(amountEl);
        container.appendChild(row);
    });
}

async function initMiniApp() {
    try {
        // Load translations first
        await loadTranslations();
        
        // Apply translations to static template elements
        applyTranslations();
        
        // Get init data from Telegram WebApp (supports both desktop and iOS)
        const initData = getInitData();
        
        if (!initData) {
            renderError(t('err_auth_failed'));
            return;
        }
        
        try {
            // Check URL for persisted admin selection
            const urlSelectedUserId = getSelectedUserIdFromUrl();
            
            // Build init URL with selected_user_id if present
            let initUrl = '/api/mini-app/init';
            if (urlSelectedUserId !== null) {
                initUrl += `?selected_user_id=${urlSelectedUserId}`;
                __selectedUserId = urlSelectedUserId;
            }
            
            // Call backend API - single call returns registration + full user context
            const response = await fetchWithTmaAuth(initUrl, initData);
            
            if (!response.ok) {
                if (response.status === 401) {
                    renderAccessDenied();
                } else {
                    renderError(t('err_server'));
                }
                return;
            }
            
            const data = await response.json();
            
            // If /init returns 200 OK, the user is registered and authenticated
            // (we removed isRegistered field since all responses represent authenticated users)
            renderWelcomeScreen(data);
            
            // Store account ID from user_context for endpoint calls
            __currentAccountId = data.user_context?.account_id || null;
            
            // Store authenticated user info (constant throughout session)
            __authenticatedUserInfo = {
                is_administrator: data.is_administrator,
                name: data.name,
                representative_id: data.representative_id,
                user_id: data.user_context?.user_id || null
            };
            
            // Store users list for admin dropdown (admin only)
            __usersList = data.users || null;
            
            // Build context from /init response using user_context object
            const userCtx = data.user_context || {};
            
            // Check if representing: user has representative_id AND is not admin
            const isRepresenting = !!data.representative_id && !data.is_administrator;
            const roles = userCtx.roles || [];
            const isOwner = roles.includes('owner');
            const isStakeholder = roles.includes('stakeholder');
            
            const context = {
                isRepresenting,
                is_administrator: data.is_administrator,
                authenticatedUserId: userCtx.user_id,
                roles,
                isOwner,
                isStakeholder,
                stakeholderUrl: data.stakeholder_url || null,
                representativeOf: null,
                greetingName: data.name,
                photo_gallery_url: data.photo_gallery_url || null
            };
            
            // Store static URLs (constant throughout session)
            __staticUrls = {
                stakeholder_url: data.stakeholder_url || null,
                photo_gallery_url: data.photo_gallery_url || null
            };
            
            // Cache the context
            __appContext = context;
            
            // Render admin dropdown OR representative info based on authenticated user's status
            if (__authenticatedUserInfo.is_administrator) {
                // Admin: show user selector dropdown
                renderAdminUserSelector(true, userCtx.user_id, __usersList);
            } else if (isRepresenting) {
                // Representative: show "Represents [TargetUserName]" block
                renderRepresentativeInfo({ 
                    user_id: userCtx.user_id, 
                    name: userCtx.name 
                });
            } else {
                // Regular user: hide representative container
                const container = document.getElementById('representative-info-container');
                if (container) container.style.display = 'none';
            }
            
            // Load all datasets with context
            await reloadAllDatasets(context);
            
        } catch (fetchError) {
            throw fetchError; // Re-throw to outer catch
        }
        
    } catch (error) {
        renderError(t('err_network'));
    }
}

/**
 * Render properties list with hierarchical grouping (main properties and additional properties)
 * @param {Array<Object>} properties - Array of property objects
 */
function renderProperties(properties) {
    const container = document.getElementById('properties-container');
    const listContainer = document.getElementById('properties-list');

    if (!container || !listContainer) {
        console.warn('Properties container not found');
        return;
    }

    // Clear existing content
    listContainer.innerHTML = '';

    // Hide if no properties
    if (!properties || properties.length === 0) {
        container.classList.remove('visible');
        return;
    }

    // Show container if there are properties
    container.classList.add('visible');

    // Build property hierarchy: separate main properties from additional ones
    const mainProperties = properties.filter(p => !p.main_property_id)
        .sort((a, b) => a.id - b.id);
    const additionalProperties = properties.filter(p => p.main_property_id);
    
    // Create a map for quick lookup of additional properties by main_property_id
    const additionalMap = {};
    additionalProperties.forEach(prop => {
        if (!additionalMap[prop.main_property_id]) {
            additionalMap[prop.main_property_id] = [];
        }
        additionalMap[prop.main_property_id].push(prop);
    });
    
    // Sort additional properties within each group by ID
    Object.keys(additionalMap).forEach(mainId => {
        additionalMap[mainId].sort((a, b) => a.id - b.id);
    });

    // Helper function to create property item element
    function createPropertyElement(property, isAdditional = false) {
        const item = document.createElement('div');
        item.className = `property-item ${isAdditional ? 'is-additional-property' : 'is-main-property'}`;

        // Create info section
        const info = document.createElement('div');
        info.className = 'property-info';

        // Property name with parent indicator for additional properties
        const nameEl = document.createElement('div');
        nameEl.className = 'property-name';
        if (isAdditional) {
            nameEl.textContent = '└─ ' + property.property_name;
        } else {
            nameEl.textContent = property.property_name;
        }
        info.appendChild(nameEl);

        // Property type
        if (property.type) {
            const typeEl = document.createElement('div');
            typeEl.className = 'property-type';
            typeEl.textContent = property.type;
            info.appendChild(typeEl);
        }

        // Meta information (badges and additional info)
        const metaEl = document.createElement('div');
        metaEl.className = 'property-meta';

        if (!property.is_ready) {
            const readyBadge = document.createElement('span');
            readyBadge.className = 'property-badge not-ready';
            readyBadge.textContent = t('status_not_ready');
            metaEl.appendChild(readyBadge);
        }

        if (property.is_for_tenant) {
            const tenantBadge = document.createElement('span');
            tenantBadge.className = 'property-badge tenant';
            tenantBadge.textContent = t('label_tenant');
            metaEl.appendChild(tenantBadge);
        }

        if (property.share_weight) {
            const weightBadge = document.createElement('span');
            weightBadge.className = 'property-badge';
            weightBadge.textContent = `${t('label_weight')}: ${property.share_weight}`;
            metaEl.appendChild(weightBadge);
        }

        if (property.sale_price) {
            const priceBadge = document.createElement('span');
            priceBadge.className = 'property-badge';
            const price = parseFloat(property.sale_price);
            const formattedPrice = formatCurrency(price);
            priceBadge.textContent = formattedPrice;
            metaEl.appendChild(priceBadge);
        }

        if (metaEl.children.length > 0) {
            info.appendChild(metaEl);
        }

        // Photo link (if available)
        if (property.photo_link) {
            const photoLinkEl = document.createElement('div');
            photoLinkEl.className = 'property-photo-link';
            const linkEl = document.createElement('a');
            linkEl.href = property.photo_link;
            linkEl.target = '_blank';
            linkEl.rel = 'noopener noreferrer';
            linkEl.textContent = t('label_view_photos');
            photoLinkEl.appendChild(linkEl);
            info.appendChild(photoLinkEl);
        }

        item.appendChild(info);
        return item;
    }

    // Render main properties and their additional properties
    mainProperties.forEach(mainProperty => {
        // Render main property
        const mainItem = createPropertyElement(mainProperty, false);
        listContainer.appendChild(mainItem);

        // Render additional properties under this main property
        const children = additionalMap[mainProperty.id] || [];
        children.forEach(additionalProperty => {
            const childItem = createPropertyElement(additionalProperty, true);
            listContainer.appendChild(childItem);
        });
    });
}

/**
 * Load properties from backend and render list
 */
async function loadProperties() {
    try {
        const initData = getInitData();

        if (!initData) {
            console.error('No Telegram init data available');
            return;
        }

        // Build URL with selected_user_id if admin has selected a user, otherwise no parameters needed
        let url = '/api/mini-app/properties';
        if (__selectedUserId !== null) {
            url += `?selected_user_id=${__selectedUserId}`;
        }
        
        const response = await fetchWithTmaAuth(url, initData);

        if (!response.ok) {
            if (response.status === 403) {
                // User is not an owner, hide properties section
                const container = document.getElementById('properties-container');
                if (container) {
                    container.classList.remove('visible');
                }
                return;
            }
            console.error('Failed to load properties:', response.status, response.statusText);
            return;
        }

        const data = await response.json();

        if (data.properties && Array.isArray(data.properties)) {
            renderProperties(data.properties);
        }

    } catch (error) {
        console.error('Error loading properties:', error);
    }
}

/**
 * Start the app when DOM is ready
 */
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMiniApp);
} else {
    initMiniApp();
}
