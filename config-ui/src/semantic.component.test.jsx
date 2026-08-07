import React from 'react';
import {afterEach, describe, expect, test, vi} from 'vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';

import {
  SemanticCatalog,
  hasGenerationDataPermission,
  hasGenerationPermission,
  hasSourcePermission,
  semanticProposalPayload,
} from './semantic.jsx';

const asset = {
  id: 'derived:derived_layers.walkability',
  version: 3,
  status: 'ready',
  generated: {
    qualifiedName: 'derived_layers.walkability',
    fields: [{id: 'field:score', name: 'score', type: 'numeric'}],
  },
  curated: {displayName: 'Walkability', description: 'Current score'},
};

const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return {promise, resolve, reject};
};

const firstPage = path => `${path}?limit=100`;
const pagePathForTest = (path, cursor) => (
  `${firstPage(path)}&cursor=${encodeURIComponent(cursor)}`
);

function fieldGenerationResult(catalogAsset, fieldId, contextOptions) {
  return {
    draft: {
      assetId: catalogAsset.id,
      baseVersion: catalogAsset.version,
      target: {kind: 'field', fieldId},
      operations: [{
        op: 'set',
        path: `/curated/fields/${fieldId}/description`,
        value: `Description for ${fieldId}`,
      }],
    },
    generation: {
      provider: 'gemini',
      model: 'gemini-2.5-flash',
      metadataOnly: !(contextOptions.sampleRows || contextOptions.statistics),
      contextOptions,
      proposalCreated: false,
    },
  };
}

function fieldBatchApi(catalogAsset, onGenerate, contextOptions = undefined) {
  return vi.fn(async (path, options = {}) => {
    if (path === '/api/semantic/status') {
      return {
        catalogRevision: 7,
        schemaVersion: 2,
        capabilities: {
          generation: {
            available: true,
            provider: 'gemini',
            model: 'gemini-2.5-flash',
            targets: ['field'],
            metadataOnly: true,
            ...(contextOptions ? {contextOptions} : {}),
          },
        },
      };
    }
    if (path === firstPage('/api/semantic/catalog')) {
      return {assets: [catalogAsset], catalogRevision: 7};
    }
    if (path === firstPage('/api/semantic/source/relations')) return {relations: []};
    if (path === firstPage('/api/semantic/derived-profiles')) {
      return {derivedProfiles: [], catalogRevision: 7};
    }
    if (path === firstPage('/api/semantic/proposals') && !options.method) {
      return {proposals: []};
    }
    if (path === '/api/semantic/generate' && options.method === 'POST') {
      return onGenerate(JSON.parse(options.body));
    }
    throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
  });
}

async function openGenerationStep() {
  const button = await screen.findByRole('button', {name: '2. Generate'});
  await waitFor(() => expect(button.disabled).toBe(false));
  fireEvent.click(button);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('semantic catalog proposal payloads', () => {
  test('bind curated changes to the selected asset version', () => {
    expect(semanticProposalPayload(
      asset,
      '{"displayName":"Walking score"}',
      'Clearer language',
    )).toEqual({
      assetId: asset.id,
      baseVersion: 3,
      operations: [{
        op: 'set',
        path: '/curated',
        value: {displayName: 'Walking score'},
      }],
      explanation: 'Clearer language',
    });
  });

  test('rejects non-object curated metadata', () => {
    expect(() => semanticProposalPayload(asset, '[]')).toThrow(
      'Curated metadata must be a JSON object.',
    );
  });
});

describe('semantic generation access matrix', () => {
  test('requires inspect and generate unless access is admin or full', () => {
    const narrowScopes = [
      'inspect',
      'propose',
      'visual',
      'apply',
      'reload',
      'derive',
      'semantic:inspect',
      'semantic:source',
      'semantic:generate',
      'semantic:propose',
      'semantic:apply',
      'semantic:admin',
    ];

    for (const scope of narrowScopes) {
      expect(hasGenerationPermission({
        actor: `token:${scope}`,
        scopes: [scope],
      })).toBe(false);
    }
    expect(hasGenerationPermission({
      actor: 'token:author',
      scopes: ['semantic:inspect', 'semantic:generate'],
    })).toBe(true);
    expect(hasGenerationPermission({
      actor: 'token:wrong-combination',
      scopes: ['semantic:generate', 'semantic:propose'],
    })).toBe(false);
    expect(hasGenerationPermission({
      actor: 'token:full',
      scopes: ['full'],
    })).toBe(true);
    expect(hasGenerationPermission({
      actor: 'admin',
      scopes: [],
    })).toBe(true);
    expect(hasGenerationPermission({
      actor: 'token:malformed',
      scopes: 'semantic:inspect,semantic:generate',
    })).toBe(false);
    expect(hasGenerationPermission()).toBe(false);
  });

  test('requires semantic:data for optional samples and statistics', () => {
    expect(hasGenerationDataPermission({
      actor: 'token:author',
      scopes: ['semantic:inspect', 'semantic:generate'],
    })).toBe(false);
    expect(hasGenerationDataPermission({
      actor: 'token:data-author',
      scopes: ['semantic:data'],
    })).toBe(true);
    expect(hasGenerationDataPermission({
      actor: 'token:full',
      scopes: ['full'],
    })).toBe(true);
    expect(hasGenerationDataPermission({actor: 'admin', scopes: []})).toBe(true);
    expect(hasGenerationDataPermission()).toBe(false);
  });
});

describe('semantic target selection', () => {
  test('clears curated metadata when the table or field target changes', async () => {
    const otherAsset = {
      ...asset,
      id: 'source:leeds.roads',
      version: 1,
      curated: {},
      generated: {
        qualifiedName: 'leeds.roads',
        fields: [
          {id: 'field:geom', name: 'geom', type: 'geometry'},
          {id: 'field:length', name: 'length', type: 'numeric'},
        ],
      },
    };
    const api = vi.fn(async path => {
      if (path === '/api/semantic/status') {
        return {
          catalogRevision: 7,
          schemaVersion: 1,
          capabilities: {generation: {
            available: true,
            provider: 'gemini',
            model: 'gemini-2.5-flash',
            targets: ['field'],
            metadataOnly: true,
            contextOptions: {
              sampleRows: {
                available: true,
                percent: 5,
                maxRows: 100,
                maxBytes: 98304,
                requiredScope: 'semantic:data',
              },
              statistics: {
                available: true,
                requiredScope: 'semantic:data',
              },
            },
          }},
        };
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [asset, otherAsset], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals')) return {proposals: []};
      throw new Error(`Unexpected request: ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{
        scopes: ['semantic:inspect', 'semantic:generate', 'semantic:data'],
      }}
    />);
    const editor = await screen.findByLabelText('Curated metadata JSON');
    await openGenerationStep();
    fireEvent.click(screen.getByText('What Gemini receives'));
    const sampleRows = screen.getByRole('checkbox', {
      name: /5% sample of row data \(capped\)/i,
    });
    fireEvent.click(sampleRows);
    fireEvent.change(editor, {target: {value: '{"displayName":"Stale"}'}});
    fireEvent.click(screen.getByRole('button', {name: /leeds\.roads/}));
    expect(editor.value).toBe('');
    expect(sampleRows.checked).toBe(false);

    fireEvent.change(editor, {target: {value: '{"displayName":"Stale"}'}});
    fireEvent.click(await screen.findByRole('button', {name: /^Specific fields/}));
    fireEvent.click(screen.getByRole('checkbox', {name: 'length'}));
    expect(editor.value).toBe('');
  });
});

describe('semantic source access matrix', () => {
  test('requires inspect and source unless access is admin or full', () => {
    expect(hasSourcePermission({
      actor: 'token:source',
      scopes: ['semantic:source'],
    })).toBe(false);
    expect(hasSourcePermission({
      actor: 'token:reader',
      scopes: ['semantic:inspect'],
    })).toBe(false);
    expect(hasSourcePermission({
      actor: 'token:source-author',
      scopes: ['semantic:inspect', 'semantic:source'],
    })).toBe(true);
    expect(hasSourcePermission({
      actor: 'token:full',
      scopes: ['full'],
    })).toBe(true);
    expect(hasSourcePermission({actor: 'admin', scopes: []})).toBe(true);
  });
});

describe('SemanticCatalog', () => {
  test('uses bounded first pages and isolated user-driven continuations', async () => {
    const secondAsset = {
      ...asset,
      id: 'source:leeds.roads',
      generated: {qualifiedName: 'leeds.roads', fields: []},
      curated: {},
    };
    const firstSource = {
      alias: 'MAPP',
      schema: 'leeds',
      relation: 'buildings',
      kind: 'table',
      assetId: 'source-buildings',
    };
    const secondSource = {
      ...firstSource,
      relation: 'roads',
      assetId: 'source-roads',
    };
    const firstDerived = {
      name: 'buildings',
      status: 'ready',
      generation: 1,
      revision: '7',
    };
    const secondDerived = {...firstDerived, name: 'roads'};
    const firstProposal = {
      id: 'proposal-1',
      assetId: asset.id,
      baseVersion: asset.version,
      state: 'declined',
    };
    const secondProposal = {...firstProposal, id: 'proposal-2'};
    const historyPath = (
      `/api/semantic/catalog/objects/${encodeURIComponent(asset.id)}/history`
    );
    const cursors = {
      catalog: 'catalog-next',
      source: 'source-next',
      derived: 'derived-next',
      proposals: 'proposals-next',
      history: 'history-next',
    };
    const api = vi.fn(async path => {
      if (path === '/api/semantic/status') {
        return {catalogRevision: 7, schemaVersion: 2};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {
          assets: [asset],
          pagination: {limit: 100, nextCursor: cursors.catalog},
        };
      }
      if (path === firstPage('/api/semantic/source/relations')) {
        return {
          relations: [firstSource],
          pagination: {limit: 100, nextCursor: cursors.source},
        };
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {
          derivedProfiles: [firstDerived],
          deliveryBlockers: [],
          pagination: {limit: 100, nextCursor: cursors.derived},
        };
      }
      if (path === firstPage('/api/semantic/proposals')) {
        return {
          proposals: [firstProposal],
          pagination: {limit: 100, nextCursor: cursors.proposals},
        };
      }
      if (path === firstPage(historyPath)) {
        return {
          history: [{catalogRevision: 1, changeType: 'generated'}],
          pagination: {limit: 100, nextCursor: cursors.history},
        };
      }
      if (path === pagePathForTest('/api/semantic/catalog', cursors.catalog)) {
        return {assets: [secondAsset], pagination: {limit: 100, nextCursor: null}};
      }
      if (path === pagePathForTest('/api/semantic/source/relations', cursors.source)) {
        return {relations: [secondSource], pagination: {limit: 100, nextCursor: null}};
      }
      if (path === pagePathForTest('/api/semantic/derived-profiles', cursors.derived)) {
        return {
          derivedProfiles: [secondDerived],
          deliveryBlockers: [],
          pagination: {limit: 100, nextCursor: null},
        };
      }
      if (path === pagePathForTest('/api/semantic/proposals', cursors.proposals)) {
        return {proposals: [secondProposal], pagination: {limit: 100, nextCursor: null}};
      }
      if (path === pagePathForTest(historyPath, cursors.history)) {
        return {
          history: [{catalogRevision: 2, changeType: 'curated'}],
          pagination: {limit: 100, nextCursor: null},
        };
      }
      throw new Error(`Unexpected request: GET ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: []}}
    />);

    await waitFor(() => {
      expect(api).toHaveBeenCalledWith(firstPage('/api/semantic/catalog'));
      expect(api).toHaveBeenCalledWith(firstPage('/api/semantic/source/relations'));
      expect(api).toHaveBeenCalledWith(firstPage('/api/semantic/derived-profiles'));
      expect(api).toHaveBeenCalledWith(firstPage('/api/semantic/proposals'));
    });
    expect(api.mock.calls.some(([path]) => path.includes('&cursor='))).toBe(false);

    fireEvent.click(screen.getByRole('button', {name: 'Advanced mode'}));
    fireEvent.click(screen.getByRole('button', {name: 'Load history'}));
    await screen.findByText(content => content.includes('"changeType": "generated"'));
    expect(api).toHaveBeenCalledWith(firstPage(historyPath));
    expect(api.mock.calls.some(([path]) => path.includes('&cursor='))).toBe(false);

    const continuations = [
      ['Load more semantic assets', '/api/semantic/catalog', cursors.catalog],
      ['Load more source relations', '/api/semantic/source/relations', cursors.source],
      ['Load more derived profiles', '/api/semantic/derived-profiles', cursors.derived],
      ['Load more semantic proposals', '/api/semantic/proposals', cursors.proposals],
      ['Load more asset history', historyPath, cursors.history],
    ];
    for (const [label, path, cursor] of continuations) {
      fireEvent.click(screen.getByRole('button', {name: label}));
      await waitFor(() => expect(
        screen.queryByRole('button', {name: label}),
      ).toBeNull());
      expect(api).toHaveBeenCalledWith(pagePathForTest(path, cursor));
    }

    expect(screen.getByRole('button', {name: /leeds\.roads/})).toBeTruthy();
    expect(screen.getByRole('option', {name: /MAPP:leeds\.roads/})).toBeTruthy();
    expect(screen.getByText('derived_layers.roads')).toBeTruthy();
    expect(screen.getByText(new RegExp(secondProposal.id))).toBeTruthy();
    expect(screen.getByText(
      content => content.includes('"catalogRevision": 2'),
    )).toBeTruthy();
    expect(
      api.mock.calls
        .map(([path]) => path)
        .filter(path => path.includes('&cursor=')),
    ).toEqual(continuations.map(([, path, cursor]) => (
      pagePathForTest(path, cursor)
    )));
  });

  test('offers context options in guided and advanced generation', async () => {
    let finishGeneration;
    const generationResult = new Promise(resolve => {
      finishGeneration = resolve;
    });
    const calls = [];
    const generatedOperations = [{
      op: 'set',
      path: '/curated/description',
      value: 'A model-authored description for review.',
    }];
    const api = vi.fn(async (path, options = {}) => {
      calls.push([path, options]);
      if (path === '/api/semantic/status') {
        return {
          catalogRevision: 7,
          schemaVersion: 2,
          capabilities: {
            generation: {
              available: true,
              provider: 'gemini',
              model: 'gemini-2.5-flash',
              targets: ['table', 'field'],
              metadataOnly: true,
              contextOptions: {
                sampleRows: {
                  available: true,
                  percent: 5,
                  maxRows: 100,
                  maxBytes: 98304,
                  requiredScope: 'semantic:data',
                },
                statistics: {
                  available: true,
                  requiredScope: 'semantic:data',
                },
              },
            },
          },
        };
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [asset], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/source/relations')) {
        return {relations: []};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (path === '/api/semantic/generate' && options.method === 'POST') {
        return generationResult;
      }
      if (path === '/api/semantic/proposals/check') {
        return {
          check: {
            fingerprint: 'generated-fingerprint',
            diff: [{path: '/curated/description'}],
          },
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: ['admin']}}
    />);

    await openGenerationStep();
    expect(screen.getByText(
      /Semantic metadata leaves MAPP for Google's Gemini service/,
    )).toBeTruthy();
    expect(screen.getByText(
      /may be processed or retained under that Gemini project's billing and data terms/,
    )).toBeTruthy();
    expect(screen.getByText(
      /do not send any metadata or selected data context you are not authorized to disclose/i,
    )).toBeTruthy();
    fireEvent.click(screen.getByText('What Gemini receives'));
    const sampleRows = screen.getByRole('checkbox', {
      name: /5% sample of row data \(capped\)/i,
    });
    const statistics = screen.getByRole('checkbox', {
      name: /table\/column statistics/i,
    });
    expect(screen.getByText(
      /Field drafts send aggregate counts and text lengths from at most 1,000 rows in a 5% sample/,
    )).toBeTruthy();
    expect(sampleRows.checked).toBe(false);
    expect(statistics.checked).toBe(false);
    fireEvent.click(sampleRows);
    fireEvent.click(statistics);
    fireEvent.click(screen.getByRole('button', {name: 'Advanced mode'}));
    expect(sampleRows.checked).toBe(false);
    expect(statistics.checked).toBe(false);
    fireEvent.click(sampleRows);
    fireEvent.click(statistics);
    fireEvent.click(screen.getByRole('button', {name: /^The layer/}));
    fireEvent.click(screen.getByRole('button', {name: 'Generate layer draft'}));
    expect(screen.getByRole('button', {
      name: 'Generating layer draft…',
    }).disabled).toBe(true);
    expect(sampleRows.disabled).toBe(true);
    expect(statistics.disabled).toBe(true);
    expect(JSON.parse(calls.find(([path]) => (
      path === '/api/semantic/generate'
    ))[1].body)).toEqual({
      assetId: asset.id,
      target: {kind: 'table'},
      contextOptions: {sampleRows: true, statistics: true},
    });

    finishGeneration({
      draft: {
        assetId: asset.id,
        baseVersion: 3,
        target: {kind: 'table'},
        operations: generatedOperations,
        explanation: 'Gemini-generated table description.',
      },
      generation: {
        provider: 'gemini',
        model: 'gemini-2.5-flash',
        metadataOnly: false,
        contextOptions: {sampleRows: true, statistics: true},
        proposalCreated: false,
      },
    });

    expect(await screen.findByRole('heading', {
      name: 'Review your draft',
    })).toBeTruthy();
    expect(screen.getByLabelText('Semantic proposal explanation').value)
      .toBe('Gemini-generated table description.');
    fireEvent.click(screen.getByRole('button', {name: 'Check proposal'}));

    await waitFor(() => expect(calls.some(([path]) => (
      path === '/api/semantic/proposals/check'
    ))).toBe(true));
    const checkCall = calls.find(([path]) => (
      path === '/api/semantic/proposals/check'
    ));
    expect(JSON.parse(checkCall[1].body)).toMatchObject({
      assetId: asset.id,
      baseVersion: 3,
      operations: generatedOperations,
      explanation: 'Gemini-generated table description.',
    });
    expect(calls.some(([path, options]) => (
      path === '/api/semantic/proposals' && options.method === 'POST'
    ))).toBe(false);
    expect(calls.some(([path]) => path.endsWith('/apply'))).toBe(false);
  });

  test('targets stable field IDs and hides generation without its scope', async () => {
    const generationCalls = [];
    const scopedApi = vi.fn(async (path, options = {}) => {
      if (path === '/api/semantic/status') {
        return {
          catalogRevision: 7,
          schemaVersion: 2,
          capabilities: {
            generation: {
              available: true,
              provider: 'gemini',
              model: 'gemini-2.5-flash',
              targets: ['table', 'field'],
              metadataOnly: true,
              contextOptions: {
                sampleRows: {
                  available: true,
                  percent: 5,
                  maxRows: 100,
                  maxBytes: 98304,
                  requiredScope: 'semantic:data',
                },
                statistics: {
                  available: true,
                  requiredScope: 'semantic:data',
                },
              },
            },
          },
        };
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {
          assets: [{
            ...asset,
            generated: {
              ...asset.generated,
              fields: [
                ...asset.generated.fields,
                {name: 'unstable_name_only', type: 'text'},
              ],
            },
          }],
          catalogRevision: 7,
        };
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (path === '/api/semantic/generate' && options.method === 'POST') {
        generationCalls.push(JSON.parse(options.body));
        return {
          draft: {
            assetId: asset.id,
            baseVersion: 3,
            target: {kind: 'field', fieldId: 'field:score'},
            operations: [{
              op: 'set',
              path: '/curated/fields/field:score/description',
              value: 'A review-only field description.',
            }],
          },
          generation: {
            provider: 'gemini',
            model: 'gemini-2.5-flash',
            metadataOnly: true,
            contextOptions: {sampleRows: false, statistics: false},
            proposalCreated: false,
          },
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    const {rerender} = render(<SemanticCatalog
      api={scopedApi}
      close={() => {}}
      identity={{actor: 'token:reader', scopes: ['semantic:inspect']}}
    />);
    await openGenerationStep();
    expect(await screen.findByText(
      /requires both semantic:inspect and semantic:generate/,
    )).toBeTruthy();
    expect(screen.queryByRole('button', {name: /^Specific fields/})).toBeNull();

    rerender(<SemanticCatalog
      api={scopedApi}
      close={() => {}}
      identity={{
        actor: 'token:author',
        scopes: ['semantic:inspect', 'semantic:generate', 'semantic:propose'],
      }}
    />);
    fireEvent.click(await screen.findByRole('button', {name: /^Specific fields/}));
    fireEvent.click(screen.getByText('What Gemini receives'));
    expect(screen.getByRole('checkbox', {
      name: /5% sample of row data \(capped\)/i,
    }).disabled).toBe(true);
    expect(screen.getByRole('checkbox', {
      name: /table\/column statistics/i,
    }).disabled).toBe(true);
    expect(screen.getByText(
      /require semantic:data/,
    )).toBeTruthy();
    fireEvent.click(screen.getByRole('checkbox', {name: 'score'}));
    fireEvent.click(screen.getByRole('button', {name: 'Generate drafts for 1 field'}));

    await waitFor(() => expect(generationCalls).toEqual([{
      assetId: asset.id,
      target: {kind: 'field', fieldId: 'field:score'},
      contextOptions: {sampleRows: false, statistics: false},
    }]));
    expect(await screen.findByRole('heading', {
      name: 'Review your draft',
    })).toBeTruthy();
  });

  test('marks saved field semantics and allows selecting up to 25 fields', async () => {
    const fields = Array.from({length: 26}, (_, index) => ({
      id: `field:${index + 1}`,
      name: `field_${index + 1}`,
      type: 'text',
    }));
    const batchAsset = {
      ...asset,
      generated: {...asset.generated, fields},
      curated: {
        ...asset.curated,
        fields: {
          'field:1': {description: 'Existing field description'},
        },
      },
    };
    const api = fieldBatchApi(batchAsset, vi.fn());

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: []}}
    />);
    await openGenerationStep();
    fireEvent.click(await screen.findByRole('button', {name: /^Specific fields/}));

    expect(screen.getByText('Choose up to 25 fields')).toBeTruthy();
    const savedStatus = screen.getByText('Saved semantic value');
    const savedField = screen.getByRole('checkbox', {name: 'field_1'});
    expect(savedField.disabled).toBe(false);
    expect(savedField.getAttribute('aria-describedby')).toBe(savedStatus.id);

    for (const field of fields.slice(0, 25)) {
      fireEvent.click(screen.getByRole('checkbox', {name: field.name}));
    }
    expect(screen.getByText('25 selected')).toBeTruthy();
    expect(screen.getByRole('checkbox', {name: 'field_26'}).disabled).toBe(true);
    expect(savedField.checked).toBe(true);
  });

  test('generates selected fields in parallel with completed progress and ordered drafts', async () => {
    const fieldIds = ['field:score', 'field:rank', 'field:notes'];
    const batchAsset = {
      ...asset,
      generated: {
        ...asset.generated,
        fields: fieldIds.map(fieldId => ({
          id: fieldId,
          name: fieldId.slice('field:'.length),
          type: 'text',
        })),
      },
    };
    const pending = Object.fromEntries(fieldIds.map(fieldId => [
      fieldId,
      deferred(),
    ]));
    const calls = [];
    const api = fieldBatchApi(
      batchAsset,
      request => {
        calls.push(request);
        return pending[request.target.fieldId].promise;
      },
      {
        sampleRows: {
          available: true,
          percent: 5,
          maxRows: 100,
          maxBytes: 98304,
          requiredScope: 'semantic:data',
        },
        statistics: {
          available: true,
          requiredScope: 'semantic:data',
        },
      },
    );

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: []}}
    />);
    await openGenerationStep();
    fireEvent.click(await screen.findByRole('button', {name: /^Specific fields/}));
    for (const fieldId of fieldIds) {
      fireEvent.click(screen.getByRole('checkbox', {
        name: fieldId.slice('field:'.length),
      }));
    }
    fireEvent.click(screen.getByText('What Gemini receives'));
    fireEvent.click(screen.getByRole('checkbox', {
      name: /table\/column statistics/i,
    }));
    fireEvent.click(screen.getByRole('button', {
      name: 'Generate drafts for 3 fields',
    }));

    await waitFor(() => expect(calls).toHaveLength(3));
    expect(screen.getByRole('button', {
      name: /0\/3 completed/,
    }).disabled).toBe(true);
    expect(calls.map(call => call.target.fieldId)).toEqual(fieldIds);
    expect(calls.every(call => (
      call.contextOptions.sampleRows === false
      && call.contextOptions.statistics === true
    ))).toBe(true);

    await act(async () => {
      pending['field:notes'].resolve(fieldGenerationResult(
        batchAsset,
        'field:notes',
        {sampleRows: false, statistics: true},
      ));
    });
    expect(screen.getByRole('button', {name: /1\/3 completed/})).toBeTruthy();

    await act(async () => {
      pending['field:score'].resolve(fieldGenerationResult(
        batchAsset,
        'field:score',
        {sampleRows: false, statistics: true},
      ));
    });
    expect(screen.getByRole('button', {name: /2\/3 completed/})).toBeTruthy();

    await act(async () => {
      pending['field:rank'].resolve(fieldGenerationResult(
        batchAsset,
        'field:rank',
        {sampleRows: false, statistics: true},
      ));
    });
    const reviewHeading = await screen.findByRole('heading', {
      name: 'Review your draft',
    });
    expect(screen.getByLabelText('Semantic proposal explanation').value)
      .toMatch(
        /explicitly selected bounded context: table\/column statistics/,
      );
    const operations = JSON.parse(
      reviewHeading.closest('section').querySelector('pre').textContent,
    );
    expect(operations.map(operation => operation.value)).toEqual(
      fieldIds.map(fieldId => `Description for ${fieldId}`),
    );
  });

  test('rejects mismatched context evidence without loading a partial field draft', async () => {
    const fieldIds = ['field:score', 'field:rank'];
    const batchAsset = {
      ...asset,
      generated: {
        ...asset.generated,
        fields: fieldIds.map(fieldId => ({
          id: fieldId,
          name: fieldId.slice('field:'.length),
          type: 'numeric',
        })),
      },
    };
    const pending = Object.fromEntries(fieldIds.map(fieldId => [
      fieldId,
      deferred(),
    ]));
    const calls = [];
    const api = fieldBatchApi(batchAsset, request => {
      calls.push(request);
      return pending[request.target.fieldId].promise;
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: []}}
    />);
    await openGenerationStep();
    fireEvent.click(await screen.findByRole('button', {name: /^Specific fields/}));
    for (const fieldId of fieldIds) {
      fireEvent.click(screen.getByRole('checkbox', {
        name: fieldId.slice('field:'.length),
      }));
    }
    fireEvent.click(screen.getByRole('button', {
      name: 'Generate drafts for 2 fields',
    }));
    await waitFor(() => expect(calls).toHaveLength(2));

    await act(async () => {
      pending['field:score'].resolve(fieldGenerationResult(
        batchAsset,
        'field:score',
        {sampleRows: true, statistics: false},
      ));
    });
    expect(screen.getByRole('button', {name: /1\/2 completed/})).toBeTruthy();
    expect(screen.queryByText(
      'Gemini returned an invalid semantic proposal draft.',
    )).toBeNull();

    await act(async () => {
      pending['field:rank'].resolve(fieldGenerationResult(
        batchAsset,
        'field:rank',
        {sampleRows: false, statistics: false},
      ));
    });
    expect(await screen.findByText(
      'Gemini returned an invalid semantic proposal draft.',
    )).toBeTruthy();
    expect(screen.queryByRole('heading', {name: 'Review your draft'})).toBeNull();
    expect(calls).toHaveLength(2);
  });

  test('surfaces generation errors without checking or creating a proposal', async () => {
    const calls = [];
    const api = vi.fn(async (path, options = {}) => {
      calls.push([path, options]);
      if (path === '/api/semantic/status') {
        return {
          catalogRevision: 7,
          schemaVersion: 2,
          capabilities: {
            generation: {
              available: true,
              provider: 'gemini',
              model: 'gemini-2.5-flash',
              targets: ['table'],
              metadataOnly: true,
            },
          },
        };
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [asset], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/source/relations')) {
        return {relations: []};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (path === '/api/semantic/generate' && options.method === 'POST') {
        throw new Error('Gemini is temporarily unavailable.');
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: ['admin']}}
    />);
    await openGenerationStep();
    fireEvent.click(screen.getByRole('button', {name: /^The layer/}));
    fireEvent.click(screen.getByRole('button', {name: 'Generate layer draft'}));

    expect(await screen.findByText('Gemini is temporarily unavailable.'))
      .toBeTruthy();
    expect(screen.getByRole('button', {
      name: 'Generate layer draft',
    }).disabled).toBe(false);
    expect(calls.some(([path]) => (
      path === '/api/semantic/proposals/check'
    ))).toBe(false);
    expect(calls.some(([path, options]) => (
      path === '/api/semantic/proposals' && options.method === 'POST'
    ))).toBe(false);
  });

  test('advances to review when a saved field semantic is already current', async () => {
    const currentAsset = {
      ...asset,
      curated: {
        ...asset.curated,
        fields: {
          'field:score': {description: 'Current score'},
        },
      },
    };
    const noChange = new Error(
      'Gemini returned the semantic annotation already stored.',
    );
    noChange.payload = {code: 'semantic.generation_no_change'};
    const api = fieldBatchApi(currentAsset, async () => {
      throw noChange;
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: []}}
    />);
    await openGenerationStep();
    fireEvent.click(await screen.findByRole('button', {name: /^Specific fields/}));
    fireEvent.click(screen.getByRole('checkbox', {name: 'score'}));
    fireEvent.click(screen.getByRole('button', {
      name: 'Generate drafts for 1 field',
    }));

    expect(await screen.findByRole('heading', {name: 'Review your draft'}))
      .toBeTruthy();
    expect(screen.getByText(
      "Gemini's result already matches the saved semantic value.",
    )).toBeTruthy();
    expect(screen.queryByText(noChange.message)).toBeNull();
    expect(screen.queryByRole('button', {name: 'Check proposal'})).toBeNull();
  });

  test('discovers and synchronizes an allowlisted source without row options', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    const calls = [];
    let registered = false;
    const source = {
      alias: 'MAPP',
      schema: 'leeds',
      relation: 'census_2021_england_oa',
      kind: 'table',
      assetId: 'source-census',
    };
    const sourceAsset = {
      id: source.assetId,
      version: 1,
      generation: 1,
      status: 'ready',
      generated: {
        name: source.relation,
        qualifiedName: `${source.schema}.${source.relation}`,
        fields: [{id: 'field:oa21cd', name: 'oa21cd', type: 'text'}],
      },
      curated: {},
    };
    const api = vi.fn(async (path, options = {}) => {
      calls.push([path, options]);
      if (path === '/api/semantic/status') {
        return {catalogRevision: registered ? 8 : 7, schemaVersion: 2};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {
          assets: registered ? [sourceAsset] : [],
          catalogRevision: registered ? 8 : 7,
        };
      }
      if (path === firstPage('/api/semantic/source/relations')) {
        return {relations: [source]};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (
        path === '/api/semantic/source/sync'
        && options.method === 'POST'
      ) {
        registered = true;
        return {
          catalogRevision: 8,
          operation: 'register',
          source,
          asset: sourceAsset,
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{
        actor: 'token:source-author',
        scopes: ['semantic:inspect', 'semantic:source'],
      }}
    />);

    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    expect(await screen.findByText(
      /Source registration reads only allowlisted relation and column metadata/,
    )).toBeTruthy();
    const relation = screen.getByLabelText('Semantic source relation');
    expect(relation.value).toBe(source.assetId);
    fireEvent.click(screen.getByRole('button', {
      name: 'Register or sync source metadata',
    }));

    await screen.findByText(/Registered leeds.census_2021_england_oa/);
    expect(globalThis.confirm).toHaveBeenCalledWith(
      'Synchronize metadata for MAPP:leeds.census_2021_england_oa?',
    );
    const syncCall = calls.find(([path, options]) => (
      path === '/api/semantic/source/sync' && options.method === 'POST'
    ));
    expect(JSON.parse(syncCall[1].body)).toEqual({
      alias: 'MAPP',
      schema: 'leeds',
      relation: 'census_2021_england_oa',
    });
    expect(screen.getByRole('heading', {
      name: 'leeds.census_2021_england_oa',
    })).toBeTruthy();
  });

  test('shows retained orphans and loads immutable asset history', async () => {
    const assetWithHistory = {
      ...asset,
      orphans: [{
        fieldId: 'field:retired',
        name: 'retired_score',
        annotation: {description: 'Former score'},
      }],
    };
    const api = vi.fn(async (path, options = {}) => {
      if (path === '/api/semantic/status') {
        return {catalogRevision: 7, schemaVersion: 2};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [assetWithHistory], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (
        path
        === firstPage(
          `/api/semantic/catalog/objects/${encodeURIComponent(asset.id)}/history`,
        )
      ) {
        return {
          catalogRevision: 7,
          history: [{catalogRevision: 7, changeType: 'curated'}],
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog api={api} close={() => {}}/>);
    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    expect(await screen.findByText('Orphaned field annotations')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: 'Load history'}));
    expect(await screen.findByText(
      content => content.includes('"changeType": "curated"'),
    )).toBeTruthy();
  });

  test('checks and creates a proposal without applying it', async () => {
    const calls = [];
    const api = vi.fn(async (path, options = {}) => {
      calls.push([path, options]);
      if (path === '/api/semantic/status') {
        return {catalogRevision: 7, schemaVersion: 1};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [asset], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (path === '/api/semantic/proposals/check') {
        return {
          check: {
            fingerprint: 'proposal-fingerprint',
            diff: [{path: '/curated/displayName', after: 'Walking score'}],
          },
        };
      }
      if (path === '/api/semantic/proposals' && options.method === 'POST') {
        return {proposal: {id: 'sem-1'}};
      }
      if (path === '/api/semantic/proposals/sem-1' && !options.method) {
        return {
          proposal: {
            id: 'sem-1',
            assetId: asset.id,
            baseVersion: asset.version,
            state: 'pending',
            explanation: 'Clearer language',
            diff: [{
              path: '/curated/displayName',
              after: 'Walking score',
            }],
          },
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog api={api} close={() => {}}/>);
    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    const editor = await screen.findByLabelText('Curated metadata JSON');
    fireEvent.change(editor, {
      target: {value: '{"displayName":"Walking score"}'},
    });
    fireEvent.change(screen.getByLabelText('Semantic proposal explanation'), {
      target: {value: 'Clearer language'},
    });
    fireEvent.click(screen.getByRole('button', {name: 'Check proposal'}));
    await screen.findByRole('heading', {name: 'Focused diff'});
    fireEvent.click(screen.getByRole('button', {name: 'Create proposal'}));

    await screen.findByText(/Created proposal sem-1/);
    const createCall = calls.find(([path, options]) => (
      path === '/api/semantic/proposals' && options.method === 'POST'
    ));
    expect(JSON.parse(createCall[1].body)).toMatchObject({
      assetId: asset.id,
      baseVersion: 3,
      fingerprint: 'proposal-fingerprint',
    });
    expect(calls.some(([path]) => path.endsWith('/apply'))).toBe(false);
  });

  test('shows stored evidence before enabling apply confirmation', async () => {
    let state = 'pending';
    vi.stubGlobal('confirm', vi.fn(() => true));
    const storedProposal = {
      id: 'sem-1',
      assetId: asset.id,
      baseVersion: 3,
      state: 'pending',
      explanation: 'Clarify how operators should interpret the score.',
      diff: [{
        op: 'set',
        path: '/curated/description',
        before: {exists: true, value: 'Current score'},
        after: {exists: true, value: 'Walking routes score'},
      }],
    };
    const api = vi.fn(async (path, options = {}) => {
      if (path === '/api/semantic/status') {
        return {catalogRevision: 7, schemaVersion: 1};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [asset], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {derivedProfiles: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {
          proposals: [{
            id: 'sem-1',
            assetId: asset.id,
            baseVersion: 3,
            state,
          }],
        };
      }
      if (path === '/api/semantic/proposals/sem-1' && !options.method) {
        return {proposal: storedProposal, catalogRevision: 7};
      }
      if (
        path === '/api/semantic/proposals/sem-1/apply'
        && options.method === 'POST'
      ) {
        state = 'applied';
        return {proposal: {id: 'sem-1', state}};
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog api={api} close={() => {}}/>);
    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    const apply = await screen.findByRole('button', {name: 'Apply'});
    expect(apply.disabled).toBe(true);
    expect(globalThis.confirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', {name: 'Review'}));
    await screen.findByText(
      'Clarify how operators should interpret the score.',
    );
    expect(screen.getByText(
      content => (
        content.includes('/curated/description')
        && content.includes('Walking routes score')
      ),
    )).toBeTruthy();
    expect(api).toHaveBeenCalledWith('/api/semantic/proposals/sem-1');
    expect(globalThis.confirm).not.toHaveBeenCalled();
    expect(apply.disabled).toBe(false);

    fireEvent.click(apply);

    await waitFor(() => expect(api).toHaveBeenCalledWith(
      '/api/semantic/proposals/sem-1/apply',
      expect.objectContaining({method: 'POST'}),
    ));
    expect(globalThis.confirm).toHaveBeenCalledWith(
      'Apply reviewed semantic proposal sem-1?',
    );
  });

  test('expands accepted proposals to show their stored evidence', async () => {
    const accepted = {
      id: 'sem-accepted',
      assetId: asset.id,
      baseVersion: 2,
      state: 'applied',
      explanation: 'Confirmed that this is the published road length.',
      diff: [{path: '/curated/description', after: 'Road length in metres'}],
    };
    const api = vi.fn(async path => {
      if (path === '/api/semantic/status') return {catalogRevision: 7, schemaVersion: 1};
      if (path === firstPage('/api/semantic/catalog')) return {assets: [asset], catalogRevision: 7};
      if (path === firstPage('/api/semantic/derived-profiles')) return {derivedProfiles: [], catalogRevision: 7};
      if (path === firstPage('/api/semantic/proposals')) {
        return {proposals: [accepted]};
      }
      throw new Error(`Unexpected request: ${path}`);
    });

    render(<SemanticCatalog api={api} close={() => {}}/>);
    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    const summary = await screen.findByText('Accepted proposal details');
    const details = summary.closest('details');
    expect(details.open).toBe(false);
    fireEvent.click(summary);
    expect(details.open).toBe(true);
    expect(screen.getByText(accepted.explanation)).toBeTruthy();
    expect(screen.getByText(content => content.includes('Road length in metres'))).toBeTruthy();
  });

  test('offers an explicit admin retry for failed semantic delivery', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    const api = vi.fn(async (path, options = {}) => {
      if (path === '/api/semantic/status') {
        return {catalogRevision: 7, schemaVersion: 1};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/source/relations')) {
        return {relations: []};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {
          catalogRevision: 7,
          deliveryBlockersMore: 'true',
          derivedProfiles: [{
            name: 'walkability',
            generation: 1,
            status: 'registering',
            revision: null,
            delivery: {
              operation: 'archive',
              generation: 3,
              status: 'repair_required',
              attempts: 8,
              lastError: 'stale generation',
            },
          }],
        };
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (
        path === '/api/semantic/derived-profiles/walkability/repair'
        && options.method === 'POST'
      ) {
        return {
          catalogRevision: 7,
          derivedProfile: {
            name: 'walkability',
            generation: 2,
            status: 'registering',
          },
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: ['admin']}}
    />);
    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    expect(await screen.findByText(/Blocking archive delivery: repair_required/))
      .toBeTruthy();
    expect(screen.queryByText(
      /More dropped-relation delivery blockers are waiting/,
    )).toBeNull();
    fireEvent.click(await screen.findByRole('button', {name: 'Retry delivery'}));

    await waitFor(() => expect(api).toHaveBeenCalledWith(
      '/api/semantic/derived-profiles/walkability/repair',
      expect.objectContaining({
        method: 'POST',
        body: '{"confirmed":true}',
      }),
    ));
    expect(globalThis.confirm).toHaveBeenCalledWith(
      'Retry semantic delivery for derived_layers.walkability?',
    );
  });

  test('shows and repairs an unmatched dropped-layer archive blocker', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true));
    const api = vi.fn(async (path, options = {}) => {
      if (path === '/api/semantic/status') {
        return {catalogRevision: 7, schemaVersion: 1};
      }
      if (path === firstPage('/api/semantic/catalog')) {
        return {assets: [], catalogRevision: 7};
      }
      if (path === firstPage('/api/semantic/source/relations')) {
        return {relations: []};
      }
      if (path === firstPage('/api/semantic/derived-profiles')) {
        return {
          catalogRevision: 7,
          derivedProfiles: [],
          deliveryBlockersMore: true,
          deliveryBlockers: [{
            name: 'already_dropped',
            relation: 'derived_layers.already_dropped',
            assetId: 'asset:dropped',
            eventId: 'event:archive',
            operation: 'archive',
            generation: 4,
            status: 'repair_required',
            attempts: 8,
            lastError: 'stale generation',
          }],
        };
      }
      if (path === firstPage('/api/semantic/proposals') && !options.method) {
        return {proposals: []};
      }
      if (
        path === '/api/semantic/derived-profiles/already_dropped/repair'
        && options.method === 'POST'
      ) {
        return {
          catalogRevision: 7,
          derivedProfile: {
            name: 'already_dropped',
            generation: 4,
            status: 'pending_archive',
          },
        };
      }
      throw new Error(`Unexpected request: ${options.method || 'GET'} ${path}`);
    });

    render(<SemanticCatalog
      api={api}
      close={() => {}}
      identity={{actor: 'admin', scopes: ['admin']}}
    />);
    fireEvent.click(await screen.findByRole('button', {name: 'Advanced mode'}));
    expect(await screen.findByText(
      /Dropped relation · blocking archive delivery: repair_required/,
    )).toBeTruthy();
    expect(await screen.findByText(
      /Repair the displayed blockers, then refresh the semantic catalog/,
    )).toBeTruthy();
    fireEvent.click(screen.getByRole('button', {name: 'Retry delivery'}));
    await waitFor(() => expect(api).toHaveBeenCalledWith(
      '/api/semantic/derived-profiles/already_dropped/repair',
      expect.objectContaining({
        method: 'POST',
        body: '{"confirmed":true}',
      }),
    ));
  });
});
