import http from "node:http";
import {randomUUID} from "node:crypto";
import {chmod, mkdir, readdir, writeFile} from "node:fs/promises";
import {chromium} from "playwright";

process.umask(0o077);

const port = Number(process.env.PORT || 8080);
const root = process.env.ARTIFACT_DIR || "/artifacts";
const allowedOrigins = new Set(
  (
    process.env.ALLOWED_ORIGINS
    || process.env.ALLOWED_ORIGIN
    || "http://caddy:8081"
  ).split(",").map(value => new URL(value.trim()).origin),
);
const maxBodyBytes = 1024 * 1024;
const configuredConcurrency = Number(
  process.env.MAX_CONCURRENT_RUNS || 1,
);
const maxConcurrentRuns = Number.isInteger(configuredConcurrency)
  ? Math.min(4, Math.max(1, configuredConcurrency))
  : 1;
let activeRuns = 0;
const safe = value => String(value).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 100);
const redactUrl = value => value.replace(
  /([?&](?:token|key|authorization|access_token|api_key|apikey|subscription-key)=)[^&]+/ig,
  "$1[redacted]",
);

function pngSize(buffer) {
  if (
    buffer.length < 24
    || buffer.toString("ascii", 1, 4) !== "PNG"
    || buffer.toString("ascii", 12, 16) !== "IHDR"
  ) {
    return null;
  }
  return {
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
  };
}

async function captureMap(page, directory, label, fullPage = true) {
  const pageImage = await page.screenshot({
    path: `${directory}/${label}-page.png`,
    fullPage,
  });
  const canvas = page.locator("canvas").first();
  let mapImage = null;
  if (await canvas.count()) {
    mapImage = await canvas.screenshot({
      path: `${directory}/${label}-map.png`,
    });
  }
  return {
    page: pngSize(pageImage),
    map: mapImage ? pngSize(mapImage) : null,
  };
}

async function interactionText(page) {
  return (await page.locator("body").innerText({timeout: 2_000}))
    .replace(/\s+/g, " ")
    .trim();
}

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
  if (!allowedOrigins.has(target.origin) || target.username || target.password) {
    throw new RequestError(400, "Visual target origin is not allowed.");
  }

  const binding = input.metadata?.source === "candidate"
    ? `${safe(input.metadata.proposalId)}-${safe(input.metadata.candidateHash).slice(0, 16)}-`
    : "";
  const runId = `${new Date().toISOString().replace(/[:.]/g, "-")}-${binding}${safe(input.layer || "workspace")}-${randomUUID().slice(0, 8)}`;
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
  const viewMode = input.viewMode === "default" ? "default" : "focus";
  const requestedLayers = Array.isArray(input.layers)
    ? input.layers.filter(value => typeof value === "string" && value)
    : input.layer
      ? [input.layer]
      : [];
  const backgroundLayers = Array.isArray(input.plan?.backgroundLayers)
    ? input.plan.backgroundLayers.filter(value => typeof value === "string" && value)
    : [];
  const activeLayers = [...new Set([...backgroundLayers, ...requestedLayers])];
  if (viewMode === "focus" && activeLayers.length) {
    target.searchParams.set("layers", activeLayers.join(","));
  }

  const requestedViewport = input.viewport || {};
  const viewport = {
    width: Math.min(2560, Math.max(320, Number(requestedViewport.width) || 1920)),
    height: Math.min(1440, Math.max(240, Number(requestedViewport.height) || 1080)),
  };
  const deviceScaleFactor = Math.min(
    3,
    Math.max(1, Number(input.deviceScaleFactor || requestedViewport.deviceScaleFactor) || 2),
  );
  const fullPage = input.fullPage !== false;
  const timeout = Math.min(60_000, Math.max(5_000, Number(input.timeout) || 45_000));
  const report = {
    runId,
    layer: input.layer,
    layers: requestedLayers,
    viewMode,
    groups: Array.isArray(input.plan?.activeGroups)
      ? input.plan.activeGroups
      : [],
    plan: input.plan,
    metadata: input.metadata,
    url: target.toString(),
    console: [],
    pageErrors: [],
    failedRequests: [],
    passed: false,
  };

  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport, deviceScaleFactor});
    page.on("console", message => report.console.push({
      type: message.type(),
      text: message.text(),
    }));
    page.on("pageerror", error => report.pageErrors.push(error.message));
    page.on("requestfailed", request => report.failedRequests.push({
      url: redactUrl(request.url()),
      error: request.failure()?.errorText,
    }));
    const response = await page.goto(target.toString(), {
      waitUntil: "networkidle",
      timeout,
    });
    report.httpStatus = response?.status();
    report.canvasCount = await page.locator("canvas").count();
    report.layerTextFound = viewMode === "default"
      ? true
      : input.layer
      ? await page.getByText(input.layer, {exact: false}).count() > 0
      : true;
    report.groupTextFound = (
      await Promise.all(
        report.groups.map(
          group => page.getByText(group, {exact: true}).count(),
        ),
      )
    ).every(count => count > 0);
    const beforeCapture = await captureMap(page, directory, "before", fullPage);
    report.interaction = null;
    if (input.plan?.interaction?.type === "click-centre-feature") {
      const beforeText = await interactionText(page);
      const mapCanvas = page.locator("canvas").first();
      const box = await mapCanvas.boundingBox();
      const clickPoint = {
        x: Math.round((box?.x ?? 0) + (box?.width ?? viewport.width) / 2),
        y: Math.round((box?.y ?? 0) + (box?.height ?? viewport.height) / 2),
      };
      await page.mouse.click(clickPoint.x, clickPoint.y);
      await page.waitForLoadState("networkidle", {timeout: Math.min(timeout, 10_000)})
        .catch(() => {});
      const infoPanel = page.locator(".location-view.expanded").first();
      await infoPanel.waitFor({
        state: "visible",
        timeout: Math.min(timeout, 10_000),
      }).catch(() => {});
      await page.waitForFunction(
        () => {
          const panel = document.querySelector(".location-view.expanded");
          return panel && !panel.classList.contains("loading");
        },
        {timeout: Math.min(timeout, 10_000)},
      ).catch(() => {});
      await page.waitForTimeout(500);
      const afterText = await interactionText(page);
      const expectedLayer = input.plan.interaction.expectedLayer || input.layer || "";
      const expectedFeatureId = input.plan.interaction.expectedFeatureId;
      const infoPanelExpanded = await infoPanel.isVisible().catch(() => false);
      let infoPanelImage = null;
      if (infoPanelExpanded) {
        infoPanelImage = await infoPanel.screenshot({
          path: `${directory}/info-panel.png`,
        });
      }
      report.interaction = {
        type: input.plan.interaction.type,
        clickPoint,
        expectedFeatureId,
        textChanged: afterText !== beforeText,
        infoPanelExpanded,
        infoPanelImage: infoPanelImage ? pngSize(infoPanelImage) : null,
        expectedLayerFound: expectedLayer
          ? afterText.toLowerCase().includes(String(expectedLayer).toLowerCase())
          : true,
        expectedFeatureIdFound: expectedFeatureId == null
          ? null
          : afterText.includes(String(expectedFeatureId)),
        beforeTextLength: beforeText.length,
        afterTextLength: afterText.length,
        afterTextSample: afterText.slice(0, 2000),
      };
    }
    const afterCapture = await captureMap(page, directory, "after", fullPage);
    const pageImage = await page.screenshot({
      path: `${directory}/page.png`,
      fullPage,
    });
    const canvas = page.locator("canvas").first();
    let mapImage = null;
    if (await canvas.count()) {
      mapImage = await canvas.screenshot({path: `${directory}/map.png`});
    }
    report.capture = {
      viewport,
      deviceScaleFactor,
      fullPage,
      images: {
        page: pngSize(pageImage),
        map: mapImage ? pngSize(mapImage) : null,
        beforePage: beforeCapture.page,
        beforeMap: beforeCapture.map,
        afterPage: afterCapture.page,
        afterMap: afterCapture.map,
      },
    };
    const interactionPassed = report.interaction
      ? (
        report.interaction.textChanged
        && (
          !input.plan?.interaction?.requireInfoPanel
          || report.interaction.infoPanelExpanded
        )
      )
      : true;
    report.passed = Boolean(
      response?.ok()
      && report.canvasCount
      && report.layerTextFound
      && report.groupTextFound
      && interactionPassed
      && !report.pageErrors.length
    );
  } catch (error) {
    report.error = error.message;
  } finally {
    await browser.close();
  }

  report.diagnosis = {
    outcome: report.passed ? "passed" : "failed",
    checks: [
      {
        id: "visual.http",
        passed: Number.isInteger(report.httpStatus)
          && report.httpStatus >= 200
          && report.httpStatus < 400,
        observed: report.httpStatus ?? null,
      },
      {
        id: "visual.canvas",
        passed: Number(report.canvasCount || 0) > 0,
        observed: Number(report.canvasCount || 0),
      },
      {
        id: "visual.layer",
        passed: report.layerTextFound === true,
        observed: report.layerTextFound ?? false,
      },
      {
        id: "visual.groups",
        passed: report.groupTextFound === true,
        observed: {
          groups: report.groups,
          found: report.groupTextFound ?? false,
        },
      },
      {
        id: "visual.page_errors",
        passed: report.pageErrors.length === 0,
        observed: report.pageErrors.length,
      },
      {
        id: "visual.feature_interaction",
        passed: report.interaction
          ? (
            report.interaction.textChanged
            && (
              !input.plan?.interaction?.requireInfoPanel
              || report.interaction.infoPanelExpanded
            )
          )
          : true,
        observed: report.interaction,
      },
    ],
    viewport,
    deviceScaleFactor,
    selectedView: {
      centre: input.plan?.centre ?? null,
      zoom: input.plan?.zoom ?? null,
      locale: input.plan?.locale ?? null,
    },
    failureClass: report.passed
      ? null
      : report.error
        ? "browser"
        : !report.httpStatus || report.httpStatus >= 400
          ? "http"
          : !report.canvasCount
            ? "render"
            : !report.layerTextFound
              ? "binding"
              : !report.groupTextFound
                ? "group"
                : report.interaction
                  && !report.interaction.textChanged
                  ? "feature"
                  : report.pageErrors.length
                    ? "page"
                    : "unknown",
  };
  await writeFile(`${directory}/report.json`, JSON.stringify(report, null, 2), {
    mode: 0o600,
  });
  return {
    ...report,
    artifacts: {
      report: `${runId}/report.json`,
      page: `${runId}/page.png`,
      map: `${runId}/map.png`,
      beforePage: `${runId}/before-page.png`,
      beforeMap: `${runId}/before-map.png`,
      afterPage: `${runId}/after-page.png`,
      afterMap: `${runId}/after-map.png`,
      infoPanel: report.interaction?.infoPanelExpanded
        ? `${runId}/info-panel.png`
        : null,
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
