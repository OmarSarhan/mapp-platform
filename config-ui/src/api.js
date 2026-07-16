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
