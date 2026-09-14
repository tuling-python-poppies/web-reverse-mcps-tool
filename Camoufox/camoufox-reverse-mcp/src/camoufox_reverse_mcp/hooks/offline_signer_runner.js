// A separate, deadline-controlled process; vm is execution context, not isolation
// from hostile code. No browser or network dependency is required.
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
(async () => {
    try {
        const payload = JSON.parse(fs.readFileSync(0, 'utf8'));
        const context = vm.createContext({
            Buffer, TextEncoder, TextDecoder, URL, URLSearchParams,
            crypto: crypto.webcrypto,
            console: {log() {}, warn() {}, error() {}, debug() {}, info() {}},
            require(name) {
                if (name === 'crypto' || name === 'node:crypto') return crypto;
                throw new Error('Only crypto/node:crypto is available in this runner');
            },
        });
        vm.runInContext(`globalThis.__signer = (${payload.code}); if (typeof __signer !== 'function') throw new Error('signer_code must evaluate to a function');`, context, {timeout: payload.timeout_ms});
        const outcomes = [];
        for (const input of payload.inputs) {
            context.__input = input;
            try {
                const computed = await vm.runInContext('__signer(__input)', context, {timeout: payload.timeout_ms});
                // Catch per-sample serialization failures, including BigInt/cycles.
                outcomes.push(JSON.parse(JSON.stringify({computed})));
            } catch (error) { outcomes.push({error: String(error)}); }
        }
        process.stdout.write(JSON.stringify({outcomes}));
    } catch (error) {
        process.stdout.write(JSON.stringify({error: String(error)}));
    }
})();
