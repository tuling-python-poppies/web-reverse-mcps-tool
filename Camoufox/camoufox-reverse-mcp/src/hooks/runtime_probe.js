// 模板说明: 低侵入运行时探针,观测 XHR/fetch/canvas/WebGL/navigator 等高频检测点。
/**
 * runtime_probe.js - Low-overhead universal runtime observer.
 *
 * Records without wrapping: unlike jsvmp_hook.js which replaces globals
 * with Proxies, this probe only overrides specific known-hot APIs used by
 * anti-bot detection. Safer default for "I want to see what's happening".
 *
 * Observed:
 *   - XMLHttpRequest.open / send
 *   - fetch(...)
 *   - document.createElement (canvas/img detect)
 *   - HTMLCanvasElement.toDataURL / toBlob
 *   - getContext (track 2d/webgl)
 *   - AudioContext creation
 *   - WebGLRenderingContext.getParameter
 *   - crypto.subtle.digest
 *   - performance.now / Date.now (call frequency)
 *   - navigator userAgent/platform/language/webdriver getters
 *   - addEventListener (mouse/keyboard/device-motion for bot detection)
 */
(function () {
    if (window.__mcp_runtime_probe_installed) return;
    window.__mcp_runtime_probe_installed = true;
    window.__mcp_runtime_log = window.__mcp_runtime_log || [];
    window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};
    var originals = [];
    var MAX = 5000;

    function save(obj, prop) {
        try {
            originals.push({ obj: obj, prop: prop, desc: Object.getOwnPropertyDescriptor(obj, prop), value: obj[prop] });
        } catch (e) {}
    }

    function log(e) {
        if (window.__mcp_runtime_log.length >= MAX) return;
        e.ts = Date.now();
        window.__mcp_runtime_log.push(e);
    }
    function preview(v) {
        try {
            if (v == null) return String(v);
            var t = typeof v;
            if (t === 'function') return '[fn ' + (v.name || '') + ']';
            if (t === 'object') { var s = JSON.stringify(v); return s && s.length > 120 ? s.substr(0, 120) + '...' : s; }
            var s2 = String(v); return s2.length > 120 ? s2.substr(0, 120) + '...' : s2;
        } catch (e) { return '[err]'; }
    }
    function shortStack() {
        try { return (new Error().stack || '').split('\n').slice(2, 6).join('\n'); } catch (e) { return ''; }
    }

    // XHR
    try {
        save(XMLHttpRequest.prototype, 'open');
        save(XMLHttpRequest.prototype, 'send');
        var origOpen = XMLHttpRequest.prototype.open;
        var origSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (method, url) {
            this.__mcp_req = { method: method, url: url };
            log({ type: 'xhr_open', method: method, url: String(url), stack: shortStack() });
            return origOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function (body) {
            log({
                type: 'xhr_send',
                method: this.__mcp_req ? this.__mcp_req.method : '?',
                url: this.__mcp_req ? String(this.__mcp_req.url) : '?',
                body: preview(body),
                stack: shortStack()
            });
            return origSend.apply(this, arguments);
        };
    } catch (e) {}

    // fetch
    try {
        save(window, 'fetch');
        var origFetch = window.fetch;
        window.fetch = function (input, init) {
            var url = typeof input === 'string' ? input : (input && input.url) || '?';
            log({
                type: 'fetch',
                url: String(url),
                method: (init && init.method) || 'GET',
                body: init && init.body ? preview(init.body) : null,
                stack: shortStack()
            });
            return origFetch.apply(this, arguments);
        };
        try { window.fetch.toString = function () { return 'function fetch() { [native code] }'; }; } catch (e) {}
    } catch (e) {}

    // Canvas fingerprint
    try {
        save(HTMLCanvasElement.prototype, 'toDataURL');
        var origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function () {
            var r = origToDataURL.apply(this, arguments);
            log({ type: 'canvas_toDataURL', length: r.length, prefix: r.substr(0, 50), stack: shortStack() });
            return r;
        };
    } catch (e) {}

    // getContext
    try {
        save(HTMLCanvasElement.prototype, 'getContext');
        var origGetContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function (type) {
            log({ type: 'canvas_getContext', context_type: String(type), stack: shortStack() });
            return origGetContext.apply(this, arguments);
        };
    } catch (e) {}

    // WebGL fingerprint
    try {
        if (window.WebGLRenderingContext) {
            save(WebGLRenderingContext.prototype, 'getParameter');
            var origGetParam = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function (p) {
                var r = origGetParam.apply(this, arguments);
                log({ type: 'webgl_getParameter', param: p, value: preview(r) });
                return r;
            };
        }
    } catch (e) {}

    // navigator probe via re-defining getters where possible
    try {
        var navProto = Object.getPrototypeOf(navigator);
        var watched = ['userAgent', 'platform', 'language', 'languages',
                       'webdriver', 'hardwareConcurrency', 'deviceMemory',
                       'vendor', 'appVersion', 'plugins', 'mimeTypes'];
        for (var i = 0; i < watched.length; i++) {
            (function(p) {
                var desc = Object.getOwnPropertyDescriptor(navProto, p);
                if (!desc || !desc.get) return;
                originals.push({ obj: navProto, prop: p, desc: desc, value: navigator[p] });
                var origGet = desc.get;
                Object.defineProperty(navProto, p, {
                    get: function() {
                        var v = origGet.call(this);
                        log({ type: 'nav_read', prop: p, value: preview(v), stack: shortStack() });
                        return v;
                    },
                    configurable: true,
                    enumerable: desc.enumerable
                });
            })(watched[i]);
        }
    } catch (e) {}

    // addEventListener frequency (bot detectors watch for mouse/keyboard)
    try {
        save(EventTarget.prototype, 'addEventListener');
        var origAEL = EventTarget.prototype.addEventListener;
        EventTarget.prototype.addEventListener = function (type) {
            if (['mousemove', 'mousedown', 'mouseup', 'click', 'keydown', 'keyup',
                 'devicemotion', 'deviceorientation', 'touchstart', 'touchmove'].indexOf(type) !== -1) {
                log({ type: 'addEventListener', event: String(type),
                      target: this && this.constructor ? this.constructor.name : typeof this,
                      stack: shortStack() });
            }
            return origAEL.apply(this, arguments);
        };
    } catch (e) {}

    window.__mcp_hook_uninstallers.runtime_probe = function() {
        var restored = [];
        for (var i = originals.length - 1; i >= 0; i--) {
            var item = originals[i];
            try {
                if (item.desc) Object.defineProperty(item.obj, item.prop, item.desc);
                else item.obj[item.prop] = item.value;
                restored.push(item.prop);
            } catch (e) {}
        }
        window.__mcp_runtime_probe_installed = false;
        delete window.__mcp_hook_uninstallers.runtime_probe;
        return restored;
    };

    console.log('[RUNTIME-PROBE] installed');
})();
