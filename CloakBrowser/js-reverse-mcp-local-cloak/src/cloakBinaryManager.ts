/**
 * @license
 * Copyright 2026
 * SPDX-License-Identifier: Apache-2.0
 */

import {execFile, execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {once} from 'node:events';
import fsSync, {createReadStream, createWriteStream} from 'node:fs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const execFileAsync = promisify(execFile);

const DOWNLOAD_TIMEOUT_MS = 15 * 60_000;
const CLOAKBROWSER_DOWNLOAD_BASE_URL = 'https://cloakbrowser.dev';
const GITHUB_API_URL =
  'https://api.github.com/repos/CloakHQ/CloakBrowser/releases';

export const MCP_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
);
export const DEFAULT_INSTALL_DIR = path.dirname(MCP_ROOT);
export const BROWSER_CONNECTION_CONFIG_FILE = path.join(
  MCP_ROOT,
  'browser-connection',
  'config.json',
);

const PROTECTED_NAMES = [path.basename(MCP_ROOT), 'profiles'];

interface BrowserConnectionConfig {
  cloakBinaryPath?: string;
  cloakInstallDir?: string;
  cloakBinaryName?: string;
}

interface ReleaseAsset {
  name: string;
  browser_download_url: string;
}

interface GithubRelease {
  tag_name: string;
  draft?: boolean;
  assets?: ReleaseAsset[];
}

export interface CloakReleaseInfo {
  version: string;
  tagName: string;
  archiveName: string;
  downloadUrl: string;
  fallbackDownloadUrl: string;
  checksumUrl: string | null;
  fallbackChecksumUrl: string | null;
}

export interface InstalledBinaryInfo {
  installed: boolean;
  installDir: string;
  binaryPath: string;
  version: string | null;
  binaryProductVersion: string | null;
  markerVersion: string | null;
  protectedPaths: string[];
  configFile: string;
}

export interface CheckBrowserUpdateResult extends InstalledBinaryInfo {
  supported: boolean;
  latestVersion: string | null;
  updateAvailable: boolean;
  release: CloakReleaseInfo | null;
  note?: string;
}

export interface UpdateBrowserBinaryResult extends CheckBrowserUpdateResult {
  status: 'dry_run' | 'updated' | 'skipped';
  backupDir: string | null;
  stagingDir: string | null;
  downloadedArchive: string | null;
  removedDownloadedArchive: string | null;
  archiveCleanupError: string | null;
  copiedFiles: number;
  backedUpFiles: number;
  plannedDownloadUrl: string | null;
}

function normalizeForCompare(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
}

function isSameOrInside(child: string, parent: string): boolean {
  const normalizedChild = normalizeForCompare(child);
  const normalizedParent = normalizeForCompare(parent);
  return (
    normalizedChild === normalizedParent ||
    normalizedChild.startsWith(`${normalizedParent}${path.sep}`)
  );
}

function expandPortablePath(value: string, baseDir: string): string {
  const expanded = value
    .replaceAll('{mcpRoot}', MCP_ROOT)
    .replaceAll('{mcpParent}', DEFAULT_INSTALL_DIR)
    .replace(/^~(?=$|[\\/])/, os.homedir());
  return path.isAbsolute(expanded)
    ? path.resolve(expanded)
    : path.resolve(baseDir, expanded);
}

export function resolveInstallDir(value?: string | null): string {
  const installDir = value?.trim()
    ? expandPortablePath(value.trim(), DEFAULT_INSTALL_DIR)
    : DEFAULT_INSTALL_DIR;
  assertInstallDirSafe(installDir);
  return installDir;
}

function assertInstallDirSafe(installDir: string): void {
  const resolved = path.resolve(installDir);
  if (path.parse(resolved).root === resolved) {
    throw new Error(
      `Refusing to use filesystem root as install_dir: ${resolved}`,
    );
  }
  if (isSameOrInside(resolved, MCP_ROOT)) {
    throw new Error(
      `Refusing to install CloakBrowser inside the MCP directory: ${resolved}`,
    );
  }
}

export function getDefaultBinaryName(): string {
  if (process.platform === 'win32') return 'chrome.exe';
  if (process.platform === 'darwin') {
    return path.join('Chromium.app', 'Contents', 'MacOS', 'Chromium');
  }
  return 'chrome';
}

export function getBinaryPath(installDir: string, binaryName?: string): string {
  return path.resolve(installDir, binaryName ?? getDefaultBinaryName());
}

export async function readBrowserConnectionConfig(): Promise<BrowserConnectionConfig> {
  try {
    const raw = await fs.readFile(BROWSER_CONNECTION_CONFIG_FILE, 'utf8');
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    return {
      cloakBinaryPath:
        typeof parsed.cloakBinaryPath === 'string'
          ? parsed.cloakBinaryPath
          : undefined,
      cloakInstallDir:
        typeof parsed.cloakInstallDir === 'string'
          ? parsed.cloakInstallDir
          : undefined,
      cloakBinaryName:
        typeof parsed.cloakBinaryName === 'string'
          ? parsed.cloakBinaryName
          : undefined,
    };
  } catch {
    return {};
  }
}

export async function writePortableBrowserConnectionConfig(): Promise<void> {
  let existing: Record<string, unknown> = {};
  try {
    existing = JSON.parse(
      await fs.readFile(BROWSER_CONNECTION_CONFIG_FILE, 'utf8'),
    ) as Record<string, unknown>;
  } catch {
    existing = {};
  }

  delete existing.cloakBinaryPath;
  existing.cloakInstallDir = '{mcpParent}';
  existing.cloakBinaryName = getDefaultBinaryName();

  await fs.mkdir(path.dirname(BROWSER_CONNECTION_CONFIG_FILE), {
    recursive: true,
  });
  await fs.writeFile(
    BROWSER_CONNECTION_CONFIG_FILE,
    `${JSON.stringify(existing, null, 2)}\n`,
    'utf8',
  );
}

export async function resolveConfiguredCloakBinaryPath(
  binaryPath?: string,
): Promise<string> {
  if (binaryPath?.trim()) {
    return expandPortablePath(binaryPath.trim(), DEFAULT_INSTALL_DIR);
  }

  const config = await readBrowserConnectionConfig();
  if (config.cloakBinaryPath?.trim()) {
    return expandPortablePath(
      config.cloakBinaryPath.trim(),
      DEFAULT_INSTALL_DIR,
    );
  }

  const installDir = resolveInstallDir(config.cloakInstallDir);
  return getBinaryPath(installDir, config.cloakBinaryName);
}

function getVersionMarkerPath(installDir: string): string {
  return path.join(installDir, '.cloakbrowser-version');
}

async function readVersionMarker(installDir: string): Promise<string | null> {
  try {
    const value = (await fs.readFile(getVersionMarkerPath(installDir), 'utf8'))
      .trim()
      .replace(/^chromium-v/, '')
      .replace(/-pro$/, '');
    return value || null;
  } catch {
    return null;
  }
}

async function writeVersionMarker(
  installDir: string,
  version: string,
): Promise<void> {
  await fs.writeFile(getVersionMarkerPath(installDir), `${version}\n`, 'utf8');
}

async function readBinaryProductVersion(
  binaryPath: string,
): Promise<string | null> {
  if (!fsSync.existsSync(binaryPath)) return null;
  try {
    if (process.platform === 'win32') {
      const script =
        '$p=$env:JS_REVERSE_MCP_BINARY_PATH; ' +
        '$v=(Get-Item -LiteralPath $p).VersionInfo; ' +
        'if ($v.ProductVersion) { $v.ProductVersion } else { $v.FileVersion }';
      const {stdout} = await execFileAsync(
        'powershell',
        ['-NoProfile', '-Command', script],
        {
          timeout: 10_000,
          env: {...process.env, JS_REVERSE_MCP_BINARY_PATH: binaryPath},
        },
      );
      return extractVersion(String(stdout));
    }

    const {stdout} = await execFileAsync(binaryPath, ['--version'], {
      timeout: 10_000,
    });
    return extractVersion(String(stdout));
  } catch {
    return null;
  }
}

function extractVersion(value: string): string | null {
  return value.match(/\d+\.\d+\.\d+\.\d+(?:\.\d+)?/)?.[0] ?? null;
}

export async function getInstalledBinaryInfo(options?: {
  installDir?: string | null;
  binaryPath?: string;
}): Promise<InstalledBinaryInfo> {
  const installDir = resolveInstallDir(options?.installDir);
  const binaryPath = options?.binaryPath?.trim()
    ? expandPortablePath(options.binaryPath.trim(), DEFAULT_INSTALL_DIR)
    : getBinaryPath(installDir);
  const markerVersion = await readVersionMarker(installDir);
  const binaryProductVersion = await readBinaryProductVersion(binaryPath);
  return {
    installed: fsSync.existsSync(binaryPath),
    installDir,
    binaryPath,
    version: markerVersion ?? binaryProductVersion,
    binaryProductVersion,
    markerVersion,
    protectedPaths: PROTECTED_NAMES,
    configFile: BROWSER_CONNECTION_CONFIG_FILE,
  };
}

function getArchiveName(): string {
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    throw new Error(
      `CloakBrowser parent-directory updater currently supports win32-x64 only; got ${process.platform}-${process.arch}`,
    );
  }
  return 'cloakbrowser-windows-x64.zip';
}

function parseReleaseVersion(tagName: string): string | null {
  return tagName.match(/^chromium-v(.+?)(?:-pro)?$/)?.[1] ?? null;
}

async function fetchGithubReleases(): Promise<GithubRelease[]> {
  const response = await fetch(`${GITHUB_API_URL}?per_page=30`, {
    headers: {'user-agent': 'js-reverse-mcp'},
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    throw new Error(
      `Failed to fetch CloakBrowser releases: HTTP ${response.status} ${response.statusText}`,
    );
  }
  return (await response.json()) as GithubRelease[];
}

export async function resolveRelease(
  requestedVersion?: string | null,
): Promise<CloakReleaseInfo> {
  const archiveName = getArchiveName();
  const normalizedRequested = requestedVersion
    ?.trim()
    .replace(/^chromium-v/, '')
    .replace(/-pro$/, '');
  const releases = await fetchGithubReleases();

  for (const release of releases) {
    if (release.draft) continue;
    const version = parseReleaseVersion(release.tag_name);
    if (!version) continue;
    if (normalizedRequested && version !== normalizedRequested) continue;

    const assets = release.assets ?? [];
    const archive = assets.find(asset => asset.name === archiveName);
    if (!archive) {
      if (normalizedRequested) {
        throw new Error(
          `CloakBrowser release ${release.tag_name} does not include ${archiveName}`,
        );
      }
      continue;
    }
    const checksum = assets.find(asset => asset.name === 'SHA256SUMS');
    return {
      version,
      tagName: release.tag_name,
      archiveName,
      downloadUrl: `${CLOAKBROWSER_DOWNLOAD_BASE_URL}/chromium-v${version}/${archiveName}`,
      fallbackDownloadUrl: archive.browser_download_url,
      checksumUrl: `${CLOAKBROWSER_DOWNLOAD_BASE_URL}/chromium-v${version}/SHA256SUMS`,
      fallbackChecksumUrl: checksum?.browser_download_url ?? null,
    };
  }

  throw new Error(
    normalizedRequested
      ? `No downloadable CloakBrowser release found for ${normalizedRequested}`
      : `No downloadable CloakBrowser release found for ${archiveName}`,
  );
}

function parseVersion(value: string | null): number[] {
  if (!value) return [];
  return value.split('.').map(part => Number.parseInt(part, 10) || 0);
}

function versionNewer(
  candidate: string | null,
  current: string | null,
): boolean {
  if (!candidate) return false;
  if (!current) return true;
  const a = parseVersion(candidate);
  const b = parseVersion(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const av = a[i] ?? 0;
    const bv = b[i] ?? 0;
    if (av > bv) return true;
    if (av < bv) return false;
  }
  return false;
}

export async function checkBrowserUpdate(options?: {
  installDir?: string | null;
  version?: string | null;
}): Promise<CheckBrowserUpdateResult> {
  const installed = await getInstalledBinaryInfo({
    installDir: options?.installDir,
  });
  if (process.platform !== 'win32' || process.arch !== 'x64') {
    return {
      ...installed,
      supported: false,
      latestVersion: null,
      updateAvailable: false,
      release: null,
      note: 'Parent-directory CloakBrowser updater currently supports win32-x64 only.',
    };
  }

  const release = await resolveRelease(options?.version);
  return {
    ...installed,
    supported: true,
    latestVersion: release.version,
    updateAvailable: versionNewer(release.version, installed.version),
    release,
  };
}

async function downloadFile(url: string, dest: string): Promise<void> {
  const errors: string[] = [];
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await fs.rm(dest, {force: true});
      await downloadFileWithFetch(url, dest);
      return;
    } catch (error) {
      errors.push(
        `fetch attempt ${attempt}: ${error instanceof Error ? error.message : String(error)}`,
      );
      await fs.rm(dest, {force: true}).catch(() => undefined);
      if (attempt < 3) await delay(5_000 * attempt);
    }
  }

  try {
    await fs.rm(dest, {force: true});
    await downloadFileWithCurl(url, dest);
    return;
  } catch (error) {
    errors.push(
      `curl fallback: ${error instanceof Error ? error.message : String(error)}`,
    );
    await fs.rm(dest, {force: true}).catch(() => undefined);
  }

  throw new Error(`Download failed after retries:\n${errors.join('\n')}`);
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function downloadFileWithFetch(url: string, dest: string): Promise<void> {
  const response = await fetch(url, {
    redirect: 'follow',
    signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS),
  });
  if (!response.ok || !response.body) {
    throw new Error(
      `Download failed: HTTP ${response.status} ${response.statusText}`,
    );
  }

  const stream = createWriteStream(dest);
  const reader = response.body.getReader();
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      if (!stream.write(Buffer.from(value))) {
        await once(stream, 'drain');
      }
    }
    await new Promise<void>((resolve, reject) => {
      stream.once('finish', resolve);
      stream.once('error', reject);
      stream.end();
    });
  } catch (error) {
    stream.destroy();
    throw error;
  }
}

async function downloadFileWithCurl(url: string, dest: string): Promise<void> {
  await execFileAsync(
    'curl',
    [
      '--fail',
      '--location',
      '--silent',
      '--show-error',
      '--connect-timeout',
      '60',
      '--retry',
      '3',
      '--retry-delay',
      '5',
      '--output',
      dest,
      url,
    ],
    {timeout: DOWNLOAD_TIMEOUT_MS},
  );
}

async function fetchChecksums(
  urls: Array<string | null>,
): Promise<Map<string, string>> {
  const errors: string[] = [];
  for (const url of urls) {
    if (!url) continue;
    try {
      const response = await fetch(url, {
        redirect: 'follow',
        signal: AbortSignal.timeout(30_000),
      });
      if (!response.ok) {
        errors.push(`HTTP ${response.status} ${response.statusText}: ${url}`);
        continue;
      }
      return parseChecksums(await response.text());
    } catch (error) {
      errors.push(
        `${url}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
  throw new Error(`Failed to fetch SHA256SUMS:\n${errors.join('\n')}`);
}

function parseChecksums(text: string): Map<string, string> {
  const checksums = new Map<string, string>();
  for (const line of text.trim().split('\n')) {
    const match = line.trim().match(/^([a-f0-9]{64})\s+\*?(.+)$/i);
    if (match) checksums.set(match[2], match[1].toLowerCase());
  }
  return checksums;
}

async function downloadReleaseArchive(
  release: CloakReleaseInfo,
  archivePath: string,
): Promise<void> {
  const errors: string[] = [];
  for (const url of [release.downloadUrl, release.fallbackDownloadUrl]) {
    try {
      await downloadFile(url, archivePath);
      return;
    } catch (error) {
      errors.push(
        `${url}: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
  throw new Error(
    `All CloakBrowser download URLs failed:\n${errors.join('\n')}`,
  );
}

async function verifyChecksum(
  filePath: string,
  expectedHash: string,
): Promise<void> {
  const actual = await fileSha256(filePath);
  if (actual !== expectedHash) {
    throw new Error(
      `Checksum verification failed for ${filePath}: expected ${expectedHash}, got ${actual}`,
    );
  }
}

async function fileSha256(filePath: string): Promise<string> {
  const hash = createHash('sha256');
  const stream = createReadStream(filePath);
  for await (const chunk of stream) hash.update(chunk);
  return hash.digest('hex').toLowerCase();
}

async function findReusableArchive(
  stagingRoot: string,
  archiveName: string,
  expectedHash: string,
): Promise<string | null> {
  try {
    const files = await listFiles(stagingRoot);
    for (const file of files) {
      if (path.basename(file) !== archiveName) continue;
      if ((await fileSha256(file)) === expectedHash) return file;
    }
  } catch {
    return null;
  }
  return null;
}

async function extractZip(archivePath: string, destDir: string): Promise<void> {
  await fs.mkdir(destDir, {recursive: true});
  const script =
    'Add-Type -AssemblyName System.IO.Compression.FileSystem; ' +
    '[System.IO.Compression.ZipFile]::ExtractToDirectory($env:JS_REVERSE_MCP_ARCHIVE_PATH, $env:JS_REVERSE_MCP_EXTRACT_DIR)';
  execFileSync('powershell', ['-NoProfile', '-Command', script], {
    timeout: 180_000,
    env: {
      ...process.env,
      JS_REVERSE_MCP_ARCHIVE_PATH: archivePath,
      JS_REVERSE_MCP_EXTRACT_DIR: destDir,
    },
  });
}

async function flattenSingleSubdir(destDir: string): Promise<void> {
  const entries = await fs.readdir(destDir, {withFileTypes: true});
  if (entries.length !== 1 || !entries[0].isDirectory()) return;
  const subdir = path.join(destDir, entries[0].name);
  for (const child of await fs.readdir(subdir)) {
    await fs.rename(path.join(subdir, child), path.join(destDir, child));
  }
  await fs.rmdir(subdir);
}

async function listFiles(root: string): Promise<string[]> {
  const result: string[] = [];
  async function visit(dir: string): Promise<void> {
    for (const entry of await fs.readdir(dir, {withFileTypes: true})) {
      const absolute = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await visit(absolute);
      } else if (entry.isFile()) {
        result.push(absolute);
      }
    }
  }
  await visit(root);
  return result;
}

function assertSafeRelativePath(relativePath: string): void {
  if (
    path.isAbsolute(relativePath) ||
    relativePath.split(/[\\/]+/).some(segment => segment === '..')
  ) {
    throw new Error(`Unsafe archive path: ${relativePath}`);
  }
  const firstSegment = relativePath.split(/[\\/]+/)[0]?.toLowerCase();
  if (PROTECTED_NAMES.some(name => name.toLowerCase() === firstSegment)) {
    throw new Error(`Refusing to write protected path: ${relativePath}`);
  }
}

function assertTargetOutsideProtectedRoots(
  targetPath: string,
  installDir: string,
): void {
  for (const protectedName of PROTECTED_NAMES) {
    const protectedRoot = path.join(installDir, protectedName);
    if (isSameOrInside(targetPath, protectedRoot)) {
      throw new Error(`Refusing to write protected path: ${targetPath}`);
    }
  }
}

async function copyWithBackup(params: {
  sourceRoot: string;
  installDir: string;
  backupDir: string;
}): Promise<{copiedFiles: number; backedUpFiles: number}> {
  const files = await listFiles(params.sourceRoot);
  let copiedFiles = 0;
  let backedUpFiles = 0;

  for (const source of files) {
    const relativePath = path.relative(params.sourceRoot, source);
    assertSafeRelativePath(relativePath);
    const target = path.join(params.installDir, relativePath);
    assertTargetOutsideProtectedRoots(target, params.installDir);
  }

  for (const source of files) {
    const relativePath = path.relative(params.sourceRoot, source);
    const target = path.join(params.installDir, relativePath);
    const backup = path.join(params.backupDir, relativePath);
    await fs.mkdir(path.dirname(target), {recursive: true});

    try {
      const stat = await fs.stat(target);
      if (stat.isDirectory()) {
        throw new Error(`Cannot overwrite directory with file: ${target}`);
      }
      await fs.mkdir(path.dirname(backup), {recursive: true});
      await fs.copyFile(target, backup);
      backedUpFiles++;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
    }

    await fs.copyFile(source, target);
    copiedFiles++;
  }

  return {copiedFiles, backedUpFiles};
}

export async function updateBrowserBinary(options?: {
  installDir?: string | null;
  version?: string | null;
  force?: boolean;
  dryRun?: boolean;
  skipChecksum?: boolean;
}): Promise<UpdateBrowserBinaryResult> {
  const check = await checkBrowserUpdate({
    installDir: options?.installDir,
    version: options?.version,
  });
  const release = check.release;
  if (!check.supported || !release) {
    throw new Error(check.note ?? 'CloakBrowser update is not supported here');
  }
  if (!check.updateAvailable && !options?.force) {
    return {
      ...check,
      status: 'skipped',
      backupDir: null,
      stagingDir: null,
      downloadedArchive: null,
      removedDownloadedArchive: null,
      archiveCleanupError: null,
      copiedFiles: 0,
      backedUpFiles: 0,
      plannedDownloadUrl: release.downloadUrl,
    };
  }
  if (options?.dryRun) {
    return {
      ...check,
      status: 'dry_run',
      backupDir: path.join(check.installDir, '.cloakbrowser-backups'),
      stagingDir: path.join(check.installDir, '.cloakbrowser-update-staging'),
      downloadedArchive: null,
      removedDownloadedArchive: null,
      archiveCleanupError: null,
      copiedFiles: 0,
      backedUpFiles: 0,
      plannedDownloadUrl: release.downloadUrl,
    };
  }

  await fs.mkdir(check.installDir, {recursive: true});
  const sessionName = `${release.version.replaceAll(/[^0-9A-Za-z.-]/g, '_')}-${Date.now()}`;
  const stagingRoot = path.join(
    check.installDir,
    '.cloakbrowser-update-staging',
  );
  const stagingDir = path.join(stagingRoot, sessionName);
  const extractDir = path.join(stagingDir, 'extracted');
  let archivePath = path.join(stagingDir, release.archiveName);
  const backupDir = path.join(
    check.installDir,
    '.cloakbrowser-backups',
    sessionName,
  );

  await fs.mkdir(stagingDir, {recursive: true});
  let expectedHash: string | undefined;
  if (!options?.skipChecksum) {
    const checksums = await fetchChecksums([
      release.checksumUrl,
      release.fallbackChecksumUrl,
    ]);
    expectedHash = checksums.get(release.archiveName);
    if (!expectedHash) {
      throw new Error(`SHA256SUMS has no entry for ${release.archiveName}`);
    }
    archivePath =
      (await findReusableArchive(
        stagingRoot,
        release.archiveName,
        expectedHash,
      )) ?? archivePath;
  }
  if (!fsSync.existsSync(archivePath)) {
    await downloadReleaseArchive(release, archivePath);
  }
  if (expectedHash) {
    await verifyChecksum(archivePath, expectedHash);
  }
  await extractZip(archivePath, extractDir);
  await flattenSingleSubdir(extractDir);
  const extractedBinary = getBinaryPath(extractDir);
  if (!fsSync.existsSync(extractedBinary)) {
    throw new Error(
      `Extracted archive does not contain expected binary: ${extractedBinary}`,
    );
  }

  const copyResult = await copyWithBackup({
    sourceRoot: extractDir,
    installDir: check.installDir,
    backupDir,
  });
  await writeVersionMarker(check.installDir, release.version);
  await writePortableBrowserConnectionConfig();

  let removedDownloadedArchive: string | null = null;
  let archiveCleanupError: string | null = null;
  try {
    if (fsSync.existsSync(archivePath)) {
      await fs.rm(archivePath, {force: true});
      removedDownloadedArchive = archivePath;
    }
  } catch (error) {
    archiveCleanupError = error instanceof Error ? error.message : String(error);
  }

  const updated = await getInstalledBinaryInfo({installDir: check.installDir});
  return {
    ...check,
    ...updated,
    latestVersion: release.version,
    updateAvailable: false,
    status: 'updated',
    backupDir,
    stagingDir,
    downloadedArchive: archiveCleanupError ? archivePath : null,
    removedDownloadedArchive,
    archiveCleanupError,
    copiedFiles: copyResult.copiedFiles,
    backedUpFiles: copyResult.backedUpFiles,
    plannedDownloadUrl: release.downloadUrl,
  };
}
