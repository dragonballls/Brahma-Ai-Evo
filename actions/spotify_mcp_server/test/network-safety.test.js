import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'brahma-spotify-safety-'));
const configPath = path.join(tempDir, 'spotify-config.json');
const previousConfigPath = process.env.SPOTIFY_CONFIG_PATH;
process.env.SPOTIFY_CONFIG_PATH = configPath;
const utils = await import(`../build/utils.js?spotify-safety=${Date.now()}`);
if (previousConfigPath === undefined) delete process.env.SPOTIFY_CONFIG_PATH;
else process.env.SPOTIFY_CONFIG_PATH = previousConfigPath;

const validConfig = {
  clientId: 'client',
  clientSecret: 'secret',
  redirectUri: 'http://127.0.0.1:8888/callback',
  accessToken: 'access',
  refreshToken: 'refresh',
  expiresAt: Date.now() + 3600000,
};

test.after(() => {
  fs.rmSync(tempDir, { recursive: true, force: true });
});

test('spotifyFetch rejects oversized response bodies and disables redirects', async () => {
  fs.writeFileSync(configPath, JSON.stringify(validConfig), 'utf8');
  const originalFetch = globalThis.fetch;
  let captured;
  try {
    globalThis.fetch = async (_input, options) => {
      captured = options;
      return new Response('x'.repeat(256 * 1024 + 1), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    };

    await assert.rejects(
      utils.spotifyFetch('me/player'),
      /exceeded the safety limit/,
    );
    assert.equal(captured.redirect, 'error');
    assert.ok(captured.signal instanceof AbortSignal);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('spotifyFetch rejects malformed JSON instead of treating it as success', async () => {
  fs.writeFileSync(configPath, JSON.stringify(validConfig), 'utf8');
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () =>
      new Response('not-json', {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });

    await assert.rejects(
      utils.spotifyFetch('me/player'),
      /invalid JSON/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('Spotify config persistence writes valid state and never follows a config symlink', async (t) => {
  utils.saveSpotifyConfig(validConfig);
  const stored = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  assert.equal(stored.clientId, validConfig.clientId);
  assert.equal(stored.refreshToken, validConfig.refreshToken);

  const outside = path.join(tempDir, 'outside.json');
  fs.writeFileSync(outside, JSON.stringify(validConfig), 'utf8');

  const linked = path.join(tempDir, 'linked-config.json');
  try {
    fs.symlinkSync(outside, linked);
  } catch (error) {
    t.skip(`Symlinks unavailable: ${error instanceof Error ? error.message : String(error)}`);
    return;
  }

  const savedPrevious = process.env.SPOTIFY_CONFIG_PATH;
  process.env.SPOTIFY_CONFIG_PATH = linked;
  const linkedUtils = await import(`../build/utils.js?spotify-link=${Date.now()}`);
  try {
    assert.throws(
      () => linkedUtils.loadSpotifyConfig(),
      /regular file/,
    );
    assert.throws(
      () => linkedUtils.saveSpotifyConfig(validConfig),
      /regular file/,
    );
  } finally {
    if (savedPrevious === undefined) delete process.env.SPOTIFY_CONFIG_PATH;
    else process.env.SPOTIFY_CONFIG_PATH = savedPrevious;
  }
});
