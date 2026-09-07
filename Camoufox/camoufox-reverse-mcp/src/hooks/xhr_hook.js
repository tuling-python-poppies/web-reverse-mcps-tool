// 模板说明: 记录 XHR open/setRequestHeader/send 的参数、调用栈与响应状态,并支持卸载恢复。
(function() {
    if (window.__mcp_xhr_hooked) return;
    window.__mcp_xhr_hooked = true;
    window.__mcp_xhr_log = window.__mcp_xhr_log || [];
    window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};

    const _origProto = XMLHttpRequest.prototype;
    const _openDesc = Object.getOwnPropertyDescriptor(_origProto, 'open');
    const _setHeaderDesc = Object.getOwnPropertyDescriptor(_origProto, 'setRequestHeader');
    const _sendDesc = Object.getOwnPropertyDescriptor(_origProto, 'send');
    const _open = _origProto.open;
    const _send = _origProto.send;
    const _setReqHeader = _origProto.setRequestHeader;

    const hookedOpen = function(method, url) {
        this.__mcp_info = { method, url: String(url), headers: {}, timestamp: Date.now() };
        return _open.apply(this, arguments);
    };
    const hookedSetHeader = function(name, value) {
        if (this.__mcp_info) this.__mcp_info.headers[name] = value;
        return _setReqHeader.apply(this, arguments);
    };
    const hookedSend = function(body) {
        if (this.__mcp_info) {
            this.__mcp_info.body = typeof body === 'string' ? body : (body ? String(body).substring(0, 5000) : null);
            this.__mcp_info.stack = new Error().stack;
            const info = this.__mcp_info;
            this.addEventListener('load', function() {
                info.status = this.status;
                info.response_length = this.responseText?.length;
                window.__mcp_xhr_log.push(info);
                if (window.__mcp_xhr_log.length > 500) window.__mcp_xhr_log.shift();
            });
        }
        return _send.apply(this, arguments);
    };

    const nativeToString = function(name) {
        return 'function ' + name + '() { [native code] }';
    };

    try {
        Object.defineProperty(_origProto, 'open', {
            value: hookedOpen, writable: true, configurable: true
        });
        Object.defineProperty(_origProto, 'setRequestHeader', {
            value: hookedSetHeader, writable: true, configurable: true
        });
        Object.defineProperty(_origProto, 'send', {
            value: hookedSend, writable: true, configurable: true
        });
    } catch(e) {
        _origProto.open = hookedOpen;
        _origProto.setRequestHeader = hookedSetHeader;
        _origProto.send = hookedSend;
    }

    hookedOpen.toString = function() { return nativeToString('open'); };
    hookedSetHeader.toString = function() { return nativeToString('setRequestHeader'); };
    hookedSend.toString = function() { return nativeToString('send'); };

    window.__mcp_hook_uninstallers.xhr = function() {
        const restored = [];
        try { _openDesc ? Object.defineProperty(_origProto, 'open', _openDesc) : (_origProto.open = _open); restored.push('XMLHttpRequest.prototype.open'); } catch(e) {}
        try { _setHeaderDesc ? Object.defineProperty(_origProto, 'setRequestHeader', _setHeaderDesc) : (_origProto.setRequestHeader = _setReqHeader); restored.push('XMLHttpRequest.prototype.setRequestHeader'); } catch(e) {}
        try { _sendDesc ? Object.defineProperty(_origProto, 'send', _sendDesc) : (_origProto.send = _send); restored.push('XMLHttpRequest.prototype.send'); } catch(e) {}
        window.__mcp_xhr_hooked = false;
        delete window.__mcp_hook_uninstallers.xhr;
        return restored;
    };
})();
