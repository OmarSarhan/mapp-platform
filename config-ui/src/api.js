export class ApiError extends Error {
  constructor(message, {status, payload, details} = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
    this.details = details;
  }
}

export async function requestJson(path, options = {}, {csrfToken = '', fetchImpl = globalThis.fetch} = {}) {
  const headers = {...(options.headers || {})};
  if (options.method && options.method !== 'GET' && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  const response = await fetchImpl(path, {...options, headers});
  const payload = await response.json().catch(() => ({error: response.statusText}));
  if (!response.ok) {
    const message = payload?.error || payload?.message || response.statusText || `HTTP ${response.status}`;
    throw new ApiError(message, {
      status: response.status,
      payload,
      details: payload?.errors,
    });
  }
  return payload;
}

export function savedWorkspaceFromError(error) {
  const payload = error?.payload;
  if (
    payload?.saved !== true
    || !payload.workspace
    || payload.revision === undefined
    || payload.revision === null
  ) {
    return null;
  }
  return {
    workspace: payload.workspace,
    revision: payload.revision,
    dirty: false,
  };
}

export function confirmedWorkspaceReload(payload) {
  const fingerprint = payload?.fingerprint;
  const reload = payload?.reload;
  const status = reload?.status;
  return (
    payload?.saved === true
    && typeof fingerprint === 'string'
    && /^[0-9a-f]{64}$/.test(fingerprint)
    && reload?.expectedWorkspaceFingerprint === fingerprint
    && Number.isInteger(reload?.requestedGeneration)
    && reload.requestedGeneration >= 0
    && status?.completed === true
    && status.healthy === true
    && Number.isInteger(status.appliedGeneration)
    && status.appliedGeneration >= reload.requestedGeneration
    && status.workspaceFingerprint === fingerprint
  );
}

export function workspaceSaveStatus(phase, errors = []) {
  const statuses = {
    restarting: {
      kind: 'pending',
      message: 'Saving workspace and restarting XYZ…',
    },
    ready: {
      kind: 'success',
      message: 'Workspace saved. XYZ restarted and is ready for connections with this workspace.',
    },
    incomplete: {
      kind: 'error',
      message: 'Workspace saved. XYZ is still restarting or its readiness could not be confirmed.',
    },
    failed: {
      kind: 'error',
      message: 'Workspace was not saved.',
    },
    unknown: {
      kind: 'error',
      message: 'Save outcome could not be confirmed. Reload the workspace before retrying.',
    },
  };
  return {...statuses[phase], ...(errors.length ? {errors} : {})};
}

export function workspaceSaveFailurePhase(error) {
  if (savedWorkspaceFromError(error)) return 'incomplete';
  const status = error?.status;
  if (
    error?.payload?.saved === false
    || (
      Number.isInteger(status)
      && status >= 400
      && status < 500
      && status !== 408
    )
  ) {
    return 'failed';
  }
  return 'unknown';
}

export function mergeLocale(base, override) {
  if (
    !base || typeof base !== 'object' || Array.isArray(base)
    || !override || typeof override !== 'object' || Array.isArray(override)
  ) {
    return structuredClone(override);
  }
  const merged = structuredClone(base);
  for (const [key, value] of Object.entries(override)) {
    if (Array.isArray(merged[key]) && Array.isArray(value)) {
      // Match XYZ mod/utils/merge.js: a source array replaces the target when
      // all of its entries are already present, otherwise it is appended.
      merged[key] = value.every(item => merged[key].includes(item))
        ? structuredClone(value)
        : [...structuredClone(merged[key]), ...structuredClone(value)];
    } else {
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        if (!merged[key]) merged[key] = {};
        if (
          merged[key] && typeof merged[key] === 'object'
          && !Array.isArray(merged[key])
        ) {
          merged[key] = mergeLocale(merged[key], value);
        }
      } else {
        merged[key] = structuredClone(value);
      }
    }
  }
  return merged;
}

export function renderedLocales(workspace) {
  if (!workspace || typeof workspace !== 'object' || Array.isArray(workspace)) {
    return [];
  }
  const named = workspace?.locales;
  const runtimeBase = (
    workspace.locale && typeof workspace.locale === 'object'
    && !Array.isArray(workspace.locale)
  ) ? workspace.locale : {layers: {}};
  const rendered = [['locale', structuredClone(runtimeBase)]];
  if (named && typeof named === 'object' && !Array.isArray(named)) {
    rendered.push(...Object.entries(named)
      .filter(([key, value]) => (
        key !== 'locale'
        && value && typeof value === 'object' && !Array.isArray(value)
      ))
      .map(([key, value]) => [
        key,
        mergeLocale(runtimeBase, value),
      ]));
  }
  return rendered;
}

export function activeLocale(workspace) {
  const first = renderedLocales(workspace)[0];
  return first ? {key: first[0], value: first[1]} : {key: 'locale', value: undefined};
}
