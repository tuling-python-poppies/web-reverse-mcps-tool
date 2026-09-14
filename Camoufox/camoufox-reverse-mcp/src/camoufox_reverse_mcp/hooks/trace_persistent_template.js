// trace_persistent_template.js - injected helper script for camoufox-reverse-mcp
(function() {
    const path = '{{FUNCTION_PATH}}';
    const parts = path.split('.');
    let parent = window;
    for (let i = 0; i < parts.length - 1; i++) {
        parent = parent[parts[i]];
        if (!parent) { console.warn('[TRACE] Cannot find:', path); return; }
    }
    const funcName = parts[parts.length - 1];
    const original = parent[funcName];
    if (typeof original !== 'function') {
        console.warn('[TRACE] Not a function:', path);
        return;
    }

    window.__mcp_traces = window.__mcp_traces || {};
    window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};
    window.__mcp_traces[path] = [];
    let captureCount = 0;
    const maxCaptures = {{MAX_CAPTURES}};

    const wrapper = function(...args) {
        if (captureCount >= maxCaptures) return original.apply(this, args);
        captureCount++;

        const entry = { callIndex: captureCount, timestamp: Date.now(), __path__: path };
        if ({{LOG_ARGS}}) entry.args = JSON.stringify(args).substring(0, 2000);
        if ({{LOG_STACK}}) entry.stack = new Error().stack;

        const result = original.apply(this, args);

        if ({{LOG_RETURN}}) {
            try { entry.returnValue = JSON.stringify(result).substring(0, 2000); }
            catch(e) { entry.returnValue = String(result).substring(0, 500); }
        }

        window.__mcp_traces[path].push(entry);
        console.log('__MCP_TRACE__:' + JSON.stringify(entry));
        return result;
    };

    Object.defineProperty(wrapper, 'name', { value: funcName });
    Object.defineProperty(wrapper, 'length', { value: original.length });
    wrapper.toString = function() { return original.toString(); };

    try {
        Object.defineProperty(parent, funcName, {
            value: wrapper, writable: true, configurable: true
        });
    } catch(e) {
        parent[funcName] = wrapper;
    }

    window.__mcp_hook_uninstallers['trace:' + path] = function() {
        if (parent[funcName] === wrapper) parent[funcName] = original;
        delete window.__mcp_hook_uninstallers['trace:' + path];
        return [path];
    };

    console.log('[TRACE] Started persistent tracing:', path, '(max', maxCaptures, 'captures)');
})();
