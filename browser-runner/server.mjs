import http from "node:http";
import {randomUUID} from "node:crypto";
import {chmod, mkdir, readdir, writeFile} from "node:fs/promises";
import {chromium} from "playwright";

process.umask(0o077);

const port = Number(process.env.PORT || 8080);
const root = process.env.ARTIFACT_DIR || "/artifacts";
const allowedOrigin = new URL(
  process.env.ALLOWED_ORIGIN || "http://caddy:8081",
).origin;
const maxBodyBytes = 1024 * 1024;
const configuredConcurrency = Number(
  process.env.MAX_CONCURRENT_RUNS || 1,
);
const maxConcurrentRuns = Number.isInteger(configuredConcurrency)
  ? Math.min(4, Math.max(1, configuredConcurrency))
  : 1;
let activeRuns = 0;
const safe = value => String(value).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 100);

async function secureArtifactTree() {
  await mkdir(root, {recursive: true, mode: 0o700});
  await chmod(root, 0o700);
  for (const run of await readdir(root, {withFileTypes: true})) {
    if (!run.isDirectory() || run.isSymbolicLink()) continue;
    const directory = `${root}/${run.name}`;
    await chmod(directory, 0o700);
    for (const artifact of await readdir(directory, {withFileTypes: true})) {
      if (artifact.isFile() && !artifact.isSymbolicLink()) {
        await chmod(`${directory}/${artifact.name}`, 0o600);
      }
    }
  }
}

class RequestError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > maxBodyBytes) throw new RequestError(413, "Request body is too large.");
    chunks.push(chunk);
  }
  if (!size) throw new RequestError(400, "Request body is required.");
  try {
    return JSON.parse(Buffer.concat(chunks));
  } catch {
    throw new RequestError(400, "Request body is not valid JSON.");
  }
}

function json(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  res.end(body);
}

async function runVisual(input) {
  const target = new URL(input.url);
  if (target.origin !== allowedOrigin || target.username || target.password) {
    throw new RequestError(400, "Visual target origin is not allowed.");
  }

  const runId = `${new Date().toISOString().replace(/[:.]/g, "-")}-${safe(input.layer || "workspace")}-${randomUUID().slice(0, 8)}`;
  const directory = `${root}/${runId}`;
  await mkdir(directory, {recursive: false, mode: 0o700});

  if (input.plan?.centre?.length === 2) {
    target.searchParams.set("lng", input.plan.centre[0]);
    target.searchParams.set("lat", input.plan.centre[1]);
  }
  if (input.plan?.zoom != null) {
    target.searchParams.set(
      "z",
      Math.min(18, Math.max(0, Number(input.plan.zoom))).toFixed(2),
    );
  }
  if (input.plan?.locale) {
    target.searchParams.set("locale", input.plan.locale);
  }
  if (input.layer) target.searchParams.set("layers", `OpenStreetMap,${input.layer}`);

  const requestedViewport = input.viewport || {};
  const viewport = {
    width: Math.min(2560, Math.max(320, Number(requestedViewport.width) || 1280)),
    height: Math.min(1440, Math.max(240, Number(requestedViewport.height) || 720)),
  };
  const timeout = Math.min(60_000, Math.max(5_000, Number(input.timeout) || 45_000));
  const report = {
    runId,
    layer: input.layer,
    plan: input.plan,
    url: target.toString(),
    console: [],
    pageErrors: [],
    failedRequests: [],
    passed: false,
  };

  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport});
    page.on("console", message => report.console.push({
      type: message.type(),
      text: message.text(),
    }));
    page.on("pageerror", error => report.pageErrors.push(error.message));
    page.on("requestfailed", request => report.failedRequests.push({
      url: request.url().replace(
        /([?&](?:token|key|authorization|access_token|api_key|apikey|subscription-key)=)[^&]+/ig,
        "$1[redacted]",
      ),
      error: request.failure()?.errorText,
    }));
    const response = await page.goto(target.toString(), {
      waitUntil: "networkidle",
      timeout,
    });
    await page.screenshot({path: `${directory}/page.png`, fullPage: true});
    const canvas = page.locator("canvas").first();
    if (await canvas.count()) await canvas.screenshot({path: `${directory}/map.png`});
    report.httpStatus = response?.status();
    report.canvasCount = await page.locator("canvas").count();
    report.layerTextFound = input.layer
      ? await page.getByText(input.layer, {exact: false}).count() > 0
      : true;
    report.passed = Boolean(
      response?.ok()
      && report.canvasCount
      && report.layerTextFound
      && !report.pageErrors.length
    );
  } catch (error) {
    report.error = error.message;
  } finally {
    await browser.close();
  }

  await writeFile(`${directory}/report.json`, JSON.stringify(report, null, 2), {
    mode: 0o600,
  });
  return {
    ...report,
    artifacts: {
      report: `${runId}/report.json`,
      page: `${runId}/page.png`,
      map: `${runId}/map.png`,
    },
  };
}

await secureArtifactTree();

http.createServer(async (req, res) => {
  if (req.url === "/healthz") {
    return json(res, 200, {
      status: "ok",
      activeRuns,
      maxConcurrentRuns,
    });
  }
  if (req.method !== "POST" || req.url !== "/run") {
    return json(res, 404, {error: "Not found."});
  }
  if (activeRuns >= maxConcurrentRuns) {
    res.setHeader("retry-after", "5");
    return json(res, 429, {
      error: "Visual runner is at its concurrency limit. Retry later.",
    });
  }
  activeRuns += 1;
  try {
    const report = await runVisual(await readJson(req));
    return json(res, report.passed ? 200 : 422, report);
  } catch (error) {
    const status = error instanceof RequestError ? error.status : 500;
    return json(res, status, {
      error: status === 500 ? "Visual runner failed." : error.message,
    });
  } finally {
    activeRuns -= 1;
  }
}).listen(port, "0.0.0.0");
