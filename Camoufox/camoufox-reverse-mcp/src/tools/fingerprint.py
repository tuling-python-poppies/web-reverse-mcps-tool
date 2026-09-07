# 模块说明: Camoufox 指纹基准导出工具。用于把浏览器侧真实指纹采样成
# env-patch/jsdom/vm 沙箱可复用的 profile。
from __future__ import annotations

from ..server import mcp, browser_manager
from ..utils.response_fmt import error_response


@mcp.tool()
async def export_fingerprint_profile(include_heavy: bool = False) -> dict:
    """Export a Camoufox runtime fingerprint profile.

    This is the Camoufox-side ground truth for environment emulation. The output
    is intentionally browser-visible runtime data, not CloakBrowser/Chromium data.

    Args:
        include_heavy: Include heavier canvas/audio samples. Default False keeps
            output compact for quick env-patch/jsdom seeding.
    """
    try:
        page = await browser_manager.get_active_page()
        profile = await page.evaluate("""async ({includeHeavy}) => {
            const safe = (fn) => { try { return { ok: true, value: fn() }; } catch (e) { return { ok: false, error: String(e && e.message || e) }; } };
            const props = (obj, names) => {
                const out = {};
                for (const name of names) out[name] = safe(() => obj[name]);
                return out;
            };
            const canvasSample = safe(() => {
                const c = document.createElement('canvas');
                c.width = 240; c.height = 60;
                const ctx = c.getContext('2d');
                ctx.textBaseline = 'top';
                ctx.font = '16px Arial';
                ctx.fillStyle = '#f60';
                ctx.fillRect(10, 10, 100, 30);
                ctx.fillStyle = '#069';
                ctx.fillText('Camoufox fingerprint 123', 12, 18);
                if (includeHeavy) return c.toDataURL();
                // Use toPlainArray (defined below) for Xray-safe TypedArray handling
                const imgData = toPlainArray(ctx.getImageData(0, 0, 16, 16).data);
                return imgData ? imgData.slice(0, 64).join(',') : '';
            });
            // v1.1.0: use index-copy helper for Firefox Xray TypedArray restriction.
            // Array.from() on WebGL-returned Int32Array inside a Xray-wrapped context
            // throws "can't access dead object" or TypeError. Read index-by-index instead.
            function toPlainArray(typedArr) {
                if (!typedArr) return null;
                try { return Array.prototype.slice.call(typedArr); } catch (e) {}
                try {
                    var out = [];
                    var len = Number(typedArr.length) || 0;
                    for (var i = 0; i < len; i++) out.push(typedArr[i]);
                    if (out.length) return out;
                } catch (e) {}
                try {
                    var s = String(typedArr);
                    if (s && s.indexOf(',') !== -1) {
                        return s.split(',').map(function(x) { return Number(x); });
                    }
                } catch (e) {}
                try {
                    var json = JSON.stringify(typedArr);
                    var parsed = JSON.parse(json);
                    if (Array.isArray(parsed)) return parsed;
                    if (parsed && typeof parsed === 'object') {
                        return Object.keys(parsed).sort().map(function(k) { return parsed[k]; });
                    }
                } catch (e) {}
                return null;
            }
            const webglSample = safe(() => {
                const c = document.createElement('canvas');
                const gl = c.getContext('webgl') || c.getContext('experimental-webgl');
                if (!gl) return null;
                var dbg = null;
                try { dbg = gl.getExtension('WEBGL_debug_renderer_info'); } catch(e) {}
                var maxViewportDims = null;
                try { maxViewportDims = toPlainArray(gl.getParameter(gl.MAX_VIEWPORT_DIMS)); }
                catch(e) { maxViewportDims = null; }
                return {
                    ok: true,
                    vendor:   dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)   : null,
                    renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                    version:  gl.getParameter(gl.VERSION),
                    shadingLanguageVersion: gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
                    maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
                    maxViewportDims: maxViewportDims,
                };
            });
            const audioSample = safe(() => {
                if (!includeHeavy) return null;
                const AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
                if (!AC) return null;
                return { supported: true, sampleRate: (new AC(1, 44100, 44100)).sampleRate };
            });
            return {
                url: location.href,
                userAgent: navigator.userAgent,
                navigator: props(navigator, [
                    'platform','language','languages','hardwareConcurrency','deviceMemory',
                    'maxTouchPoints','vendor','cookieEnabled','webdriver','pdfViewerEnabled'
                ]),
                screen: props(screen, ['width','height','availWidth','availHeight','colorDepth','pixelDepth']),
                window: {
                    innerWidth: window.innerWidth,
                    innerHeight: window.innerHeight,
                    outerWidth: window.outerWidth,
                    outerHeight: window.outerHeight,
                    devicePixelRatio: window.devicePixelRatio,
                },
                timezone: {
                    offset: new Date().getTimezoneOffset(),
                    name: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    locale: Intl.DateTimeFormat().resolvedOptions().locale,
                },
                webgl: webglSample,
                canvas: canvasSample,
                audio: audioSample,
                storage: {
                    localStorage: safe(() => !!window.localStorage),
                    sessionStorage: safe(() => !!window.sessionStorage),
                    indexedDB: safe(() => !!window.indexedDB),
                },
                performance: {
                    timeOrigin: performance.timeOrigin,
                    navigationType: performance.getEntriesByType('navigation')[0]?.type || null,
                },
            };
        }""", {"includeHeavy": include_heavy})
        return {"status": "ok", "browser": "camoufox", "profile": profile}
    except Exception as e:
        return error_response(e)
