// 模板说明: 包装 WebSocket 构造器与 send/message 事件,采集 WS 连接和消息方向。
(function() {
    if (window.__mcp_ws_hooked) return;
    window.__mcp_ws_hooked = true;
    window.__mcp_ws_log = [];
    window.__mcp_hook_uninstallers = window.__mcp_hook_uninstallers || {};
    const _WebSocketDesc = Object.getOwnPropertyDescriptor(window, 'WebSocket');
    const _WebSocket = window.WebSocket;

    window.WebSocket = function(url, protocols) {
        console.log('[WS] New connection:', url);
        const ws = protocols ? new _WebSocket(url, protocols) : new _WebSocket(url);
        const connInfo = { url, messages: [], timestamp: Date.now() };
        window.__mcp_ws_log.push(connInfo);

        const _send = ws.send.bind(ws);
        ws.send = function(data) {
            const msg = { direction: 'send', data: String(data).substring(0, 5000), timestamp: Date.now() };
            connInfo.messages.push(msg);
            console.log('[WS:send]', String(data).substring(0, 200));
            return _send(data);
        };

        ws.addEventListener('message', function(event) {
            const msg = { direction: 'recv', data: String(event.data).substring(0, 5000), timestamp: Date.now() };
            connInfo.messages.push(msg);
            console.log('[WS:recv]', String(event.data).substring(0, 200));
        });

        ws.addEventListener('close', function(event) {
            connInfo.closedAt = Date.now();
            connInfo.closeCode = event.code;
            console.log('[WS] Closed:', url, 'code:', event.code);
        });

        return ws;
    };
    window.WebSocket.prototype = _WebSocket.prototype;
    window.WebSocket.CONNECTING = _WebSocket.CONNECTING;
    window.WebSocket.OPEN = _WebSocket.OPEN;
    window.WebSocket.CLOSING = _WebSocket.CLOSING;
    window.WebSocket.CLOSED = _WebSocket.CLOSED;

    window.__mcp_hook_uninstallers.websocket = function() {
        const restored = [];
        try {
            _WebSocketDesc ? Object.defineProperty(window, 'WebSocket', _WebSocketDesc) : (window.WebSocket = _WebSocket);
            restored.push('window.WebSocket');
        } catch(e) {}
        window.__mcp_ws_hooked = false;
        delete window.__mcp_hook_uninstallers.websocket;
        return restored;
    };
})();
