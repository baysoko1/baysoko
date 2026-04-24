/**
 * Notifications WebSocket Client
 * 
 * Real-time notification delivery via WebSocket with automatic fallback to polling.
 * Features:
 * - Automatic reconnection with exponential backoff
 * - Heartbeat mechanism to keep connection alive
 * - Graceful fallback to HTTP polling if WebSocket unavailable
 * - Local storage for tracking shown notifications
 * - Toast notifications for new messages
 * - Efficient handling of connection state changes
 */

(function() {
    'use strict';

    // Configuration
    const CONFIG = {
        WS_URL: (window.location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + window.location.host + '/ws/notifications/',
        AUTH_WS_URL: (window.location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + window.location.host + '/ws/auth/',
        POLL_ENDPOINT: (typeof notificationsListUrl !== 'undefined') ? notificationsListUrl : '/notifications/',
        POLL_INTERVAL: 30000, // 30 seconds
        HEARTBEAT_INTERVAL: 45000, // 45 seconds
        RECONNECT_INTERVALS: [1000, 2000, 5000, 10000, 30000], // Exponential backoff: 1s, 2s, 5s, 10s, 30s
        MAX_RECONNECT_ATTEMPTS: 10,
        STORAGE_KEY: 'baysoko_shown_notifications_v2',
        NOTIFICATION_DISPLAY_DURATION: 4000, // ms
    };

    // State
    let state = {
        ws: null,
        authWs: null,
        isConnected: false,
        isPollFallback: false,
        reconnectAttempts: 0,
        reconnectTimeout: null,
        heartbeatInterval: null,
        pollInterval: null,
        lastMessageTime: Date.now(),
    };

    function getAndroidBridge() {
        return window.BaysokoAndroidApp || null;
    }

    function shouldUseNativeNotification() {
        return !!getAndroidBridge() && (document.hidden || !document.hasFocus());
    }

    function notifyNativeApp(payload) {
        const bridge = getAndroidBridge();
        if (!bridge || typeof bridge.notify !== 'function' || !shouldUseNativeNotification()) {
            return;
        }
        try {
            bridge.notify(payload);
        } catch (error) {
            console.warn('[Notifications] Native notification bridge failed:', error);
        }
    }

    function syncNativeBadges() {
        const bridge = getAndroidBridge();
        if (!bridge || typeof bridge.setBadgeCounts !== 'function') {
            return;
        }
        const notifications = parseInt((document.getElementById('dropdownNotificationsBadge') || {}).textContent || '0', 10) || 0;
        const messages = parseInt((document.getElementById('dropdownMessagesBadge') || {}).textContent || '0', 10) || 0;
        try {
            bridge.setBadgeCounts({ notifications, messages });
        } catch (error) {
            console.warn('[Notifications] Native badge sync failed:', error);
        }
    }

    // ==================== WebSocket Connection Management ====================

    function connectWebSocket() {
        if (!window.currentUserId) {
            console.warn('[Notifications] User not authenticated, skipping WebSocket connection');
            startPollingFallback();
            return;
        }

        try {
            state.ws = new WebSocket(CONFIG.WS_URL);
            
            state.ws.onopen = handleWebSocketOpen;
            state.ws.onmessage = handleWebSocketMessage;
            state.ws.onerror = handleWebSocketError;
            state.ws.onclose = handleWebSocketClose;
            
            console.log('[Notifications] Attempting WebSocket connection...');
        } catch (error) {
            console.error('[Notifications] WebSocket creation failed:', error);
            startPollingFallback();
        }

        // Also connect to auth WS to receive immediate toasts (e.g., after register/login)
        try {
            state.authWs = new WebSocket(CONFIG.AUTH_WS_URL);
            state.authWs.onopen = handleAuthWebSocketOpen;
            state.authWs.onmessage = handleAuthWebSocketMessage;
            state.authWs.onerror = handleAuthWebSocketError;
            state.authWs.onclose = handleAuthWebSocketClose;
        } catch (e) {
            console.debug('[Notifications] Auth WebSocket not available:', e);
        }
    }

    function handleAuthWebSocketOpen() {
        console.log('[Auth WS] connected');
    }

    function handleAuthWebSocketMessage(event) {
        try {
            const message = JSON.parse(event.data);
            if (message.type === 'toast' && message.toast) {
                showImmediateToast(message.toast);
            }
        } catch (err) {
            console.error('[Auth WS] message parse error:', err);
        }
    }

    function handleAuthWebSocketError(err) {
        console.warn('[Auth WS] error', err);
    }

    function handleAuthWebSocketClose() {
        console.log('[Auth WS] closed');
        state.authWs = null;
    }

    function handleWebSocketOpen() {
        console.log('[Notifications] WebSocket connected');
        state.isConnected = true;
        state.isPollFallback = false;
        state.reconnectAttempts = 0;
        
        // Clear any pending reconnect timeouts
        if (state.reconnectTimeout) {
            clearTimeout(state.reconnectTimeout);
            state.reconnectTimeout = null;
        }

        // Stop polling if we switched from polling fallback
        if (state.pollInterval) {
            clearInterval(state.pollInterval);
            state.pollInterval = null;
            console.log('[Notifications] Switched from polling to WebSocket');
        }

        // Start heartbeat to keep connection alive
        startHeartbeat();

        // Display visual indicator
        updateConnectionStatus(true);
    }

    function handleWebSocketMessage(event) {
        try {
            const message = JSON.parse(event.data);
            state.lastMessageTime = Date.now();

            console.log('[Notifications] Message received:', message.type);

            switch (message.type) {
                case 'connection_established':
                    handleConnectionEstablished(message);
                    break;
                case 'notification_created':
                    handleNotificationCreated(message);
                    break;
                case 'notification_marked_read':
                    handleNotificationMarkedRead(message);
                    break;
                case 'bulk_marked_read':
                    handleBulkMarkedRead(message);
                    break;
                case 'notification_deleted':
                    handleNotificationDeleted(message);
                    break;
                case 'chat_unread':
                    handleChatUnread(message);
                    break;
                case 'mark_read_response':
                    handleMarkReadResponse(message);
                    break;
                case 'heartbeat_ack':
                    // Keep-alive confirmed
                    break;
                case 'listing_created':
                    handleListingCreated(message);
                    break;
                case 'listing_liked':
                    handleListingLiked(message);
                    break;
                case 'cart_updated':
                    handleCartUpdated(message);
                    break;
                case 'listing_changed':
                    handleListingChanged(message);
                    break;
                case 'listing_deleted':
                    handleListingDeleted(message);
                    break;
                case 'error':
                    console.error('[Notifications] Server error:', message.message);
                    break;
                default:
                    console.warn('[Notifications] Unknown message type:', message.type);
            }
        } catch (error) {
            console.error('[Notifications] Error parsing WebSocket message:', error);
        }
    }

    function handleWebSocketError(error) {
        console.error('[Notifications] WebSocket error:', error);
        state.isConnected = false;
        updateConnectionStatus(false);
    }

    function handleWebSocketClose(event) {
        console.log('[Notifications] WebSocket closed, code:', event.code);
        state.isConnected = false;
        state.ws = null;
        updateConnectionStatus(false);

        // Clear heartbeat
        if (state.heartbeatInterval) {
            clearInterval(state.heartbeatInterval);
            state.heartbeatInterval = null;
        }

        // Attempt reconnection with exponential backoff
        if (event.code !== 4001) { // Don't reconnect if unauthorized
            scheduleReconnect();
        } else {
            startPollingFallback();
        }
    }

    function scheduleReconnect() {
        if (state.reconnectAttempts >= CONFIG.MAX_RECONNECT_ATTEMPTS) {
            console.warn('[Notifications] Max reconnection attempts reached, falling back to polling');
            startPollingFallback();
            return;
        }

        const delay = CONFIG.RECONNECT_INTERVALS[Math.min(state.reconnectAttempts, CONFIG.RECONNECT_INTERVALS.length - 1)];
        state.reconnectAttempts++;

        console.log(`[Notifications] Reconnecting in ${delay}ms (attempt ${state.reconnectAttempts}/${CONFIG.MAX_RECONNECT_ATTEMPTS})`);

        state.reconnectTimeout = setTimeout(() => {
            connectWebSocket();
        }, delay);
    }

    // ==================== Heartbeat ====================

    function startHeartbeat() {
        if (state.heartbeatInterval) {
            clearInterval(state.heartbeatInterval);
        }

        state.heartbeatInterval = setInterval(() => {
            if (state.isConnected && state.ws && state.ws.readyState === WebSocket.OPEN) {
                try {
                    state.ws.send(JSON.stringify({ action: 'heartbeat' }));
                } catch (error) {
                    console.warn('[Notifications] Heartbeat send failed:', error);
                }
            }
        }, CONFIG.HEARTBEAT_INTERVAL);
    }

    // ==================== Message Handlers ====================

    function handleConnectionEstablished(message) {
        const unreadCount = message.unread_count || 0;
        updateBadgeCount(unreadCount);
        console.log('[Notifications] Connection established, unread:', unreadCount);
    }

    function handleNotificationCreated(message) {
        const notification = message.notification;
        const shown = getShownNotifications();

        if (!shown.includes(notification.id)) {
            showNotificationToast(notification);
            notifyNativeApp({
                id: `notif-${notification.id}`,
                title: notification.title || 'Baysoko',
                body: truncateText(notification.message || '', 120),
                channelId: 'baysoko-notifications',
                url: notification.url || '/notifications/',
            });
            markNotificationShown(notification.id);
        }
    }

    function handleNotificationMarkedRead(message) {
        // Update internal state if needed
        console.log('[Notifications] Notification marked as read:', message.notification_id);
    }

    function handleBulkMarkedRead(message) {
        // Update badges
        updateBadgeCount(message.unread_count || 0);

        // Update notification list UI if present: mark all as read
        try {
            document.querySelectorAll('.notification-item.unread').forEach(item => {
                item.classList.remove('unread');
                item.classList.add('read');
                const dot = item.querySelector('.status-dot');
                if (dot) dot.classList.remove('unread');

                const statusText = item.querySelector('.notification-status span:last-child');
                if (statusText) statusText.textContent = 'Read';

                const markReadBtn = item.querySelector('.notification-action-btn.read');
                const btnMarkRead = item.querySelector('.btn-mark-read');
                if (markReadBtn) {
                    markReadBtn.style.transition = 'all 0.2s ease';
                    markReadBtn.style.opacity = '0';
                    markReadBtn.style.transform = 'scale(0.8)';
                    setTimeout(() => markReadBtn.remove(), 200);
                }
                if (btnMarkRead) {
                    btnMarkRead.style.transition = 'all 0.2s ease';
                    btnMarkRead.style.opacity = '0';
                    btnMarkRead.style.transform = 'scale(0.8)';
                    setTimeout(() => btnMarkRead.remove(), 200);
                }
            });

            // Update large unread badge in the notifications page if present
            const largeBadge = document.querySelector('.unread-count-badge-large');
            if (largeBadge) largeBadge.innerHTML = '<i class="bi bi-bell"></i> 0 Unread';

            // Also update central BadgeManager if present
            if (window.BadgeManager && typeof window.BadgeManager.update === 'function') {
                try { window.BadgeManager.update('notifications', 0); } catch (e) { /* swallow */ }
            }
        } catch (e) {
            console.warn('[Notifications] Could not update notification list UI for bulk_marked_read', e);
        }
    }

    function handleNotificationDeleted(message) {
        console.log('[Notifications] Notification deleted:', message.notification_id);
    }

    function handleChatUnread(message) {
        // Message expected to be { type: 'chat_unread', unread_count: N }
        const count = parseInt(message.unread_count || 0, 10) || 0;
        updateMessagesBadge(count);
        if (count > 0) {
            notifyNativeApp({
                id: `chat-unread-${count}`,
                title: message.title || 'New message on Baysoko',
                body: message.message || 'Open your inbox to reply.',
                channelId: 'baysoko-messages',
                url: message.url || '/chats/',
            });
        }
    }

    function handleMarkReadResponse(message) {
        if (message.success) {
            updateBadgeCount(message.unread_count);
        }
    }

    function handleListingCreated(message) {
        try {
            console.log('[Notifications] Listing created:', message.listing && message.listing.id);
            // Dispatch a global event that pages (like all_listings) can listen to
            document.dispatchEvent(new CustomEvent('listingCreated', { detail: message.listing }));
            // If page exposes applyFilters (listings page), refresh current filters to pick up new listing
            if (typeof window.applyFilters === 'function') {
                // Debounce slightly to avoid rapid repeated refreshes
                setTimeout(() => { window.applyFilters(); }, 300);
            }
        } catch (e) {
            console.warn('[Notifications] Failed to handle listing_created', e);
        }
    }

    function handleListingLiked(message) {
        try {
            const listing = message.listing;
            console.log('[Notifications] Listing liked event:', listing && listing.id);
            // Dispatch a global event pages can listen to
            document.dispatchEvent(new CustomEvent('listingLiked', { detail: listing }));
            // Update any inline favorite counters if present
            if (listing && listing.id) {
                // update elements with data-listing-id
                document.querySelectorAll(`[data-listing-id="${listing.id}"] .listing-favorite-stats, [data-listing-id="${listing.id}"] .listing-favorite-count`).forEach(el => {
                    // Prefer total_favorites field
                    const count = listing.total_favorites || listing.favorite_count || 0;
                    el.textContent = `${count} Like${count === 1 ? '' : 's'}`;
                });
            }
        } catch (e) {
            console.warn('[Notifications] Failed to handle listing_liked', e);
        }
    }

    function handleListingChanged(message) {
        try {
            const listing = message.listing;
            if (!listing || !listing.id) return;
            console.log('[Notifications] Listing changed event:', listing.id);

            // Dispatch DOM event
            document.dispatchEvent(new CustomEvent('listingChanged', { detail: listing }));

            // Update price elements
            document.querySelectorAll(`[data-listing-id="${listing.id}"] .listing-price, [data-listing-id="${listing.id}"] .listing-price-amount`).forEach(el => {
                if (typeof listing.price !== 'undefined' && listing.price !== null) {
                    // Use simple formatting; pages can listen to event for custom rendering
                    el.textContent = typeof listing.price === 'number' ? listing.price.toFixed(2) : listing.price;
                }
            });

            // Update stock elements
            document.querySelectorAll(`[data-listing-id="${listing.id}"] .listing-stock, [data-listing-id="${listing.id}"] .stock-count`).forEach(el => {
                if (typeof listing.stock !== 'undefined' && listing.stock !== null) {
                    el.textContent = listing.stock;
                    // Add out-of-stock visual cue
                    if (parseInt(listing.stock, 10) <= 0) {
                        el.classList.add('out-of-stock');
                    } else {
                        el.classList.remove('out-of-stock');
                    }
                }
            });

            // Optionally show a small toast about the change
            if (window.Toast && listing.old_price != null && listing.old_price !== listing.price) {
                showImmediateToast({
                    title: 'Price changed',
                    message: `Price updated: ${listing.old_price} → ${listing.price}`,
                    variant: 'info',
                    duration: 5000
                });
            }
        } catch (e) {
            console.warn('[Notifications] Failed to handle listing_changed', e);
        }
    }

    function handleListingDeleted(message) {
        try {
            const listing = message.listing;
            if (!listing || !listing.id) return;
            console.log('[Notifications] Listing deleted:', listing.id);

            // Remove listing cards from DOM
            document.querySelectorAll(`[data-listing-id="${listing.id}"]`).forEach(el => el.remove());

            // Dispatch event for other scripts
            document.dispatchEvent(new CustomEvent('listingDeleted', { detail: listing }));

            // Optionally show a toast
            showImmediateToast({
                title: 'Listing removed',
                message: 'A listing was removed from the marketplace.',
                variant: 'warning',
                duration: 3500
            });
        } catch (e) {
            console.warn('[Notifications] Failed to handle listing_deleted', e);
        }
    }

    function handleCartUpdated(message) {
        try {
            const cart = message.cart || {};
            console.log('[Notifications] Cart updated:', cart);
            // Dispatch event for pages to react
            document.dispatchEvent(new CustomEvent('cartUpdated', { detail: cart }));

            // Update site-wide cart indicators if present
            const cartBadge = document.getElementById('cartItemCountBadge');
            if (cartBadge) {
                const count = cart.cart_item_count || cart.cart_item_count === 0 ? cart.cart_item_count : (cart.item_count || 0);
                cartBadge.textContent = count > 0 ? count : '';
                cartBadge.style.display = count > 0 ? 'inline-block' : 'none';
            }

            const cartTotalEl = document.getElementById('cartTotalAmount');
            if (cartTotalEl && typeof cart.cart_total !== 'undefined') {
                cartTotalEl.textContent = typeof cart.cart_total === 'number' ? cart.cart_total.toFixed(2) : cart.cart_total;
            }
        } catch (e) {
            console.warn('[Notifications] Failed to handle cart_updated', e);
        }
    }

    // ==================== Toast Notifications ====================

    function showNotificationToast(notification) {
        const container = document.querySelector('.toast-container') || createToastContainer();
        
        const typeMap = {
            'system': 'primary',
            'order_placed': 'info',
            'order_shipped': 'info',
            'order_delivered': 'success',
            'order_disputed': 'danger',
            'payment_received': 'success',
            'review_received': 'info',
            'listing_sold': 'success',
            'favorite': 'info',
            'promotional': 'warning',
            'message': 'info',
        };

        const toastClass = 'toast-' + (typeMap[notification.type] || 'info');
        const iconClass = notification.type.includes('error') || notification.type === 'order_disputed' 
            ? 'bi-exclamation-triangle-fill' 
            : 'bi-bell-fill';

        const div = document.createElement('div');
        div.className = `custom-toast ${toastClass} custom-toast-notif`;
        div.innerHTML = `
            <i class="bi ${iconClass}"></i>
            <div class="fw-medium">${escapeHtml(notification.title)}</div>
            <div style="opacity:.92; font-size:0.85rem; margin-left:.5rem;">${escapeHtml(truncateText(notification.message, 120))}</div>
            <div class="toast-progress-bar" style="animation-duration:4s"></div>
        `;

        container.appendChild(div);

        setTimeout(() => {
            div.style.animation = 'slideOutRight 0.28s';
            setTimeout(() => div.remove(), 280);
        }, CONFIG.NOTIFICATION_DISPLAY_DURATION);
    }

    function showImmediateToast(toast) {
        const container = document.querySelector('.toast-container') || createToastContainer();

        const div = document.createElement('div');
        const variant = toast.variant || 'success';
        div.className = `custom-toast toast-${variant} custom-toast-immediate`;
        div.innerHTML = `
            <i class="bi bi-bell-fill"></i>
            <div class="fw-medium">${escapeHtml(toast.title || '')}</div>
            <div style="opacity:.92; font-size:0.9rem; margin-left:.5rem;">${escapeHtml(toast.message || '')}</div>
            <div class="toast-progress-bar" style="animation-duration:${(toast.duration||4000)/1000}s"></div>
        `;

        container.appendChild(div);

        setTimeout(() => {
            div.style.animation = 'slideOutRight 0.28s';
            setTimeout(() => div.remove(), 280);
        }, toast.duration || CONFIG.NOTIFICATION_DISPLAY_DURATION);
    }

    function createToastContainer() {
        const container = document.createElement('div');
        container.className = 'toast-container';
        document.body.appendChild(container);
        return container;
    }

    // ==================== Polling Fallback ====================

    function startPollingFallback() {
        if (state.isPollFallback) {
            return; // Already polling
        }

        console.log('[Notifications] Starting polling fallback');
        state.isPollFallback = true;
        updateConnectionStatus(false, 'polling');

        // Initial poll
        pollNotifications();

        // Set up interval
        state.pollInterval = setInterval(pollNotifications, CONFIG.POLL_INTERVAL);
    }

    function stopPollingFallback() {
        if (state.pollInterval) {
            clearInterval(state.pollInterval);
            state.pollInterval = null;
        }
        state.isPollFallback = false;
    }

    async function pollNotifications() {
        if (!window.currentUserId) return;

        try {
            const response = await fetch(CONFIG.POLL_ENDPOINT, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            });

            if (!response.ok) {
                console.warn('[Notifications] Poll failed with status:', response.status);
                return;
            }

            const data = await response.json();
            const notifications = data.notifications || [];
            const shown = getShownNotifications();

            // Process notifications in chronological order
            notifications.reverse().forEach(notification => {
                if (!shown.includes(notification.id)) {
                    showNotificationToast(notification);
                    markNotificationShown(notification.id);
                }
            });

            // Update badge
            updateBadgeCount(data.unread_count || 0);

        } catch (error) {
            console.warn('[Notifications] Polling error:', error);
        }
    }

    // ==================== Utility Functions ====================

    function getShownNotifications() {
        try {
            return JSON.parse(localStorage.getItem(CONFIG.STORAGE_KEY) || '[]');
        } catch (e) {
            return [];
        }
    }

    function markNotificationShown(id) {
        try {
            const shown = getShownNotifications();
            if (!shown.includes(id)) {
                shown.push(id);
                localStorage.setItem(CONFIG.STORAGE_KEY, JSON.stringify(shown));
            }
        } catch (e) {
            console.warn('[Notifications] Could not mark notification as shown:', e);
        }
    }

    function truncateText(text, maxLength) {
        return text && text.length > maxLength ? text.slice(0, maxLength - 1) + '…' : text;
    }

    function escapeHtml(text) {
        const map = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#039;'
        };
        return text.replace(/[&<>"']/g, m => map[m]);
    }

    function updateBadgeCount(count) {
        // Update desktop notification badge
        const desktopBadge = document.getElementById('dropdownNotificationsBadge');
        if (desktopBadge) {
            desktopBadge.textContent = count > 0 ? count : '';
            desktopBadge.style.display = count > 0 ? 'inline-block' : 'none';
        }

        // Update mobile notification badge  
        const mobileBadge = document.getElementById('mobileNotificationsBadge');
        if (mobileBadge) {
            mobileBadge.textContent = count > 0 ? count : '';
            mobileBadge.style.display = count > 0 ? 'inline-block' : 'none';
        }
        syncNativeBadges();
    }

    function updateMessagesBadge(count) {
        // Desktop messages badge
        const desktopBadge = document.getElementById('dropdownMessagesBadge') || document.getElementById('dropdownMessagesBadge');
        if (desktopBadge) {
            desktopBadge.textContent = count > 0 ? count : '';
            desktopBadge.style.display = count > 0 ? 'inline-block' : 'none';
        }

        // Mobile messages badge
        const mobileBadge = document.getElementById('mobileMessagesBadge') || document.getElementById('mobileMessagesBadge');
        if (mobileBadge) {
            mobileBadge.textContent = count > 0 ? count : '';
            mobileBadge.style.display = count > 0 ? 'inline-block' : 'none';
        }
        syncNativeBadges();
    }

    function updateConnectionStatus(connected, type = 'websocket') {
        // Add visual indicator if needed
        // This can be extended to show connection status to user
        const statusIndicator = document.getElementById('notification-connection-status');
        if (statusIndicator) {
            if (connected) {
                statusIndicator.classList.add('connected');
                statusIndicator.classList.remove('disconnected', 'polling');
                if (type === 'polling') {
                    statusIndicator.classList.add('polling');
                }
            } else {
                statusIndicator.classList.remove('connected');
                statusIndicator.classList.add('disconnected');
            }
        }
    }

    // ==================== Public API ====================

    window.NotificationsClient = {
        start: function() {
            console.log('[Notifications] Initializing notifications client');
            if (!window.currentUserId) {
                console.warn('[Notifications] User not authenticated');
                return;
            }
            connectWebSocket();
        },

        stop: function() {
            console.log('[Notifications] Stopping notifications client');
            if (state.ws) {
                state.ws.close();
            }
            if (state.reconnectTimeout) {
                clearTimeout(state.reconnectTimeout);
            }
            if (state.heartbeatInterval) {
                clearInterval(state.heartbeatInterval);
            }
            stopPollingFallback();
        },

        isConnected: function() {
            return state.isConnected;
        },

        isPolling: function() {
            return state.isPollFallback;
        },

        getStatus: function() {
            return {
                connected: state.isConnected,
                polling: state.isPollFallback,
                reconnectAttempts: state.reconnectAttempts,
                lastMessageTime: state.lastMessageTime
            };
        }
    };

    // ==================== Initialization ====================

    // Start the notifications client when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            window.NotificationsClient.start();
        });
    } else {
        window.NotificationsClient.start();
    }

    // Clean up on page unload
    window.addEventListener('beforeunload', function() {
        window.NotificationsClient.stop();
    });

})();
