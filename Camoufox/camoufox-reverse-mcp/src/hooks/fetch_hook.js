// 模板说明: 记录 fetch 请求参数与调用栈,同时保留发起者日志给 get_request_initiator 使用。
(function() {
    if (window.__mcp_fetch_hooked) return;
    window.__mcp_fetch_hooked = true;
    window.__mcp_fetch_log = window.__mcp_fetch_log || [];
    window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};

    const _fetchDesc = Object.getOwnPropertyDescriptor(window, 'fetch');
    const _fetch = window.fetch;

    const hookedFetch = async function(input, init) {
        init = init || {};
        const url = typeof input === 'string' ? input : (input instanceof Request ? input.url : String(input));
        const method = init.method || (input instanceof Request ? input.method : 'GET') || 'GET';
        const info = {
            url, method,
            headers: init.headers ? (typeof init.headers === 'object' ? Object.assign({}, init.headers) : {}) : {},
            body: init.body ? String(init.body).substring(0, 5000) : null,
            stack: new Error().stack,
            timestamp: Date.now()
        };

        // v0.6.0: dedicated initiator log for get_request_initiator fallback
        window.__mcp_fetch_initiator_log = window.__mcp_fetch_initiator_log || [];
        try {
            var _stack = '';
            try { _stack = new Error().stack || ''; } catch (e) {}
            window.__mcp_fetch_initiator_log.push({
                url: String(url),
                method: method,
                stack: _stack.split('\n').slice(0, 15).join('\n'),
                ts: Date.now()
            });
            if (window.__mcp_fetch_initiator_log.length > 500) {
                window.__mcp_fetch_initiator_log.shift();
            }
        } catch (e) {}

        try {
            const response = await _fetch.apply(this, arguments);
            info.status = response.status;
            info.ok = response.ok;
            window.__mcp_fetch_log.push(info);
            if (window.__mcp_fetch_log.length > 500) window.__mcp_fetch_log.shift();
            return response;
        } catch (e) {
            info.error = e.message;
            window.__mcp_fetch_log.push(info);
            throw e;
        }
    };

    hookedFetch.toString = function() { return 'function fetch() { [native code] }'; };

    try {
        Object.defineProperty(window, 'fetch', {
            value: hookedFetch, writable: true, configurable: true
        });
    } catch(e) {
        window.fetch = hookedFetch;
    }

    window.__mcp_hook_uninstallers.fetch = function() {
        const restored = [];
        try {
            _fetchDesc ? Object.defineProperty(window, 'fetch', _fetchDesc) : (window.fetch = _fetch);
            restored.push('window.fetch');
        } catch(e) {}
        window.__mcp_fetch_hooked = false;
        delete window.__mcp_hook_uninstallers.fetch;
        return restored;
    };
})();
