/**
 * Private Python subprocess bridge to Qwen's extension secret storage.
 *
 * Usage: node qwen_credentials.mjs <qwen-package-root> <project-root>
 * stdout is a private pipe: {token, source}. Never run this helper as a model tool.
 * Qwen has no public secret reader API. This bridge uses internal named exports
 * and deliberately supports only the verified Qwen Code 0.27.0 contract.
 * Qwen itself selects the OS keychain or its encrypted-file fallback; its
 * keychain availability check may create and delete a temporary probe entry.
 */
import { readFile, readdir, stat } from 'node:fs/promises';
import { homedir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const writeResult = process.stdout.write.bind(process.stdout);
const writeError = process.stderr.write.bind(process.stderr);
// Imported Qwen modules may emit debug output, warnings or parser diagnostics.
// Neither stream may expose credentials or untrusted storage error details.
process.stdout.write = () => true;
process.stderr.write = () => true;
for (const method of ['log', 'info', 'warn', 'error', 'debug', 'trace']) {
  console[method] = () => {};
}

const EXTENSION = 'alla-launch-credentials';
const TOKEN = 'ALLURE_TOKEN';

class BridgeError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function fail(code) {
  throw new BridgeError(code);
}

async function rejectPlaintextFile(filename) {
  let content;
  try {
    content = await readFile(filename, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return;
    throw error;
  }
  for (const line of content.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const match = line.match(/^\s*(?:export\s+)?ALLURE_TOKEN\s*(?:=|:\s)(.*)$/);
    if (!match) continue;
    const value = match[1].trim();
    if (value && !value.startsWith('#') && !/^(?:""|'')\s*(?:#.*)?$/.test(value)) {
      fail('QWEN_PLAINTEXT');
    }
  }
}

async function findReader(packageRoot) {
  let manifest;
  try {
    manifest = JSON.parse(await readFile(path.join(packageRoot, 'package.json'), 'utf8'));
  } catch {
    fail('QWEN_UNSUPPORTED');
  }
  if (manifest.name !== '@qwen-code/qwen-code' || manifest.version !== '0.27.0') {
    fail('QWEN_UNSUPPORTED');
  }
  const chunks = path.join(packageRoot, 'chunks');
  const candidates = [];
  for (const entry of await readdir(chunks, { withFileTypes: true })) {
    if (!entry.isFile() || !entry.name.endsWith('.js')) continue;
    const filename = path.join(chunks, entry.name);
    const content = await readFile(filename, 'utf8');
    for (const block of content.matchAll(/\bexport\s*\{([^{}]*)\}\s*;/g)) {
      const names = new Set(block[1].split(',').map((item) => item.trim().split(/\s+as\s+/).at(-1)));
      if (names.has('ExtensionManager') && names.has('getScopedEnvContents')) {
        candidates.push(filename);
        break;
      }
    }
  }
  if (candidates.length !== 1) fail('QWEN_UNSUPPORTED');
  const api = await import(pathToFileURL(candidates[0]).href);
  if (typeof api.ExtensionManager !== 'function' || typeof api.getScopedEnvContents !== 'function') {
    fail('QWEN_UNSUPPORTED');
  }
  return api;
}

async function readCredentials(packageRoot, projectRoot) {
  const { ExtensionManager, getScopedEnvContents } = await findReader(packageRoot);
  const qwenHome = process.env.QWEN_HOME
    ? path.resolve(process.env.QWEN_HOME.replace(/^~(?=$|[/\\])/, homedir()))
    : path.join(homedir(), '.qwen');
  const extensionDir = path.join(qwenHome, 'extensions', EXTENSION);
  await rejectPlaintextFile(path.join(extensionDir, '.env'));
  await rejectPlaintextFile(path.join(projectRoot, '.env'));
  await rejectPlaintextFile(path.join(projectRoot, '.qwen', '.env'));
  try {
    if (!(await stat(extensionDir)).isDirectory()) fail('QWEN_NOT_INSTALLED');
  } catch (error) {
    if (error instanceof BridgeError || error.code === 'ENOENT') fail('QWEN_NOT_INSTALLED');
    throw error;
  }
  // Metadata loading computes the native installation identity, including link
  // and snapshot installs. Do not refresh the extension cache: it can migrate or
  // recover extension-store state and load unrelated extension resources.
  const manager = new ExtensionManager({
    workspaceDir: projectRoot,
    enabledExtensionOverrides: ['none'],
  });
  if (typeof manager.loadExtensionManifestHead !== 'function') fail('QWEN_UNSUPPORTED');
  const head = await manager.loadExtensionManifestHead(
    { extensionDir, workspaceDir: projectRoot },
    { createDataDir: false },
  );
  const extension = head.extension;
  if (head.loadedManifest.format !== 'qwen' || extension.name !== EXTENSION) {
    fail('QWEN_UNSUPPORTED');
  }
  const settings = extension.config.settings;
  if (!Array.isArray(settings)) fail('QWEN_UNSUPPORTED');
  const tokenSettings = settings.filter((setting) => setting.envVar === TOKEN);
  if (tokenSettings.length !== 1 || tokenSettings[0].sensitive !== true) {
    fail('QWEN_UNSUPPORTED');
  }

  // Without settings definitions Qwen returns only .env contents. Reject an
  // existing plaintext copy before accepting any value from secure storage.
  const plainConfig = { ...extension.config, settings: [] };
  for (const scope of ['user', 'workspace']) {
    const plain = await getScopedEnvContents(plainConfig, extension.id, scope);
    if (typeof plain[TOKEN] === 'string' && plain[TOKEN].trim()) fail('QWEN_PLAINTEXT');
  }
  // Read only the one declared secret, not settings of other integrations.
  const secretConfig = { ...extension.config, settings: tokenSettings };
  for (const scope of ['workspace', 'user']) {
    const values = await getScopedEnvContents(secretConfig, extension.id, scope);
    const token = values[TOKEN];
    if (typeof token === 'string' && token.trim()) {
      return { token, source: `qwen:${scope}` };
    }
  }
  fail('QWEN_SECRET_MISSING');
}

try {
  if (process.argv.length !== 4) fail('QWEN_UNSUPPORTED');
  const projectRoot = path.resolve(process.argv[3]);
  process.chdir(projectRoot); // Qwen's workspace secret namespace uses process.cwd().
  const result = await readCredentials(path.resolve(process.argv[2]), projectRoot);
  writeResult(`${JSON.stringify(result)}\n`);
} catch (error) {
  const identityUnavailable = error.code === 'ERR_SYSTEM_ERROR'
    && error.info?.syscall === 'uv_os_get_passwd';
  writeError(`${error instanceof BridgeError ? error.code
    : identityUnavailable ? 'QWEN_IDENTITY_UNAVAILABLE' : 'QWEN_STORAGE_ERROR'}\n`);
  process.exitCode = 1;
}
