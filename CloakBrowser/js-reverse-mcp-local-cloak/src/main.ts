/**
 * @license
 * Copyright 2025 Google LLC
 * SPDX-License-Identifier: Apache-2.0
 */

import './polyfill.js';

import * as fs from 'node:fs';
import * as path from 'node:path';

import {
  closeBrowser,
  ensureBrowserConnected,
  ensureBrowserLaunched,
  getRuntimeLaunchOverrides,
} from './browser.js';
import type {BrowserResult} from './browser.js';
import {parseArguments} from './cli.js';
import {features} from './features.js';
import {resolveProxyGeo, resolveLocalGeo} from './geoip.js';
import {resolveHumanConfig} from './human.js';
import {loadIssueDescriptions} from './issue-descriptions.js';
import {logger, saveLogsToFile} from './logger.js';
import {McpContext} from './McpContext.js';
import {McpResponse} from './McpResponse.js';
import {Mutex} from './Mutex.js';
import {
  McpServer,
  StdioServerTransport,
  type CallToolResult,
  SetLevelRequestSchema,
} from './third_party/index.js';
import {ToolCategory} from './tools/categories.js';
import * as cloakCompatTools from './tools/cloakCompat.js';
import * as consoleTools from './tools/console.js';
import * as debuggerTools from './tools/debugger.js';
import * as frameTools from './tools/frames.js';
import * as networkTools from './tools/network.js';
import * as pagesTools from './tools/pages.js';
import * as screenshotTools from './tools/screenshot.js';
import * as scriptTools from './tools/script.js';
import * as siteDataTools from './tools/siteData.js';
import type {ToolDefinition} from './tools/ToolDefinition.js';
import * as websocketTools from './tools/websocket.js';

// Read the version from package.json at runtime so it never drifts from the
// published package. Releases here are driven by `npm version` + a git tag, not
// release-please, so a hardcoded constant would go stale.
const VERSION = (
  JSON.parse(
    fs.readFileSync(
      path.join(import.meta.dirname, '../../package.json'),
      'utf8',
    ),
  ) as {version: string}
).version;

export const args = parseArguments(VERSION);

cloakCompatTools.setCloakConfig(args.cloak, args.cloakBinaryPath);
cloakCompatTools.setLaunchDefaults({
  headless: !!args.headless,
  cloak: !!args.cloak,
});

const logFile = args.logFile ? saveLogsToFile(args.logFile) : undefined;

logger(`Starting Chrome DevTools MCP Server v${VERSION}`);
const server = new McpServer(
  {
    name: 'js-reverse',
    title: 'JS Reverse Engineering MCP Server',
    description: `JavaScript reverse engineering and debugging via Chrome DevTools (v${VERSION}). Built on Patchright anti-detection engine — passes mainstream browser fingerprint checks (Zhihu, Google, etc.) out of the box.`,
    version: VERSION,
  },
  {capabilities: {logging: {}}},
);
server.server.setRequestHandler(SetLevelRequestSchema, () => {
  return {};
});

let context: McpContext | undefined;

// No JS-level init scripts — Patchright's protocol-layer stealth handles
// automation signal suppression. JS patches (Error.prepareStackTrace, screen
// property overrides, fake chrome.runtime) actually CAUSE detection because
// anti-bot systems check for Object.defineProperty tampering. Source-level
// fingerprint patches (canvas/WebGL/GPU) are opt-in via --cloak.

async function getContext(): Promise<McpContext> {
  let result: BrowserResult;
  if (args.browserUrl) {
    result = await ensureBrowserConnected({browserURL: args.browserUrl});
  } else {
    // --geoip: resolve timezone/locale from the proxy's exit IP before launch.
    let locale = args.locale;
    let timezone = args.timezone;
    if (args.geoip) {
      try {
        const geo = args.proxy
          ? await resolveProxyGeo(args.proxy)
          : await resolveLocalGeo();
        if (geo.timezone && !args.timezone) timezone = geo.timezone;
        if (geo.locale && !args.locale) locale = geo.locale;
        logger(
          `GeoIP resolved: ip=${geo.ip} country=${geo.country} locale=${locale} timezone=${timezone}`,
        );
      } catch (e) {
        logger('GeoIP lookup failed (proceeding without geo settings):', e);
      }
    }
    const overrides = getRuntimeLaunchOverrides();
    const cloak = overrides?.cloak ?? args.cloak;
    const headless = overrides?.headless ?? args.headless;
    const cloakBinaryPath = overrides?.cloak
      ? overrides.cloakBinaryPath
      : args.cloakBinaryPath;
    result = await ensureBrowserLaunched({
      isolated: args.isolated,
      logFile,
      cloak,
      cloakBinaryPath,
      fingerprintSeed: args.fingerprintSeed,
      proxy: args.proxy,
      locale,
      timezone,
      headless,
      blockWebRtc: args.blockWebRtc,
      blockImages: args.blockImages,
      windowWidth: args.windowWidth,
      windowHeight: args.windowHeight,
    });
  }

  if (!context || context.browserContext !== result.context) {
    context?.dispose();
    context = await McpContext.from(result.context, logger);
  }
  // Propagate humanize settings to compatibility tools layer.
  cloakCompatTools.setHumanizeConfig(
    args.humanize ?? false,
    resolveHumanConfig(
      (args.humanPreset ?? 'default') as 'default' | 'careful',
    ),
  );
  // Propagate CLI defaults so browser_binary_info / launch_browser can report
  // effective headless/cloak without importing main.ts.
  cloakCompatTools.setCloakConfig(args.cloak, args.cloakBinaryPath);
  cloakCompatTools.setLaunchDefaults({
    headless: !!args.headless,
    cloak: !!args.cloak,
  });
  return context;
}

const logDisclaimers = () => {
  console.error(
    `js-reverse-mcp exposes content of the browser instance to the MCP clients allowing them to inspect,
debug, and modify any data in the browser or DevTools.
Avoid sharing sensitive or personal information that you do not want to share with MCP clients.`,
  );
};

const toolMutex = new Mutex();
const DEFAULT_TOOL_TIMEOUT_MS = 35_000;
const SHUTDOWN_TIMEOUT_MS = 5_000;

function textOnlyResult(
  toolName: string,
  response: McpResponse,
): CallToolResult {
  return {
    content: [
      {
        type: 'text',
        text: [`# ${toolName} response`, ...response.responseLines].join('\n'),
      },
    ],
  };
}

function getErrorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function errorResult(text: string): CallToolResult {
  return {
    content: [
      {
        type: 'text',
        text,
      },
    ],
    isError: true,
  };
}

function withToolTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  toolName: string,
): Promise<T> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timeoutId = setTimeout(() => {
      reject(
        new Error(
          `Tool "${toolName}" timed out after ${timeoutMs}ms. If execution is paused at a breakpoint, call pause_or_resume and retry.`,
        ),
      );
    }, timeoutMs);
  });

  return Promise.race([promise, timeout]).finally(() => {
    if (timeoutId) {
      clearTimeout(timeoutId);
    }
  });
}

function registerTool(tool: ToolDefinition): void {
  server.registerTool(
    tool.name,
    {
      description: tool.description,
      inputSchema: tool.schema,
      annotations: tool.annotations,
    },
    async (params): Promise<CallToolResult> => {
      let guard: InstanceType<typeof Mutex.Guard>;
      try {
        guard = await toolMutex.acquire({timeoutMs: DEFAULT_TOOL_TIMEOUT_MS});
      } catch (error) {
        return errorResult(getErrorText(error));
      }

      try {
        return await withToolTimeout(
          (async () => {
            logger(
              `${tool.name} request: ${JSON.stringify(params, null, '  ')}`,
            );
            const requiresBrowserContext = tool.requiresBrowserContext ?? true;
            // launch_browser must apply headless/cloak overrides and close the
            // old browser BEFORE getContext(), otherwise getContext() would
            // first relaunch with CLI defaults (often headful) and flash a window.
            // After preflight close, getContext() relaunches with the new options
            // in this same tool call (status: relaunched).
            if (tool.name === 'launch_browser') {
              const p = params as {
                headless?: boolean;
                cloakBinaryPath?: string;
                humanize?: boolean;
              };
              await cloakCompatTools.preflightLaunchBrowser({
                headless: p.headless,
                cloakBinaryPath: p.cloakBinaryPath,
                humanize: p.humanize,
              });
            }
            const context = requiresBrowserContext
              ? await getContext()
              : undefined;
            logger(
              `${tool.name} context: ${context ? 'resolved' : 'not required'}`,
            );

            // Navigation and browser-state tools must operate in CDP silence
            // except for their own explicit protocol calls.
            // Anti-bot systems detect ANY CDP activity during page load,
            // including session creation from detectOpenDevToolsWindows().
            if (
              context &&
              tool.annotations.category !== ToolCategory.NAVIGATION &&
              tool.annotations.category !== ToolCategory.BROWSER_STATE
            ) {
              await context.ensureCollectorsInitialized();
              await context.detectOpenDevToolsWindows();
            }
            const response = new McpResponse();
            await tool.handler(
              {
                params,
              },
              response,
              context as never,
            );

            return {
              content: context
                ? await response.handle(tool.name, context)
                : textOnlyResult(tool.name, response).content,
            };
          })(),
          tool.timeoutMs ?? DEFAULT_TOOL_TIMEOUT_MS,
          tool.name,
        );
      } catch (err) {
        const errorText = getErrorText(err);
        logger(`${tool.name} error: ${errorText}`);
        return errorResult(errorText);
      } finally {
        guard.dispose();
      }
    },
  );
}

const tools = [
  ...Object.values(cloakCompatTools),
  ...Object.values(consoleTools),
  ...Object.values(debuggerTools),
  ...Object.values(frameTools),
  ...Object.values(networkTools),
  ...Object.values(pagesTools),
  ...Object.values(screenshotTools),
  ...Object.values(scriptTools),
  ...Object.values(siteDataTools),

  ...Object.values(websocketTools),
].filter((tool): tool is ToolDefinition => {
  return (
    typeof tool === 'object' &&
    tool !== null &&
    'name' in tool &&
    'handler' in tool &&
    'schema' in tool &&
    'annotations' in tool
  );
});

tools.sort((a, b) => {
  return a.name.localeCompare(b.name);
});

let shuttingDown = false;

function requestShutdown(reason: string, exitCode: number): void {
  void shutdown(reason, exitCode);
}

async function shutdown(reason: string, exitCode: number): Promise<void> {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  logger(`Shutdown requested: ${reason}`);

  await withShutdownTimeout(
    (async () => {
      context?.dispose();
      context = undefined;

      await closeBrowser(reason);

      await server.close().catch(error => {
        logger('Failed to close MCP server during shutdown', error);
      });

      await closeLogFile();
    })(),
    reason,
  );

  process.exit(exitCode);
}

async function withShutdownTimeout(
  promise: Promise<void>,
  reason: string,
): Promise<void> {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<void>(resolve => {
    timeoutId = setTimeout(() => {
      logger(
        `Shutdown cleanup timed out after ${SHUTDOWN_TIMEOUT_MS}ms: ${reason}`,
      );
      resolve();
    }, SHUTDOWN_TIMEOUT_MS);
  });

  await Promise.race([promise, timeout]);
  if (timeoutId) {
    clearTimeout(timeoutId);
  }
}

function closeLogFile(): Promise<void> {
  if (!logFile || logFile.destroyed || logFile.writableEnded) {
    return Promise.resolve();
  }

  return new Promise(resolve => {
    logFile.end(resolve);
  });
}

function getStreamErrorCode(error: unknown): string | undefined {
  return typeof error === 'object' && error !== null && 'code' in error
    ? String(error.code)
    : undefined;
}

process.on('SIGINT', () => requestShutdown('SIGINT', 130));
process.on('SIGTERM', () => requestShutdown('SIGTERM', 143));
process.on('SIGHUP', () => requestShutdown('SIGHUP', 129));
process.on('disconnect', () => requestShutdown('process disconnect', 0));

process.stdin.on('end', () => requestShutdown('stdin end', 0));
process.stdin.on('close', () => requestShutdown('stdin close', 0));
process.stdin.on('error', error => {
  requestShutdown(`stdin error: ${getErrorText(error)}`, 1);
});

process.stdout.on('error', error => {
  const code = getStreamErrorCode(error);
  requestShutdown(
    code === 'EPIPE' || code === 'ECONNRESET'
      ? `stdout ${code}`
      : `stdout error: ${getErrorText(error)}`,
    code === 'EPIPE' || code === 'ECONNRESET' ? 0 : 1,
  );
});

for (const tool of tools) {
  registerTool(tool);
}

if (features.issues) {
  await loadIssueDescriptions();
}

const transport = new StdioServerTransport();
await server.connect(transport);
logger('Chrome DevTools MCP Server connected');
logDisclaimers();
