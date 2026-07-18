import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiError,
  activeLocale,
  confirmedWorkspaceReload,
  mergeLocale,
  renderedLocales,
  requestJson,
  savedWorkspaceFromError,
  workspaceSaveFailurePhase,
  workspaceSaveStatus,
} from './api.js';

test('requestJson retains a parsed error payload', async () => {
  const payload = {
    error: 'Workspace saved, but XYZ reload did not complete.',
    saved: true,
    workspace: {key: 'demo'},
    revision: 0,
    reload: {completed: false},
  };

  await assert.rejects(
    requestJson('/api/workspace', {method: 'POST'}, {
      fetchImpl: async () => new Response(JSON.stringify(payload), {
        status: 504,
        headers: {'Content-Type': 'application/json'},
      }),
    }),
    error => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 504);
      assert.deepEqual(error.payload, payload);
      assert.deepEqual(savedWorkspaceFromError(error), {
        workspace: payload.workspace,
        revision: 0,
        dirty: false,
      });
      assert.equal(error.message, 'Workspace saved, but XYZ reload did not complete.');
      return true;
    },
  );
});

test('savedWorkspaceFromError rejects an ordinary failed save', () => {
  assert.equal(savedWorkspaceFromError(new ApiError('Validation failed.', {
    status: 422,
    payload: {saved: false, workspace: {key: 'demo'}, revision: 'rev-2'},
  })), null);
});

test('workspace save status distinguishes restart progress and readiness', () => {
  assert.deepEqual(workspaceSaveStatus('restarting'), {
    kind: 'pending',
    message: 'Saving workspace and restarting XYZ…',
  });
  assert.deepEqual(workspaceSaveStatus('ready'), {
    kind: 'success',
    message: 'Workspace saved. XYZ restarted and is ready for connections with this workspace.',
  });

  const errors = [{path: 'server', message: 'Timed out waiting for XYZ.'}];
  assert.deepEqual(workspaceSaveStatus('incomplete', errors), {
    kind: 'error',
    message: 'Workspace saved. XYZ is still restarting or its readiness could not be confirmed.',
    errors,
  });
  assert.deepEqual(workspaceSaveStatus('failed', errors), {
    kind: 'error',
    message: 'Workspace was not saved.',
    errors,
  });
  assert.deepEqual(workspaceSaveStatus('unknown', errors), {
    kind: 'error',
    message: 'Save outcome could not be confirmed. Reload the workspace before retrying.',
    errors,
  });
});

test('workspace save failure phase distinguishes definite and ambiguous outcomes', () => {
  assert.equal(workspaceSaveFailurePhase(new ApiError('Validation failed.', {
    status: 422,
    payload: {errors: [{path: '/key', message: 'Required.'}]},
  })), 'failed');
  assert.equal(workspaceSaveFailurePhase(new ApiError('Reload timed out.', {
    status: 504,
    payload: {
      saved: true,
      workspace: {key: 'demo'},
      revision: 'rev-2',
    },
  })), 'incomplete');
  assert.equal(workspaceSaveFailurePhase(new TypeError('Network failed.')), 'unknown');
  assert.equal(workspaceSaveFailurePhase(new ApiError('Gateway timeout.', {
    status: 504,
    payload: {},
  })), 'unknown');
});

test('confirmedWorkspaceReload requires fingerprint-matched healthy readiness', () => {
  const fingerprint = 'a'.repeat(64);
  const payload = {
    saved: true,
    fingerprint,
    reload: {
      expectedWorkspaceFingerprint: fingerprint,
      requestedGeneration: 4,
      status: {
        appliedGeneration: 4,
        completed: true,
        healthy: true,
        workspaceFingerprint: fingerprint,
      },
    },
  };
  assert.equal(confirmedWorkspaceReload(payload), true);
  assert.equal(confirmedWorkspaceReload({
    ...payload,
    reload: {
      ...payload.reload,
      status: {...payload.reload.status, workspaceFingerprint: 'b'.repeat(64)},
    },
  }), false);
  assert.equal(confirmedWorkspaceReload({
    ...payload,
    reload: {
      ...payload.reload,
      status: {...payload.reload.status, completed: false},
    },
  }), false);
  assert.equal(confirmedWorkspaceReload({saved: true}), false);
});

test('activeLocale identifies the locale currently rendered by the dashboard', () => {
  const defaultLocale = {name: 'Default'};
  assert.deepEqual(activeLocale({locale: defaultLocale}), {
    key: 'locale',
    value: defaultLocale,
  });

  const namedLocale = {name: 'Leeds'};
  assert.deepEqual(activeLocale({locales: {leeds: namedLocale, york: {name: 'York'}}}), {
    key: 'locale',
    value: {layers: {}},
  });
  assert.deepEqual(
    renderedLocales({locales: {leeds: namedLocale}}),
    [
      ['locale', {layers: {}}],
      ['leeds', {layers: {}, name: 'Leeds'}],
    ],
  );

  const workspace = {
    locale: {
      layers: {
        Stops: {format: 'mvt', name: 'Stops', style: {default: {strokeWidth: 2}}},
      },
    },
    locales: {
      cy: {layers: {Stops: {name: 'Safleoedd', style: {default: {strokeColor: '#123456'}}}}},
    },
  };
  assert.deepEqual(renderedLocales(workspace), [[
    'locale',
    workspace.locale,
  ], [
    'cy',
    {
      layers: {
        Stops: {
          format: 'mvt',
          name: 'Safleoedd',
          style: {default: {strokeWidth: 2, strokeColor: '#123456'}},
        },
      },
    },
  ]]);
});

test('mergeLocale matches XYZ array composition rules', () => {
  assert.deepEqual(
    mergeLocale(
      {controls: ['zoom', 'scale'], infoj: [{field: 'name'}]},
      {controls: ['scale'], infoj: [{field: 'name'}]},
    ),
    {
      controls: ['scale'],
      infoj: [{field: 'name'}, {field: 'name'}],
    },
  );
  assert.deepEqual(
    mergeLocale(
      {truthy: 'keep', array: [1], falsy: ''},
      {
        truthy: {ignored: true},
        array: {ignored: true},
        falsy: {added: true},
      },
    ),
    {
      truthy: 'keep',
      array: [1],
      falsy: {added: true},
    },
  );
});
