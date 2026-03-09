// Agent Widget – handles rich platform entities, apply to form (including title), and creative answers
(function() {
  function wsUrl() {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${window.location.host}/ws/agent/`;
  }

  function escapeHtml(unsafe) {
    return String(unsafe)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Convert Cloudinary URL to local media URL (same as inbox)
  function cloudinaryToLocalUrl(cloudinaryUrl) {
    if (!cloudinaryUrl) return null;
    try {
      const url = new URL(cloudinaryUrl);
      const uploadIndex = url.pathname.indexOf('/upload/');
      if (uploadIndex !== -1) {
        let localPath = url.pathname.substring(uploadIndex + 7);
        localPath = localPath.replace(/^v\d+\//, '');
        localPath = localPath.replace(/^media\//, '');
        // Prefer global LOCAL_MEDIA_URL if provided by page (inbox.html sets this)
        const globalBase = (typeof window !== 'undefined' && window.LOCAL_MEDIA_URL) ? window.LOCAL_MEDIA_URL : '/media/';
        const base = globalBase.endsWith('/') ? globalBase.slice(0, -1) : globalBase;
        const path = localPath.startsWith('/') ? localPath.slice(1) : localPath;
        return ensureHttps(base + '/' + path);
      }
    } catch (e) {}
    return null;
  }

  function ensureHttps(url) {
    if (!url) return url;
    if (window.location.protocol === 'https:' && url.startsWith('http://')) {
      return url.replace(/^http:\/\//i, 'https://');
    }
    return url;
  }

  function parseJsonObject(value) {
    if (!value || typeof value !== 'string') return null;
    const trimmed = value.trim();
    if (!(trimmed.startsWith('{') || trimmed.startsWith('['))) return null;
    try {
      return JSON.parse(trimmed);
    } catch (e) {
      return null;
    }
  }

  function normalizeMediaVersionPath(url) {
    if (!url || typeof url !== 'string') return null;
    const m = url.match(/^\/media\/v\d+\/(.+)$/i);
    if (m && m[1]) return '/media/' + m[1];
    return null;
  }

  function toAbsoluteUrl(url) {
    if (!url) return null;
    if (/^https?:\/\//i.test(url)) return ensureHttps(url);
    if (url.startsWith('/')) return window.location.origin + url;
    return null;
  }

  function uniqueNonEmpty(values) {
    const out = [];
    const seen = new Set();
    values.forEach((v) => {
      if (!v || typeof v !== 'string') return;
      const k = v.trim();
      if (!k || seen.has(k)) return;
      seen.add(k);
      out.push(k);
    });
    return out;
  }

  function compactSuggestionReason(item) {
    if (!item || typeof item !== 'object') return '';
    if (item.type === 'action_suggestion') return '';
    const raw = String(item.reason || item.suggestion_reason || '').trim();
    if (!raw) return '';
    const cleaned = raw.replace(/\s+/g, ' ').replace(/[.;:,!?]+$/g, '');
    return cleaned.length > 52 ? (cleaned.slice(0, 49) + '...') : cleaned;
  }

  function normalizeAssistantHref(href) {
    const raw = String(href || '').trim();
    if (!raw) return null;
    if (raw === '/cart/') return '/listings/cart/';
    if (raw === '/checkout/') return '/listings/checkout/';
    if (raw === '/orders/') return '/listings/orders/';
    if (raw.startsWith('/')) return raw;
    if (/^https?:\/\//i.test(raw)) return raw;
    if (/^www\./i.test(raw)) return 'https://' + raw;
    return null;
  }

  function linkifyEscapedText(text) {
    const urlRe = /((https?:\/\/|www\.)[^\s<]+)/gi;
    return String(text || '').replace(urlRe, (m) => {
      let href = m;
      if (!/^https?:\/\//i.test(href)) href = 'https://' + href;
      return `<a class="agent-inline-link" href="${href}" target="_blank" rel="noopener noreferrer">${m}</a>`;
    });
  }

  class AgentWidget {
    constructor(container) {
      this.container = container;
      this.input = container.querySelector('[data-agent-input]');
      this.btn = container.querySelector('[data-agent-send]');
      this.applyBtn = container.querySelector('[data-agent-apply]');
      this.closeBtn = container.querySelector('.agent-close');
      this.toggleBtn = container.querySelector('.agent-toggle');
      this.minimizeBtn = container.querySelector('.agent-minimize');
      this.panel = container.querySelector('.agent-panel');
      this.messagesEl = container.querySelector('[data-agent-messages]');
      this.typingEl = container.querySelector('[data-agent-typing]');
      this.unreadBadge = container.querySelector('[data-agent-unread]');
      this.autofillWrap = container.querySelector('[data-agent-autofill-wrap]');
      this.autofillToggle = container.querySelector('[data-agent-autofill-toggle]');
      this.lastData = null;
      this.ws = null;
      this.lastUserPrompt = '';
      this.lastAssistantText = '';
      this._lastScrollY = Math.max(0, window.scrollY || window.pageYOffset || 0);
      this._userScrollingMessages = false;
      this._historyLoadingEl = null;
      this._requestCounter = 0;
      this._pendingRequestModes = {};
      this._pendingPromptByRequestId = {};
      this._messageTextById = {};
      this._messageIdCounter = 0;
      this._titleAutofillEnabled = false;
      this._currentUserId = String(this.container?.dataset?.agentUserId || '').trim() || 'guest';
      this._storageKeys = {
        panelOpen: `agent_panel_open:${this._currentUserId}`,
        history: `agent_chat_history_v1:${this._currentUserId}`,
        offlineQueue: `agent_offline_queue_v1:${this._currentUserId}`,
        titleAutofill: `agent_autofill_title_enabled:${this._currentUserId}`,
        dailyWelcomeDate: `agent_daily_welcome_date:${this._currentUserId}`
      };
      this._connect();
      this._bind();
      this._bindScrollVisibility();
      this._bindMessageScrollState();
      this._initProactivePrompt();
      // restore panel state
      try {
        const s = localStorage.getItem(this._storageKeys.panelOpen);
        if (s === 'true') this.container.classList.add('open');
      } catch (e) {}
      // Optional title-based listing autofill (explicit toggle required)
      try {
        const listingTitle = document.getElementById('id_title');
          if (listingTitle) {
          if (this.autofillWrap) this.autofillWrap.hidden = false;
          const saved = localStorage.getItem(this._storageKeys.titleAutofill);
          this._titleAutofillEnabled = saved === 'true';
          if (this.autofillToggle) {
            this.autofillToggle.checked = this._titleAutofillEnabled;
            this.autofillToggle.addEventListener('change', () => {
              this._titleAutofillEnabled = !!this.autofillToggle.checked;
              localStorage.setItem(this._storageKeys.titleAutofill, this._titleAutofillEnabled ? 'true' : 'false');
            });
          }
          let tmr;
          listingTitle.addEventListener('input', () => {
            if (!this._titleAutofillEnabled) return;
            clearTimeout(tmr);
            tmr = setTimeout(() => {
              const v = listingTitle.value.trim();
              if (v.length > 4) {
                this._showTyping(true);
                this._sendGenerate(v, { mode: 'listing_fields' });
              }
            }, 900);
          });
        }
      } catch (e) {}
    }

    _setStatus(text) {}

    _storageKey(name) {
      return this._storageKeys[name] || null;
    }

    _getDisplayName() {
      const first = String(this.container?.dataset?.agentUserFirstName || '').trim();
      const username = String(this.container?.dataset?.agentUsername || '').trim();
      return first || username || 'there';
    }

    _dailyWelcomeText() {
      return `Hello ${this._getDisplayName()}, I am Baysoko Assistant. I can help with your cart, listings, stores, subscriptions, and orders.`;
    }

    _todayStamp() {
      const d = new Date();
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      return `${y}-${m}-${day}`;
    }

    _sanitizeLegacyWelcome(text) {
      const raw = String(text || '').trim();
      if (!raw) return raw;
      if (/hello,\s*welcome to baysoko/i.test(raw) || /call me bay bee/i.test(raw)) {
        return this._dailyWelcomeText();
      }
      return raw;
    }

    _maybeShowDailyWelcome() {
      try {
        const key = this._storageKeys.dailyWelcomeDate;
        const today = this._todayStamp();
        if (localStorage.getItem(key) === today) return;
        const welcome = this._dailyWelcomeText();
        this._appendMessage('bot', welcome, false, new Date().toISOString(), true);
        this.persistMessage('assistant', welcome);
        localStorage.setItem(key, today);
      } catch (e) {}
    }

    _connect() {
      try {
        this.ws = new WebSocket(wsUrl());
        this.ws.onopen = () => this._setStatus('connected');
        this.ws.onopen = () => { this._setStatus('connected'); try{ window.agentSocket = this.ws; }catch(e){} };
        this.ws.onclose = () => {
          this._setStatus('disconnected');
          try{ window.agentSocket = null; }catch(e){}
          setTimeout(() => this._connect(), 3000);
        };
        this.ws.onerror = (e) => console.error('WS error', e);
        this.ws.onmessage = (evt) => {
          try {
            const data = JSON.parse(evt.data);
            this._handleMessage(data);
          } catch (e) {
            console.error('Invalid message', e);
          }
        };
      } catch (e) {
        console.error('WS connect failed', e);
      }
    }

    _bind() {
      if (this.toggleBtn) {
        this.toggleBtn.addEventListener('click', () => {
          const open = this.container.classList.toggle('open');
          if (this.panel) this.panel.setAttribute('aria-hidden', (!open).toString());
          this.toggleBtn.setAttribute('aria-expanded', open);
          if (open) {
            this.input?.focus();
            this._scrollToLatest();
            if (this.unreadBadge) this.unreadBadge.hidden = true;
            localStorage.setItem(this._storageKeys.panelOpen, 'true');
          } else {
            localStorage.setItem(this._storageKeys.panelOpen, 'false');
          }
        });
      }

      if (this.minimizeBtn) {
        this.minimizeBtn.addEventListener('click', () => this._closePanel());
      }
      if (this.closeBtn) {
        this.closeBtn.addEventListener('click', () => {
          this._closePanel();
          if (this.input) this.input.value = '';
        });
      }

      if (this.btn && this.input) {
        this.btn.addEventListener('click', () => {
          const text = this.input.value.trim();
          if (!text) return;
          this.lastUserPrompt = text;
          const userEl = this._appendMessage('user', text);
          const msgId = userEl?.dataset?.agentMsgId || null;
          if (msgId) this._messageTextById[msgId] = text;
          this.persistMessage('user', text);
          this.input.value = '';
          this._showTyping(true);
          this._sendGenerate(text, { mode: 'assistant', sourceMessageId: msgId });
        });

        if (this.applyBtn) {
          this.applyBtn.addEventListener('click', () => {
            if (this.lastData) applyToListingForm(this.lastData);
          });
        }

        this.input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            this.btn.click();
          }
        });
        }

        // Offline queue: send queued messages when back online
        window.addEventListener('online', () => {
          this._flushOfflineQueue();
        });

        if (this.messagesEl) {
          this.messagesEl.addEventListener('click', (e) => {
            const pill = e.target.closest('.agent-suggestion-pill');
            if (!pill || pill.classList.contains('view-more')) return;
            const url = pill.getAttribute('data-item-url');
            if (url && url !== '#') window.location.href = url;
          });

          this.messagesEl.addEventListener('error', (e) => {
            const img = e.target;
            if (!(img instanceof HTMLImageElement) || !img.classList.contains('agent-suggestion-image')) return;
            let fallbacks = [];
            try {
              fallbacks = JSON.parse(img.dataset.fallbacks || '[]');
            } catch (err) {
              fallbacks = [];
            }
            const idx = parseInt(img.dataset.fallbackIndex || '0', 10);
            const nextIndex = Number.isNaN(idx) ? 1 : idx + 1;
            if (fallbacks[nextIndex]) {
              img.dataset.fallbackIndex = String(nextIndex);
              img.src = fallbacks[nextIndex];
              return;
            }
            img.src = '/static/images/placeholder.png';
          }, true);
        }
    }

    _bindMessageScrollState() {
      if (!this.messagesEl) return;
      this.messagesEl.addEventListener('scroll', () => {
        const el = this.messagesEl;
        const nearBottom = (el.scrollTop + el.clientHeight) >= (el.scrollHeight - 20);
        this._userScrollingMessages = !nearBottom;
      });
    }

    _initProactivePrompt() {
      try {
        const heroImage = document.querySelector('.main-image');
        if (!heroImage || !('IntersectionObserver' in window)) return;
        const obs = new IntersectionObserver((entries) => {
          entries.forEach((en) => {
            if (!en.isIntersecting) return;
            setTimeout(() => {
              if (!this.container.classList.contains('open')) {
                this._showPopupPrompt('Need help with this item? Ask me anything.');
              }
            }, 2000);
          });
        }, { threshold: 0.6 });
        obs.observe(heroImage);
      } catch (e) {}
    }

    _showHistoryLoading(show) {
      if (!this.messagesEl) return;
      if (show) {
        if (!this._historyLoadingEl) {
          const el = document.createElement('div');
          el.className = 'agent-history-loading';
          el.innerHTML = '<span class=\"agent-history-spinner\"></span><span>Loading previous messages...</span>';
          this._historyLoadingEl = el;
        }
        if (!this.messagesEl.contains(this._historyLoadingEl)) {
          this.messagesEl.appendChild(this._historyLoadingEl);
        }
      } else if (this._historyLoadingEl && this.messagesEl.contains(this._historyLoadingEl)) {
        this.messagesEl.removeChild(this._historyLoadingEl);
      }
    }

    _scrollToLatest() {
      if (!this.messagesEl) return;
      const run = () => {
        if (!this.messagesEl) return;
        this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
        this._userScrollingMessages = false;
      };
      run();
      window.requestAnimationFrame(run);
      setTimeout(run, 40);
      setTimeout(run, 180);
    }

    _bindScrollVisibility() {
      if (!this.toggleBtn) return;
      let raf = null;
      const onScroll = () => {
        if (raf) return;
        raf = window.requestAnimationFrame(() => {
          raf = null;
          if (!this.container || this.container.classList.contains('open')) return;
          const y = Math.max(0, window.scrollY || window.pageYOffset || 0);
          const delta = y - this._lastScrollY;
          if (delta > 8 && y > 120) {
            this.container.classList.add('scroll-hidden');
          } else if (delta < -8 || y <= 80) {
            this.container.classList.remove('scroll-hidden');
          }
          this._lastScrollY = y;
        });
      };
      window.addEventListener('scroll', onScroll, { passive: true });
    }

    // Offline message queue persisted in localStorage
    _enqueueOfflineMessage(msg) {
      try {
        const key = this._storageKeys.offlineQueue;
        const arr = JSON.parse(localStorage.getItem(key) || '[]');
        arr.push(msg);
        localStorage.setItem(key, JSON.stringify(arr.slice(-50)));
      } catch (e) {}
    }

    async _flushOfflineQueue() {
      try {
        const key = this._storageKeys.offlineQueue;
        const arr = JSON.parse(localStorage.getItem(key) || '[]');
        if (!arr || !arr.length) return;
        for (const m of arr) {
          // attempt sending via websocket or fetch
          if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ type: 'user_message', conversation_id: m.conversation_id || null, content: m.content }));
          } else {
            await fetch('/chats/api/agent-send/', { method: 'POST', credentials: 'same-origin', headers: {'Content-Type':'application/json'}, body: JSON.stringify(m) });
          }
        }
        localStorage.removeItem(key);
      } catch (e) {
        console.warn('flushOfflineQueue failed', e);
      }
    }

    _showPopupPrompt(text) {
      try {
        // small transient bubble near the toggle button
        const bubble = document.createElement('div');
        bubble.className = 'agent-proactive-bubble';
        bubble.textContent = text;
        bubble.style.position = 'fixed';
        bubble.style.right = '20px';
        bubble.style.bottom = '120px';
        bubble.style.background = 'rgba(0,0,0,0.8)';
        bubble.style.color = 'white';
        bubble.style.padding = '10px 14px';
        bubble.style.borderRadius = '18px';
        bubble.style.zIndex = 99999;
        document.body.appendChild(bubble);
        setTimeout(() => { bubble.classList.add('visible'); }, 50);
        bubble.addEventListener('click', () => { this.container.classList.add('open'); if (this.input) this.input.focus(); bubble.remove(); });
        setTimeout(() => { try{ bubble.remove(); }catch(e){} }, 10000);
      } catch (e) {}
    }

    _closePanel() {
      this.container.classList.remove('open');
      if (this.panel) this.panel.setAttribute('aria-hidden', 'true');
      if (this.toggleBtn) this.toggleBtn.setAttribute('aria-expanded', 'false');
      localStorage.setItem(this._storageKeys.panelOpen, 'false');
      this.input?.blur();
    }

    async loadHistory() {
      this._showHistoryLoading(true);
      try {
        const r = await fetch('/chats/api/agent-history/', { credentials: 'same-origin' });
        if (r.ok) {
          const j = await r.json();
          if (j.success && Array.isArray(j.history)) {
            if (this.messagesEl) this.messagesEl.innerHTML = '';
            j.history.forEach(h => this._renderStoredMessage(h, { autoScroll: false }));
            this._maybeShowDailyWelcome();
            this._scrollToLatest();
            this._showHistoryLoading(false);
            return;
          }
        }
      } catch (e) {}
      try {
        const raw = localStorage.getItem(this._storageKeys.history);
        if (raw) {
          if (this.messagesEl) this.messagesEl.innerHTML = '';
          JSON.parse(raw).forEach(h => this._renderStoredMessage(h, { autoScroll: false }));
          this._maybeShowDailyWelcome();
          this._scrollToLatest();
        }
      } catch (e) {}
      this._maybeShowDailyWelcome();
      this._showHistoryLoading(false);
    }

    _renderStoredMessage(h, options = {}) {
      const role = h.role === 'user' ? 'user' : 'bot';
      const content = role === 'bot' ? this._sanitizeLegacyWelcome(h.content) : h.content;
      const rawContent = String(content || '').trim();
      if (role === 'bot' && rawContent && (rawContent.startsWith('{') || rawContent.startsWith('['))) {
        try {
          const parsed = this._normalizeAssistantData(JSON.parse(rawContent));
          if (typeof parsed === 'object' && parsed !== null) {
            const html = this._formatStructuredMessage(parsed);
            this._appendMessage('bot', html, true, h.timestamp, !!options.autoScroll);
            return;
          }
        } catch (e) {}
      }
      this._appendMessage(role, escapeHtml(content), false, h.timestamp, !!options.autoScroll);
    }

    async persistMessage(role, content, meta, timestamp) {
      const msg = { role, content, meta: meta || null, timestamp: timestamp || new Date().toISOString() };
      try {
        const key = this._storageKeys.history;
        const arr = JSON.parse(localStorage.getItem(key) || '[]');
        arr.push(msg);
        localStorage.setItem(key, JSON.stringify(arr.slice(-200)));
      } catch (e) {}
      try {
        await fetch('/chats/api/agent-history/', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(msg)
        });
      } catch (e) {}
    }

    _sendGenerate(text, options = {}) {
      this.lastUserPrompt = typeof text === 'string' ? text : '';
      let history = null;
      try {
        const raw = localStorage.getItem(this._storageKeys.history);
        if (raw) history = JSON.parse(raw).slice(-40).map(h => ({ role: h.role, content: h.content }));
      } catch (e) {}
      const mode = options.mode || 'assistant';
      const requestId = `req_${Date.now()}_${++this._requestCounter}`;
      this._pendingRequestModes[requestId] = mode;
      if (options.sourceMessageId) this._pendingPromptByRequestId[requestId] = String(options.sourceMessageId);
      const payload = { type: 'generate', title: text, history, mode, request_id: requestId };
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(payload));
      } else {
        setTimeout(() => {
          if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(payload));
          } else {
            this._showTyping(false);
            const failedId = options.sourceMessageId ? String(options.sourceMessageId) : null;
            this._markPromptFailed(failedId, 'Unable to reach assistant');
          }
        }, 1000);
      }
    }

    _handleMessage(data) {
      if (data.type === 'generate_response') {
        this._showTyping(false);
        if (data.ok) {
          const requestId = data.request_id || null;
          const mode = data.mode || (requestId ? this._pendingRequestModes[requestId] : null) || 'assistant';
          const sourceMsgId = requestId ? this._pendingPromptByRequestId[requestId] : null;
          if (requestId) delete this._pendingRequestModes[requestId];
          if (requestId) delete this._pendingPromptByRequestId[requestId];
          if (sourceMsgId) this._clearPromptFailed(sourceMsgId);
          const normalized = this._normalizeAssistantData(data.data);
          this.lastData = normalized;
          if (mode === 'listing_fields') {
            if (typeof normalized === 'object' && normalized !== null) {
              applyToListingForm(normalized);
              this.lastData = normalized;
            }
            return;
          }
          const html = this._formatStructuredMessage(normalized);
          this._appendMessage('bot', html, true);
          this.persistMessage('assistant', JSON.stringify(normalized));
          this._syncGlobalCartState(normalized);
          if (!this.container.classList.contains('open') && this.unreadBadge) this.unreadBadge.hidden = false;
          if (document.getElementById('id_title') && typeof normalized === 'object') this._showApplyModal(normalized);
        } else {
          const requestId = data.request_id || null;
          const sourceMsgId = requestId ? this._pendingPromptByRequestId[requestId] : null;
          if (requestId) delete this._pendingRequestModes[requestId];
          if (requestId) delete this._pendingPromptByRequestId[requestId];
          this._markPromptFailed(sourceMsgId, data.error || 'Prompt failed');
        }
      } else if (data.type === 'error') {
        this._showTyping(false);
        this._markPromptFailed(null, data.error || 'Prompt failed');
      }
    }

    _findMessageElById(msgId) {
      if (!this.messagesEl || !msgId) return null;
      return this.messagesEl.querySelector(`.agent-msg[data-agent-msg-id="${String(msgId)}"]`);
    }

    _clearPromptFailed(msgId) {
      const el = this._findMessageElById(msgId);
      if (!el) return;
      el.classList.remove('failed');
      const badge = el.querySelector('.agent-retry-badge');
      if (badge) badge.remove();
    }

    _markPromptFailed(msgId, reason) {
      let target = msgId ? this._findMessageElById(msgId) : null;
      if (!target && this.messagesEl) {
        const candidates = Array.from(this.messagesEl.querySelectorAll('.agent-msg.user'));
        target = candidates.length ? candidates[candidates.length - 1] : null;
      }
      if (!target) return;
      const mId = target.dataset.agentMsgId;
      target.classList.add('failed');
      const existing = target.querySelector('.agent-retry-badge');
      if (existing) existing.remove();
      const badge = document.createElement('button');
      badge.type = 'button';
      badge.className = 'agent-retry-badge';
      badge.innerHTML = '<i class="bi bi-arrow-clockwise"></i> Retry';
      badge.title = reason || 'Retry prompt';
      badge.addEventListener('click', (e) => {
        e.stopPropagation();
        const txt = this._messageTextById[mId] || '';
        if (!txt) return;
        this._clearPromptFailed(mId);
        this._showTyping(true);
        this._sendGenerate(txt, { mode: 'assistant', sourceMessageId: mId });
      });
      target.appendChild(badge);
    }

    _normalizeAssistantData(raw) {
      let d = raw;
      if (typeof d === 'string') {
        const parsed = parseJsonObject(d);
        if (parsed && typeof parsed === 'object') d = parsed;
        else return { text: d, platform_items: [] };
      }

      if (!d || typeof d !== 'object') {
        return { text: String(d || ''), platform_items: [] };
      }

      if (typeof d.text === 'string') {
        d.text = this._sanitizeLegacyWelcome(d.text);
        const nested = parseJsonObject(d.text);
        if (nested && typeof nested === 'object') {
          d = Object.assign({}, d, nested);
        }
      }

      const items = Array.isArray(d.platform_items) ? d.platform_items : (Array.isArray(d.items) ? d.items : []);
      d.platform_items = items.filter(Boolean);
      return d;
    }

    _formatAssistantText(text) {
      let safeText = escapeHtml(String(text || '')).trim();
      if (!safeText) return '';
      safeText = safeText.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
      safeText = safeText.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, label, href) => {
        const normalized = normalizeAssistantHref(href);
        if (!normalized) return `${label} (${href})`;
        const external = /^https?:\/\//i.test(normalized);
        const target = external ? ' target="_blank" rel="noopener noreferrer"' : '';
        return `<a class="agent-inline-link" href="${escapeHtml(normalized)}"${target}>${label}</a>`;
      });
      safeText = linkifyEscapedText(safeText);
      const blocks = safeText.split(/\n\s*\n/);
      const htmlBlocks = blocks.map((block) => {
        const lines = block.split('\n').map((line) => line.trim()).filter(Boolean);
        if (!lines.length) return '';
        const isBulletBlock = lines.every((line) => /^[-*]\s+/.test(line));
        if (isBulletBlock) {
          const lis = lines.map((line) => `<li>${line.replace(/^[-*]\s+/, '')}</li>`).join('');
          return `<ul class="agent-formatted-list">${lis}</ul>`;
        }
        return `<p class="agent-formatted-paragraph">${lines.join('<br>')}</p>`;
      }).filter(Boolean);
      return htmlBlocks.join('');
    }

    _extractTerms(text) {
      if (!text) return [];
      return String(text)
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .split(/\s+/)
        .filter((t) => t.length > 2);
    }

    _isItemRelevant(item, contextTerms, contextText) {
      const title = String(item.title || item.name || item.store_name || '').toLowerCase().trim();
      if (!title) return false;
      if (contextText.includes(title)) return true;
      const titleTerms = this._extractTerms(title);
      if (!titleTerms.length) return false;
      const overlap = titleTerms.filter((t) => contextTerms.includes(t)).length;
      return overlap > 0;
    }

    _filterRelevantItems(items, text) {
      if (!Array.isArray(items) || !items.length) return [];
      const contextText = `${text || ''} ${this.lastUserPrompt || ''}`.toLowerCase();
      const contextTerms = this._extractTerms(contextText);
      const actionItems = items.filter((it) => it && it.type === 'action_suggestion');
      const relevant = items.filter((item) => this._isItemRelevant(item, contextTerms, contextText));
      if (relevant.length) return [...relevant, ...actionItems].slice(0, 8);

      if (/\b(order|orders|track|delivery)\b/.test(contextText)) {
        return [...items.filter((it) => it && it.type === 'order'), ...actionItems].slice(0, 8);
      }
      if (/\b(store|shop|seller)\b/.test(contextText)) {
        return [...items.filter((it) => it && it.type === 'store'), ...actionItems].slice(0, 8);
      }
      if (/\b(subscription|subscriptions|plan|plans|billing|renew|upgrade|downgrade|cancel subscription|payment option)\b/.test(contextText)) {
        return [...items.filter((it) => it && (it.type === 'subscription' || it.type === 'subscription_plan' || it.type === 'store')), ...actionItems].slice(0, 8);
      }
      if (/\b(cart|checkout)\b/.test(contextText)) {
        return [...items.filter((it) => it && (it.type === 'cart_item' || it.type === 'cart')), ...actionItems].slice(0, 8);
      }
      if (/\b(arrival|arrivals|listing|listings|item|items|featured|product|products)\b/.test(contextText)) {
        return [...items.filter((it) => it && (it.type === 'listing' || it.type === 'favorite')), ...actionItems].slice(0, 8);
      }
      return actionItems.slice(0, 4);
    }

    _buildImageCandidates(item) {
      const raw = item.image || item.image_url || item.avatar || item.logo || item.store_logo || item.store_image || item.cover_image || null;
      const candidates = [];
      if (!raw) return ['/static/images/placeholder.png'];

      const absRaw = toAbsoluteUrl(raw) || ensureHttps(raw);
      if (absRaw) candidates.push(absRaw);

      if (typeof raw === 'string' && raw.startsWith('/')) {
        const normalized = normalizeMediaVersionPath(raw);
        if (normalized) {
          const absNormalized = toAbsoluteUrl(normalized);
          if (absNormalized) candidates.push(absNormalized);
        }
      }

      const localFromCloudinary = cloudinaryToLocalUrl(raw);
      if (localFromCloudinary) {
        const absLocal = toAbsoluteUrl(localFromCloudinary) || ensureHttps(localFromCloudinary);
        if (absLocal) candidates.push(absLocal);
      }

      if (typeof raw === 'string' && /^https?:\/\//i.test(raw)) {
        const localFromCloudinaryAbs = cloudinaryToLocalUrl(raw);
        if (localFromCloudinaryAbs) {
          const abs = toAbsoluteUrl(localFromCloudinaryAbs) || ensureHttps(localFromCloudinaryAbs);
          if (abs) candidates.push(abs);
        }
      }

      candidates.push('/static/images/placeholder.png');
      return uniqueNonEmpty(candidates);
    }

    _formatStructuredMessage(d) {
      d = this._normalizeAssistantData(d);
      let html = '';
      // Plain text response
      if (d.text) html += '<div class="agent-desc">' + this._formatAssistantText(d.text) + '</div>';
      else if (d.description) html += '<div class="agent-desc">' + this._formatAssistantText(d.description) + '</div>';

      // Key features (for listing generation)
      if (d.key_features && Array.isArray(d.key_features)) {
        html += '<ul class="agent-features">' + d.key_features.map(f => '<li>' + escapeHtml(f) + '</li>').join('') + '</ul>';
      }
      if (d.category) html += '<div class="agent-meta"><strong>Category:</strong> ' + escapeHtml(d.category) + '</div>';

      // Platform items (render as compact pills inside bubbles)
      const items = this._filterRelevantItems(d.platform_items || d.items || [], d.text || d.description || '');
      if (items.length) {
        html += '<div class="agent-suggestions"><strong>Suggestions:</strong><div class="agent-suggestion-pills">';
        // show at most 3 pills inline
        const visible = items.slice(0, 3);
        visible.forEach(it => {
          const imageCandidates = this._buildImageCandidates(it);
          const imgUrl = imageCandidates[0] || '/static/images/placeholder.png';

          const title = escapeHtml(it.name || it.title || it.store_name || 'Item');
          const reason = escapeHtml(compactSuggestionReason(it));
          const url = it.url || '#';
          html += `<div class="agent-suggestion-pill" data-item-id="${it.id}" data-item-type="${it.type}" data-item-url="${escapeHtml(url)}">` +
                  `<img class="agent-suggestion-image" src="${escapeHtml(imgUrl)}" alt="" data-fallback-index="0" data-fallbacks="${escapeHtml(JSON.stringify(imageCandidates))}">` +
                  `<div class="pill-content"><div class="pill-title">${title}</div>${reason ? `<div class="pill-reason">${reason}</div>` : ''}</div></div>`;
        });
        if (items.length > 3) {
          const more = items.length - 3;
          html += `<div class="agent-suggestion-pill view-more" data-more-count="${more}">View ${more} more</div>`;
        }
        html += '</div></div>';
      }
      return html;
    }

    _showTyping(show) {
      if (!this.typingEl) return;
      this.typingEl.hidden = !show;
    }

    _sendFeedback(kind, value, assistantText) {
      try {
        fetch('/chats/api/agent-feedback/', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message_id: null,
            feedback: kind,
            value: !!value,
            prompt: this.lastUserPrompt || '',
            assistant_text: String(assistantText || this.lastAssistantText || '').slice(0, 1500),
          })
        }).catch(e => console.warn('feedback send failed', e));
      } catch (e) {}
      if (window.showToast) {
        window.showToast(value ? 'Thanks for your feedback!' : 'Feedback removed', 'info');
      }
    }

    _syncGlobalCartState(data) {
      try {
        let count = null;
        let total = null;
        const items = Array.isArray((data || {}).platform_items) ? data.platform_items : [];
        for (const it of items) {
          if (!it || typeof it !== 'object') continue;
          if (it.cart_item_count !== undefined && it.cart_item_count !== null) count = Number(it.cart_item_count);
          if (it.cart_total !== undefined && it.cart_total !== null) total = Number(it.cart_total);
        }
        if (count === null && data && data.cart_item_count !== undefined) count = Number(data.cart_item_count);
        if (total === null && data && data.cart_total !== undefined) total = Number(data.cart_total);
        if (Number.isFinite(count) || Number.isFinite(total)) {
          document.dispatchEvent(new CustomEvent('cartUpdated', {
            detail: {
              itemCount: Number.isFinite(count) ? count : undefined,
              total: Number.isFinite(total) ? total : undefined,
            }
          }));
        }
      } catch (e) {}
    }

    _appendMessage(kind, htmlOrText, allowHtml = false, timestamp, forceScroll = false) {
      if (!this.messagesEl) return;
      const el = document.createElement('div');
      el.className = 'agent-msg ' + kind;
      el.dataset.agentMsgId = `m_${++this._messageIdCounter}`;
      const contentWrap = document.createElement('div');
      contentWrap.className = 'agent-msg-content';
      if (allowHtml) contentWrap.innerHTML = htmlOrText;
      else contentWrap.textContent = htmlOrText;
      contentWrap.style.whiteSpace = 'pre-wrap';
      contentWrap.style.wordBreak = 'break-word';
      el.appendChild(contentWrap);

      if (timestamp) {
        const ts = document.createElement('div');
        ts.className = 'agent-msg-ts';
        ts.textContent = this._formatTime(timestamp);
        el.appendChild(ts);
      }

      if (kind === 'bot') {
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'agent-message-actions';
        const copyBtn = document.createElement('button');
        copyBtn.className = 'agent-action-btn';
        copyBtn.innerHTML = '<i class="bi bi-files"></i> Copy';
        copyBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const text = contentWrap.innerText || contentWrap.textContent;
          navigator.clipboard.writeText(text).then(() => {
            copyBtn.innerHTML = '<i class="bi bi-check-lg"></i> Copied!';
            setTimeout(() => { copyBtn.innerHTML = '<i class="bi bi-files"></i> Copy'; }, 2000);
          }).catch(() => alert('Failed to copy'));
        });
        actionsDiv.appendChild(copyBtn);

        const likeBtn = document.createElement('button');
        likeBtn.className = 'agent-action-btn';
        likeBtn.innerHTML = '<i class="bi bi-hand-thumbs-up"></i>';
        likeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          likeBtn.classList.toggle('active');
          this._sendFeedback('like', likeBtn.classList.contains('active'), contentWrap.innerText || contentWrap.textContent || '');
        });
        actionsDiv.appendChild(likeBtn);

        const dislikeBtn = document.createElement('button');
        dislikeBtn.className = 'agent-action-btn';
        dislikeBtn.innerHTML = '<i class="bi bi-hand-thumbs-down"></i>';
        dislikeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          dislikeBtn.classList.toggle('active');
          this._sendFeedback('dislike', dislikeBtn.classList.contains('active'), contentWrap.innerText || contentWrap.textContent || '');
        });
        actionsDiv.appendChild(dislikeBtn);

        el.appendChild(actionsDiv);
      }

      // Update like/dislike buttons to call feedback endpoint with message id when available
      el.addEventListener('click', (e) => {
        const like = el.querySelector('.agent-action-btn .bi-hand-thumbs-up');
        const dislike = el.querySelector('.agent-action-btn .bi-hand-thumbs-down');
        // nothing to do here — feedback handlers already attached when creating buttons
      });

      this.messagesEl.appendChild(el);
      if (kind === 'bot') {
        el.classList.add('msg-enter');
        window.setTimeout(() => {
          try { el.classList.remove('msg-enter'); } catch (e) {}
        }, 420);
      }
      if (kind === 'bot') {
        this.lastAssistantText = contentWrap.innerText || contentWrap.textContent || this.lastAssistantText;
      }
      if (forceScroll || !this._userScrollingMessages) {
        this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
      }
      return el;
    }

    _formatTime(iso) {
      try {
        const d = new Date(iso);
        const now = new Date();
        const diff = Math.floor((now - d) / 1000);
        if (diff < 10) return 'just now';
        if (diff < 60) return diff + 's';
        if (diff < 3600) return Math.floor(diff / 60) + 'm';
        if (diff < 86400) return Math.floor(diff / 3600) + 'h';
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      } catch (e) {
        return iso;
      }
    }

    _ensureApplyModal() {
      if (this._applyModal) return this._applyModal;
      const m = document.createElement('div');
      m.className = 'agent-apply-modal';
      m.innerHTML = `
        <div class="agent-apply-preview"></div>
        <div style="text-align:right">
          <button class="agent-apply-cancel btn btn-outline-secondary">Cancel</button>
          <button class="agent-apply-confirm btn btn-primary">Apply</button>
        </div>
      `;
      document.body.appendChild(m);
      m.querySelector('.agent-apply-cancel').addEventListener('click', () => { m.style.display = 'none'; });
      m.querySelector('.agent-apply-confirm').addEventListener('click', () => {
        if (this._pendingApplyData) applyToListingForm(this._pendingApplyData);
        m.style.display = 'none';
      });
      this._applyModal = m;
      return m;
    }

    _showApplyModal(data) {
      try {
        const modal = this._ensureApplyModal();
        this._pendingApplyData = data;
        const preview = modal.querySelector('.agent-apply-preview');
        let html = '';
        if (typeof data === 'object') {
          if (data.title) html += '<p><strong>Title:</strong> ' + escapeHtml(data.title) + '</p>';
          if (data.description) html += '<p><strong>Description:</strong> ' + escapeHtml(data.description) + '</p>';
          if (data.key_features && Array.isArray(data.key_features)) {
            html += '<p><strong>Key features:</strong><ul>' + data.key_features.map(f => '<li>' + escapeHtml(f) + '</li>').join('') + '</ul></p>';
          }
          if (data.category) html += '<p><strong>Category:</strong> ' + escapeHtml(data.category) + '</p>';
        } else {
          html = '<p>' + escapeHtml(String(data)) + '</p>';
        }
        preview.innerHTML = html;
        modal.style.display = 'block';
      } catch (e) {
        console.warn('ShowApplyModal failed', e);
      }
    }
  }

  window.agentAddToCart = function(listingId) {
    if (window.GlobalCart && typeof window.GlobalCart.add === 'function') {
      window.GlobalCart.add(listingId, 1);
      return;
    }
    const csrftoken = (window.getCookie && window.getCookie('csrftoken')) ||
      (function() {
        const v = document.cookie.split(';').map(c => c.trim()).find(c => c.startsWith('csrftoken='));
        return v ? decodeURIComponent(v.split('=')[1]) : '';
      })();
    fetch('/cart/add/' + listingId + '/', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'X-CSRFToken': csrftoken
      },
      body: JSON.stringify({ quantity: 1 })
    })
      .then(async r => {
        const j = await r.json();
        if (r.ok && j.success) alert(j.message || 'Added to cart');
        else alert(j.error || 'Failed to add to cart');
      })
      .catch(() => alert('Failed to add to cart'));
  };

  window.agentRemoveFromCart = function(itemId) {
    if (!itemId) return;
    if (window.GlobalCart && typeof window.GlobalCart.remove === 'function') {
      window.GlobalCart.remove(itemId);
      return;
    }
    if (typeof window.removeFromCart === 'function') {
      window.removeFromCart(itemId);
      return;
    }
    alert('Unable to remove item from cart right now.');
  };

  function applyToListingForm(data) {
    try {
      // Title
      if (data.title) {
        const titleField = document.getElementById('id_title');
        if (titleField && (!titleField.value || titleField.value.length < 5)) titleField.value = data.title;
      }
      // Description
      if (data.description) {
        const desc = document.getElementById('id_description');
        if (desc && (!desc.value || desc.value.length < 20)) desc.value = data.description;
        const meta = document.getElementById('id_meta_description');
        if (meta && (!meta.value || meta.value.length < 20)) meta.value = (data.meta_description || data.description).slice(0, 160);
      }
      // Category
      if (data.category_id) {
        const cat = document.getElementById('id_category');
        if (cat) {
          cat.value = String(data.category_id);
          cat.dispatchEvent(new Event('change'));
        }
      }
      // Other fields
      const fields = Object.assign({}, data.dynamic_fields || {}, {
        brand: data.brand, model: data.model, color: data.color,
        material: data.material, dimensions: data.dimensions,
        weight: data.weight, price: data.price
      });
      Object.keys(fields).forEach(fn => {
        if (fields[fn] === undefined || fields[fn] === null) return;
        const std = document.getElementById('id_' + fn);
        if (std) {
          if (std.type === 'checkbox') std.checked = !!fields[fn];
          else std.value = fields[fn];
        }
        const dyn = document.getElementById('dynamic_' + fn);
        if (dyn) {
          if (dyn.type === 'checkbox') dyn.checked = !!fields[fn];
          else dyn.value = fields[fn];
        }
      });
    } catch (e) {
      console.warn('applyToListingForm error', e);
    }
  }

  function initAll() {
    document.querySelectorAll('[data-agent-widget]').forEach(el => {
      if (!el.__agent_inited) {
        const aw = new AgentWidget(el);
        aw.loadHistory();
        el.__agent_inited = true;
        // expose a simple global API for other scripts to push messages / interact
        try {
          window.baysokoAgent = window.baysokoAgent || {};
          window.baysokoAgent.instance = aw;
          window.baysokoAgent.pushMessage = function(role, content){
            try{ aw._appendMessage(role, String(content), false, new Date().toISOString()); aw.persistMessage(role, String(content)); }catch(e){}
          };
          // Update suggestions programmatically: accepts array of items like server payload
          window.baysokoAgent.updateSuggestions = function(items){
            try{
              if (!items || !items.length) return;
              const data = { platform_items: items };
              const html = aw._formatStructuredMessage(data);
              // append as bot message with HTML allowed
              aw._appendMessage('bot', html, true);
              aw.persistMessage('assistant', JSON.stringify(data));
            } catch(e){ console.warn('updateSuggestions failed', e); }
          };
        } catch (e) { console.debug('Failed to expose baysokoAgent API', e); }
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
  } else {
    initAll();
  }
})();
