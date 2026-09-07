// 模板说明: 用 Proxy 代理目标对象,记录属性读取行为,适合轻量级环境探测观测。
(function() {
    window.__mcp_prop_access_log = window.__mcp_prop_access_log || [];
    const MAX_ENTRIES = 2000;
    const targets = JSON.parse('{{TARGETS}}');

    function installProxy(objPath, obj) {
        const parts = objPath.split('.');
        let parent = window;
        for (let i = 0; i < parts.length - 1; i++) {
            parent = parent[parts[i]];
            if (!parent) return;
        }
        const propName = parts[parts.length - 1];
        const orig = parent[propName];
        if (!orig || typeof orig !== 'object') return;

        const proxy = new Proxy(orig, {
            get: function(target, prop, receiver) {
                const val = Reflect.get(target, prop, receiver);
                if (typeof prop === 'string' && !prop.startsWith('__mcp_')) {
                    const fullPath = objPath + '.' + prop;
                    let serializedVal;
                    try {
                        serializedVal = typeof val === 'function' ? '[function]' : JSON.stringify(val);
                        if (serializedVal && serializedVal.length > 500) serializedVal = serializedVal.substring(0, 500) + '...';
                    } catch(e) {
                        serializedVal = String(val).substring(0, 200);
                    }
                    if (window.__mcp_prop_access_log.length < MAX_ENTRIES) {
                        window.__mcp_prop_access_log.push({
                            property: fullPath,
                            value: serializedVal,
                            type: typeof val,
                            stack: new Error().stack,
                            timestamp: Date.now()
                        });
                    }
                }
                return val;
            }
        });

        try {
            Object.defineProperty(parent, propName, {
                value: proxy, writable: false, configurable: false
            });
        } catch(e) {
            parent[propName] = proxy;
        }
    }

    function installPropertyGetter(fullPath) {
        const parts = fullPath.split('.');
        let parent = window;
        for (let i = 0; i < parts.length - 1; i++) {
            parent = parent[parts[i]];
            if (!parent) return;
        }
        const propName = parts[parts.length - 1];
        const descriptor = Object.getOwnPropertyDescriptor(parent, propName);
        const origValue = parent[propName];

        if (descriptor && descriptor.get) {
            const origGetter = descriptor.get;
            Object.defineProperty(parent, propName, {
                get: function() {
                    const val = origGetter.call(this);
                    if (window.__mcp_prop_access_log.length < MAX_ENTRIES) {
                        let sv;
                        try { sv = JSON.stringify(val); if (sv && sv.length > 500) sv = sv.substring(0, 500) + '...'; }
                        catch(e) { sv = String(val).substring(0, 200); }
                        window.__mcp_prop_access_log.push({
                            property: fullPath, value: sv, type: typeof val,
                            stack: new Error().stack, timestamp: Date.now()
                        });
                    }
                    return val;
                },
                set: descriptor.set,
                enumerable: descriptor.enumerable,
                configurable: false
            });
        } else if (typeof origValue !== 'object' || origValue === null) {
            Object.defineProperty(parent, propName, {
                get: function() {
                    if (window.__mcp_prop_access_log.length < MAX_ENTRIES) {
                        let sv;
                        try { sv = JSON.stringify(origValue); } catch(e) { sv = String(origValue); }
                        window.__mcp_prop_access_log.push({
                            property: fullPath, value: sv, type: typeof origValue,
                            stack: new Error().stack, timestamp: Date.now()
                        });
                    }
                    return origValue;
                },
                enumerable: true,
                configurable: false
            });
        }
    }

    for (const target of targets) {
        if (target.endsWith('.*')) {
            installProxy(target.slice(0, -2), null);
        } else {
            installPropertyGetter(target);
        }
    }

    console.log('[PROP-ACCESS] Tracking', targets.length, 'targets');
})();
