// 模板说明: 观察 btoa/atob/JSON.stringify 的输入输出,帮助定位简单编码/签名前后数据。
(function() {
    if (window.__mcp_crypto_hooked) return;
    window.__mcp_crypto_hooked = true;
    window.__mcp_crypto_log = [];
    window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};

    const _btoaDesc = Object.getOwnPropertyDescriptor(window, 'btoa');
    const _atobDesc = Object.getOwnPropertyDescriptor(window, 'atob');
    const _stringifyDesc = Object.getOwnPropertyDescriptor(JSON, 'stringify');
    const _btoa = window.btoa;
    const _atob = window.atob;
    window.btoa = function(s) {
        const result = _btoa.call(this, s);
        const info = { func: 'btoa', input: s, output: result, stack: new Error().stack, timestamp: Date.now() };
        window.__mcp_crypto_log.push(info);
        console.log('[CRYPTO] btoa:', s.substring(0, 100), '->', result.substring(0, 100));
        return result;
    };
    window.atob = function(s) {
        const result = _atob.call(this, s);
        const info = { func: 'atob', input: s, output: result, stack: new Error().stack, timestamp: Date.now() };
        window.__mcp_crypto_log.push(info);
        return result;
    };

    const _stringify = JSON.stringify;
    JSON.stringify = function() {
        const result = _stringify.apply(this, arguments);
        if (result && result.length < 2000) {
            window.__mcp_crypto_log.push({
                func: 'JSON.stringify',
                input: _stringify(arguments[0]).substring(0, 500),
                output: result.substring(0, 500),
                timestamp: Date.now()
            });
        }
        return result;
    };

    window.__mcp_hook_uninstallers.crypto = function() {
        const restored = [];
        try { _btoaDesc ? Object.defineProperty(window, 'btoa', _btoaDesc) : (window.btoa = _btoa); restored.push('window.btoa'); } catch(e) {}
        try { _atobDesc ? Object.defineProperty(window, 'atob', _atobDesc) : (window.atob = _atob); restored.push('window.atob'); } catch(e) {}
        try { _stringifyDesc ? Object.defineProperty(JSON, 'stringify', _stringifyDesc) : (JSON.stringify = _stringify); restored.push('JSON.stringify'); } catch(e) {}
        window.__mcp_crypto_hooked = false;
        delete window.__mcp_hook_uninstallers.crypto;
        return restored;
    };
})();
