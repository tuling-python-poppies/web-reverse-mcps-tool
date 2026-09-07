/**
 * @license
 * Copyright 2026
 * SPDX-License-Identifier: Apache-2.0
 */

import assert from 'node:assert/strict';
import {test} from 'node:test';

import {
  getLastLaunchSnapshot,
  getRuntimeLaunchOverrides,
  patchRuntimeLaunchOverrides,
  recordLastLaunchSnapshot,
  setRuntimeLaunchOverrides,
} from '../../src/browser.js';
import {rewriteInstrumentedSource} from '../../src/tools/cloakCompat.js';
import {boolParam, intParam, stringParam} from '../../src/tools/paramHelpers.js';

function resetLaunchOverrides(): void {
  setRuntimeLaunchOverrides(undefined);
}

test('patchRuntimeLaunchOverrides merges headless without wiping cloak', () => {
  resetLaunchOverrides();
  patchRuntimeLaunchOverrides({
    cloak: true,
    cloakBinaryPath: 'D:\\\\cloak\\\\chrome.exe',
  });
  patchRuntimeLaunchOverrides({headless: true});
  assert.deepEqual(getRuntimeLaunchOverrides(), {
    cloak: true,
    cloakBinaryPath: 'D:\\\\cloak\\\\chrome.exe',
    headless: true,
  });
  resetLaunchOverrides();
});

test('patchRuntimeLaunchOverrides preserves headless when switching back to Chrome', () => {
  resetLaunchOverrides();
  patchRuntimeLaunchOverrides({
    cloak: true,
    cloakBinaryPath: 'D:\\\\cloak\\\\chrome.exe',
    headless: true,
  });
  patchRuntimeLaunchOverrides({
    cloak: false,
    cloakBinaryPath: undefined,
  });
  assert.deepEqual(getRuntimeLaunchOverrides(), {
    cloak: false,
    headless: true,
  });
  resetLaunchOverrides();
});

test('patchRuntimeLaunchOverrides can clear all fields back to undefined', () => {
  resetLaunchOverrides();
  patchRuntimeLaunchOverrides({headless: false});
  assert.deepEqual(getRuntimeLaunchOverrides(), {headless: false});
  patchRuntimeLaunchOverrides({headless: undefined});
  assert.equal(getRuntimeLaunchOverrides(), undefined);
  resetLaunchOverrides();
});

test('setRuntimeLaunchOverrides(undefined) clears runtime state (close_browser contract)', () => {
  resetLaunchOverrides();
  patchRuntimeLaunchOverrides({
    cloak: true,
    cloakBinaryPath: 'x',
    headless: true,
  });
  setRuntimeLaunchOverrides(undefined);
  assert.equal(getRuntimeLaunchOverrides(), undefined);
});

test('recordLastLaunchSnapshot stores effective headless/cloak', () => {
  recordLastLaunchSnapshot({
    cloak: false,
    headless: true,
  });
  assert.deepEqual(getLastLaunchSnapshot(), {
    cloak: false,
    headless: true,
  });
});

test('preflightLaunchBrowser marks headless switch as relaunched', async () => {
  const {
    preflightLaunchBrowser,
    takeLaunchBrowserPreflight,
    setLaunchDefaults,
  } = await import('../../src/tools/cloakCompat.js');
  setLaunchDefaults({headless: false, cloak: false});
  resetLaunchOverrides();
  recordLastLaunchSnapshot({cloak: false, headless: false});
  const result = await preflightLaunchBrowser({headless: true});
  assert.equal(result.status, 'relaunched');
  assert.equal(result.effective_headless, true);
  assert.deepEqual(getRuntimeLaunchOverrides(), {headless: true});
  assert.equal(takeLaunchBrowserPreflight()?.status, 'relaunched');
  assert.equal(takeLaunchBrowserPreflight(), undefined);
  resetLaunchOverrides();
});

test('preflightLaunchBrowser no-ops when headless already matches', async () => {
  const {
    preflightLaunchBrowser,
    setLaunchDefaults,
  } = await import('../../src/tools/cloakCompat.js');
  setLaunchDefaults({headless: false, cloak: false});
  resetLaunchOverrides();
  recordLastLaunchSnapshot({cloak: false, headless: true});
  patchRuntimeLaunchOverrides({headless: true});
  const result = await preflightLaunchBrowser({headless: true});
  assert.equal(result.status, 'already_running');
  assert.equal(result.effective_headless, true);
  resetLaunchOverrides();
});

test('AST instrumentation rewrites safe member reads only', () => {
  const result = rewriteInstrumentedSource(
    `const ua = navigator.userAgent;
     navigator.userAgent = 'patched';
     navigator.getBattery();
     const literal = 'navigator.userAgent';`,
    {
      tag: 'probe',
      mode: 'ast',
      rewriteMemberAccess: true,
      maxRewrites: 20,
      filterPropertyNames: ['userAgent'],
      filterObjectNames: ['navigator'],
      fallbackOnError: false,
    },
  );

  assert.equal(result.mode, 'ast');
  assert.equal(result.edits, 1);
  assert.match(
    result.source,
    /__mcp_tap_get\("probe","navigator\.userAgent",navigator\.userAgent\)/,
  );
  assert.match(result.source, /navigator\.userAgent=["']patched["']/);
  assert.match(result.source, /navigator\.getBattery\(\)/);
  assert.match(result.source, /["']navigator\.userAgent["']/);
});

test('AST instrumentation falls back to scanner on parse errors', () => {
  const result = rewriteInstrumentedSource(
    'const broken = ; navigator.userAgent;',
    {
      tag: 'probe',
      mode: 'ast',
      rewriteMemberAccess: true,
      maxRewrites: 20,
      filterPropertyNames: ['userAgent'],
      filterObjectNames: ['navigator'],
      fallbackOnError: true,
    },
  );

  assert.equal(result.mode, 'regex');
  assert.equal(result.edits, 1);
  assert.match(result.fallbackReason ?? '', /Unexpected token/);
});

// boolParam: OpenCode-style string boolean coercion
test('boolParam coerces string "true"/"false" to boolean', () => {
  const schema = boolParam();
  assert.equal(schema.parse('true'), true);
  assert.equal(schema.parse('false'), false);
  assert.equal(schema.parse('TRUE'), true);
  assert.equal(schema.parse('  false  '), false);
});

test('boolParam passes through real booleans', () => {
  const schema = boolParam().optional().default(false);
  assert.equal(schema.parse(true), true);
  assert.equal(schema.parse(false), false);
  assert.equal(schema.parse(undefined), false);
});

test('boolParam rejects invalid strings', () => {
  const schema = boolParam();
  assert.throws(() => schema.parse('yes'), /Expected boolean/);
  assert.throws(() => schema.parse(1), /Expected boolean/);
});

// intParam: 0 → undefined coercion
test('intParam coerces 0 to undefined', () => {
  const schema = intParam().optional();
  assert.equal(schema.parse(0), undefined);
  assert.equal(schema.parse(12), 12);
  assert.equal(schema.parse('7'), 7);
  assert.equal(schema.parse('0'), undefined);
});

// stringParam: empty string → undefined coercion
test('stringParam coerces empty/blank to undefined', () => {
  const schema = stringParam().optional();
  assert.equal(schema.parse(''), undefined);
  assert.equal(schema.parse('  '), undefined);
  assert.equal(schema.parse('hello'), 'hello');
});
