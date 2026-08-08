import http from "node:http";
import {randomUUID} from "node:crypto";
import {access, chmod, mkdir, readdir, writeFile} from "node:fs/promises";
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
const browserProxyServer = process.env.BROWSER_PROXY_SERVER || null;
const browserProxyBypass = process.env.BROWSER_PROXY_BYPASS || "";
const maxBodyBytes = 1024 * 1024;
const configuredConcurrency = Number(
  process.env.MAX_CONCURRENT_RUNS || 1,
);
const maxConcurrentRuns = Number.isInteger(configuredConcurrency)
  ? Math.min(4, Math.max(1, configuredConcurrency))
  : 1;
const configuredRunTimeout = Number(process.env.VISUAL_RUN_TIMEOUT_MS || 90_000);
const defaultRunTimeout = Number.isFinite(configuredRunTimeout)
  ? Math.min(180_000, Math.max(10_000, configuredRunTimeout))
  : 90_000;
const cleanupTimeout = 5_000;
let activeRuns = 0;
const safe = value => String(value).replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 100);
const redactUrl = value => value.replace(
  /([?&](?:token|key|authorization|access_token|api_key|apikey|subscription-key)=)[^&]+/ig,
  "$1[redacted]",
);

class VisualStageTimeout extends Error {
  constructor(stage, timeoutMilliseconds) {
    super(`Visual runner timed out during '${stage}'.`);
    this.code = "visual.run_timeout";
    this.failedStage = stage;
    this.status = 504;
    this.timeoutMilliseconds = timeoutMilliseconds;
  }
}

async function boundedStage(stage, timeoutMilliseconds, task) {
  if (timeoutMilliseconds <= 0) {
    throw new VisualStageTimeout(stage, 0);
  }
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(task),
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(new VisualStageTimeout(stage, timeoutMilliseconds)),
          timeoutMilliseconds,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function browserDiagnostics(report, stage) {
  return {
    runId: report.runId,
    stage,
    console: report.console.slice(-50),
    pageErrors: report.pageErrors.slice(-50),
    failedRequests: report.failedRequests.slice(-50),
  };
}

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

function requestedPanels(input) {
  const raw = Array.isArray(input.panels)
    ? input.panels
    : typeof input.panel === "string"
      ? [input.panel]
      : [];
  return [...new Set(raw.filter(value => (
    value === "filtering" || value === "styling"
  )))];
}

async function locatorWithDataId(scope, selector, dataId) {
  if (!dataId) return null;
  const candidates = scope.locator(selector);
  const count = await candidates.count().catch(() => 0);
  for (let index = 0; index < count; index += 1) {
    const candidate = candidates.nth(index);
    if (await candidate.getAttribute("data-id").catch(() => null) === dataId) {
      return candidate;
    }
  }
  return null;
}

async function drawerWithHeading(scope, selector, heading) {
  if (!heading) return null;
  const expected = String(heading).replace(/\s+/g, " ").trim();
  const candidates = scope.locator(selector);
  const count = await candidates.count().catch(() => 0);
  for (let index = 0; index < count; index += 1) {
    const candidate = candidates.nth(index);
    const text = await candidate.locator(
      ":scope > .header h1, :scope > .header h2, :scope > .header h3",
    ).first().innerText().catch(() => "");
    if (text.replace(/\s+/g, " ").trim() === expected) return candidate;
  }
  return null;
}

async function ensureDrawerOpen(drawer) {
  if (!drawer) {
    return {attempted: false, opened: false, failureReason: "drawer-not-found"};
  }
  const visible = await drawer.isVisible().catch(() => false);
  const state = await drawer.evaluate(element => ({
    empty: element.classList.contains("empty"),
    expandable: element.classList.contains("expandable"),
    expanded: element.classList.contains("expanded"),
  })).catch(() => null);
  if (!visible || !state) {
    return {attempted: false, opened: false, failureReason: "drawer-not-visible"};
  }
  if (state.empty) {
    return {attempted: false, opened: false, failureReason: "drawer-empty"};
  }
  if (!state.expandable || state.expanded) {
    return {attempted: false, opened: true, failureReason: null};
  }
  const header = drawer.locator(":scope > .header").first();
  const attempted = await header.isVisible().catch(() => false);
  if (attempted) {
    await header.click({timeout: 2_000}).catch(() => null);
  }
  const opened = await drawer.evaluate(
    element => element.classList.contains("expanded"),
  ).catch(() => false);
  return {
    attempted,
    opened,
    failureReason: opened ? null : "drawer-did-not-open",
  };
}

async function findRequestedLayer(page, input) {
  const byKey = await locatorWithDataId(
    page,
    ".drawer.layer-view[data-id]",
    input.layer,
  );
  if (byKey) return {locator: byKey, match: "key"};
  const byTitle = await drawerWithHeading(
    page,
    ".drawer.layer-view",
    input.layerTitle || input.layer,
  );
  return byTitle
    ? {locator: byTitle, match: "title"}
    : {locator: null, match: null};
}

async function expandRequestedLayer(page, input) {
  const groups = Array.isArray(input.plan?.activeGroups)
    ? input.plan.activeGroups
    : [];
  const openedGroups = [];
  const groupResults = [];
  for (const group of groups) {
    const drawer = (
      await locatorWithDataId(
        page,
        ".drawer.layer-group[data-id]",
        group,
      )
      || await drawerWithHeading(page, ".drawer.layer-group", group)
    );
    const result = await ensureDrawerOpen(drawer);
    groupResults.push({group, found: Boolean(drawer), ...result});
    if (result.opened) openedGroups.push(group);
  }
  const layer = await findRequestedLayer(page, input);
  if (!layer.locator) {
    return {
      openedGroups,
      groups: groupResults,
      layerFound: false,
      layerOpened: false,
      layerMatch: null,
      failureReason: "layer-not-found",
    };
  }
  const parentGroup = layer.locator.locator(
    "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' layer-group ')][1]",
  );
  if (await parentGroup.count().catch(() => 0)) {
    await ensureDrawerOpen(parentGroup);
  }
  const result = await ensureDrawerOpen(layer.locator);
  return {
    openedGroups,
    groups: groupResults,
    layerFound: true,
    layerOpened: result.opened,
    layerMatch: layer.match,
    failureReason: result.failureReason,
  };
}

async function revealFirstFilteringControl(panel, expectedText) {
  const selector = panel.locator(
    'select[data-id$="-filter-dropdown"]',
  ).first();
  if (!await selector.isVisible().catch(() => false)) return null;
  const option = await selector.evaluate((element, expected) => {
    const values = [...element.options].map((item, index) => ({
      index,
      disabled: item.disabled,
      label: (item.textContent || "").replace(/\s+/g, " ").trim(),
    }));
    const wanted = expected.map(value => value.toLowerCase());
    return values.find(item => (
      !item.disabled
      && wanted.some(value => (
        item.label.toLowerCase().includes(value)
        || value.includes(item.label.toLowerCase())
      ))
    )) || values.find(item => !item.disabled) || null;
  }, expectedText).catch(() => null);
  if (!option) return null;
  await selector.selectOption({index: option.index}).catch(() => null);
  await panel.locator('[data-id="card"]').first().waitFor({
    state: "visible",
    timeout: 3_000,
  }).catch(() => null);
  return option.label;
}

async function openPanel(page, panel, input, directory) {
  const label = panel === "filtering" ? "Filtering" : "Styling";
  const dataId = panel === "filtering" ? "filter-drawer" : "style-drawer";
  const artifactKey = panel === "filtering" ? "filteringPanel" : "stylingPanel";
  const filename = panel === "filtering" ? "filtering-panel.png" : "styling-panel.png";
  const expectedText = Array.isArray(input.expectedPanelText)
    ? input.expectedPanelText.filter(value => typeof value === "string" && value)
    : [];
  const layer = await findRequestedLayer(page, input);
  if (!layer.locator) {
    return {
      panel,
      label,
      artifactKey,
      dataId,
      found: false,
      attempted: false,
      opened: false,
      captured: false,
      image: null,
      textLength: 0,
      textSample: "",
      expectedTextFound: Object.fromEntries(
        expectedText.map(value => [value, false]),
      ),
      missingExpectedText: expectedText,
      failureReason: "layer-not-found",
      passed: false,
    };
  }
  // Panel controls are nested in the layer drawer.  They may be present in
  // the DOM while that drawer is collapsed, so reveal the layer before
  // deciding whether its panel can be opened.
  const layerNavigation = await ensureDrawerOpen(layer.locator);
  if (!layerNavigation.opened) {
    return {
      panel,
      label,
      artifactKey,
      dataId,
      found: true,
      attempted: layerNavigation.attempted,
      opened: false,
      captured: false,
      image: null,
      textLength: 0,
      textSample: "",
      expectedTextFound: Object.fromEntries(
        expectedText.map(value => [value, false]),
      ),
      missingExpectedText: expectedText,
      failureReason: layerNavigation.failureReason,
      passed: false,
    };
  }
  const trigger = await locatorWithDataId(
    layer.locator,
    "[data-id]",
    dataId,
  );
  if (!trigger) {
    return {
      panel,
      label,
      artifactKey,
      dataId,
      found: false,
      attempted: false,
      opened: false,
      captured: false,
      image: null,
      textLength: 0,
      textSample: "",
      expectedTextFound: Object.fromEntries(
        expectedText.map(value => [value, false]),
      ),
      missingExpectedText: expectedText,
      failureReason: "panel-not-found",
      passed: false,
    };
  }
  const tagName = await trigger.evaluate(
    element => element.tagName.toLowerCase(),
  ).catch(() => "");
  let target = trigger;
  let navigation;
  if (tagName === "button") {
    const attempted = await trigger.isVisible().catch(() => false);
    if (attempted) {
      await trigger.click({timeout: 2_000}).catch(() => null);
    }
    target = await locatorWithDataId(
      page,
      "[data-id]",
      `${dataId}-dialog`,
    );
    const opened = Boolean(target)
      && await target.isVisible().catch(() => false);
    navigation = {
      attempted,
      opened,
      failureReason: opened ? null : "dialog-did-not-open",
    };
  } else {
    navigation = await ensureDrawerOpen(trigger);
  }
  if (navigation.opened && panel === "filtering") {
    await revealFirstFilteringControl(target, expectedText);
  }
  const panelText = navigation.opened && target
    ? (await target.innerText().catch(() => ""))
      .replace(/\s+/g, " ")
      .trim()
    : "";
  const expectedTextFound = Object.fromEntries(
    expectedText.map(value => [
      value,
      panelText.toLowerCase().includes(value.toLowerCase()),
    ]),
  );
  const missingExpectedText = Object.entries(expectedTextFound)
    .filter(([, found]) => !found)
    .map(([value]) => value);
  let image = null;
  if (navigation.opened && target) {
    const buffer = await target.screenshot({
      path: `${directory}/${filename}`,
    }).catch(() => null);
    image = buffer ? pngSize(buffer) : null;
  }
  const captured = Boolean(image);
  const failureReason = (
    navigation.failureReason
    || (!captured ? "panel-capture-failed" : null)
    || (missingExpectedText.length ? "expected-text-missing" : null)
  );
  return {
    panel,
    label,
    artifactKey,
    dataId,
    found: true,
    attempted: navigation.attempted,
    opened: navigation.opened,
    captured,
    image,
    textLength: panelText.length,
    textSample: panelText.slice(0, 2000),
    expectedTextFound,
    missingExpectedText,
    failureReason,
    passed: navigation.opened
      && captured
      && missingExpectedText.length === 0,
  };
}

async function interactionText(page) {
  return (await page.locator("body").innerText({timeout: 2_000}))
    .replace(/\s+/g, " ")
    .trim();
}

async function exerciseHover(page, input, directory, timeout) {
  const configured = input.plan?.hover?.type === "hover-centre-feature";
  const expectedText = Array.isArray(input.expectedHoverText)
    ? input.expectedHoverText.filter(value => typeof value === "string" && value)
    : [];
  const requested = input.hover !== false
    && (input.hover === true || expectedText.length > 0 || configured);
  const result = {
    requested,
    configured,
    suppressed: input.hover === false,
    attempted: false,
    opened: false,
    point: null,
    field: input.plan?.hover?.field ?? null,
    title: input.plan?.hover?.title ?? null,
    text: null,
    textLength: 0,
    image: null,
    expectedText,
    expectedTextFound: Object.fromEntries(
      expectedText.map(value => [value, false]),
    ),
    passed: !requested,
  };
  if (!requested) return result;

  const mapCanvas = page.locator("canvas").first();
  const box = await mapCanvas.boundingBox().catch(() => null);
  if (!box) {
    result.reason = "The map canvas was unavailable.";
    return result;
  }
  const hoverPoint = {
    x: Math.round(box.x + box.width / 2),
    y: Math.round(box.y + box.height / 2),
  };
  result.point = hoverPoint;
  result.attempted = true;
  await page.mouse.move(
    Math.round(box.x + Math.min(box.width / 4, 100)),
    Math.round(box.y + Math.min(box.height / 4, 100)),
  );
  await page.mouse.move(hoverPoint.x, hoverPoint.y, {steps: 12});

  const tooltip = page.locator(".infotip").last();
  await tooltip.waitFor({
    state: "visible",
    timeout: Math.min(timeout, 10_000),
  }).catch(() => {});
  await page.waitForTimeout(400);
  result.opened = await tooltip.evaluate(element => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return (
      style.display !== "none"
      && style.visibility !== "hidden"
      && Number.parseFloat(style.opacity || "1") > 0
      && box.width > 0
      && box.height > 0
    );
  }).catch(() => false);
  let observedText = "";
  if (result.opened) {
    observedText = (await tooltip.innerText().catch(() => ""))
      .replace(/\s+/g, " ")
      .trim();
    result.text = observedText.slice(0, 2000);
    result.textLength = observedText.length;
    const image = await tooltip.screenshot({
      path: `${directory}/hover-tooltip.png`,
    }).catch(() => null);
    result.image = image ? pngSize(image) : null;
  }
  result.expectedTextFound = Object.fromEntries(
    expectedText.map(value => [
      value,
      Boolean(observedText)
        && observedText.toLowerCase().includes(value.toLowerCase()),
    ]),
  );
  result.passed = Boolean(
    configured
    && result.attempted
    && result.opened
    && observedText
    && result.image
    && Object.values(result.expectedTextFound).every(Boolean)
  );
  if (!result.passed) {
    result.reason = !configured
      ? "The selected layer has no active hover configuration."
      : !result.opened
        ? "No visible hover tooltip was observed."
        : !observedText
          ? "The hover tooltip contained no observable text."
          : !result.image
            ? "The hover tooltip artifact was not captured."
            : "Expected hover text was absent.";
  }
  return result;
}

async function evaluatePluginChecks(page, requested, report) {
  const checks = [];
  for (const plugin of Array.isArray(requested) ? requested : []) {
    const registration = await page.evaluate(
      key => typeof globalThis.mapp?.plugins?.[key] === "function",
      plugin.registrationKey,
    ).catch(() => false);
    const assertions = [];
    for (const assertion of Array.isArray(plugin.assertions) ? plugin.assertions : []) {
      let passed = false;
      let observed = null;
      if (assertion.type === "registration") {
        passed = registration;
        observed = registration;
      } else if (assertion.type === "selector-exists" || assertion.type === "selector-visible") {
        const locator = page.locator(assertion.selector).first();
        const count = await locator.count().catch(() => 0);
        observed = assertion.type === "selector-visible"
          ? await locator.isVisible().catch(() => false)
          : count > 0;
        passed = Boolean(observed);
      } else if (assertion.type === "layer-dispatch" || assertion.type === "locale-dispatch") {
        // Dispatch has no generic XYZ completion hook. Registration plus any
        // declared observable selector is the bounded platform-owned evidence.
        const selectors = plugin.assertions.filter(item => item.type?.startsWith("selector-"));
        passed = registration && selectors.length > 0;
        observed = {registered: registration, observableAssertions: selectors.length};
      } else if (assertion.type === "no-plugin-console-errors") {
        const tokens = [plugin.id, plugin.registrationKey, plugin.entryUrl].filter(Boolean);
        const messages = [
          ...report.console.filter(item => item.type === "error").map(item => item.text),
          ...report.pageErrors,
          ...report.failedRequests.map(item => `${item.url} ${item.error || ""}`),
        ];
        const matched = messages.filter(message => tokens.some(token => message.includes(token)));
        passed = matched.length === 0;
        observed = matched;
      }
      assertions.push({type: assertion.type, passed, observed});
    }
    checks.push({
      id: plugin.id,
      entryUrl: plugin.entryUrl,
      entryHash: plugin.entryHash,
      registrationKey: plugin.registrationKey,
      registered: registration,
      assertions,
      passed: registration && assertions.every(assertion => assertion.passed),
    });
  }
  return checks;
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
  const runTimeout = Math.min(
    180_000,
    Math.max(10_000, Number(input.runTimeout) || defaultRunTimeout),
  );
  const deadline = Date.now() + runTimeout;
  let currentStage = "artifact-persistence";
  const runStage = async (stage, task) => {
    currentStage = stage;
    const remaining = deadline - Date.now();
    return boundedStage(stage, remaining, task);
  };
  const panels = requestedPanels(input);
  const report = {
    runId,
    layer: input.layer,
    layers: requestedLayers,
    viewMode,
    groups: Array.isArray(input.plan?.activeGroups)
      ? input.plan.activeGroups
      : [],
    panels,
    plan: input.plan,
    metadata: input.metadata,
    url: target.toString(),
    console: [],
    pageErrors: [],
    failedRequests: [],
    passed: false,
  };
  const recordFailure = error => {
    const timedOut = (
      error instanceof VisualStageTimeout
      || error?.name === "TimeoutError"
    );
    report.passed = false;
    report.error = error?.message || "Visual runner failed.";
    report.code = error?.code || (
      timedOut ? "visual.run_timeout" : "visual.browser_stage_failed"
    );
    report.failedStage = error?.failedStage || currentStage;
    if (timedOut) {
      report.timeoutMilliseconds = (
        error?.timeoutMilliseconds ?? Math.min(timeout, runTimeout)
      );
    }
    report.diagnostics = {
      ...browserDiagnostics(report, report.failedStage),
      exception: {
        name: error?.name || "Error",
        message: report.error,
      },
    };
  };

  let browser = null;
  try {
    await runStage(
      "artifact-persistence",
      () => mkdir(directory, {recursive: false, mode: 0o700}),
    );
    browser = await runStage("browser-launch", () => chromium.launch({
      headless: true,
      timeout: Math.max(1, deadline - Date.now()),
      ...(browserProxyServer ? {
        proxy: {
          server: browserProxyServer,
          ...(browserProxyBypass ? {bypass: browserProxyBypass} : {}),
        },
      } : {}),
    }));
    let page;
    let response;
    await runStage("page-readiness", async () => {
      page = await browser.newPage({viewport, deviceScaleFactor});
      const configuredLocaleResponses = [];
      page.on("console", message => report.console.push({
        type: message.type(),
        text: message.text(),
      }));
      page.on("pageerror", error => report.pageErrors.push(error.message));
      page.on("requestfailed", request => report.failedRequests.push({
        url: redactUrl(request.url()),
        error: request.failure()?.errorText,
      }));
      page.on("response", browserResponse => {
        const responseUrl = new URL(browserResponse.url());
        if (
          responseUrl.pathname.endsWith("/api/workspace/locale")
          && responseUrl.searchParams.get("layers") === "true"
        ) {
          configuredLocaleResponses.push(
            browserResponse.json().catch(() => null),
          );
        }
      });
      response = await page.goto(target.toString(), {
        waitUntil: "networkidle",
        timeout: Math.min(timeout, Math.max(1, deadline - Date.now())),
      });
      const configuredLocales = (await Promise.all(configuredLocaleResponses))
        .filter(locale => locale && Array.isArray(locale.layers));
      report.httpStatus = response?.status();
      report.canvasCount = await page.locator("canvas").count();
      const layerText = input.layerTitle || input.layer;
      report.layerTextFound = viewMode === "default"
        ? true
        : layerText
        ? await page.getByText(layerText, {exact: false}).count() > 0
        : true;
      report.groupTextFound = (
        await Promise.all(
          report.groups.map(
            group => page.getByText(group, {exact: true}).count(),
          ),
        )
      ).every(count => count > 0);
      const configuredLocale = configuredLocales.at(-1) || null;
      report.activationDiagnostics = await page.evaluate(({configured, planned}) => {
        const configuredLayers = Array.isArray(configured?.layers)
          ? configured.layers
          : [];
        const groupMembership = Object.fromEntries(configuredLayers.map(layer => [
          layer.key,
          layer.group || null,
        ]));
        const registeredLayerKeys = [...document.querySelectorAll(
          ".drawer.layer-view[data-id]",
        )].map(element => element.dataset.id);
        const resolvedUrlLayerKeys = Array.isArray(
          globalThis.mapp?.hooks?.current?.layers,
        ) ? [...globalThis.mapp.hooks.current.layers] : [];
        const registeredGroups = Object.fromEntries(
          [...document.querySelectorAll(".drawer.layer-group[data-id]")].map(group => [
            group.dataset.id,
            [...group.querySelectorAll(".drawer.layer-view[data-id]")]
              .map(layer => layer.dataset.id),
          ]),
        );
        return {
          configuredCandidateLayerKeys: Array.isArray(planned?.configuredLayerKeys)
            ? planned.configuredLayerKeys
            : configuredLayers.map(layer => layer.key),
          resolvedLocaleLayerKeys: configuredLayers.map(layer => layer.key),
          resolvedUrlLayerKeys,
          groupMembership: planned?.groupMembership || groupMembership,
          registeredLayerKeys,
          registeredGroups,
          finalActiveOpenLayersLayerSet: resolvedUrlLayerKeys.filter(
            key => registeredLayerKeys.includes(key),
          ),
        };
      }, {
        configured: configuredLocale,
        planned: input.plan?.candidateLayerDiagnostics || null,
      });
      const requestedSet = new Set(activeLayers);
      const diagnostics = report.activationDiagnostics;
      report.activationDiagnostics.requestedLayersRegistered = activeLayers.every(
        key => diagnostics.registeredLayerKeys.includes(key),
      );
      report.activationDiagnostics.requestedLayersActive = activeLayers.every(
        key => diagnostics.finalActiveOpenLayersLayerSet.includes(key),
      );
      report.activationDiagnostics.unexpectedActiveLayers = (
        diagnostics.finalActiveOpenLayersLayerSet.filter(key => !requestedSet.has(key))
      );
      report.activationDiagnostics.activationRequired = (
        viewMode === "focus" && activeLayers.length > 0
      );
      report.activationDiagnostics.activationPassed = (
        !report.activationDiagnostics.activationRequired
        || (
          report.activationDiagnostics.requestedLayersRegistered
          && report.activationDiagnostics.requestedLayersActive
          && report.activationDiagnostics.unexpectedActiveLayers.length === 0
        )
      );
    });
    await runStage("screenshot-capture", async () => {
      if (Array.isArray(input.pluginChecks) && input.pluginChecks.length) {
      report.pluginNavigation = await expandRequestedLayer(page, input);
      }
      report.plugins = await evaluatePluginChecks(page, input.pluginChecks, report);
      const beforeCapture = await captureMap(page, directory, "before", fullPage);
      report.hover = await exerciseHover(page, input, directory, timeout);
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
      const expectedLayer = input.plan.interaction.expectedLayerTitle
        || input.plan.interaction.expectedLayer
        || input.layerTitle
        || input.layer
        || "";
      const expectedFeatureId = input.plan.interaction.expectedFeatureId;
      const infoPanelExpanded = await infoPanel.isVisible().catch(() => false);
      const infoPanelText = infoPanelExpanded
        ? (await infoPanel.innerText().catch(() => ""))
          .replace(/\s+/g, " ")
          .trim()
        : "";
      const expectedInfoPanelText = Array.isArray(
        input.plan.interaction.expectedInfoPanelText,
      )
        ? input.plan.interaction.expectedInfoPanelText.filter(
          value => typeof value === "string" && value,
        )
        : [];
      const expectedInfoPanelTextFound = Object.fromEntries(
        expectedInfoPanelText.map(value => [
          value,
          infoPanelText.toLowerCase().includes(value.toLowerCase()),
        ]),
      );
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
        expectedInfoPanelText,
        expectedInfoPanelTextFound,
        infoPanelTextLength: infoPanelText.length,
        infoPanelTextSample: infoPanelText.slice(0, 2000),
        expectedLayerFound: expectedLayer
          ? afterText.toLowerCase().includes(String(expectedLayer).toLowerCase())
          : true,
        expectedFeatureIdFound: expectedFeatureId == null
          ? null
          : afterText.includes(String(expectedFeatureId)),
        beforeTextLength: beforeText.length,
        afterTextLength: afterText.length,
        afterTextSample: afterText.slice(0, 2000),
        passed: (
          afterText !== beforeText
          && (
            !input.plan.interaction.requireInfoPanel
            || infoPanelExpanded
          )
          && Object.values(expectedInfoPanelTextFound).every(Boolean)
        ),
      };
      }
      report.panelNavigation = panels.length
      ? await expandRequestedLayer(page, input)
      : null;
    report.panels = {};
    for (const panel of panels) {
      report.panels[panel] = await openPanel(page, panel, input, directory);
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
      ? report.interaction.passed
      : true;
    const hoverPassed = report.hover ? report.hover.passed : true;
    const panelsPassed = panels.every(panel => report.panels?.[panel]?.passed);
    const pluginsPassed = report.plugins.every(plugin => plugin.passed);
      report.passed = Boolean(
      response?.ok()
      && report.canvasCount
      && report.layerTextFound
      && report.groupTextFound
      && report.activationDiagnostics?.activationPassed
      && hoverPassed
      && interactionPassed
      && panelsPassed
      && pluginsPassed
      && !report.pageErrors.length
      );
    });
  } catch (error) {
    recordFailure(error);
  } finally {
    if (browser) {
      try {
        await boundedStage(
          "browser-close",
          cleanupTimeout,
          () => browser.close(),
        );
      } catch (error) {
        if (!report.code) recordFailure(error);
        report.diagnostics = {
          ...(report.diagnostics || browserDiagnostics(report, currentStage)),
          browserCloseError: error.message,
        };
      }
      if (report.diagnostics) {
        report.diagnostics = {
          ...browserDiagnostics(report, report.failedStage || currentStage),
          ...report.diagnostics,
          console: report.console.slice(-50),
          pageErrors: report.pageErrors.slice(-50),
          failedRequests: report.failedRequests.slice(-50),
        };
      }
    }
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
        id: "visual.layer_activation",
        passed: report.activationDiagnostics?.activationPassed === true,
        observed: report.activationDiagnostics ?? null,
      },
      {
        id: "visual.page_errors",
        passed: report.pageErrors.length === 0,
        observed: report.pageErrors.length,
      },
      {
        id: "visual.hover",
        passed: report.hover ? report.hover.passed : true,
        observed: report.hover ?? null,
      },
      {
        id: "visual.feature_interaction",
        passed: report.interaction ? report.interaction.passed : true,
        observed: report.interaction,
      },
      ...panels.map(panel => ({
        id: `visual.panel.${panel}`,
        passed: report.panels?.[panel]?.passed === true,
        observed: report.panels?.[panel] ?? null,
      })),
      ...(report.plugins || []).map(plugin => ({
        id: `visual.plugin.${plugin.id}`,
        passed: plugin.passed,
        observed: plugin,
      })),
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
                : report.hover && !report.hover.passed
                  ? "hover"
                  : report.interaction && !report.interaction.passed
                  ? "feature"
                  : panels.some(panel => !report.panels?.[panel]?.passed)
                    ? "panel"
                  : (report.plugins || []).some(plugin => !plugin.passed)
                    ? "plugin"
                  : report.pageErrors.length
                    ? "page"
                    : "unknown",
  };
  try {
    const persistenceTimeout = Math.max(
      1,
      report.code === "visual.run_timeout"
        ? cleanupTimeout
        : deadline - Date.now(),
    );
    await boundedStage("artifact-persistence", persistenceTimeout, async () => {
      const retainedArtifact = async filename => {
        try {
          await access(`${directory}/${filename}`);
          return `${runId}/${filename}`;
        } catch {
          return null;
        }
      };
      report.artifacts = {
        report: `${runId}/report.json`,
        page: await retainedArtifact("page.png"),
        map: await retainedArtifact("map.png"),
        beforePage: await retainedArtifact("before-page.png"),
        beforeMap: await retainedArtifact("before-map.png"),
        afterPage: await retainedArtifact("after-page.png"),
        afterMap: await retainedArtifact("after-map.png"),
        infoPanel: await retainedArtifact("info-panel.png"),
        hoverTooltip: await retainedArtifact("hover-tooltip.png"),
        filteringPanel: await retainedArtifact("filtering-panel.png"),
        stylingPanel: await retainedArtifact("styling-panel.png"),
      };
      await writeFile(
        `${directory}/report.json`,
        JSON.stringify(report, null, 2),
        {mode: 0o600},
      );
    });
  } catch (error) {
    report.artifacts = {
      ...(report.artifacts || {}),
      report: null,
    };
    error.code = error.code === "visual.run_timeout"
      ? "visual.artifact_persistence_timeout"
      : "visual.artifact_persistence_failed";
    error.failedStage = "artifact-persistence";
    recordFailure(error);
  }
  return report;
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
  let input;
  try {
    input = await readJson(req);
  } catch (error) {
    const status = error instanceof RequestError ? error.status : 500;
    return json(res, status, {
      error: status === 500 ? "Visual runner failed." : error.message,
    });
  }
  if (activeRuns >= maxConcurrentRuns) {
    res.setHeader("retry-after", "5");
    return json(res, 429, {
      error: "Visual runner is at its concurrency limit. Retry later.",
      metadata: input.metadata,
      state: "rejected",
    });
  }
  activeRuns += 1;
  try {
    const report = await runVisual(input);
    const status = report.code === "visual.run_timeout"
      || report.code === "visual.artifact_persistence_timeout"
      ? 504
      : report.code === "visual.artifact_persistence_failed"
        ? 500
        : report.passed
          ? 200
          : 422;
    return json(res, status, report);
  } catch (error) {
    const status = error instanceof RequestError ? error.status : 500;
    return json(res, status, {
      error: status === 500 ? "Visual runner failed." : error.message,
      metadata: input.metadata,
      state: "rejected",
    });
  } finally {
    activeRuns -= 1;
  }
}).listen(port, "0.0.0.0");
