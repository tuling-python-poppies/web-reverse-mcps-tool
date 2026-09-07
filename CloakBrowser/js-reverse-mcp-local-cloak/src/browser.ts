/**
 * @license
 * Copyright 2025 Google LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import type fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

import {setupCloak} from './cloak.js';
import {installConsoleBridge} from './consoleBridge.js';
import {logger} from './logger.js';
import type {Browser, BrowserContext} from './third_party/index.js';
import {chromium} from './third_party/index.js';

type BrowserCloseMode = 'connected-cdp' | 'launched' | 'persistent-context';

export interface BrowserResult {
  browser: Browser | undefined;
  context: BrowserContext;
  closeMode: BrowserCloseMode;
}

let browserResult: BrowserResult | undefined;

// Runtime launch option overrides set by tools (e.g. launch_browser with
// cloakBinaryPath / headless). These take precedence over CLI args and allow
// switching engine and headless mode at runtime without restarting MCP.
export interface RuntimeLaunchOverrides {
  /** When set, forces cloak on/off. When unset, CLI --cloak wins. */
  cloak?: boolean;
  cloakBinaryPath?: string;
  /** When set, forces headless on/off. When unset, CLI --headless wins. */
  headless?: boolean;
}

/** Effective options after the last successful launched browser start. */
export interface LastLaunchSnapshot {
  cloak: boolean;
  headless: boolean;
  cloakBinaryPath?: string;
}

let runtimeOverrides: RuntimeLaunchOverrides | undefined;
let lastLaunchSnapshot: LastLaunchSnapshot | undefined;

export function setRuntimeLaunchOverrides(
  overrides: RuntimeLaunchOverrides | undefined,
): void {
  runtimeOverrides = overrides;
}

/** Field-level merge so switching headless alone does not wipe cloak state. */
export function patchRuntimeLaunchOverrides(
  patch: RuntimeLaunchOverrides,
): RuntimeLaunchOverrides {
  const next: RuntimeLaunchOverrides = {...runtimeOverrides};
  if (Object.prototype.hasOwnProperty.call(patch, 'cloak')) {
    if (patch.cloak === undefined) {
      delete next.cloak;
    } else {
      next.cloak = patch.cloak;
    }
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'cloakBinaryPath')) {
    if (patch.cloakBinaryPath === undefined) {
      delete next.cloakBinaryPath;
    } else {
      next.cloakBinaryPath = patch.cloakBinaryPath;
    }
  }
  if (Object.prototype.hasOwnProperty.call(patch, 'headless')) {
    if (patch.headless === undefined) {
      delete next.headless;
    } else {
      next.headless = patch.headless;
    }
  }
  // Drop empty override object so CLI defaults apply cleanly.
  if (
    next.cloak === undefined &&
    next.cloakBinaryPath === undefined &&
    next.headless === undefined
  ) {
    runtimeOverrides = undefined;
  } else {
    runtimeOverrides = next;
  }
  return runtimeOverrides ?? {};
}

export function getRuntimeLaunchOverrides():
  | RuntimeLaunchOverrides
  | undefined {
  return runtimeOverrides;
}

export function getLastLaunchSnapshot(): LastLaunchSnapshot | undefined {
  return lastLaunchSnapshot;
}

export function recordLastLaunchSnapshot(snapshot: LastLaunchSnapshot): void {
  lastLaunchSnapshot = snapshot;
}

export function clearLastLaunchSnapshot(): void {
  lastLaunchSnapshot = undefined;
}

export function isBrowserRunning(): boolean {
  return !!browserResult;
}

const BROWSER_OCCUPIED_MESSAGE =
  'The MCP browser is currently occupied by another session. Ask the user to close the other MCP/browser debugging window, or start a separate session with --isolated or a different --browserUrl.';

// Persistent user data directories.
//
// IMPORTANT: cloak and non-cloak profiles MUST be physically isolated. They
// use different Chromium binaries with different feature sets — mixing state
// (extensions, shader cache, service workers) across them causes startup
// races and broken sessions. Pick the directory based on whether --cloak is
// set; never share.
//
// Keep js-reverse-mcp profiles local to this MCP checkout so they never lock
// chrome-devtools-mcp's default profile.
const MCP_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
);
const DEFAULT_USER_DATA_DIR = path.join(MCP_ROOT, 'chrome-reverse-profile');
const DEFAULT_CLOAK_DATA_DIR = path.join(MCP_ROOT, 'cloak-reverse-profile');

export async function ensureBrowserConnected(options: {
  browserURL?: string;
}): Promise<BrowserResult> {
  if (browserResult) {
    return browserResult;
  }

  if (!options.browserURL) {
    throw new Error('browserURL must be provided');
  }

  // Resolve the WebSocket debugger URL from the CDP HTTP endpoint.
  const url = new URL('/json/version', options.browserURL);
  const res = await fetch(url.toString());
  const json = (await res.json()) as {webSocketDebuggerUrl?: string};
  const endpoint = json.webSocketDebuggerUrl;
  if (!endpoint) {
    throw new Error(
      `No webSocketDebuggerUrl in CDP /json/version response from ${options.browserURL}. ` +
        'Make sure the browser was started with --remote-debugging-port.',
    );
  }

  logger('Connecting Patchright via CDP to', endpoint);
  let browser: Browser;
  try {
    browser = await chromium.connectOverCDP(endpoint);
  } catch (error) {
    if (isBrowserOccupiedError(error)) {
      throw new Error(
        `${BROWSER_OCCUPIED_MESSAGE} The CDP endpoint ${options.browserURL} appears to be in use.`,
        {cause: error},
      );
    }
    throw error;
  }
  logger('Connected Patchright');

  const context = browser.contexts()[0];
  if (!context) {
    throw new Error('No browser context found after connecting');
  }

  browserResult = {browser, context, closeMode: 'connected-cdp'};

  // Clear cached result when browser disconnects so we can reconnect.
  browser.on('disconnected', () => {
    logger('Browser disconnected, clearing cached browser result');
    browserResult = undefined;
  });

  return browserResult;
}

interface McpLaunchOptions {
  userDataDir?: string;
  isolated: boolean;
  logFile?: fs.WriteStream;
  cloak?: boolean;
  cloakBinaryPath?: string;
  fingerprintSeed?: number;
  proxy?: string;
  locale?: string;
  timezone?: string;
  headless?: boolean;
  blockWebRtc?: boolean;
  blockImages?: boolean;
  windowWidth?: number;
  windowHeight?: number;
}

/** Block image loading via route interception (mirrors Python's block_images).
 *  Uses resourceType() for accurate detection (catches extensionless image
 *  endpoints and query-string URLs). route.fallback() passes non-image
 *  requests to the next registered handler so instrumentation routes are
 *  not bypassed.
 */
async function blockImages(context: BrowserContext): Promise<void> {
  await context.route('**/*', route => {
    if (route.request().resourceType() === 'image') {
      return route.abort();
    }
    return route.fallback();
  });
}

export async function launch(
  options: McpLaunchOptions,
): Promise<BrowserResult> {
  const {isolated} = options;

  // --cloak: resolve the CloakBrowser binary and fingerprint seed before
  // anything else. For persistent profiles the seed is persisted there so the
  // virtual identity is stable across launches; --isolated gets a fresh seed.
  //
  // Cloak and non-cloak modes use SEPARATE persistent profile directories —
  // they're different browsers with different feature sets, sharing profile
  // state breaks both.
  const persistentProfileDir = isolated
    ? undefined
    : (options.userDataDir ??
      (options.cloak ? DEFAULT_CLOAK_DATA_DIR : DEFAULT_USER_DATA_DIR));
  const cloakSetup = options.cloak
    ? await setupCloak(
        persistentProfileDir,
        options.cloakBinaryPath,
        options.fingerprintSeed,
      )
    : null;
  const executablePath = cloakSetup?.executablePath;

  const args: string[] = [
    '--test-type',
    '--hide-crash-restore-bubble',
    ...(options.windowWidth && options.windowHeight
      ? [`--window-size=${options.windowWidth},${options.windowHeight}`]
      : []),
    ...(cloakSetup?.args ?? []),
    // Disable WebRTC to prevent IP leaks (mirrors Python's block_webrtc option).
    ...(options.blockWebRtc
      ? ['--enforce-webrtc-ip-handling-policy=disable-non-proxied-udp']
      : []),
  ];

  // System Chrome stable when not using cloak; cloak provides its own binary.
  const channel = executablePath ? undefined : 'chrome';

  // Build context options. viewport:null exposes real OS dimensions (avoids
  // the 1280x720 fake-viewport bot signal). New options mirror Python version.
  const contextOptions = {
    viewport: null as null,
    ignoreHTTPSErrors: true,
    ...(options.proxy ? {proxy: {server: options.proxy}} : {}),
    ...(options.locale ? {locale: options.locale} : {}),
    ...(options.timezone ? {timezoneId: options.timezone} : {}),
  };

  // --isolated mode: launch() + newContext() for clean isolated context.
  // Creates an incognito-like context with no persisted state.
  if (isolated) {
    const browser = await chromium.launch({
      channel,
      executablePath,
      headless: options.headless ?? false,
      chromiumSandbox: true,
      args,
    });

    const context = await browser.newContext(contextOptions);
    await installConsoleBridge(context);
    if (options.blockImages) await blockImages(context);
    if (context.pages().length === 0) await context.newPage();
    return {browser, context, closeMode: 'launched'};
  }

  // Default: launchPersistentContext for full state persistence
  // (cookies, IndexedDB, Cache Storage, Service Workers, localStorage).
  // persistentProfileDir is non-undefined here because the isolated branch
  // returned above; assert via the non-null assertion to satisfy the type.
  const userDataDir = persistentProfileDir!;
  try {
    const context = await chromium.launchPersistentContext(userDataDir, {
      channel,
      executablePath,
      headless: options.headless ?? false,
      chromiumSandbox: true,
      args,
      ...contextOptions,
    });

    await installConsoleBridge(context);
    if (options.blockImages) await blockImages(context);
    return {browser: undefined, context, closeMode: 'persistent-context'};
  } catch (error) {
    if (isBrowserOccupiedError(error)) {
      throw new Error(
        `${BROWSER_OCCUPIED_MESSAGE} The persistent browser profile is already in use: ${userDataDir}.`,
        {cause: error},
      );
    }
    throw error;
  }
}

export async function ensureBrowserLaunched(
  options: McpLaunchOptions,
): Promise<BrowserResult> {
  if (browserResult) {
    return browserResult;
  }
  browserResult = await launch(options);
  recordLastLaunchSnapshot({
    cloak: !!options.cloak,
    headless: options.headless ?? false,
    cloakBinaryPath: options.cloakBinaryPath,
  });

  // Clear cached result when browser is manually closed so we can relaunch.
  const {browser, context} = browserResult;
  if (browser) {
    browser.on('disconnected', () => {
      logger('Browser disconnected, clearing cached browser result');
      browserResult = undefined;
    });
  } else {
    // Persistent context mode (no browser object) — listen on context.
    context.on('close', () => {
      logger('Browser context closed, clearing cached browser result');
      browserResult = undefined;
    });
  }

  return browserResult;
}

function isBrowserOccupiedError(error: unknown): boolean {
  const message = (
    error instanceof Error ? error.message : String(error)
  ).toLowerCase();
  return [
    'the browser is already running',
    'processsingleton',
    'another cdp client already connected',
    'already connected',
    'already attached',
    'already in use',
  ].some(fragment => message.includes(fragment));
}

export async function closeBrowser(reason: string): Promise<void> {
  const result = browserResult;
  if (!result) {
    return;
  }
  browserResult = undefined;
  // Snapshot is intentionally kept until the next successful launch so
  // launch_browser can compare requested vs previous effective mode after close.

  const closeReason = `MCP shutdown: ${reason}`;
  logger('Closing browser due to', closeReason);

  if (result.closeMode === 'connected-cdp' && result.browser) {
    await closeConnectedCdpBrowser(result.browser, closeReason);
    return;
  }

  if (result.closeMode === 'launched' && result.browser) {
    await result.context.close({reason: closeReason}).catch(error => {
      logger('Failed to close browser context during shutdown', error);
    });
    await result.browser.close({reason: closeReason}).catch(error => {
      logger('Failed to close browser during shutdown', error);
    });
    return;
  }

  await result.context.close({reason: closeReason}).catch(error => {
    logger('Failed to close persistent browser context during shutdown', error);
  });
}

async function closeConnectedCdpBrowser(
  browser: Browser,
  reason: string,
): Promise<void> {
  if (browser.isConnected()) {
    try {
      const session = await browser.newBrowserCDPSession();
      await session.send('Browser.close');
    } catch (error) {
      logger('Failed to send Browser.close over CDP during shutdown', error);
    }
  }

  await browser.close({reason}).catch(error => {
    logger(
      'Failed to close connected browser transport during shutdown',
      error,
    );
  });
}
