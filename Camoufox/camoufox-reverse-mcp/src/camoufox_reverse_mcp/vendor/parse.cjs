// Parser-only bridge. It never evaluates the supplied JavaScript.
'use strict';
const fs = require('node:fs');
const acorn = require('./acorn.cjs');
try {
    const input = JSON.parse(fs.readFileSync(0, 'utf8'));
    const options = {ecmaVersion: 'latest', ranges: true, locations: !!input.locations, allowHashBang: true};
    let tree;
    try { tree = acorn.parse(input.source, {...options, sourceType: 'script'}); }
    catch (scriptError) { tree = acorn.parse(input.source, {...options, sourceType: 'module'}); }
    let count = 0;
    const stack = [tree];
    while (stack.length) {
        const node = stack.pop();
        if (!node || typeof node !== 'object') continue;
        if (++count > 200000) throw new Error('AST node budget exceeded');
        for (const key of Object.keys(node)) {
            if (key === 'loc' || key === 'range') continue;
            const value = node[key];
            if (Array.isArray(value)) stack.push(...value);
            else if (value && typeof value === 'object') stack.push(value);
        }
    }
    const encoded = JSON.stringify({tree, parser: 'acorn-8.15.0'},
        (key, value) => typeof value === 'bigint' ? {__bigint_literal: String(value)} : value);
    if (Buffer.byteLength(encoded) > 32 * 1024 * 1024) throw new Error('AST output budget exceeded');
    process.stdout.write(encoded);
} catch (error) {
    process.stdout.write(JSON.stringify({error: String(error)}));
    process.exitCode = 1;
}
