/**
 * @license
 * Copyright 2026
 * SPDX-License-Identifier: Apache-2.0
 */

import assert from 'node:assert/strict';
import path from 'node:path';
import {test} from 'node:test';

import {
  DEFAULT_INSTALL_DIR,
  getBinaryPath,
  MCP_ROOT,
  resolveInstallDir,
  updateBrowserBinary,
} from '../src/cloakBinaryManager.js';

test('default Cloak install dir resolves to the MCP parent directory', () => {
  assert.equal(resolveInstallDir(), DEFAULT_INSTALL_DIR);
  assert.equal(resolveInstallDir('{mcpParent}'), DEFAULT_INSTALL_DIR);
  assert.equal(path.dirname(MCP_ROOT), DEFAULT_INSTALL_DIR);
});

test('default Cloak binary path is chrome.exe inside the install dir on Windows', () => {
  assert.equal(
    getBinaryPath(DEFAULT_INSTALL_DIR),
    path.join(DEFAULT_INSTALL_DIR, 'chrome.exe'),
  );
});

test('Cloak updater refuses to target the MCP directory itself', async () => {
  assert.throws(
    () => resolveInstallDir('{mcpRoot}'),
    /Refusing to install CloakBrowser inside the MCP directory/,
  );
  await assert.rejects(
    updateBrowserBinary({installDir: '{mcpRoot}', dryRun: true}),
    /Refusing to install CloakBrowser inside the MCP directory/,
  );
});
