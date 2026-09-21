// O20: does `form-action 'self'` on the consent page block the consent POST's
// 302 to the client's registered redirect_uri, which is a different origin?
//
// Run it:
//
//   ./bin/mapp mcp-client-register --name "CSP check" \
//       --redirect-uri http://127.0.0.1:8484/callback \
//       --scope mcp:connect --scope inspect
//
//   docker run --rm --network container:<project>-caddy-1 \
//       -v "$PWD/scripts/check_consent_form_action.mjs":/runner/check.mjs \
//       -w /runner -e ORIGIN=http://mcp.localhost -e CLIENT_ID=<id> \
//       -e SESSION=<an mcp-auth operator session> \
//       --entrypoint node mapp-browser-runner:local /runner/check.mjs
//
// Caddy's network namespace is not an optimisation: browsers resolve
// *.localhost to loopback themselves and ignore /etc/hosts and any
// --add-host, so the server has to *be* on that loopback.
//
// Result on 2026-09-21: chromium and firefox both followed the redirect and
// reported no violation. webkit could not launch in mapp-browser-runner:local
// -- the image lacks its system libraries and carries no package manager to
// add them -- so Safari's engine is untested and that is why this script is
// kept rather than deleted after one use.
//
// The Phase 0 suite drives http.client, which enforces no CSP, so this is
// unverifiable there by construction. This drives real engines and settles it
// the only way it can be settled: by seeing whether the callback is reached.
//
// Runs inside Caddy's network namespace, because browsers resolve *.localhost
// to loopback themselves and ignore any host mapping -- so loopback has to be
// where the server actually is.
import crypto from 'node:crypto';
import http from 'node:http';
import { chromium, firefox, webkit } from 'playwright';

const ORIGIN = process.env.ORIGIN;
const CLIENT = process.env.CLIENT_ID;
const SESSION = process.env.SESSION;
const PORT = 8484;
const REDIRECT = `http://127.0.0.1:${PORT}/callback`;

let hits = [];
const catcher = http.createServer((request, response) => {
  hits.push(request.url);
  response.writeHead(200, { 'Content-Type': 'text/plain' });
  response.end('caught');
});
await new Promise(resolve => catcher.listen(PORT, '127.0.0.1', resolve));

function authorizeUrl() {
  const verifier = crypto.randomBytes(48).toString('base64url');
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  return `${ORIGIN}/oauth/authorize?` + new URLSearchParams({
    response_type: 'code', client_id: CLIENT, redirect_uri: REDIRECT,
    scope: 'mcp:connect inspect', state: 'o20',
    code_challenge: challenge, code_challenge_method: 'S256',
    resource: `${ORIGIN}/mcp`,
  });
}

for (const [name, engine] of [['chromium', chromium], ['firefox', firefox],
                              ['webkit', webkit]]) {
  let browser;
  try {
    browser = await engine.launch();
  } catch (error) {
    console.log(`${name.padEnd(9)} UNAVAILABLE  ${String(error).split('\n')[0].slice(0, 80)}`);
    continue;
  }
  hits = [];
  const violations = [];
  try {
    const context = await browser.newContext();
    await context.addCookies([{
      name: 'mapp_oauth_session', value: SESSION, url: `${ORIGIN}/oauth`,
    }]);
    const page = await context.newPage();
    page.on('console', message => {
      const text = message.text();
      if (/Content Security Policy|form-action|Refused to send form data/i.test(text)) {
        violations.push(text.slice(0, 120));
      }
    });
    await page.goto(authorizeUrl(), { waitUntil: 'domcontentloaded', timeout: 20000 });
    const heading = (await page.textContent('h1').catch(() => '')) || '';
    const allow = page.locator('button[value="allow"]').first();
    const found = await allow.count();
    await allow.click({ timeout: 8000 }).catch(() => {});
    await page.waitForTimeout(2500);
    const verdict = hits.length
      ? 'PASS  redirect followed to the client origin'
      : (violations.length
          ? 'BLOCKED by CSP'
          : `no callback (page at ${page.url().slice(0, 60)})`);
    console.log(`${name.padEnd(9)} consent "${heading.trim().slice(0, 28)}" allow=${found} -> ${verdict}`);
    if (violations.length) console.log(`${' '.repeat(9)} ${violations.join(' / ')}`);
  } catch (error) {
    console.log(`${name.padEnd(9)} ERROR ${String(error).split('\n')[0].slice(0, 100)}`);
  }
  await browser.close();
}
catcher.close();
