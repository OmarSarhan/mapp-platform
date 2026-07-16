import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ApiError,
  activeLocale,
  mergeLocale,
  renderedLocales,
  requestJson,
  savedWorkspaceFromError,
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
